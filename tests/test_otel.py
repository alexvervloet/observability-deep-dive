"""
tests/test_otel.py: pin the instrumentation, because nothing else will.

obs/otel.py argues that instrumentation rots silently: rename an attribute and
every dashboard and alert keyed to it goes quiet, while the code keeps running,
the spans keep flowing, and no exception is ever raised. A test is the only thing
that turns that into a red build, so this file makes the argument instead of just
stating it.

What it pins is deliberately the *contract*, not the internals: the conventional
attribute names a backend keys off, the span name shape, the status mapping, the
PII default, and the metric shape (including which attributes are allowed to
appear on a metric at all). Those are the things a backend and an alert depend on.

    python -m unittest discover -s tests

Skips itself when the optional OpenTelemetry extras are not installed, so it is
safe to run in the default environment.
"""

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import logs, otel, simulate

if not otel.available():
    raise unittest.SkipTest("OpenTelemetry extras not installed")


def record(**overrides) -> logs.LogRecord:
    """One representative record, with the fields a test cares about overridable."""
    base = logs.LogRecord(
        ts=1_800_000_000.0, trace_id="070000042", question="How do I reset my password?",
        prompt_version="v2", model="acme-support-1", provider="mock",
        prompt_tokens=70, completion_tokens=30, cost_usd=0.00005, duration_ms=420.0,
        cache="miss", outcome="answered", answer_chars=100, segment="pro",
        answer="Open Settings, then Security.",
    )
    return dataclasses.replace(base, **overrides)


