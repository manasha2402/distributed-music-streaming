# Distributed Music Streaming Simulation
**CS6650 — Building Scalable Distributed Systems**
Team: Ananyaa Mylsamy · Manasha Sekar

---

## Why We Built This

Modern music streaming platforms (Spotify, Apple Music) must serve thousands of concurrent audio streams without rebuffering. We built a simulation of that delivery problem to answer three concrete questions:

1. How many concurrent streams can a single Fargate instance handle before rebuffering begins?
2. Does adaptive bitrate switching meaningfully reduce rebuffering under CPU saturation?
3. Does horizontal scaling produce near-linear capacity gains — and does it matter *which* tier you scale?

We built two real Go microservices, deployed them on AWS ECS Fargate, and ran controlled load tests to measure the answers.

---

## Team

| Name | Contributions |
|------|--------------|
| Ananyaa Mylsamy | Streaming service, Terraform/ECS infrastructure, Experiment 1, Experiment 3, Report |
| Manasha Sekar | CDN cache service, Terraform/ECS infrastructure, Experiment 1, Experiment 2, Report |

---

## Architecture

```
Locust clients
      │
      ▼
Application Load Balancer (port 80)
      │                    │
   /streams/*           /cache/*
      │                    │
Streaming Service    CDN Cache Service
(Go, ECS Fargate)    (Go, ECS Fargate)
```

**Streaming Service** — manages in-memory audio streams, tries CDN first (500ms timeout + circuit breaker), falls back to local CPU chunk generation. Supports adaptive bitrate switching (320kbps → 128kbps when latency > 300ms).

**CDN Cache Service** — pre-populates 100 songs × 300 chunks at startup. Serves cached chunks or 404. Supports injecting failures/latency via `/admin/config` for circuit breaker testing.

**Resilience patterns:** Fail-fast timeout (500ms), circuit breaker (5 failures → open 30s), adaptive quality degradation.

---

## Experiments

| # | Question | Key Finding |
|---|----------|-------------|
| 1 | Single-instance breaking point | ~400–500 concurrent users on 0.25 vCPU before p99 > 500ms |
| 2 | Adaptive bitrate under saturation | 32% lower p50 latency, 66% more throughput vs fixed 320kbps |
| 3 | Horizontal scaling (streaming only vs both tiers) | Co-scaling CDN pushed p50 below 500ms rebuffer threshold; circuit breaker architecture limits near-linear gains |

Full experiment results, charts, and analysis are in [`documentation/DETAILED_REPORT.md`](documentation/DETAILED_REPORT.md).

Design decisions, architecture evolution, and division of work are in [`documentation/PROJECT_MANAGEMENT.md`](documentation/PROJECT_MANAGEMENT.md).

The final submitted report is [`documentation/EXPERIMENT_REPORT.pdf`](documentation/EXPERIMENT_REPORT.pdf).

---

## Repo Structure

```
streaming-service/                        — Go audio streaming service (port 8080)
cdn-cache-service/                        — Go CDN cache service (port 8081)
terraform/                                — AWS ECS Fargate infrastructure (ALB, ECR, auto-scaling)
locust/                                   — Load test scripts (Exp 1, 2, 3)
public/                                   — Screenshots and charts for all experiments
documentation/
  DETAILED_REPORT.md                      — Full experiment write-up with results and analysis
  EXPERIMENT_REPORT.pdf                   — Final submitted report (PDF)
  PROJECT_MANAGEMENT.md                   — Design evolution, division of work, decisions log
```
---

## Authors
Ananyaa Mylsamy · Manasha Sekar
