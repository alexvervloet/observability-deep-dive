#!/usr/bin/env python3
"""
09_otel_export.py: the same telemetry, emitted as real OpenTelemetry over OTLP.

    python examples/09_otel_export.py              # offline: span shapes, no network
    python examples/09_otel_export.py --console    # raw SDK console exporter output
    python examples/09_otel_export.py --otlp       # real OTLP/HTTP to localhost:4318

Every other example in this repo *analyzes* logs. This one *emits* them, in the
format the industry actually ships: OpenTelemetry spans and metrics, over OTLP.
Three things happen here:

  1. One log record becomes one real span, printed in full, so you can read what
     a span actually contains and which attribute names are conventional.
  2. A slice of the simulated history is replayed through the pipeline, and we
     read the aggregated metrics back out, which shows the deep difference
     between spans (one event per request) and metrics (pre-aggregated, cheap).
  3. A *live* call is instrumented with a context manager, which is how you would
     really use this in an app: three lines around the call you already make.

The honest framing this example ends on: adopting OTel replaces the *plumbing*
you built in Sections 2 and 3. It does not replace Sections 4 through 10, because
no amount of well-formed telemetry tells you that quality drifted. Transport is
not judgement.
"""

import argparse
import json
import os
import socket
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obs import otel, providers, simulate

ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
ap.add_argument("--otlp", action="store_true", help="export over real OTLP/HTTP")
ap.add_argument("--console", action="store_true", help="use the SDK's console exporter")
ap.add_argument("--endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", otel.DEFAULT_OTLP_ENDPOINT))
ap.add_argument("--records", type=int, default=300, help="how many historical records to replay")
ap.add_argument("--capture-content", action="store_true",
                help="also put question/answer text on spans (a PII decision, off by default)")
args = ap.parse_args()

otel.require()  # a clear install message beats an ImportError traceback

records, _ = simulate.generate(7, requests_per_day=120)
print(f"Simulated history: {len(records)} requests over 7 days.\n")

# --- 1. What one span actually looks like -----------------------------------
print("=" * 72)
print("1. ONE REQUEST, AS A SPAN")
print("=" * 72)
solo = otel.setup(exporter="memory")
otel.emit(solo, records[0], capture_content=args.capture_content)
span = solo.finished_spans()[0]
print(json.dumps(json.loads(span.to_json()), indent=2)[:1800])
solo.shutdown()
print("""
Read the attribute keys, not the values. The `gen_ai.*` ones are **semantic
conventions**: an LLM-aware backend recognizes them and renders a model call view
without being told anything about our app. The `app.*` ones are ours, because no
convention covers per-request dollars, prompt version, or cache outcome. Cost in
particular is deliberately unstandardized: it is priced per vendor, per model,
per contract, so OTel refuses to guess.

Two things that are NOT in the span by default: the question and the answer. Span
attributes travel to a third-party store and get indexed and retained there, so
message content is gated behind a flag in every real GenAI instrumentation
(--capture-content here). Most teams turn it on for a sampled slice, never for
all traffic.""")

# --- 2. Spans vs metrics ----------------------------------------------------
print("\n" + "=" * 72)
print(f"2. REPLAYING {args.records} REQUESTS: SPANS ARE EVENTS, METRICS ARE AGGREGATES")
print("=" * 72)
batch = records[: args.records]
if args.console:
    print("(--console: the SDK's ConsoleSpanExporter prints every span as JSON.")
    print(" Watch it scroll. This is exactly why nobody runs it in production.)\n")
    bulk = otel.setup(exporter="console")
    otel.replay(bulk, batch[:5])
    bulk.shutdown()
    print(f"\n(Stopped after 5 of {len(batch)}. You get the idea.)")
else:
    bulk = otel.setup(exporter="memory")
    n = otel.replay(bulk, batch)
    spans = bulk.finished_spans()
    errors = sum(1 for s in spans if s.status.status_code.name == "ERROR")
    tokens = sum(s.attributes["gen_ai.usage.input_tokens"] + s.attributes["gen_ai.usage.output_tokens"]
                 for s in spans)
    print(f"  {n} requests  ->  {len(spans)} spans, {errors} marked ERROR, {tokens:,} tokens total")
    print(f"\n  ... and the SAME traffic, as metrics (read straight back out of the SDK):\n")
    snapshot = bulk.metric_snapshot()
    for row in snapshot:
        keys = row["attributes"]
        label = keys.get("gen_ai.token.type") or (
            f"{keys.get('app.outcome', '')}/{keys['app.cache']}" if "app.cache" in keys else "all")
        if row["kind"] == "histogram":
            avg = row["sum"] / row["count"] if row["count"] else 0
            body = f"n={row['count']:<5} sum={row['sum']:<12.3f} avg={avg:.3f}"
        else:
            body = f"total={row['value']:<12.6f}" if row["unit"] == "USD" else f"total={row['value']:<12.0f}"
        print(f"    {row['name']:<34} {row['kind']:<10} {body:<36} {row['unit']:<10} [{label}]")
    print(f"\n  {len(spans)} span events, versus {len(snapshot)} metric points. Ship 10x the traffic")
    print("  and the left number grows 10x; the right one does not move.")
    bulk.shutdown()
    print("""
That ratio is the whole design. Spans are one event per request: rich, sliceable,
and priced per event, so at real volume you sample them. Metrics are aggregated
in-process before they leave: 300 requests or 300 million, you ship the same few
histograms, so you keep them at 100%. The rule of thumb that follows: **alert on
metrics, debug on traces.** Note also that the metric attributes are a smaller set
than the span attributes. Every distinct combination is a separate time series, so
trace_id on a metric would be a cardinality explosion, while on a span it is free.""")

