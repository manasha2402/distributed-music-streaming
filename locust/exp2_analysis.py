"""
Experiment 2 Analysis: Adaptive vs Fixed Quality Under Load
===========================================================

Reads Locust CSV output + server-side metrics snapshots and produces:
  1. Time-series latency chart: p50/p95 for adaptive vs fixed chunks
  2. Rebuffer rate comparison bar chart (adaptive vs fixed)
  3. Quality switch vs rebuffer count over time (adaptive only)
  4. Printed summary table

Verified against Locust 2.43.2 _stats_history.csv columns:
  Timestamp, User Count, Type, Name, Requests/s, Failures/s,
  50%, 66%, 75%, 80%, 90%, 95%, 98%, 99%, 99.9%, 99.99%, 100%,
  Total Request Count, Total Failure Count,
  Total Median Response Time, Total Average Response Time,
  Total Min Response Time, Total Max Response Time, Total Average Content Size

Usage:
  python exp2_analysis.py \\
      --history exp2_stats_history.csv \\
      --snapshots exp2_metrics_snapshots.csv \\
      --out exp2_results/
"""

import argparse
import os
import sys

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker


# ─── Loaders ──────────────────────────────────────────────────────────────────

def load_history(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()

    # Convert percentile columns — they can be "N/A" when no requests have arrived yet
    pct_cols = ["50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%"]
    for col in pct_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Requests/s"] = pd.to_numeric(df["Requests/s"], errors="coerce")
    df["Timestamp"]  = pd.to_numeric(df["Timestamp"],  errors="coerce")
    df["elapsed_s"]  = df["Timestamp"] - df["Timestamp"].min()

    # Only keep per-request rows (exclude the Aggregated summary row per tick)
    df = df[df["Name"] != "Aggregated"].copy()
    return df


def load_snapshots(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["elapsed_s"] = pd.to_numeric(df["elapsed_s"], errors="coerce")
    for col in ["adaptive_rebuffers", "fixed_rebuffers", "total_quality_switches",
                "adaptive_streams", "fixed_streams"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ─── Helpers ──────────────────────────────────────────────────────────────────

def chunk_rows(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Filter to GET chunk rows for the given mode label (adaptive or fixed)."""
    return df[
        (df["Type"] == "GET") &
        (df["Name"].str.contains(rf"\[{label}\]", na=False, regex=True))
    ].copy()


def save_fig(fig, path: str):
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  saved → {path}")


# ─── Chart 1: Latency time-series ─────────────────────────────────────────────

def plot_latency(df: pd.DataFrame, out_dir: str):
    adaptive = chunk_rows(df, "adaptive")
    fixed    = chunk_rows(df, "fixed")

    if adaptive.empty or fixed.empty:
        print(f"  [warn] latency chart skipped — adaptive rows: {len(adaptive)}, fixed rows: {len(fixed)}")
        print(f"         Request names found: {df['Name'].unique().tolist()}")
        return

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    fig.suptitle("Experiment 2 — Chunk Delivery Latency: Adaptive vs Fixed", fontsize=13)

    for ax, (title, sub) in zip(axes, [
        ("Adaptive (320→128 kbps)", adaptive),
        ("Fixed (320 kbps always)", fixed),
    ]):
        ax.plot(sub["elapsed_s"], sub["50%"], label="p50",  linewidth=1.5)
        ax.plot(sub["elapsed_s"], sub["95%"], label="p95",  linewidth=1.5, linestyle="--")
        ax.plot(sub["elapsed_s"], sub["99%"], label="p99",  linewidth=1,   linestyle=":")
        ax.axhline(500, color="red",    linewidth=0.8, linestyle="--", label="500 ms rebuffer threshold")
        ax.axhline(300, color="orange", linewidth=0.8, linestyle=":",  label="300 ms degrade threshold")
        ax.set_title(title)
        ax.set_xlabel("Elapsed (s)")
        ax.set_ylabel("Latency (ms)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.0f}"))

    save_fig(fig, os.path.join(out_dir, "latency_comparison.png"))


# ─── Chart 2: Rebuffer rate bar chart ─────────────────────────────────────────

def plot_rebuffer_rate(snap: pd.DataFrame, out_dir: str):
    if snap.empty:
        print("  [warn] rebuffer chart skipped — no snapshot data")
        return

    last = snap.iloc[-1]
    a_streams = float(last.get("adaptive_streams", 0)) or 1
    f_streams = float(last.get("fixed_streams",    0)) or 1
    a_rate = float(last.get("adaptive_rebuffers", 0)) / a_streams * 100
    f_rate = float(last.get("fixed_rebuffers",    0)) / f_streams * 100

    fig, ax = plt.subplots(figsize=(6, 5))
    bars = ax.bar(["Adaptive", "Fixed"], [a_rate, f_rate],
                  color=["steelblue", "tomato"], width=0.5)
    for bar, val in zip(bars, [a_rate, f_rate]):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.3,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=12)
    ax.set_ylabel("Rebuffer events per stream started (%)")
    ax.set_title("Experiment 2 — Rebuffer Rate: Adaptive vs Fixed")
    ax.set_ylim(0, max(a_rate, f_rate) * 1.35 + 1)
    ax.grid(axis="y", alpha=0.3)
    save_fig(fig, os.path.join(out_dir, "rebuffer_rate.png"))


# ─── Chart 3: Quality switches vs rebuffers over time ─────────────────────────

def plot_quality_switches(snap: pd.DataFrame, out_dir: str):
    if snap.empty or "total_quality_switches" not in snap.columns:
        print("  [warn] quality switches chart skipped — no snapshot data")
        return

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(snap["elapsed_s"], snap["total_quality_switches"],
            color="steelblue", linewidth=1.5, label="Quality switches (adaptive streams)")
    ax.plot(snap["elapsed_s"], snap["adaptive_rebuffers"],
            color="steelblue", linewidth=1.5, linestyle="--", label="Rebuffers — adaptive")
    ax.plot(snap["elapsed_s"], snap["fixed_rebuffers"],
            color="tomato", linewidth=1.5, linestyle="--", label="Rebuffers — fixed")
    ax.set_xlabel("Elapsed (s)")
    ax.set_ylabel("Cumulative count")
    ax.set_title("Experiment 2 — Quality Switches vs Rebuffers Over Time")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    save_fig(fig, os.path.join(out_dir, "quality_switches.png"))


# ─── Summary table ─────────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame, snap: pd.DataFrame, out_dir: str):
    lines = [
        "Experiment 2 Summary — Adaptive vs Fixed Quality Under Load",
        "=" * 62,
    ]

    if not df.empty:
        # Stats over the last 60 s of the test (steady-state at peak load)
        window_start = df["elapsed_s"].max() - 60
        window = df[df["elapsed_s"] >= window_start]
        for label in ["adaptive", "fixed"]:
            sub = chunk_rows(window, label)
            if not sub.empty:
                lines.append(f"\n{label.capitalize()} chunks (last 60 s):")
                lines.append(f"  p50  = {sub['50%'].mean():.1f} ms")
                lines.append(f"  p95  = {sub['95%'].mean():.1f} ms")
                lines.append(f"  p99  = {sub['99%'].mean():.1f} ms")
                lines.append(f"  RPS  = {sub['Requests/s'].mean():.1f}")

    if not snap.empty:
        last = snap.iloc[-1]
        a_s = float(last.get("adaptive_streams", 0)) or 1
        f_s = float(last.get("fixed_streams",    0)) or 1
        a_r = float(last.get("adaptive_rebuffers", 0))
        f_r = float(last.get("fixed_rebuffers",    0))
        lines += [
            "\nServer-side counters (end of test):",
            f"  Adaptive streams started : {int(a_s)}",
            f"  Fixed streams started    : {int(f_s)}",
            f"  Adaptive rebuffers       : {int(a_r)}  ({a_r/a_s*100:.1f}%)",
            f"  Fixed rebuffers          : {int(f_r)}  ({f_r/f_s*100:.1f}%)",
            f"  Total quality switches   : {int(last.get('total_quality_switches', 0))}",
            f"  CDN circuit open at end  : {last.get('cdn_circuit_open', '?')}",
        ]

    # Sanity checks
    lines.append("\nExperiment validity checks:")
    if not snap.empty:
        last = snap.iloc[-1]
        cdn_ok = str(last.get("cdn_circuit_open", "")).lower() in ("true", "1")
        lines.append(f"  CDN circuit breaker was open : {'YES ✓' if cdn_ok else 'NO — CDN may have been serving chunks, results unreliable'}")
        a_s = float(last.get("adaptive_streams", 0))
        f_s = float(last.get("fixed_streams",    0))
        balanced = abs(a_s - f_s) / max(a_s, f_s, 1) < 0.2
        lines.append(f"  Adaptive/fixed stream ratio  : {a_s:.0f}/{f_s:.0f} {'(balanced ✓)' if balanced else '(unbalanced — check locust user class weights)'}")
        q_switches = float(last.get("total_quality_switches", 0))
        lines.append(f"  Quality switches fired       : {q_switches:.0f} {'✓' if q_switches > 0 else '— adaptive logic may not have triggered'}")

    summary = "\n".join(lines)
    print(summary)
    out = os.path.join(out_dir, "summary.txt")
    with open(out, "w") as f:
        f.write(summary + "\n")
    print(f"\n  saved → {out}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Experiment 2 Analysis")
    parser.add_argument("--history",   default="exp2_stats_history.csv",
                        help="Locust _stats_history.csv (requires --csv-full-history flag at run time)")
    parser.add_argument("--snapshots", default="exp2_metrics_snapshots.csv",
                        help="Server-side metrics snapshots written by exp2_locustfile.py")
    parser.add_argument("--out",       default="exp2_results",
                        help="Output directory for charts and summary")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if not os.path.exists(args.history) and not os.path.exists(args.snapshots):
        print("No data files found. Run the load test first.")
        print(f"  Expected: {args.history}  and/or  {args.snapshots}")
        sys.exit(1)

    df   = load_history(args.history)     if os.path.exists(args.history)   else pd.DataFrame()
    snap = load_snapshots(args.snapshots) if os.path.exists(args.snapshots) else pd.DataFrame()

    if df.empty:
        print(f"WARNING: {args.history} not found or empty — latency charts will be skipped.")
        print("         Did you pass --csv-full-history to locust?")

    print("Generating charts...")
    plot_latency(df, args.out)
    plot_rebuffer_rate(snap, args.out)
    plot_quality_switches(snap, args.out)
    print_summary(df, snap, args.out)
    print(f"\nDone. Results in {args.out}/")


if __name__ == "__main__":
    main()
