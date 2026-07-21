#!/usr/bin/env python3
"""
07_classic_mlops_sidebar.py: the vocabulary you'll be asked about (and why half
of it doesn't fit LLM apps).
================================================================================

    python examples/07_classic_mlops_sidebar.py            # offline, no key

Search "AI observability" and you'll get the classic MLOps monitoring curriculum:
feature drift, concept drift, PSI, KS tests, SHAP/LIME explainability. That
curriculum is real and correct, for the model it grew up around: a **tabular
predictor** (churn, fraud, credit) with a fixed feature vector and labels that
eventually arrive so you can measure accuracy after the fact.

An LLM app mostly doesn't have those handles, and pretending it does is the trap
this sidebar exists to name:

  • There's no feature vector to drift; the input is free text. (The honest analog
    is embedding drift, Section 3: same idea, fuzzier.)
  • Labels rarely arrive. Nobody tells you the "right" support answer next week, so
    you can't compute accuracy over time. You sample a judge (Section 4) instead.
  • "Concept drift" (the input→output relationship changes) has no clean analog;
    the closest thing is your provider silently swapping the model under you.
  • LLM "explainability" is a research field (probing, attention, mechanistic
    interp), NOT an engineering practice you run in prod. Attention weights are not
    SHAP values. Be skeptical of a vendor selling "LLM explainability."

So: learn the vocabulary (you'll be asked it), but map each term to what actually
works for LLM apps. The table below is that map. Then we run PSI in its *native*
habitat, a numeric feature, so you've seen the real thing, not a mimicry.
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import drift

MAP = [
    ("Feature drift (PSI/KS on columns)", "Embedding drift on the input text (§3)"),
    ("Concept drift (X→y relationship)",  "Silent provider/model swap; prompt changes (§4)"),
    ("Accuracy/AUC on arriving labels",   "Sampled LLM-as-judge + sparse thumbs (§4, §6)"),
    ("SHAP/LIME feature attributions",    "Citations & retrieved-context inspection (RAG dive)"),
    ("Prediction-distribution monitoring", "Refusal rate, answer length, judge-score trend"),
    ("Label lag / delayed ground truth",  "Ground truth may NEVER arrive; mine failures (§6)"),
]
print(f"{'Classic tabular-MLOps concept':<38}  LLM-app analog that actually works")
print("-" * 84)
for classic, llm in MAP:
    print(f"{classic:<38}  {llm}")

# --- PSI in its native habitat: a numeric feature drifting ------------------
print("\nPSI where it belongs: a churn model's 'customer_age' feature:")
rng = random.Random(0)
baseline_age = [rng.gauss(45, 12) for _ in range(5000)]          # training distribution
same_age = [rng.gauss(45, 12) for _ in range(5000)]              # a later, stable month
younger = [rng.gauss(34, 12) for _ in range(5000)]               # marketing shifted the mix

print(f"  PSI(baseline vs a stable later month) = {drift.psi(baseline_age, same_age)}   (expect <0.1)")
print(f"  PSI(baseline vs a younger cohort)      = {drift.psi(baseline_age, younger)}   (expect >0.25)")
print("  Same statistic obs/drift.py used on your LLM inputs. It's genuinely useful;")
print("  it just needs a numeric column, which an LLM app has to manufacture (lengths,")
print("  scores, embedding distances) rather than read off a feature store.")

print("\nTakeaway: the classic stack isn't wrong, it's aimed at a different model.")
print("For an LLM app, the load-bearing signals are the ones this repo built:")
print("embedding drift, refusal/latency/cost trends, and a sampled judge, because")
print("those are the handles you actually have.")
