"""
Music Streaming Simulation — Locust Load Tests
==============================================

Three experiments matching the project proposal:

  Exp 1: Max Concurrent Streams Before Rebuffering
         → Run with StreamUser, ramp from 10 to 500+ users
         → Watch median latency and rebuffer_rate in /metrics

  Exp 2: Adaptive Bitrate vs Fixed Quality Under Load
         → Run AdaptiveStreamUser vs FixedStreamUser at same concurrency
         → Compare quality_switch_rate and rebuffer_rate

  Exp 3: Horizontal Scaling
         → Deploy 1, 2, 4 ECS instances; run same load each time
         → Compare RPS ceiling and latency distribution

Usage:
  locust -f locustfile.py --host http://<ALB_DNS>
  locust -f locustfile.py --host http://localhost:8080 -u 50 -r 5 --headless -t 2m

Set EXPERIMENT env var to run a specific class:
  EXPERIMENT=adaptive locust -f locustfile.py ...
"""

import os
import random
import time
from locust import HttpUser, task, between, events
from locust.runners import MasterRunner, WorkerRunner

# ─── Song pool ────────────────────────────────────────────────────────────────

SONGS = [f"song_{i}" for i in range(100)]
POPULAR_SONGS = [f"song_{i}" for i in range(10)]  # always cached in CDN


# ─── Base streaming behaviour ─────────────────────────────────────────────────

class BaseStreamUser(HttpUser):
    """
    Simulates one concurrent audio stream:
      1. Start a stream (POST /streams)
      2. Repeatedly fetch chunks (GET /streams/{id}/chunk)
      3. Stop the stream (DELETE /streams/{id})

    Each chunk request = one 100ms audio segment delivered over HTTP.
    Locust measures the HTTP response time → that IS the delivery latency.
    When p95 > 500ms the stream would rebuffer in a real player.
    """

    abstract = True
    wait_time = between(0.05, 0.15)  # ~100ms between chunk fetches (real-time audio)
    adaptive = False                   # override in subclasses

    def on_start(self):
        self.stream_id = None
        self.song_id = random.choice(POPULAR_SONGS)
        self._start_stream()

    def on_stop(self):
        if self.stream_id:
            self._stop_stream()

    def _start_stream(self):
        resp = self.client.post(
            "/streams",
            json={"song_id": self.song_id, "adaptive": self.adaptive},
            name="POST /streams",
        )
        if resp.status_code == 201:
            self.stream_id = resp.json().get("stream_id")

    def _stop_stream(self):
        self.client.delete(f"/streams/{self.stream_id}", name="DELETE /streams/{id}")
        self.stream_id = None

    @task
    def get_chunk(self):
        if not self.stream_id:
            self._start_stream()
            return
        resp = self.client.get(
            f"/streams/{self.stream_id}/chunk",
            name="GET /streams/{id}/chunk",
        )
        if resp.status_code == 404:
            # Stream expired server-side; restart
            self.stream_id = None


# ─── Experiment 1: Max Concurrent Streams Before Rebuffering ─────────────────

class StreamUser(BaseStreamUser):
    """
    Fixed high-quality streams.
    Goal: find how many concurrent streams a single instance handles before
    chunk delivery latency exceeds 500ms (rebuffering threshold).

    Run command:
      locust -f locustfile.py StreamUser --host http://<ALB> \
             -u 500 -r 10 --headless -t 5m

    Expected result: rebuffering starts ~200-300 concurrent users as
    CPU on the 0.25 vCPU Fargate instance saturates.
    """
    adaptive = False


# ─── Experiment 2: Adaptive Bitrate vs Fixed Quality ─────────────────────────

class AdaptiveStreamUser(BaseStreamUser):
    """
    Adaptive quality: switches 320kbps → 128kbps when latency > 300ms.
    Compare against FixedStreamUser at the same concurrency.

    Run both classes in the same test:
      locust -f locustfile.py AdaptiveStreamUser FixedStreamUser \
             --host http://<ALB> -u 400 -r 10 --headless -t 5m

    Expected: AdaptiveStreamUser has lower rebuffer_rate, higher quality_switch_rate.
    """
    adaptive = True


class FixedStreamUser(BaseStreamUser):
    """Fixed 320kbps — never downgrades quality."""
    adaptive = False


# ─── Experiment 3: Horizontal Scaling ────────────────────────────────────────

class ScalingUser(BaseStreamUser):
    """
    Use this class for the horizontal scaling experiment.
    Run identical loads against 1, 2, and 4 ECS instances by changing
    the streaming service desired_count in Terraform and rerunning Locust.

    Suggested loads: 500, 1000, 2000 concurrent users.

    Run command:
      locust -f locustfile.py ScalingUser --host http://<ALB> \
             -u 1000 -r 20 --headless -t 5m

    Expected: ~linear scaling — 2 instances handle ~2x streams before rebuffering.
    """
    adaptive = True  # adaptive reduces noise; we care about throughput/latency


# ─── Metrics polling task ─────────────────────────────────────────────────────

class MetricsPoller(HttpUser):
    """
    Lightweight user that polls /metrics every 5 seconds.
    Add 1-2 of these to any test run to track server-side rebuffer counts
    alongside Locust's client-side latency measurements.

    Do NOT ramp this up — keep count=1 or 2.
    """
    wait_time = between(5, 5)

    @task
    def poll_streaming_metrics(self):
        self.client.get("/metrics", name="GET /metrics (streaming)")

    @task
    def poll_cdn_metrics(self):
        self.client.get("/metrics", name="GET /metrics (cdn)")  # routed to CDN via ALB


# ─── Circuit Breaker experiment helper ───────────────────────────────────────

class CDNFailureInjector(HttpUser):
    """
    NOT a load test user — use this once to inject CDN failure, then stop.
    Triggers the circuit breaker on the streaming service.

    Steps:
      1. Run test with StreamUser
      2. Manually call: POST /admin/config {"fail": true, "delay_ms": 600}
         (or use this class with count=1 for a few seconds)
      3. Watch circuit breaker trip in streaming service /metrics
      4. Call: POST /admin/config {"fail": false, "delay_ms": 0} to recover
    """
    wait_time = between(30, 30)

    @task
    def inject_failure(self):
        self.client.post(
            "/admin/config",
            json={"fail": True, "delay_ms": 600},
            name="POST /admin/config (inject failure)",
        )

    def on_stop(self):
        self.client.post(
            "/admin/config",
            json={"fail": False, "delay_ms": 0},
            name="POST /admin/config (recover)",
        )
