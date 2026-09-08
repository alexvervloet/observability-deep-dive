"""
tests/test_detection.py: pin the claims the prose makes about detection.

The README, the textbook chapter, and the exercises all quote numbers this repo
produces: which incidents the capstone catches, how many days late, that the
latency *trend* guardrail stays silent on a one-day spike, that a clean history
fires nothing, and how big the top failure cluster is. Every one of those is
prose until something checks it, and prose cannot fail. An audit found four of
them had drifted away from what the code prints while every reading of the text
missed it, which is the same lesson `tests/test_otel.py` learned about
instrumentation, applied to the detectors.

So this file asserts the claims rather than the internals. Retuning a detector is
allowed; retuning it without updating the sentence that quotes it is not.

    python -m unittest discover -s tests

Everything here runs on the offline simulator, so it needs no key and no network.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hands_on"))

from obs import mining, simulate
import watch

# The capstone's own defaults. A test that quietly used different ones would be
# grading a different dashboard than the reader runs.
DAYS, SEED, PER_DAY, BASELINE_DAYS = 42, 7, 20, 7


def _run(healthy=False):
    incidents = [] if healthy else None
    records, incidents = simulate.generate(DAYS, seed=SEED, incidents=incidents)
    rows = watch.build_rows(records, PER_DAY, BASELINE_DAYS)
    for _, _, det in watch.DETECTORS:
        det.baseline_days = BASELINE_DAYS
    fired = watch.run_alerts(rows)
    days = [r["day"] for r in rows]
    graded, trend_fired = watch.grade(incidents, fired, days)
    return records, rows, fired, graded, trend_fired


class DetectionReport(unittest.TestCase):
    """README section 10 and chapter 16.5 quote this report line by line."""

    @classmethod
    def setUpClass(cls):
        cls.records, cls.rows, cls.fired, cls.graded, cls.trend_fired = _run()

    def test_every_injected_incident_is_caught(self):
        """'On the default history it catches all four incidents.'"""
        missed = [g["kind"] for g in self.graded if not g["caught"]]
        self.assertEqual(missed, [], f"detectors missed {missed}")
        self.assertEqual(len(self.graded), 4)

    def test_detection_lag_per_incident(self):
        """The lags the README and the chapter both quote.

        If a detector is retuned these move, and the sentence quoting them has to
        move with it. That is the whole point of pinning them.
        """
        lags = {g["kind"]: g["lag"] for g in self.graded}
        self.assertEqual(lags, {
            "latency_spike": 0,
            "input_drift": 2,
            "cost_creep": 2,
            "quality_regression": 2,
        })

    def test_the_trend_guardrail_stays_silent_on_a_one_day_spike(self):
        """'the latency regression detector correctly stays silent'.

        This is the claim the whole alerting section rests on: persistence is
        what tells a bad Tuesday apart from a regression. If this fires, the
        repo's central tradeoff demo is broken.
        """
        self.assertFalse(self.trend_fired)

    def test_the_spike_detector_does_fire(self):
        """The counterpart. A guardrail that is silent because nothing works is
        not a guardrail, so pin that the persistence=1 detector still sees it."""
        spike = [a for a in self.fired if a["label"] == "latency spike"]
        self.assertEqual(len(spike), 1)


class HealthyHistory(unittest.TestCase):
    """'On --healthy it fires nothing.' The false-alarm half of the claim."""

    def test_no_detector_fires_on_a_clean_history(self):
        _, _, fired, graded, trend_fired = _run(healthy=True)
        self.assertEqual([a["label"] for a in fired], [])
        self.assertEqual(graded, [])
        self.assertFalse(trend_fired)


class MiningClaims(unittest.TestCase):
    """README section 8 and chapter 16.6 quote the top cluster's size."""

    def test_top_cluster_is_the_unsupported_mobile_app(self):
        records, _ = simulate.generate(DAYS, seed=SEED)
        fails = mining.failures(records)
        top = mining.cluster(fails, records, top=1)[0]
        self.assertEqual(top["term"], "app")
        self.assertEqual(top["count"], 900)

    def test_most_failures_carry_no_feedback(self):
        """'Most failures are silent' is an argument, not a mood. If feedback
        ever became dense enough to wait for, the section's advice would change."""
        records, _ = simulate.generate(DAYS, seed=SEED)
        fails = mining.failures(records)
        silent = mining.unrated_but_failing(records)
        self.assertGreater(silent / len(fails), 0.5)


class BaselineClaims(unittest.TestCase):
    """README section 4 and chapter 16.3 quote the cost baseline and its z."""

    def test_cost_baseline_and_peak_z(self):
        from obs import alerts, metrics
        records, _ = simulate.generate(DAYS, seed=SEED)
        rows = metrics.daily(records)
        det = alerts.Detector("cost_per_request_usd", "up", baseline_days=BASELINE_DAYS)
        z = alerts.signed_z(rows, det)
        center, _ = alerts.baseline_stats([r["value"] for r in z[:BASELINE_DAYS]])
        peak = max(z, key=lambda r: r["z"])
        # Quoted as "$0.000107, up from a $0.000055 baseline, that's +115sigma".
        self.assertAlmostEqual(center, 0.000055, places=6)
        self.assertAlmostEqual(peak["value"], 0.000107, places=6)
        self.assertAlmostEqual(peak["z"], 115, delta=1)


if __name__ == "__main__":
    unittest.main()
