#!/usr/bin/env python3
"""
06_mining_traffic.py — production is your best eval set.
========================================================

    python examples/06_mining_traffic.py            # offline, no key

Monitoring isn't for admiring dashboards; it's for turning what production teaches
you back into fixes and tests. Every refusal, thumbs-down, and terse answer is a
free, real-user-labelled example of something you got wrong. This closes the
feedback flywheel from the Evals dive: mine the failures, cluster them, and emit
them as candidate eval cases (obs/mining.py).

Three moves:
  1. Surface the failures out of the traffic.
  2. Cluster them by topic — so "scattered failures" becomes "31 of them are the
     mobile app," a fixable finding (and the same event input drift saw as a
     distribution shift).
  3. Emit them as JSONL eval candidates in the Evals dive's shape, ready for a
     human to write the gold answer and drop into the regression suite.

And one honest caveat printed at the end: most failures are *silent* — no thumbs
at all — so you cannot wait for feedback to find them.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import mining, simulate

records, _ = simulate.generate()
fails = mining.failures(records)

print(f"Total requests: {len(records)}")
print(f"Failures surfaced (refusals, thumbs-down, terse answers): {len(fails)}\n")

print("Clustered by what they're about — the biggest themes to fix first:")
for c in mining.cluster(fails, records, top=5):
    print(f"  {c['count']:>4}×  “{c['term']}”")
    print(f"         e.g. {c['examples'][0]}")

print("\nCandidate eval cases (Evals-dive JSONL shape — `expected` for a human to fill):")
for case in mining.gold_candidates(fails, limit=5):
    print("  " + json.dumps(case))

silent = mining.unrated_but_failing(records)
print(f"\n{silent} of {len(fails)} failures had NO user feedback at all.")
print("Thumbs are sparse — most failures are silent, which is exactly why you")
print("monitor proxies (refusals, drift, judge samples) instead of waiting for a")
print("thumbs-down. The flywheel: these candidates become regression tests (Evals),")
print("and the mobile-app cluster becomes a KB article or a scoped refusal.")
