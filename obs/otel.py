"""
obs/otel.py: the same telemetry, emitted as *real* OpenTelemetry.

Everything else in this repo analyzes logs the app already wrote. This file goes
the other direction: it takes the same `LogRecord` shape and emits it as genuine
OpenTelemetry **spans** and **metrics**, over the real **OTLP** wire protocol that
Grafana Tempo, Jaeger, Honeycomb, Datadog, Langfuse, and Arize all speak.

Why bother, when the from-scratch stack already works? Because the from-scratch
stack is a closed world. OTel buys you three things you cannot hand-roll cheaply:

  1. **A wire format everyone accepts.** Emit OTLP once and you can change
     backends without touching the app. That is the whole point of the standard.
  2. **Context propagation.** A trace id that survives across services, threads,
     and queues, so one user request is one story even when six processes touched it.
  3. **Instrumentation you did not write.** The HTTP client, the database driver,
     and the OpenAI SDK all have community instrumentation that emits spans into
     the same trace, for free.

And the honest limit, which is the reason the rest of this repo exists: **OTel is
transport, not judgement.** It will happily ship you a million perfectly formed
spans and never once tell you that quality drifted. Baselines, z-scores,
persistence, the sampled judge, the drift detectors: those are still yours to
build (or buy), on top of whatever the backend stores. Swapping in OTel replaces
Sections 2 and 3 of this repo. It does not replace Sections 4 through 10.

## The three exporters here

  console  (default) prints each span as JSON to stdout. Fully offline, no
           backend, no Docker. This is how you see the *shape* of a span.
  otlp     posts real protobuf over HTTP to an OTLP endpoint (default
           http://localhost:4318). Point it at hands_on/otel_collector.py, at a
           real collector, or at a vendor.
  memory   keeps spans in a list so code can assert on them. This is also how you
           test instrumentation, which is the only way it stays correct.

## Semantic conventions

Attribute names are not free-form. OTel publishes **semantic conventions** so that
"which model was this?" is the same key in every language and every backend, and
the GenAI ones (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, and friends)
are what an LLM-aware backend renders as a nice trace view. Two honest caveats:

  - They are still **experimental**. The GenAI group has renamed attributes more
    than once (`gen_ai.usage.prompt_tokens` became `gen_ai.usage.input_tokens`),
    which is why they live under `semconv._incubating` in the Python package. Pin
    your SDK version and expect churn.
  - **There is no conventional cost attribute.** Cost is priced per vendor per
    model per day, so OTel refuses to standardize it. We put it under our own
    `app.*` namespace, which is exactly what you should do: convention names for
    conventional things, your own prefix for the rest.

Nothing here runs by default in the other sections; this module is optional and
so is its dependency.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from obs.logs import LogRecord

_INSTALL_HINT = (
    "This section needs {what}, which is an optional extra:\n"
    "    pip install {packages}\n"
    "(or `pip install -r requirements.txt`, which includes it). Every other\n"
    "section of this repo runs without it."
)

# The SDK and the OTLP exporter are two separate installs, and having one without
# the other is a real state to land in, not a hypothetical: the console and memory
# exporters work on the SDK alone. So each capability is checked on its own.
_SDK_PACKAGE = "opentelemetry-sdk"
_OTLP_PACKAGE = "opentelemetry-exporter-otlp-proto-http"


def _importable(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def available(*, otlp: bool = False) -> bool:
    """True if the optional OTel packages are importable.

    Pass otlp=True to also require the exporter package, which is what putting
    telemetry on the wire needs and what the console and memory exporters do not.
    """
    if not _importable("opentelemetry.sdk.trace"):
        return False
    if otlp and not _importable("opentelemetry.exporter.otlp.proto.http.trace_exporter"):
        return False
    return True


def require(*, otlp: bool = False) -> None:
    """Fail with an actionable message instead of a bare ImportError."""
    if not _importable("opentelemetry.sdk.trace"):
        raise SystemExit("\n" + _INSTALL_HINT.format(
            what="the OpenTelemetry SDK",
            packages=f"{_SDK_PACKAGE} {_OTLP_PACKAGE}") + "\n")
    if otlp and not _importable("opentelemetry.exporter.otlp.proto.http.trace_exporter"):
        raise SystemExit("\n" + _INSTALL_HINT.format(
            what="the OpenTelemetry OTLP/HTTP exporter (the SDK alone is installed)",
            packages=_OTLP_PACKAGE) + "\n")


# The service this telemetry claims to come from. In a real deployment this is
# the single most important attribute you set: every backend groups, filters, and
# bills by service.name first.
SERVICE_NAME = "acme-support-assistant"

DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"

# Every conventional key this module is allowed to set, listed once. Two jobs: it
# documents which names are borrowed rather than invented, and it lets
# tests/test_otel.py fail the build if someone adds a gen_ai.* attribute of their
# own devising. Inventing a name inside somebody else's namespace is how you
# collide with next year's convention; unconventional things belong under app.*.
CONVENTIONAL_ATTRIBUTES = frozenset({
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.evaluation.name",
    "gen_ai.evaluation.score.value",
    "gen_ai.token.type",
})


@dataclass
class Telemetry:
    """A configured OTel pipeline: what you emit through, and how to shut it down.

    Holding this as one object (rather than reaching for globals) keeps the
    example honest about lifecycle. Spans sit in a batch queue until something
    ships them, so *when* the queue drains is a real question rather than a
    detail. Python's SDK is kinder here than its reputation suggests: both
    providers default to `shutdown_on_exit=True` and register an `atexit` hook,
    so a normally-exiting process flushes even if you forget. What that hook
    cannot save you from is every exit that skips `atexit`: `os._exit()`, a
    SIGKILL, an OOM kill, a container stopped past its grace period, a forked
    worker that never runs the parent's handlers. Calling `shutdown()` yourself is
    how you stop depending on which kind of exit you got.
    """

    tracer: Any
    meter: Any
    exporter: str
    endpoint: str = ""
    _providers: tuple = ()
    _span_exporter: Any = None  # only set by exporter="memory"
    _reader: Any = None  # the in-memory metric reader, when there is one

    # Instruments, created once at setup: creating them per call is a real
    # performance bug and some backends treat it as a new time series.
    duration: Any = None
    token_usage: Any = None
    cost: Any = None
    outcomes: Any = None

    def flush(self) -> None:
        """Force everything queued out to the exporter, without stopping.

        This is the one for a long-running process that needs data out *now*: at
        the end of a job, before a risky operation, or in a signal handler.
        """
        for p in self._providers:
            p.force_flush()

    def shutdown(self) -> None:
        """Stop the pipeline, exporting whatever is still queued.

        There is no `force_flush()` call here, because `shutdown()` already
        exports what is queued and doing both sends the batch twice. Whether that
        second copy hurts depends on **temporality**, which is worth knowing: the
        default here is *cumulative*, where every export carries the running
        total, so a duplicate point is the same total at the same timestamp and
        the backend simply overwrites it. Under *delta* temporality, which several
        vendors ask for, each export carries only what happened since the last
        one, and a duplicate is genuinely counted twice. Same code, different
        blast radius, decided by a setting most people never look at."""
        for p in self._providers:
            p.shutdown()

    def finished_spans(self) -> tuple:
        """The spans collected so far, for exporter="memory".

        Asserting on these is how you keep instrumentation from rotting. Rename an
        attribute and nothing throws: the code runs, the spans flow, and every
        dashboard and alert keyed to the old name goes blank. See
        tests/test_otel.py, which pins the conventional names, the status mapping,
        the PII default, and the metric shape for exactly that reason.
        """
        if self._span_exporter is None:
            raise RuntimeError('finished_spans() needs setup(exporter="memory")')
        self.flush()
        return self._span_exporter.get_finished_spans()

    def metric_snapshot(self) -> list[dict]:
        """Read the *aggregated* metrics back out, the way a backend receives them.

        Available with exporter="memory". This is the concrete demonstration that
        a metric is not a pile of events: whatever the traffic volume, what leaves
        the process is a handful of pre-aggregated points.
        """
        if self._reader is None:
            raise RuntimeError('metric_snapshot() needs setup(exporter="memory")')
        data = self._reader.get_metrics_data()
        out: list[dict] = []
        if data is None:
            return out
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    for point in metric.data.data_points:
                        row = {
                            "name": metric.name,
                            "unit": metric.unit,
                            "attributes": dict(point.attributes or {}),
                        }
                        if hasattr(point, "count"):  # a histogram point
                            row.update(kind="histogram", count=point.count, sum=point.sum)
                        else:  # a counter (sum) point
                            row.update(kind="counter", value=point.value)
                        out.append(row)
        return out

    def describe(self) -> str:
        where = f" -> {self.endpoint}" if self.endpoint else ""
        return f"{self.exporter}{where}"


def setup(
    *,
    exporter: str = "console",
    endpoint: str | None = None,
    service_name: str = SERVICE_NAME,
    service_version: str = "1.4.2",
    environment: str = "production",
) -> Telemetry:
    """Build a real TracerProvider + MeterProvider and return a handle to them.

    This is the whole of "adding OpenTelemetry to an app": a resource that says
    who you are, a provider that owns the pipeline, a processor that batches, and
    an exporter that ships. Everything after this is just calling `start_span`.
    """
    require(otlp=(exporter == "otlp"))

    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        InMemoryMetricReader,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # A Resource is the "who" of telemetry: attributes attached to every single
    # span and metric this process emits. Backends key their whole UI off these.
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
        "deployment.environment.name": environment,
    })

    if exporter == "console":
        span_exporter = ConsoleSpanExporter()
        metric_exporter: Any = ConsoleMetricExporter()
        endpoint = ""
    elif exporter == "memory":
        span_exporter = InMemorySpanExporter()
        metric_exporter = None
        endpoint = ""
    elif exporter == "otlp":
        # The real thing: protobuf over HTTP. `grpc` is the other option, and the
        # only difference to your code is which package you import.
        from opentelemetry.exporter.otlp.proto.http import Compression
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = (endpoint or DEFAULT_OTLP_ENDPOINT).rstrip("/")
        # Compression is OFF by default in this SDK (Compression.NoCompression),
        # which surprises people who assume telemetry is compressed. Turning it on
        # is one argument, and it is not a rounding error: 100 spans of this
        # traffic measure 47KB of protobuf and 7KB on the wire, about 7x, because
        # span payloads are the same attribute keys repeated thousands of times.
        # Telemetry bills are usually metered on ingest volume. The receiving end
        # has to handle both, which is why a collector checks Content-Encoding
        # instead of assuming (ours does, in hands_on/otel_collector.py).
        span_exporter = OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces", timeout=5, compression=Compression.Gzip)
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics", timeout=5, compression=Compression.Gzip)
    else:
        raise ValueError(f"unknown exporter {exporter!r}: use console, otlp, or memory")

    tracer_provider = TracerProvider(resource=resource)
    if exporter == "memory":
        # Simple (not batched) so an assertion right after the span sees it.
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        # Batching is what you want in production: it amortizes the network cost
        # and never blocks the request path. The price is that spans leave late,
        # so an app that exits without flushing loses its last batch.
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    if metric_exporter is None:
        reader: Any = InMemoryMetricReader()
    else:
        # Metrics are pre-aggregated in-process and shipped on an interval, which
        # is the deep difference from spans: 10k requests become one histogram,
        # not 10k events. That is why metrics stay cheap at any volume.
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])

    # Registering globally is optional but is what library instrumentation looks
    # for, so an auto-instrumented HTTP client lands in the same trace as our spans.
    # The global provider is deliberately **set-once**: the SDK logs a warning and
    # keeps the first one, on the theory that whoever configured the pipeline first
    # meant it. That is why real apps set it up exactly once at startup, and why we
    # check rather than clobber (this example builds several pipelines in one run,
    # which an app never does).
    if not isinstance(otel_trace.get_tracer_provider(), TracerProvider):
        otel_trace.set_tracer_provider(tracer_provider)
    if not isinstance(otel_metrics.get_meter_provider(), MeterProvider):
        otel_metrics.set_meter_provider(meter_provider)

    tel = Telemetry(
        tracer=tracer_provider.get_tracer("obs.otel", "1.0.0"),
        meter=meter_provider.get_meter("obs.otel", "1.0.0"),
        exporter=exporter,
        endpoint=endpoint or "",
        _providers=(tracer_provider, meter_provider),
        _span_exporter=span_exporter if exporter == "memory" else None,
        _reader=reader if metric_exporter is None else None,
    )
    _make_instruments(tel)
    return tel


def _make_instruments(tel: Telemetry) -> None:
    """Create the four instruments once, at setup.

    The first two are GenAI *conventional* metrics, so an LLM-aware backend
    already knows how to chart them. The last two are ours, under `app.`, because
    no convention covers per-request dollars or app-specific outcomes.
    """
    m = tel.meter
    tel.duration = m.create_histogram(
        "gen_ai.client.operation.duration",
        unit="s",  # the convention is SECONDS, not milliseconds. Backends assume it.
        description="Duration of the model call, as the client saw it.",
    )
    tel.token_usage = m.create_histogram(
        "gen_ai.client.token.usage",
        unit="{token}",
        description="Tokens used, split by gen_ai.token.type (input/output).",
    )
    tel.cost = m.create_counter(
        "app.llm.cost.usd",
        unit="USD",
        description="Money spent. No OTel convention covers this; it is vendor-priced.",
    )
    tel.outcomes = m.create_counter(
        "app.llm.requests",
        unit="{request}",
        description="Request count by outcome, so refusal and error rates are queryable.",
    )


def _trace_id_128(raw: str) -> int:
    """Turn this repo's short trace id into a real 128-bit W3C trace id.

    Your existing logs almost certainly carry *some* request id. Hashing it into
    the OTel id space (deterministically, so re-running lands on the same id) is
    how you keep continuity with telemetry you already have. A greenfield app
    would just let the SDK generate ids and never do this.
    """
    return int.from_bytes(hashlib.blake2b(raw.encode(), digest_size=16).digest(), "big")


def _parent_context(raw_trace_id: str):
    """A non-recording remote parent, so our span joins that trace id.

    This is also exactly how a service picks up a `traceparent` header from
    upstream: build a SpanContext you did not create, and start your span under it.

    One consequence to expect when you point a replay at a real backend: each
    span names a parent that **is never exported**, because the parent is the
    upstream request this repo never recorded. Jaeger will show a trace whose root
    is missing, which looks broken and is not. A live service does not have this
    problem, since the upstream that sent the traceparent exports its own span. If
    you want self-contained traces from a replay instead, drop this parent and let
    each span start a trace of its own.
    """
    from opentelemetry import trace as otel_trace

    ctx = otel_trace.SpanContext(
        trace_id=_trace_id_128(raw_trace_id),
        span_id=_trace_id_128(raw_trace_id + ":root") & ((1 << 64) - 1),
        is_remote=True,
        trace_flags=otel_trace.TraceFlags(otel_trace.TraceFlags.SAMPLED),
    )
    return otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(ctx))


def record_attributes(rec: LogRecord, *, capture_content: bool = False) -> dict:
    """The attribute dict for one request: conventions first, then our own.

    `capture_content` is off by default on purpose. Putting the question and the
    answer on a span is a genuine debugging superpower and a genuine **PII sink**
    (Production §3): those strings land in a third-party store, get indexed, and
    are retained on someone else's schedule. Real instrumentations gate this
    behind exactly such a flag (OTel's own env var is
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT), and most teams turn it on
    for a sampled slice, never for all traffic.
    """
    attrs: dict[str, Any] = {
        # --- GenAI semantic conventions (experimental, but widely rendered) ---
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": rec.provider,
        "gen_ai.request.model": rec.model,
        "gen_ai.response.model": rec.model,
        "gen_ai.usage.input_tokens": rec.prompt_tokens,
        "gen_ai.usage.output_tokens": rec.completion_tokens,
        # --- ours: no convention exists, so we namespace honestly ---
        "app.prompt.version": rec.prompt_version,
        "app.outcome": rec.outcome,
        "app.cache": rec.cache,
        "app.cost.usd": rec.cost_usd,
        "app.answer.chars": rec.answer_chars,
        "app.log.trace_id": rec.trace_id,
    }
    if rec.segment:
        # The cohort dimension from the segmentation section. Free to slice by on a
        # span; on a *metric* it would multiply your time series, which is the
        # tradeoff the metrics below make deliberately.
        attrs["app.segment"] = rec.segment
    if rec.feedback is not None:
        attrs["app.feedback"] = rec.feedback
    if capture_content:
        # The convention is not a bare string: these are JSON-encoded arrays of
        # messages with typed parts, so a backend can render a conversation rather
        # than a blob. Span attributes must be primitives, hence json.dumps.
        attrs["gen_ai.input.messages"] = json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": rec.question}]}])
        attrs["gen_ai.output.messages"] = json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": rec.answer}],
              "finish_reason": "stop"}])
    return attrs


def emit(tel: Telemetry, rec: LogRecord, *, capture_content: bool = False,
         end_time_ns: int | None = None) -> None:
    """Emit one log record as a real span plus its metric contributions.

    Note that the span is created with explicit start and end timestamps taken
    from the record, because we are replaying history rather than serving a live
    request. Live instrumentation never does this; it just wraps the call.
    """
    from opentelemetry import trace as otel_trace

    end_ns = end_time_ns if end_time_ns is not None else int(rec.ts * 1e9)
    start_ns = end_ns - int(rec.duration_ms * 1e6)
    attrs = record_attributes(rec, capture_content=capture_content)

    span = tel.tracer.start_span(
        # The conventional span name is "{operation} {model}", low-cardinality on
        # purpose: never put the user's question in a span name.
        f"chat {rec.model}",
        context=_parent_context(rec.trace_id),
        kind=otel_trace.SpanKind.CLIENT,
        attributes=attrs,
        start_time=start_ns,
    )
    if rec.outcome == "error":
        span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, "request failed"))
        # error.type is the conventional key for "what kind of failure".
        span.set_attribute("error.type", "provider_error")
    else:
        span.set_status(otel_trace.Status(otel_trace.StatusCode.OK))
    span.end(end_time=end_ns)

    # Metric attributes are deliberately a SMALLER set than span attributes. Every
    # distinct combination is its own time series and its own storage cost, so
    # trace_id or the question would be catastrophic here and are fine above.
    common = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": rec.provider,
        "gen_ai.request.model": rec.model,
    }
    tel.duration.record(rec.duration_ms / 1000.0, common)
    tel.token_usage.record(rec.prompt_tokens, {**common, "gen_ai.token.type": "input"})
    tel.token_usage.record(rec.completion_tokens, {**common, "gen_ai.token.type": "output"})
    tel.cost.add(rec.cost_usd, common)
    tel.outcomes.add(1, {**common, "app.outcome": rec.outcome, "app.cache": rec.cache})


def replay(tel: Telemetry, records: list[LogRecord], *, capture_content: bool = False,
           shift_to_now: bool = True) -> int:
    """Push a batch of historical records through the pipeline. Returns the count.

    `shift_to_now` matters more than it looks: most backends **reject or hide
    spans whose timestamps are old** (Tempo and Jaeger have retention and
    look-back windows; some vendors drop anything older than a few hours). Our
    simulated history is weeks old, so by default we slide the whole batch
    forward, keeping the relative spacing, and land it at "now". That is a
    replay-only concern, and knowing it saves an afternoon of "where did my spans
    go" the first time you backfill.
    """
    if not records:
        return 0
    offset_ns = 0
    if shift_to_now:
        newest = max(r.ts for r in records)
        offset_ns = int((time.time() - newest) * 1e9)
    for rec in records:
        emit(tel, rec, capture_content=capture_content,
             end_time_ns=int(rec.ts * 1e9) + offset_ns)
    return len(records)


@contextmanager
def llm_span(tel: Telemetry, *, model: str, provider: str, operation: str = "chat",
             **attributes: Any) -> Iterator[Any]:
    """Instrument a **live** call: the way you would actually use this in an app.

    Everything above replays logs you already have. This is the other half, and
    the shorter one:

        with otel.llm_span(tel, model="gpt-5.4-nano", provider="openai") as span:
            answer = call_the_model(question)
            span.set_attribute("gen_ai.usage.output_tokens", n_tokens)

    Timing, status, and exception recording come for free from the context
    manager. If the body raises, the span is marked ERROR with the stack trace
    attached, which is the behaviour people forget when they hand-roll a timer and
    a try/except around the call.

    The duration metric is measured with `time.perf_counter()` rather than the
    wall clock, deliberately. Wall-clock time can jump backwards or forwards
    (NTP corrections, a suspended laptop) and would silently poison a latency
    histogram; a monotonic clock is the right instrument for "how long did this
    take". The span keeps its own wall-clock start and end because a span is an
    *event on a timeline* that has to line up with other services, which is a
    different question from elapsed time and deserves a different clock.
    """
    from opentelemetry import trace as otel_trace

    attrs = {
        "gen_ai.operation.name": operation,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        **attributes,
    }
    start = time.perf_counter()
    with tel.tracer.start_as_current_span(
        f"{operation} {model}", kind=otel_trace.SpanKind.CLIENT, attributes=attrs
    ) as span:
        try:
            yield span
        finally:
            tel.duration.record(
                time.perf_counter() - start,
                {"gen_ai.operation.name": operation, "gen_ai.provider.name": provider,
                 "gen_ai.request.model": model},
            )
