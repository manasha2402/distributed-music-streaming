# Design Evolution - Distributed Music Streaming Simulation

**Team:** Ananyaa Mylsamy · Manasha Sekar
**Course:** CS6650 - Building Scalable Distributed Systems

---

## 1. Initial Design

### The Core Question

We started from a single question: *how many concurrent audio streams can a real world constrained cloud instance serve before listeners notice?*

Audio streaming has a narrow latency contract, a 200–300ms buffer stall is perceptible, but a quality downgrade from 320kbps to 128kbps usually is not. That asymmetry shaped every design decision we made.

### First Architecture Sketch

Our original design was a single monolithic Go service that both cached and delivered chunks. It had no CDN tier and no fallback path. We planned to ramp load with Locust until it broke.

**Problems with this approach, identified before we wrote a line of code:**

- A single service makes it impossible to isolate where the bottleneck is CPU, memory, or network.
- No fallback path means the service crashes rather than degrades gracefully. Real streaming platforms (Spotify, Apple Music) tolerate CDN unavailability, we wanted to simulate that.
- A monolith can't answer Experiment 3's question (does scaling one tier vs both tiers produce different results?).

We split into two services before writing any Go.

---

## 2. Service Split and Resilience Patterns

### Two-Service Architecture

| Service | Responsibility |
|---------|---------------|
| `streaming-service` | Stream lifecycle, chunk delivery, adaptive bitrate logic, metrics |
| `cdn-cache-service` | In-memory chunk cache, fault injection endpoint for circuit breaker testing |

This split let us independently scale each tier in Experiment 3, the central architectural bet of the project.

### Resilience Patterns Added Early

We added three patterns during the initial Go implementation, before any load testing:

| Pattern | Trigger | Why added |
|---------|---------|-----------|
| Fail Fast | CDN call > 500ms | Prevents one slow CDN from blocking all goroutines |
| Circuit Breaker | 5 CDN failures → open for 30s | Stops hammering a degraded CDN; forces local fallback |
| Adaptive Quality | Delivery latency > 300ms → drop to 128kbps | 10× CPU reduction per chunk; buys headroom under saturation |

**What we didn't anticipate:** the CDN circuit breaker tripped in *every single run* from Experiment 1 onward, at every concurrency level. The CDN was overwhelmed before the streaming service was. This was the first major design surprise.

---

## 3. Experiment 1 : Finding the Capacity Wall

**Goal:** Find the concurrency level where a single 0.25 vCPU Fargate instance crosses the 500ms rebuffer threshold.

### Who Did What

| Task | Teammate |
|------|-------|
| Go service implementation | Ananyaa |
| Terraform / ECS Fargate deployment | Manasha |
| Local load testing (MacBook baseline) | Ananyaa |
| AWS load testing (300, 400, 500 users) | Manasha |
| Locust scenario design (`StreamUser`) | Both |

### Results (AWS, Fargate, Single Instance, 0.25 vCPU)

p99 crosses 500ms at 500 users. RPS plateaus between 400→500, confirming CPU saturation. HTTP error rate: 0% across all runs.

### Problems Encountered

**CDN circuit breaker tripped immediately at 300 users.** We expected this to happen closer to the streaming service's capacity limit, not as the *first* observable event. The CDN was the bottleneck, not the streaming service, the streaming service never ran out of capacity before the CDN did. This forced us to reframe Experiment 1's findings: the result isn't "the streaming service breaks at N" but "the CDN breaks first, and the circuit breaker + local fallback keeps the streaming service alive."

**Local vs AWS discrepancy was larger than expected.** Local Mac handled 2,000 concurrent users with p95 = 320ms and zero rebuffers. The same code on 0.25 vCPU Fargate broke at 400–500 users. The ~4× gap matched the CPU ratio (0.25 vCPU vs ~1 effective vCPU) but made the local test useful only as a correctness check, not a capacity predictor.

### What Changed Because of Experiment 1

- We confirmed 400–500 users as the single-instance baseline, which anchored Experiment 3's load target at 1,000 users (2× the known limit).
- We locked the CDN's `desired_count` to 1 for Experiment 3 Part A, specifically to reproduce the CDN-as-bottleneck scenario at scale.

---

## 4. Experiment 2 : Does Adaptive Bitrate Actually Help?

**Goal:** Measure whether the 128kbps fallback mode meaningfully reduces rebuffering when CPU is already saturated.

**Teammate:** Manasha

