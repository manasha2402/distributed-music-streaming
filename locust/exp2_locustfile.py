"""
Experiment 2: Adaptive Bitrate vs Fixed Quality Under Load
==========================================================

Key design choices to guarantee a clear result
-----------------------------------------------
1. Songs use the prefix "exp2_track_" — these are NOT in the CDN cache
   (CDN only pre-populates "song_0" … "song_99"), so every chunk falls
   through to local CPU generation, where quality actually matters.

2. CDN failure is injected at test start (POST /admin/config {fail:true})
   and restored at test stop. Belt-and-suspenders: even if a song ID
   accidentally collided with the cache, CDN still returns 503 so the
   circuit breaker trips and all requests go to local generateChunk().

3. AdaptiveStreamUser switches 320→128 kbps when latency >300 ms and
   recovers back to 320 kbps when latency <150 ms (hysteresis added in
   streaming-service main.go). Fixed streams never change.

4. Request names carry [adaptive] / [fixed] tags so Locust CSV stats
   are fully separated for analysis.

Load shape (Exp2Shape)
----------------------
  0 –  60 s →  50 users  (25 adaptive + 25 fixed) — baseline
 60 – 150 s → 150 users
150 – 270 s → 300 users
270 – 450 s → 400 users  — hold at peak, above Exp-1 rebuffer threshold

Run:
  locust -f exp2_locustfile.py \\
         --host http://<ALB_DNS> \\
         --headless --csv=exp2 --csv-full-history

Then analyse:
  python exp2_analysis.py \\
         --history exp2_stats_history.csv \\
         --snapshots exp2_metrics_snapshots.csv
"""

import csv
import os
import random
import threading
import time
from datetime import datetime

import requests
from locust import HttpUser, LoadTestShape, events, task, between
from locust.runners import WorkerRunner

# Songs guaranteed to miss CDN cache (CDN only knows "song_0" … "song_99")
EXP2_SONGS = [f"exp2_track_{i}" for i in range(500)]

METRICS_SNAPSHOT_FILE = "exp2_metrics_snapshots.csv"

# ─── Base ─────────────────────────────────────────────────────────────────────

class BaseStreamUser(HttpUser):
    abstract = True
    wait_time = between(0.05, 0.15)
    adaptive = False
    _label = "unknown"

    def on_start(self):
        self.stream_id = None
        self.song_id = random.choice(EXP2_SONGS)
        self._start_stream()

    def on_stop(self):
        if self.stream_id:
            self.client.delete(
                f"/streams/{self.stream_id}",
                name=f"DELETE /streams/{{id}} [{self._label}]",
            )

    def _start_stream(self):
        resp = self.client.post(
            "/streams",
            json={"song_id": self.song_id, "adaptive": self.adaptive},
            name=f"POST /streams [{self._label}]",
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
            name=f"GET /streams/{{id}}/chunk [{self._label}]",
        )
        if resp.status_code == 404:
            self.stream_id = None


# ─── Experiment 2 user classes ────────────────────────────────────────────────

class AdaptiveStreamUser(BaseStreamUser):
    """320→128 kbps; downgrades at >300 ms, recovers at <150 ms."""
    adaptive = True
    _label = "adaptive"


class FixedStreamUser(BaseStreamUser):
    """Fixed 320 kbps — never adapts."""
    adaptive = False
    _label = "fixed"


# ─── Load shape ───────────────────────────────────────────────────────────────

class Exp2Shape(LoadTestShape):
    # (cumulative_end_seconds, total_users, spawn_rate)
    stages = [
        (60,  50,  5),
        (150, 150, 10),
        (270, 300, 10),
        (450, 400, 10),
    ]

    def tick(self):
        t = self.get_run_time()
        for end, users, rate in self.stages:
            if t < end:
                return users, rate
        return None


# ─── Metrics snapshots ────────────────────────────────────────────────────────

_stop_event = threading.Event()
_snapshot_thread = None


