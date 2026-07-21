#!/usr/bin/env python3
"""
02_baselines_trends.py: a number is meaningless without a baseline.

    python examples/02_baselines_trends.py            # offline, no key

"Cost per request is $0.00006" tells you nothing. "$0.00006, up from a baseline of
$0.00003, that's +5.4σ" is an incident. The move from the first sentence to the
second is the whole idea of monitoring: learn what *normal* looked like from a
clean baseline window, then measure every new day in standard deviations from it.

A **z-score** does exactly that: (today − baseline mean) / baseline spread. It's
unitless, so the same "3σ is weird" rule works for latency, cost, and refusals
alike, so you don't need a hand-tuned threshold per metric.

Here we take the first 7 days as the baseline and print the z-score trend for cost
per request. Watch it sit near zero through the healthy period, then climb once the
prompt-bloat cost creep starts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import alerts, metrics, simulate

records, _ = simulate.generate()
rows = metrics.daily(records)

BASELINE_DAYS = 7
det = alerts.Detector("cost_per_request_usd", direction="up", baseline_days=BASELINE_DAYS)
z = alerts.signed_z(rows, det)

base_vals = [r["cost_per_request_usd"] for r in rows[:BASELINE_DAYS]]
mean, spread = alerts.baseline_stats(base_vals)
print(f"Baseline (first {BASELINE_DAYS} days): cost/req ≈ ${mean*1e6:.1f}µ  (±${spread*1e6:.1f}µ)\n")

print(f"{'day':<11}{'$/req':>9}{'z':>8}   trend")
print("-" * 46)
for i, zr in enumerate(z[::3]):
    bar = "" if zr["z"] < 1 else "▁▂▃▅▆▇█"[min(6, int(zr["z"]))] * min(20, int(zr["z"]) + 1)
    tag = "  ← baseline" if i * 3 < BASELINE_DAYS else ""
    print(f"{zr['day']:<11}{zr['value']*1e6:>8.1f}µ{zr['z']:>8.1f}   {bar}{tag}")

print("\nThe z-score is flat through the healthy weeks, then climbs steadily. That")
print("steadiness is the tell of a real regression rather than a noisy day. But a")
print("rising z alone still isn't an alert: you have to decide *how high, for how")
print("long* before you page someone. That decision, and its unavoidable tradeoff")
print("between false alarms and detection lag, is Section 5.")
print("\nFirst, two kinds of regression this cost trend can't see: the questions")
print("changing (Section 3) and the answers getting worse (Section 4).")
