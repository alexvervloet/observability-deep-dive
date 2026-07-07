"""
obs/metrics.py — turn a pile of log records into the numbers you watch.
=======================================================================

A log file is data; a *metric* is a decision aid. This module reduces a batch of
`LogRecord`s to the dozen or so numbers an on-call engineer actually looks at:
how much traffic, how slow at the tail, how much it cost, how often it errored or
refused, how often the cache saved a call.

Two ideas worth internalizing here:

  - **Percentiles, not averages, for latency.** The mean hides the tail, and the
    tail is what users feel. p95 ("19 of 20 requests were faster than this") is
    the number that tells you whether the slow experiences are rare or common. A
    single slow request barely moves the mean; it *is* the p99.

  - **A metric is only meaningful over a window.** "Cost is $0.004" means nothing;
    "cost per request rose from $0.003 to $0.006 over two weeks" is an incident.
    So the core function computes metrics for *one* window, and you call it per day
    (see `daily`) to get the time series everything else consumes.

Pure standard library, pure arithmetic — no model, no key, no network.
"""

from __future__ import annotations

from obs.logs import LogRecord, by_day


def percentile(values: list[float], p: float) -> float:
    """The p-th percentile (0..100) via linear interpolation. p95 = percentile(x, 95)."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    rank = (p / 100) * (len(xs) - 1)
    lo = int(rank)
    frac = rank - lo
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def window_metrics(records: list[LogRecord]) -> dict:
    """The operational metrics for one window (a day, an hour, whatever you pass).

    Keys are stable strings so the drift, alerting, and dashboard code can refer to
    a metric by name. Rates are fractions in [0, 1].
    """
    n = len(records)
    if n == 0:
        return {"requests": 0}

    latencies = [r.duration_ms for r in records]
    misses = [r for r in records if r.cache == "miss"]
    rated = [r for r in records if r.feedback is not None]
    return {
        "requests": n,
        "p50_latency_ms": round(percentile(latencies, 50), 1),
        "p95_latency_ms": round(percentile(latencies, 95), 1),
        "error_rate": round(sum(r.outcome == "error" for r in records) / n, 4),
        "refusal_rate": round(sum(r.outcome == "refused" for r in records) / n, 4),
        "cache_hit_rate": round(sum(r.cache == "hit" for r in records) / n, 4),
        "cost_usd_total": round(sum(r.cost_usd for r in records), 6),
        # Cost per request counts only paid (cache-miss) calls — the number that
        # moves when the prompt bloats, undiluted by free cache hits.
        "cost_per_request_usd": round(
            sum(r.cost_usd for r in misses) / len(misses), 8) if misses else 0.0,
        "avg_total_tokens": round(sum(r.total_tokens for r in misses) / len(misses), 1) if misses else 0.0,
        "avg_answer_chars": round(sum(r.answer_chars for r in records) / n, 1),
        # Feedback is sparse, so report both the rate AND how many ratings it's from
        # — a -100% neg rate off 2 ratings is noise, not a fire.
        "feedback_count": len(rated),
        "neg_feedback_rate": round(sum(r.feedback == -1 for r in rated) / len(rated), 4) if rated else 0.0,
    }


def daily(records: list[LogRecord]) -> list[dict]:
    """One metrics dict per calendar day, in order. Each row carries its `day` and
    is the time series that baselines, drift detection, and alerting run on."""
    rows = []
    for day, recs in by_day(records).items():
        row = {"day": day, **window_metrics(recs)}
        rows.append(row)
    return rows


def series(rows: list[dict], metric: str) -> list[float]:
    """Pull one metric's values across a list of daily rows (skipping days that
    lack it, e.g. an all-cache day has no cost_per_request)."""
    return [row[metric] for row in rows if metric in row]


def daily_by_segment(records: list[LogRecord]) -> dict[str, list[dict]]:
    """The daily metric series computed *separately for each cohort*.

    This is the one line that catches what a global average hides: a problem
    concentrated in a minority segment (one plan, one region) can leave the overall
    numbers looking fine while that cohort is on fire. Slice first, then run the
    same detectors per segment.
    """
    groups: dict[str, list[LogRecord]] = {}
    for r in records:
        groups.setdefault(r.segment or "unknown", []).append(r)
    return {seg: daily(recs) for seg, recs in sorted(groups.items())}
