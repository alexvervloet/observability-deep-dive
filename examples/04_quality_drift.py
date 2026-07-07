#!/usr/bin/env python3
"""
04_quality_drift.py — same questions, worse answers.
=====================================================

    python examples/04_quality_drift.py            # offline (mock judge)

Input drift (Section 3) is users changing. This is the provider changing under
you: a silent model swap around day 28 makes answers terser and more evasive, then
it's rolled back around day 35. The questions are identical; the *answers* got
worse. Latency, cost, and error rate never move — the only signal is the content.

Quality is the one metric that isn't free to measure. You can't afford to grade
every request, so you **sample** a slice per day and score it with a judge — the
mock's rule-based scorer here, or a real LLM-as-judge (from the Evals dive) when
you flip PROVIDER. And because a mean over 20 sampled answers is a point estimate,
we report it **with a confidence interval**: a dip that sits inside the margin
isn't a regression yet.

Watch the sampled judge score dip well below its own error bars during the
regression window, then recover — while the refusal rate (input drift's signal)
does its own, unrelated thing. Two different failures, two different detectors.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from obs import judge, metrics, providers, simulate

load_dotenv()
print(f"Judge via: {providers.describe()}\n")

records, _ = simulate.generate()
rows = metrics.daily(records)
refusal_by_day = {r["day"]: r["refusal_rate"] for r in rows}
jrows = judge.judge_by_day(records, per_day=20)

base = [j["judge_score"] for j in jrows[:7] if j["judge_score"] is not None]
baseline = sum(base) / len(base)
print(f"Baseline sampled quality (first 7 days): {baseline:.3f}")
print("A day is a likely regression when its score + margin still sits below baseline.\n")

print(f"{'day':<11}{'quality (±ci)':>18}{'refusal':>9}   quality vs baseline")
print("-" * 62)
for j in jrows[::3]:
    q, m = j["judge_score"], j["margin"]
    if q is None:
        continue
    # A simple visual: how far below baseline, in units of the day's margin.
    below = baseline - q
    flag = "  ◀ REGRESSION (beyond noise)" if below - m > 0.05 else ""
    bar = "█" * int(q * 30)
    print(f"{j['day']:<11}{q:>10.3f} ±{m:<5.3f}{refusal_by_day[j['day']]:>9.2f}   {bar}{flag}")

print("\nThe judge score falls clear of its error bars in the regression window and")
print("climbs back after the rollback — a real, temporary quality drop. Meanwhile")
print("refusal_rate marches to its own tune (input drift), proving these are")
print("independent failures. A single 'quality' number would have conflated them.")
print("\nNow: a rising signal isn't a page. Turning these trends into alerts that")
print("wake you for real incidents but NOT for noise is the real craft — next.")
