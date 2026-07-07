#!/usr/bin/env python3
"""
03_input_drift.py — the questions changed, and nothing errored.
===============================================================

    python examples/03_input_drift.py            # offline (mock embeddings)

The most dangerous failure in a deployed LLM app is the one that doesn't throw:
users gradually start asking things your system was never good at. No exception,
no latency bump — the model just answers questions it shouldn't, and quality rots
while every ops dashboard stays green. In this history, users start asking about a
mobile app the knowledge base has no answer for, beginning around day 14.

Three lenses, cheapest first (obs/drift.py):

  1. **Novel-term rate** — share of questions using words the baseline barely saw.
     Pure string counting, no model. The earliest, cheapest smoke alarm.
  2. **Embedding drift** — how far each day's questions sit, on average, from the
     baseline's center of mass *in meaning*. Catches drift even in ordinary words.
  3. **PSI** — the classic MLOps statistic for "how much did this distribution
     move?", here on question length. You'll meet PSI everywhere (Section 7).

Uses the offline mock embeddings (words hashed to stable vectors), so it runs with
no key. Flip PROVIDER=openai to measure drift with real embeddings — the numbers
change, the story doesn't.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from obs import drift, providers, simulate
from obs.logs import by_day

load_dotenv()
print(f"Embeddings via: {providers.describe()}\n")

records, _ = simulate.generate()
days = by_day(records)
day_keys = list(days)
BASELINE_DAYS = 7

baseline_records = [r for d in day_keys[:BASELINE_DAYS] for r in days[d]]
vocab = drift.baseline_vocabulary(baseline_records)
emb_rows = drift.embedding_drift_by_day(records, baseline_days=BASELINE_DAYS)
emb_by_day = {r["day"]: r["drift"] for r in emb_rows}

print(f"{'day':<11}{'novel-term':>12}{'embed-drift':>13}   (baseline = first 7 days)")
print("-" * 52)
for i, d in enumerate(day_keys[::3]):
    ntr = drift.novel_term_rate(days[d], vocab)
    bar = "█" * int(ntr * 30)
    tag = "  ← baseline" if i * 3 < BASELINE_DAYS else ""
    print(f"{d:<11}{ntr:>12.2f}{emb_by_day[d]:>13.3f}   {bar}{tag}")

# PSI on question length: baseline distribution vs the final week.
base_lens = [len(r.question) for r in baseline_records]
late_lens = [len(r) for d in day_keys[-7:] for r in [x.question for x in days[d]]]
psi = drift.psi(base_lens, late_lens)
print(f"\nPSI(question length, baseline vs final week) = {psi}")
print("  rule of thumb:  <0.1 stable · 0.1–0.25 moderate shift · >0.25 major shift")

print("\nAll three light up in the back half: new words, questions drifting away in")
print("meaning, a shifted length distribution — the mobile-app questions arriving.")
print("This is the same event Section 6 sees from the other side, as a cluster of")
print("failures worth turning into eval cases. Next, the subtler cousin: the inputs")
print("stay the same but the *answers* get worse.")
