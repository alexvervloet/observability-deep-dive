"""
obs/drift.py — detect when the *inputs* stop looking like they used to.
=======================================================================

Your eval passed on launch day against the questions you had then. Six weeks
later, are users still asking those questions? **Input drift** is the shift in the
distribution of what comes in — and it's dangerous precisely because nothing
errors. The model dutifully answers questions it was never good at; your latency
and error dashboards stay green while the answers quietly get worse.

Three ways to see it, cheapest first:

  1. **Novel-term rate** — the fraction of questions using words that barely
     appeared in your baseline period. Pure string counting, no model. A cheap
     early warning that the *vocabulary* changed.

  2. **Embedding drift** — embed each question and measure how far the day's
     questions sit, on average, from the baseline period's center of mass. This
     catches drift in *meaning* even when the words are ordinary. Needs an
     embedding function (the mock's hashed vectors work offline).

  3. **PSI (Population Stability Index)** — the classic MLOps statistic for "how
     much did this distribution move?", here applied to any numeric signal (answer
     length, a similarity score). We implement it once, honestly, because you'll
     meet it everywhere — and reuse it in the classic-MLOps sidebar.

None of these tells you drift is *bad* — only that it happened. Deciding whether a
shift matters is a judgment call the alerting layer frames, not a number.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from obs.logs import LogRecord, by_day
from obs import providers

_WORD_RE = re.compile(r"[a-z0-9']+")


# --- 1. Novel-term rate -----------------------------------------------------

def baseline_vocabulary(records: list[LogRecord], min_count: int = 3) -> set[str]:
    """The set of words that appeared often enough in the baseline to count as
    'normal'. Rare one-off words are excluded so a single oddball doesn't define
    the vocabulary."""
    counts: Counter[str] = Counter()
    for r in records:
        counts.update(_WORD_RE.findall(r.question.lower()))
    return {w for w, c in counts.items() if c >= min_count}


def novel_term_rate(records: list[LogRecord], vocab: set[str]) -> float:
    """Fraction of questions containing at least one word not in the baseline
    vocabulary. Rises when users start using words your history never saw."""
    if not records:
        return 0.0
    novel = 0
    for r in records:
        words = set(_WORD_RE.findall(r.question.lower()))
        if words - vocab:
            novel += 1
    return round(novel / len(records), 4)


# --- 2. Embedding drift -----------------------------------------------------

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def centroid(vectors: list[list[float]]) -> list[float]:
    """The mean vector — the 'center of mass' of a set of questions."""
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    return [x / len(vectors) for x in acc]


def mean_distance_to(center: list[float], vectors: list[list[float]]) -> float:
    """Average cosine *distance* (1 - similarity) from a set of vectors to a fixed
    center. Higher = the questions moved away from where they used to sit."""
    if not center or not vectors:
        return 0.0
    return round(sum(1.0 - cosine(center, v) for v in vectors) / len(vectors), 4)


def embedding_drift_by_day(
    records: list[LogRecord], baseline_days: int, embed=providers.embed
) -> list[dict]:
    """For each day, the mean embedding distance of that day's questions from the
    baseline period's centroid. Returns rows: {day, drift, requests}.

    The baseline is the first `baseline_days` days — 'what normal looked like'. Days
    after it are scored against that fixed center, so a rising `drift` means today's
    questions no longer resemble the ones you launched on.
    """
    days = by_day(records)
    day_keys = list(days)
    base_records = [r for d in day_keys[:baseline_days] for r in days[d]]
    base_centroid = centroid([embed(r.question) for r in base_records])
    rows = []
    for d in day_keys:
        vecs = [embed(r.question) for r in days[d]]
        rows.append({"day": d, "drift": mean_distance_to(base_centroid, vecs), "requests": len(vecs)})
    return rows


# --- 3. PSI (Population Stability Index) ------------------------------------

def psi(expected: list[float], actual: list[float], bins: int = 10) -> float:
    """Population Stability Index between a baseline ('expected') and current
    ('actual') sample of a numeric quantity.

    Bin the baseline into `bins` equal-frequency buckets, then compare what
    fraction of each sample falls in each bucket:  Σ (a - e) * ln(a / e).
    Rule of thumb from credit-risk modeling, where it was born:
        < 0.1  no meaningful shift · 0.1-0.25 moderate · > 0.25 major shift.
    """
    if not expected or not actual:
        return 0.0
    xs = sorted(expected)
    # Equal-frequency bin edges from the baseline.
    edges = [xs[min(int(len(xs) * i / bins), len(xs) - 1)] for i in range(1, bins)]

    def bucket(v: float) -> int:
        for i, e in enumerate(edges):
            if v <= e:
                return i
        return bins - 1

    eps = 1e-6
    e_counts = [0] * bins
    a_counts = [0] * bins
    for v in expected:
        e_counts[bucket(v)] += 1
    for v in actual:
        a_counts[bucket(v)] += 1
    total = 0.0
    for e_c, a_c in zip(e_counts, a_counts):
        e_frac = e_c / len(expected) or eps
        a_frac = a_c / len(actual) or eps
        e_frac, a_frac = max(e_frac, eps), max(a_frac, eps)
        total += (a_frac - e_frac) * math.log(a_frac / e_frac)
    return round(total, 4)