# --- 3. Instrumenting a live call -------------------------------------------
print("\n" + "=" * 72)
print("3. A LIVE CALL, INSTRUMENTED")
print("=" * 72)
print(f"Provider: {providers.describe()}\n")
live = otel.setup(exporter="memory")
question = "How do I rotate my API key?"
answer = "Open Settings, then API keys, and click Rotate. The old key stops working in 24 hours."
with otel.llm_span(live, model="mock-judge", provider=providers.provider_name(),
                   operation="chat") as sp:
    score = providers.score_answer(question, answer)
    sp.set_attribute("gen_ai.evaluation.name", "answer_quality")
    sp.set_attribute("gen_ai.evaluation.score.value", score)
live_span = live.finished_spans()[0]
ms = (live_span.end_time - live_span.start_time) / 1e6
print(f"  judge score {score:.2f}, captured on span {live_span.name!r} ({ms:.1f}ms, timed by the SDK)")
print("""
That is the whole integration for real code:

    with otel.llm_span(tel, model=..., provider=...) as span:
        answer = call_the_model(question)
        span.set_attribute("gen_ai.usage.output_tokens", n)

Timing, status, and exception recording come free. If the body raises, the span is
marked ERROR with the traceback attached, which is the part people forget when
they hand-roll timing around a try/except. Note the judge score riding along as
`gen_ai.evaluation.*`: the sampled quality signal from Section 6 is telemetry too,
and putting it on the same span is how a backend can chart quality beside latency.""")
live.shutdown()

# --- 4. The real wire -------------------------------------------------------
print("\n" + "=" * 72)
print("4. THE WIRE")
print("=" * 72)
if not args.otlp:
    print(f"""Nothing above touched the network; the exporters were in-memory. To send the
same telemetry over the real OTLP protocol, start a receiver in another terminal:

    python hands_on/otel_collector.py         # a 150-line OTLP/HTTP server, no Docker

then re-run this with --otlp:

    python examples/09_otel_export.py --otlp

The receiver decodes the actual gzipped protobuf and prints what arrived. A real
collector, or Jaeger, or a vendor endpoint, takes byte-for-byte the same payload:

    docker run -p 4318:4318 -p 16686:16686 jaegertracing/all-in-one

Only the URL and an auth header change. That interchangeability is what the
standard is for, and it is why "we use OpenTelemetry" is a decision you can
make before you have picked a backend.""")
else:
    parsed = urlparse(args.endpoint)
    host, port = parsed.hostname or "localhost", parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except OSError as exc:
        print(f"Nothing is listening on {args.endpoint} ({exc}).\n")
        print("Start the local receiver in another terminal, then re-run:")
        print("    python hands_on/otel_collector.py")
        sys.exit(1)

    wire = otel.setup(exporter="otlp", endpoint=args.endpoint)
    sent = otel.replay(wire, batch, capture_content=args.capture_content)
    print(f"Exporting {sent} spans, pipeline {wire.describe()}, as gzipped protobuf ...")
    # shutdown() flushes: without it, the batch processor's queue dies with the
    # process and your spans never leave. This is the #1 "my traces are missing" bug.
    wire.shutdown()
    print("Flushed. Check the receiver's terminal: those spans crossed a socket.")
    print("""
Timestamps: the replay slid the whole batch forward to land at 'now'. Backends
reject or hide spans that are too old (Tempo and Jaeger have look-back windows,
some vendors drop anything older than a few hours), so backfilling weeks-old
history needs that shift. Live instrumentation never has this problem.""")

print("\n" + "-" * 72)
print("""What OTel replaced, and what it did not:

  replaced   writing telemetry to a file (Section 2), and computing metrics from
             it yourself (Section 3). A backend does both, at any volume, with
             retention and a query language you did not write.
  NOT replaced  baselines and z-scores (Section 4), drift detection (Section 5),
             the sampled judge (Section 6), alert tuning (Section 7), mining
             failures into eval cases (Section 8). OTel moves numbers. Deciding
             which numbers mean 'this is worse than last week' is still yours.

That is the honest shape of every 'just use the industry tool' upgrade in this
series: it buys you the plumbing, not the judgement.""")
