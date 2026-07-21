"""
obs/judge.py: score a *sample* of answers, because you can't grade them all.

Latency and cost are free to measure; they're already in the log. **Quality
isn't.** The only ways to know if answers are getting worse are to ask a human
(slow, expensive) or ask a model (an LLM-as-judge, from the Evals dive: cheap per
call, but not free). Either way you cannot afford to grade every request, so
production quality monitoring is inherently a **sampling** problem:

  1. Take a representative sample of answers per window (per day, here).
  2. Score each with a judge (the mock's rule-based scorer offline, or a real LLM).
  3. Aggregate to a mean **with a confidence interval**, because a mean over 20
     sampled answers is a point estimate; a drop from 0.82 to 0.78 might be noise.

That last point is the whole discipline, straight from the Evals dive: report the
uncertainty, and don't declare a quality regression that sits inside it. This is
what lets the capstone catch the injected model-swap regression *and* not cry wolf
on the day-to-day wobble around it.
"""

from __future__ import annotations

import math
import random

from obs.logs import LogRecord, by_day
from obs import providers


def mean_ci(values: list[float], z: float = 1.96) -> tuple[float, float]:
    """Sample mean and its 95% margin of error (z * sd / sqrt(n)). The margin is
    what keeps you honest: a 0.04 drop with a ±0.06 margin is not a regression yet."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n == 1:
        return round(mean, 4), 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return round(mean, 4), round(z * math.sqrt(var / n), 4)


def _judgeable(records: list[LogRecord]) -> list[LogRecord]:
    """Answers worth scoring: an actual attempted answer (skip refusals, empties,
    errors). We deliberately do NOT judge refusals; refusal *rate* is already its
    own metric, and mixing it in would make a judge-score drop un-attributable
    (was the model worse, or just refusing more?). Judging only attempted answers
    keeps this metric about the quality of the answers you did give."""
    return [r for r in records if r.outcome == "answered" and r.answer]


def judge_by_day(
    records: list[LogRecord],
    *,
    per_day: int = 20,
    score=providers.score_answer,
    seed: int = 0,
) -> list[dict]:
    """Score a per-day sample of answers and return rows:
    {day, judge_score, margin, n}. `per_day` is your sampling budget; bigger costs
    more (real judge calls) but shrinks the margin. Sampling is seeded, so a run is
    reproducible."""
    rows = []
    for day, recs in by_day(records).items():
        pool = _judgeable(recs)
        if not pool:
            rows.append({"day": day, "judge_score": None, "margin": 0.0, "n": 0})
            continue
        rng = random.Random(f"{seed}:{day}")
        sample = rng.sample(pool, min(per_day, len(pool)))
        scores = [score(r.question, r.answer) for r in sample]
        mean, margin = mean_ci(scores)
        rows.append({"day": day, "judge_score": round(mean, 4), "margin": margin, "n": len(sample)})
    return rows