class SpanContract(unittest.TestCase):
    def setUp(self):
        self.tel = otel.setup(exporter="memory")
        self.addCleanup(self.tel.shutdown)

    def test_conventional_attributes_are_present_and_spelled_correctly(self):
        """The gen_ai.* names are the whole reason a backend understands us."""
        otel.emit(self.tel, record())
        attrs = self.tel.finished_spans()[0].attributes
        self.assertEqual(attrs["gen_ai.operation.name"], "chat")
        self.assertEqual(attrs["gen_ai.provider.name"], "mock")
        self.assertEqual(attrs["gen_ai.request.model"], "acme-support-1")
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], 70)
        self.assertEqual(attrs["gen_ai.usage.output_tokens"], 30)
        # The rename that already happened once upstream: prompt/completion became
        # input/output. If a future SDK bump reintroduces the old spelling, fail.
        self.assertNotIn("gen_ai.usage.prompt_tokens", attrs)
        self.assertNotIn("gen_ai.usage.completion_tokens", attrs)

    def test_our_own_attributes_stay_in_our_own_namespace(self):
        """Anything not covered by a convention must be prefixed, never invented
        into the gen_ai namespace where a future convention could collide."""
        otel.emit(self.tel, record())
        attrs = self.tel.finished_spans()[0].attributes
        self.assertEqual(attrs["app.cost.usd"], 0.00005)
        self.assertEqual(attrs["app.prompt.version"], "v2")
        self.assertEqual(attrs["app.segment"], "pro")
        invented = [k for k in attrs if k.startswith("gen_ai.")
                    and k not in otel.CONVENTIONAL_ATTRIBUTES]
        self.assertEqual(invented, [], f"unrecognized gen_ai.* keys: {invented}")

    def test_span_name_is_low_cardinality(self):
        """A span name is an index key. The question must never reach it."""
        rec = record(question="something unique to this one user")
        otel.emit(self.tel, rec)
        span = self.tel.finished_spans()[0]
        self.assertEqual(span.name, "chat acme-support-1")
        self.assertNotIn(rec.question, span.name)

    def test_error_outcome_sets_error_status(self):
        otel.emit(self.tel, record(outcome="error"))
        span = self.tel.finished_spans()[0]
        self.assertEqual(span.status.status_code.name, "ERROR")
        self.assertEqual(span.attributes["error.type"], "provider_error")

    def test_message_content_is_off_by_default(self):
        """The PII default. If this test starts failing, someone made user text
        leave the process by accident."""
        rec = record()
        otel.emit(self.tel, rec)
        attrs = self.tel.finished_spans()[0].attributes
        self.assertNotIn("gen_ai.input.messages", attrs)
        self.assertNotIn("gen_ai.output.messages", attrs)
        self.assertNotIn(rec.question, str(dict(attrs)))

    def test_message_content_is_structured_when_enabled(self):
        import json

        otel.emit(self.tel, record(), capture_content=True)
        attrs = self.tel.finished_spans()[0].attributes
        messages = json.loads(attrs["gen_ai.input.messages"])
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["parts"][0]["content"], "How do I reset my password?")

    def test_the_same_log_trace_id_always_maps_to_the_same_otel_trace_id(self):
        """Continuity with the ids already in your logs is the point of the hash."""
        otel.emit(self.tel, record(trace_id="abc"))
        otel.emit(self.tel, record(trace_id="abc"))
        otel.emit(self.tel, record(trace_id="xyz"))
        a, b, c = (s.context.trace_id for s in self.tel.finished_spans())
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class MetricContract(unittest.TestCase):
    def setUp(self):
        self.tel = otel.setup(exporter="memory")
        self.addCleanup(self.tel.shutdown)

    def test_metric_names_and_units(self):
        otel.emit(self.tel, record())
        by_name = {row["name"]: row for row in self.tel.metric_snapshot()}
        self.assertEqual(by_name["gen_ai.client.operation.duration"]["unit"], "s")
        self.assertEqual(by_name["gen_ai.client.token.usage"]["unit"], "{token}")
        self.assertEqual(by_name["app.llm.cost.usd"]["unit"], "USD")
        self.assertEqual(by_name["app.llm.requests"]["unit"], "{request}")

    def test_duration_is_recorded_in_seconds_not_milliseconds(self):
        """The convention is seconds. Shipping milliseconds under a unit of 's'
        is a silent 1000x error that every chart inherits."""
        otel.emit(self.tel, record(duration_ms=420.0))
        duration = next(r for r in self.tel.metric_snapshot()
                        if r["name"] == "gen_ai.client.operation.duration")
        self.assertAlmostEqual(duration["sum"], 0.42, places=6)

    def test_metrics_carry_no_high_cardinality_attributes(self):
        """One new time series per request is how a cheap instrument becomes an
        unbounded bill. Spans may carry these; metrics may not."""
        forbidden = {"app.log.trace_id", "gen_ai.input.messages", "app.segment"}
        for row in self.tel.metric_snapshot():
            self.assertEqual(forbidden & set(row["attributes"]), set(),
                             f"{row['name']} carries a high-cardinality attribute")

    def test_metric_count_is_bounded_by_attributes_not_by_traffic(self):
        """The claim the README makes, asserted in the form that is actually true.

        Metric points scale with the number of distinct *attribute combinations*,
        not with request count. Ten times the traffic gives ten times the spans and
        roughly the same handful of points, but not always the identical number:
        a rarer outcome (an error, say) that never occurred in the small sample
        adds one combination when it finally appears. Bounded, not frozen. The
        first version of this test asserted equality and was right to fail.
        """
        small, _ = simulate.generate(1, requests_per_day=30)
        big, _ = simulate.generate(1, requests_per_day=300)

        otel.replay(self.tel, small)
        small_points = len(self.tel.metric_snapshot())
        small_spans = len(self.tel.finished_spans())

        other = otel.setup(exporter="memory")
        self.addCleanup(other.shutdown)
        otel.replay(other, big)
        big_points = len(other.metric_snapshot())
        big_spans = len(other.finished_spans())

        self.assertGreater(big_spans, small_spans * 5, "spans should track traffic")
        # A few new combinations may appear; an order of magnitude may not.
        self.assertLessEqual(big_points, small_points + 4)
        # The real point: points are a rounding error against span count.
        self.assertLess(big_points * 20, big_spans)


if __name__ == "__main__":
    unittest.main()
