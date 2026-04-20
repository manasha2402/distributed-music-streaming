"""
Experiment 3 — Horizontal Scaling
==================================

Part A: Scale streaming only (CDN fixed at 1 instance)
  - Run same 1000-user load against 1, 2, 4 streaming instances
  - Expect: CDN becomes bottleneck, circuit breaker opens, sub-linear gains

Part B: Scale both tiers (streaming + CDN together)
  - Run same loads against 1+1, 2+2, 4+4 instances
  - Expect: CDN stays healthy, near-linear gains

Usage:
  export ALB=http://<alb-dns>

  # Set streaming_min_capacity=1 in terraform, apply, then:
  locust -f exp3_locustfile.py ScalingStreamUser \
    --host $ALB --headless -u 1000 -r 20 -t 7m \
    --csv=exp3_1s_1c --csv-full-history \
    2>&1 | tee exp3_1s_1c.log

  # Set streaming_min_capacity=2, apply, then rerun with --csv=exp3_2s_1c
  # Set streaming_min_capacity=4, apply, then rerun with --csv=exp3_4s_1c
"""

import csv
import os
import random
import threading
import time
from locust import HttpUser, task, between, events

SONGS = [f"song_{i}" for i in range(100)]  # all pre-cached in CDN at startup

SNAPSHOT_FILE = os.environ.get("SNAPSHOT_FILE", "exp3_metrics_snapshots.csv")

_snapshot_started = False
_snapshot_lock = threading.Lock()


def _start_snapshot_thread(host: str):
    global _snapshot_started
    with _snapshot_lock:
        if _snapshot_started:
            return
        _snapshot_started = True

    import http.client
    import json as _json
    from urllib.parse import urlparse

    with open(SNAPSHOT_FILE, "w", newline="") as f:
        csv.writer(f).writerow([
            "timestamp", "active_streams", "total_rebuffers",
            "cdn_circuit_open", "total_streams",
        ])

    def _poll():
        parsed = urlparse(host)
        while True:
            time.sleep(10)
            try:
                conn = http.client.HTTPConnection(parsed.netloc, timeout=3)
                conn.request("GET", "/metrics")
                resp = conn.getresponse()
                data = _json.loads(resp.read())
                with open(SNAPSHOT_FILE, "a", newline="") as f:
                    csv.writer(f).writerow([
                        round(time.time(), 1),
                        data.get("active_streams", 0),
                        data.get("total_rebuffers", 0),
                        data.get("cdn_circuit_open", False),
                        data.get("total_streams", 0),
                    ])
            except Exception:
                pass

    threading.Thread(target=_poll, daemon=True).start()


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    if environment.host:
        _start_snapshot_thread(environment.host)


class ScalingStreamUser(HttpUser):
    """
    Fixed 320kbps stream — one concurrent audio listener.
    Uses songs within CDN cache range (song_0–song_99) so CDN can serve
    hits when healthy. Fetches one chunk every ~100ms, matching a real player.
    """
    wait_time = between(0.08, 0.12)

    def on_start(self):
        self.stream_id = None
        self._start_stream()

    def on_stop(self):
        if self.stream_id:
            self.client.delete(
                f"/streams/{self.stream_id}",
                name="DELETE /streams/{id}",
            )

    def _start_stream(self):
        resp = self.client.post(
            "/streams",
            json={"song_id": random.choice(SONGS), "adaptive": False},
            name="POST /streams",
        )
        if resp.status_code == 201:
            self.stream_id = resp.json().get("stream_id")

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
            self.stream_id = None