def _snapshot_loop(host: str):
    fields = [
        "timestamp", "elapsed_s",
        "active_streams", "total_streams",
        "adaptive_streams", "fixed_streams",
        "total_rebuffers", "adaptive_rebuffers", "fixed_rebuffers",
        "total_quality_switches", "cdn_circuit_open",
    ]
    write_header = not os.path.exists(METRICS_SNAPSHOT_FILE)
    start_time = time.time()
    with open(METRICS_SNAPSHOT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()
        while not _stop_event.is_set():
            try:
                r = requests.get(f"{host}/metrics", timeout=5)
                if r.status_code == 200:
                    m = r.json()
                    writer.writerow({
                        "timestamp": datetime.utcnow().isoformat(),
                        "elapsed_s": round(time.time() - start_time, 1),
                        "active_streams":        m.get("active_streams", 0),
                        "total_streams":         m.get("total_streams", 0),
                        "adaptive_streams":      m.get("adaptive_streams", 0),
                        "fixed_streams":         m.get("fixed_streams", 0),
                        "total_rebuffers":       m.get("total_rebuffers", 0),
                        "adaptive_rebuffers":    m.get("adaptive_rebuffers", 0),
                        "fixed_rebuffers":       m.get("fixed_rebuffers", 0),
                        "total_quality_switches": m.get("total_quality_switches", 0),
                        "cdn_circuit_open":      m.get("cdn_circuit_open", False),
                    })
                    f.flush()
            except Exception:
                pass
            _stop_event.wait(10)


def _inject_cdn_failure(host: str, fail: bool) -> bool:
    """
    Returns True on success (HTTP 204). Raises SystemExit on failure when
    injecting (fail=True), because a silent failure here breaks the experiment.
    On restore (fail=False) we just warn — experiment is already over.
    """
    state = "FAILING" if fail else "healthy"
    url = f"{host}/admin/config"
    try:
        resp = requests.post(url, json={"fail": fail, "delay_ms": 0}, timeout=5)
        if resp.status_code == 204:
            print(f"[exp2] CDN set to {state} (POST {url} → 204 OK)")
            return True
        else:
            msg = f"[exp2] CDN config got HTTP {resp.status_code} from {url} — expected 204"
            if fail:
                print(f"ERROR: {msg}")
                print("       Is CDN_HOST set correctly? Are you hitting the ALB (not the streaming port)?")
                raise SystemExit(1)
            else:
                print(f"WARNING: {msg} (CDN may not have been restored)")
            return False
    except SystemExit:
        raise
    except Exception as e:
        msg = f"[exp2] CDN config request to {url} failed: {e}"
        if fail:
            print(f"ERROR: {msg}")
            print("       Set CDN_HOST env var to the ALB URL (same as --host for on-AWS runs).")
            raise SystemExit(1)
        else:
            print(f"WARNING: {msg} (CDN may not have been restored)")
        return False


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    global _snapshot_thread
    if isinstance(environment.runner, WorkerRunner):
        return

    host = os.environ.get("STREAMING_HOST") or environment.host
    # CDN_HOST is the same as host on AWS (ALB routes /admin/* to CDN).
    # Only differs when running locally (CDN on a separate port).
    cdn_host = os.environ.get("CDN_HOST") or environment.host
    if not os.environ.get("CDN_HOST"):
        print(f"[exp2] CDN_HOST not set — using --host ({cdn_host}) as CDN endpoint.")
        print(f"       On AWS this is correct (ALB routes /admin/* to CDN).")
        print(f"       Locally, set CDN_HOST=http://localhost:8081 if CDN is on a separate port.")

    # 1. Reset streaming service counters
    try:
        resp = requests.post(f"{host}/debug/reset", timeout=5)
        if resp.status_code == 204:
            print(f"[exp2] streaming metrics reset OK")
        else:
            print(f"[exp2] WARNING: reset returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"[exp2] WARNING: reset failed: {e}")

    # 2. Force CDN to fail — this MUST succeed or adaptive/fixed results are identical
    _inject_cdn_failure(cdn_host, fail=True)

    # 3. Start snapshot thread
    _stop_event.clear()
    _snapshot_thread = threading.Thread(target=_snapshot_loop, args=(host,), daemon=True)
    _snapshot_thread.start()
    print(f"[exp2] snapshots → {METRICS_SNAPSHOT_FILE}")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    _stop_event.set()
    if _snapshot_thread:
        _snapshot_thread.join(timeout=15)

    cdn_host = os.environ.get("CDN_HOST") or environment.host
    _inject_cdn_failure(cdn_host, fail=False)
    print(f"[exp2] done — snapshots in {METRICS_SNAPSHOT_FILE}")
