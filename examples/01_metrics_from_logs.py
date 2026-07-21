#!/usr/bin/env python3
"""
01_metrics_from_logs.py: from a pile of logs to the numbers you watch.

    python examples/01_metrics_from_logs.py            # offline, no key

A log file is data; a metric is a decision aid. obs/metrics.py reduces a batch of
records to the dozen numbers an on-call engineer reads. Two habits to build here:

  • **p95, not the average, for latency.** The mean hides the tail; the tail is
    what users feel. Watch how far p95 sits above p50; that gap *is* the slow
    experiences.
  • **A metric means nothing without a window.** So we compute per day and print
    the series. A single number ("cost is $0.00003") is noise; a trend across days
    is a story.

We print a handful of days. Nothing here flags a problem yet; that's the next
few sections. First you have to be able to *see* the numbers move.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import metrics, simulate

records, _ = simulate.generate()
rows = metrics.daily(records)

cols = ["day", "requests", "p50_latency_ms", "p95_latency_ms", "refusal_rate",
        "cache_hit_rate", "cost_per_request_usd"]
header = f"{'day':<11}{'reqs':>6}{'p50':>8}{'p95':>8}{'refuse':>8}{'cache':>7}{'$/req':>10}"
print(header)
print("-" * len(header))
# Every 4th day, so the whole 6 weeks fits on one screen.
for row in rows[::4]:
    print(f"{row['day']:<11}{row['requests']:>6}{row['p50_latency_ms']:>8.0f}"
          f"{row['p95_latency_ms']:>8.0f}{row['refusal_rate']:>8.2f}"
          f"{row['cache_hit_rate']:>7.2f}{row['cost_per_request_usd']*1e6:>9.1f}µ")

print("\nThings you can already see by eye (and will detect automatically next):")
print("  • p95 runs well above p50: the latency tail is real, and mostly stable.")
print("  • one day's p95 jumps far above the rest: a transient spike (Section 5).")
print("  • refuse creeps up in the back half: users asking things we can't answer.")
print("  • $/req roughly doubles later on: tokens per call are growing (cost creep).")
print("\nSeeing it by eye doesn't scale to 500 metrics. Sections 4–5 make it a number")
print("and then an alert. First, though: none of these means anything without a")
print("*baseline* to compare to. That's next.")