### Design Iteration

Our first plan was a single mixed run (200 adaptive + 200 fixed users simultaneously). We ran it and got a confusing result: adaptive mode showed only a marginal improvement (~5ms at p50) despite firing 3,270 quality switches.

**The problem:** in a mixed run, CPU savings from one adaptive stream are immediately consumed by a fixed-rate stream on the same core. The two variables (strategy and resource availability) are conflated.

**The fix:** we added two isolation runs, adaptive-only (400 users) and fixed-only (400 users) to compare the strategies on equal footing. The isolation runs revealed the true delta:

**Result:**
The mixed run was not wrong, it reflected realistic conditions where different stream types share infrastructure. But the isolation runs proved the mechanism works. There is a significant improvement in both latency and throughput.

### Problems Encountered

- **Rebuffer event counts were nearly identical** across adaptive and fixed modes (~670% per stream). This was counterintuitive. Investigation showed that rebuffering is triggered by latency crossing 300ms, and both modes spend time in that zone under saturation, adaptive mode just resolves the stall faster because the next chunk costs 10× less CPU.
- **Songs had to be outside the CDN cache range** to keep the CPU genuinely saturated. If a song hit the CDN cache, the streaming service got a free chunk instead of burning CPU, this would have invalidated comparisons between adaptive and fixed modes. We used `exp2_track_*` IDs outside the `song_0`–`song_99` cached range to force local generation throughout.

---

## 5. Experiment 3 : Horizontal Scaling

**Goal:** Determine whether scaling one tier vs both tiers produces different capacity gains.

**Teammate:** Ananyaa

### Design

The two-part structure (Part A: scale streaming only; Part B: scale both tiers together) came directly from the Experiment 1 finding that the CDN fails first. If we had only run "scale everything together," we would never have isolated *which tier's ceiling* limits horizontal scaling.

| Part | Prediction | Basis |
|------|-----------|-------|
| A: streaming scales, CDN fixed | Sub-linear gains, CDN becomes wall | CDN circuit opened at 300 users in Exp 1 |
| B: both tiers scale together | Near-linear gains, CDN stays healthy, chunks served from cache | Removing the CDN bottleneck should unlock the streaming service's true scaling |

### Infrastructure Changes

- Terraform `streaming_min_capacity` variable added to allow 1 → 2 → 4 instance changes without code edits.
- `cdn_min_capacity` added as a matching variable for Part B.
- `ScalingStreamUser` Locust class uses songs within the CDN cache range (`song_0`–`song_4`) so that CDN availability (not chunk availability) is the variable under test.

CDN circuit breaker opened in all three runs. Gains are real but sub-linear on latency (p50 78% improvement) and near-linear on RPS (3.5×). The RPS gains come from CPU scheduling: fewer streams per instance means each goroutine gets more CPU time for local chunk generation.

**Operational discovery: sticky sessions required.** Without ALB `lb_cookie` sticky sessions, 33% of chunk requests failed with 404 because stream state is in-memory per instance. Cross-instance routing sent chunk requests to instances with no knowledge of that stream. This was not anticipated in the design and is a hard requirement for this architecture.

**Result**

At 4+4, each CDN instance handled ~250 users' chunk requests. The circuit breaker opened during ramp-up and remained open throughout (confirmed by server metrics mid-run) — chunks were still generated locally. The p50 improvement (380ms, below the 500ms rebuffer threshold) came from CPU scheduling: fewer streams per instance means each goroutine gets more CPU time for local generation. **p50 = 380ms, the first run across all experiments below the 500ms rebuffer threshold at 1,000 users.**

---

## 6. Division of Work Summary

| Experiment | Task | Teammate |
|------------|------|-------|
| Exp 1 | Go streaming service implementation | Ananyaa |
| Exp 1 | CDN cache service implementation | Ananyaa |
| Exp 1 | Terraform / ECS Fargate infrastructure | Manasha |
| Exp 1 | Local load testing (MacBook baseline, 2000 users) | Ananyaa |
| Exp 1 | AWS load testing (300 / 400 / 500 users on Fargate) | Manasha |
| Exp 2 | All runs (mixed, adaptive-only, fixed-only) + analysis | Manasha |
| Exp 3 | Part A and Part B design, runs, and analysis | Ananyaa |
| Locust scenarios | `StreamUser`, `AdaptiveUser`, `ScalingStreamUser` | Both |
| Report / README | Problem statement, methodology, results writeup | Both |