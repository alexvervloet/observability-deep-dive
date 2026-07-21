"""
obs/alerts.py: turn a metric time series into alerts, without paging on noise.

A dashboard nobody is staring at is useless; the point of monitoring is to be
*told* when something breaks. But the naive version ("page me if p95 > 300ms")
fails both ways at once: too tight and you get woken at 3am by normal daily
wobble until you mute the alert (alert fatigue, and the muted alert is the one that
misses the real outage); too loose and you sleep through the incident.

This module builds a more honest detector from three ideas:

  - **Baseline + z-score.** Learn what normal looks like from a clean baseline
    window (mean and spread), then measure each new day in *standard deviations*
    from it. "3σ above baseline" travels across metrics; "> 300ms" doesn't.

  - **Direction.** For latency, cost, refusals, errors, *up* is bad; for a quality
    score, *down* is bad. A detector only fires on the bad direction.

  - **Persistence (hysteresis).** Require the breach to hold for N consecutive days
    before paging. This is the dial that separates a **transient spike** (one bad
    day, worth a look but not a 3am page) from a **sustained regression** (the drift
    that actually costs you). Set persistence=1 to catch spikes, 3 to catch trends.

The honest tradeoff lives in those two knobs, `z_threshold` and `persistence`:
every setting trades false alarms against detection lag, and there is no value
that gives you zero of both. The capstone tunes them so the injected 1-day
latency blip does *not* page as a trend while the multi-week drifts do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def baseline_stats(values: list[float], robust: bool = False) -> tuple[float, float]:
    """Center and spread of the baseline window.

    `robust=True` uses the median and MAD (median absolute deviation) instead of
    mean/stdev, so a single wild day in the baseline doesn't inflate 'normal' and
    hide later problems. Robust statistics are the right default when the baseline
    might itself contain a blip.
    """
    if not values:
        return 0.0, 0.0
    if robust:
        med = _median(values)
        mad = _median([abs(v - med) for v in values])
        return med, mad * 1.4826  # scale MAD to be comparable to a stdev
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(var)


def _median(values: list[float]) -> float:
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def ewma(values: list[float], alpha: float = 0.3) -> list[float]:
    """Exponentially weighted moving average: a smoother that reacts to a
    sustained shift but rides over single-day spikes. An alternative to persistence
    for suppressing noise; shown alongside it in the alerting example."""
    out: list[float] = []
    acc = values[0] if values else 0.0
    for v in values:
        acc = alpha * v + (1 - alpha) * acc
        out.append(round(acc, 6))
    return out


@dataclass
class Detector:
    """A rule that watches one metric.

    metric       the key in each daily row to watch (e.g. "p95_latency_ms").
    direction    "up" if a rising value is bad, "down" if a falling value is bad.
    z_threshold  how many baseline std-devs past normal counts as a breach.
    persistence  consecutive breach days required before it fires (1 = spike).
    baseline_days how many leading days define 'normal' (must be a clean window).
    robust       use median/MAD for the baseline instead of mean/stdev.
    """

    metric: str
    direction: str = "up"
    z_threshold: float = 3.0
    persistence: int = 3
    baseline_days: int = 7
    robust: bool = False


def signed_z(rows: list[dict], detector: Detector) -> list[dict]:
    """Per-day rows: {day, value, z} where z is signed so that positive always
    means 'in the bad direction'. Days without the metric are skipped."""
    pts = [(r["day"], r[detector.metric]) for r in rows
           if detector.metric in r and r[detector.metric] is not None]
    base_vals = [v for _, v in pts[:detector.baseline_days]]
    center, spread = baseline_stats(base_vals, robust=detector.robust)
    spread = max(spread, 1e-9)  # avoid divide-by-zero on a flat baseline
    sign = 1.0 if detector.direction == "up" else -1.0
    return [{"day": day, "value": v, "z": round(sign * (v - center) / spread, 2)} for day, v in pts]


def detect(rows: list[dict], detector: Detector) -> list[dict]:
    """Run a detector over the daily rows and return one alert per breach *run*.

    An alert fires on the day a run of `persistence` consecutive breaches
    completes, so a single bad day never trips a persistence=3 trend detector, and
    the alert carries the detection day (for measuring how fast you caught it).
    We never re-fire within the same contiguous breach run.
    """
    zrows = signed_z(rows, detector)
    alerts = []
    run = 0
    fired_this_run = False
    for i, zr in enumerate(zrows):
        if i < detector.baseline_days:
            continue  # warmup: never alert on the baseline itself
        if zr["z"] >= detector.z_threshold:
            run += 1
            if run >= detector.persistence and not fired_this_run:
                start = zrows[i - run + 1]["day"]
                alerts.append({
                    "metric": detector.metric,
                    "direction": detector.direction,
                    "fired_on": zr["day"],
                    "breach_started": start,
                    "z": zr["z"],
                    "value": zr["value"],
                })
                fired_this_run = True
        else:
            run = 0
            fired_this_run = False
    return alerts
