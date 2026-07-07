#!/usr/bin/env python3
"""
05_alerting.py — page me for incidents, not for Tuesdays.
=========================================================

    python examples/05_alerting.py            # offline, no key

A dashboard nobody watches is useless; the point is to be *told*. But the naive
alert fails both ways at once. Watch what a static threshold does here:

    "page if p95 > 300ms"  →  fires almost every day, because normal p95 is ~400ms.

That's alert fatigue: you mute it, and the muted alert is the one that misses the
real outage. The fix is three ideas from obs/alerts.py — a **baseline z-score** (so
"weird" is relative, not a magic constant), a **direction**, and **persistence**
(require the breach to hold N days). Persistence is the dial that tells a one-day
spike apart from a real regression.

The honest part: there is **no setting that gives zero false alarms and zero
misses.** Every knob trades one for the other. This example makes that tradeoff
visible, then picks a sensible operating point for each metric.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import alerts, judge, metrics, simulate

records, incidents = simulate.generate()
rows = metrics.daily(records)
# Fold the sampled quality score into the daily rows so a detector can watch it.
for row, j in zip(rows, judge.judge_by_day(records, per_day=20)):
    row["judge_score"] = j["judge_score"]

# --- 1. Why a static threshold is a trap -----------------------------------
naive = sum(1 for r in rows if r["p95_latency_ms"] > 300)
print(f"Naive rule 'p95 > 300ms' would fire on {naive}/{len(rows)} days — "
      f"mostly normal ones.\nThat's alert fatigue. Now the z-score + persistence version:\n")

# --- 2. The persistence tradeoff on one metric -----------------------------
print("Latency: same z-threshold (3σ), different persistence —")
for p in (1, 2, 3):
    det = alerts.Detector("p95_latency_ms", "up", z_threshold=3, persistence=p)
    fired = alerts.detect(rows, det)
    days = ", ".join(a["fired_on"] for a in fired) or "(none)"
    print(f"  persistence={p}: {len(fired)} alert(s) → {days}")
print("  persistence=1 catches the 1-day spike; persistence=3 rides over it and")
print("  only fires on sustained trends. Neither is 'correct' — it's the tradeoff.\n")

# --- 3. A sensible detector per metric -------------------------------------
detectors = [
    ("latency spike",     alerts.Detector("p95_latency_ms", "up", z_threshold=4, persistence=1)),
    ("latency regression", alerts.Detector("p95_latency_ms", "up", z_threshold=3, persistence=3)),
    ("input drift",       alerts.Detector("refusal_rate", "up", z_threshold=3, persistence=3)),
    ("cost creep",        alerts.Detector("cost_per_request_usd", "up", z_threshold=3, persistence=3)),
    ("quality drop",      alerts.Detector("judge_score", "down", z_threshold=3, persistence=3)),
]
print("Alerts from the tuned detector suite:")
print(f"  {'detector':<20}{'fired on':<13}{'breach began':<14}{'z':>6}")
print("  " + "-" * 51)
for name, det in detectors:
    for a in alerts.detect(rows, det):
        print(f"  {name:<20}{a['fired_on']:<13}{a['breach_started']:<14}{a['z']:>6.1f}")

print("\nCompare each 'breach began' to when the incident really started:")
for inc in incidents:
    print(f"  {inc.kind:<20} truly started day {inc.start_day} "
          f"({inc.metric})")
print("\nThe latency *regression* detector stays silent — correctly, because the")
print("spike was transient. Detection lag (breach-began vs alert-fired) is the price")
print("persistence charges for not paging on noise. That's monitoring, honestly.")
