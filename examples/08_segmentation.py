#!/usr/bin/env python3
"""
08_segmentation.py: the global average lies; slice by cohort.

    python examples/08_segmentation.py            # offline, no key

Every metric so far has been computed over *all* traffic. That's exactly how you
miss the most common production incident there is: a problem concentrated in one
**segment**, a single plan, region, language, or prompt version, that a global
average dilutes into invisibility. Here one cohort's backend is mis-provisioned
and their requests slow to a crawl starting around day 21. That cohort
("enterprise") is only 15% of traffic, so:

  • the **global** p95 latency detector stays silent; the overall number looks fine
  • the **enterprise** p95, computed on its own, screams

The fix is one line of discipline: `metrics.daily_by_segment` computes the same
series *per cohort*, and you run the same detectors on each. One honest wrinkle
the example makes concrete: small cohorts have fewer requests per day, so their
metrics are noisier, so you either loosen the detector (shorter persistence here) or
widen the window. Monitoring per segment is not free, but it's how you catch the
incident that's someone's entire experience.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import alerts, metrics, simulate

# One incident: an enterprise-only latency regression, invisible in the aggregate.
outage = simulate.Incident(
    "segment_outage", 21, 30, "p95_latency_ms",
    "Enterprise plan's backend is mis-provisioned; their requests slow down.",
    segment="enterprise",
)
records, _ = simulate.generate(35, seed=11, requests_per_day=300, incidents=[outage])
rows = metrics.daily(records)
by_seg = metrics.daily_by_segment(records)
total = len(records)

# --- 1. The global view: nothing to see here --------------------------------
g_det = alerts.Detector("p95_latency_ms", "up", z_threshold=3, persistence=3, baseline_days=7)
g_alerts = alerts.detect(rows, g_det)
g_series = alerts.signed_z(rows, g_det)
g_base = sum(z["value"] for z in g_series[:7]) / 7
g_window = sum(z["value"] for z in g_series[21:31]) / 10
print("GLOBAL p95 latency (all traffic):")
print(f"  baseline {g_base:.0f}ms → outage window {g_window:.0f}ms   "
      f"peak z={max(z['z'] for z in g_series[7:]):.1f}   "
      f"alerts fired: {len(g_alerts)}   → looks fine.\n")

# --- 2. The same detector, per segment --------------------------------------
# Small cohorts are noisier, so per-segment detection loosens persistence to 2.
print("PER-SEGMENT p95 latency (persistence=2, for smaller samples):")
print(f"  {'segment':<12}{'traffic':>9}{'baseline':>10}{'window':>9}{'peak z':>8}   verdict")
print("  " + "-" * 60)
for seg, seg_rows in by_seg.items():
    det = alerts.Detector("p95_latency_ms", "up", z_threshold=3, persistence=2, baseline_days=7)
    z = alerts.signed_z(seg_rows, det)
    fired = alerts.detect(seg_rows, det)
    share = sum(r["requests"] for r in seg_rows) / total
    base = sum(zr["value"] for zr in z[:7]) / 7
    window = sum(zr["value"] for zr in z[21:31]) / 10
    peak = max(zr["z"] for zr in z[7:])
    verdict = "ALERT: on fire" if fired else "ok"
    print(f"  {seg:<12}{share:>8.0%}{base:>9.0f}ms{window:>8.0f}ms{peak:>8.1f}   {verdict}")

print(f"\nThe outage was in one cohort ({outage.segment}, ~15% of traffic). Globally it")
print("hid inside normal p95 noise and fired nothing; sliced by segment it's obvious")
print("and localized: you know exactly whose backend to fix. This is why an aggregate")
print("dashboard is necessary but not sufficient: always be able to group by tenant,")
print("plan, region, and prompt version. The catch is cost and noise: more segments")
print("means more series to watch and smaller samples per series, so you slice on the")
print("dimensions that actually carry different risk, not every field you log.")
