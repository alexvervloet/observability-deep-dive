#!/usr/bin/env python3
"""
otel_collector.py: a real OTLP/HTTP receiver in about 150 lines, no Docker.

    python hands_on/otel_collector.py            # listens on http://localhost:4318

Then, in another terminal:

    python examples/09_otel_export.py --otlp

Why this exists: the usual way to see OTLP working is `docker run` a collector
plus Jaeger plus a browser tab, and that is three things to install before you
learn anything. This repo runs offline, so instead we implement the receiving end
of the protocol directly. It is genuinely the real protocol: the exporter posts
gzipped **protobuf** to `/v1/traces` and `/v1/metrics`, and this server decodes it
with the same generated classes the exporter used to encode it (`opentelemetry-proto`,
which the exporter already pulls in), then prints what arrived.

What it is NOT: a collector. The real
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) is a
production data pipeline (receivers, processors like batching/filtering/PII
redaction, exporters that fan out to several backends, retries, queues). This is
just its front door, so you can watch bytes cross the wire and read what is in
them. When you want the real one:

    docker run -p 4318:4318 -p 16686:16686 jaegertracing/all-in-one
    # then point the exporter at http://localhost:4318 and open localhost:16686

Nothing about the *emitting* code changes when you switch. That interchangeability
is the entire selling point of the standard.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
except ImportError:  # pragma: no cover - the actionable message is the point
    sys.exit(
        "\nThis needs the OTLP exporter package (for its protobuf definitions):\n"
        "    pip install opentelemetry-exporter-otlp-proto-http\n"
        "(or `pip install -r requirements.txt`).\n"
    )

TOTALS = {"spans": 0, "metrics": 0, "requests": 0}


def _attr_value(v) -> object:
    """Unwrap an OTLP AnyValue into a plain Python value."""
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_attr_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _attr_value(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, which)


def _attrs(pairs) -> dict:
    return {kv.key: _attr_value(kv.value) for kv in pairs}


def _clock(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%H:%M:%S")


def handle_traces(body: bytes, verbose: bool) -> None:
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)  # this is the whole "decode OTLP" step
    for resource_spans in req.resource_spans:
        res = _attrs(resource_spans.resource.attributes)
        service = res.get("service.name", "unknown")
        env = res.get("deployment.environment.name", "?")
        for scope_spans in resource_spans.scope_spans:
            spans = list(scope_spans.spans)
            TOTALS["spans"] += len(spans)
            print(f"\n📦 {len(spans)} span(s) from service={service} env={env} "
                  f"scope={scope_spans.scope.name}")
            shown = spans if verbose else spans[:3]
            for s in shown:
                a = _attrs(s.attributes)
                ms = (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6
                trace = s.trace_id.hex()
                status = "ERROR" if s.status.code == 2 else "OK"
                print(f"   {_clock(s.end_time_unix_nano)}  {s.name:<24} {ms:7.1f}ms  {status:<5} "
                      f"trace={trace[:16]}…")
                tokens = f"{a.get('gen_ai.usage.input_tokens', '?')}→{a.get('gen_ai.usage.output_tokens', '?')}"
                print(f"        tokens {tokens}  cost ${a.get('app.cost.usd', 0):.6f}  "
                      f"outcome={a.get('app.outcome')}  cache={a.get('app.cache')}  "
                      f"segment={a.get('app.segment', '-')}")
            if not verbose and len(spans) > 3:
                print(f"   … and {len(spans) - 3} more (pass --verbose to print every span)")


def handle_metrics(body: bytes, verbose: bool) -> None:
    req = ExportMetricsServiceRequest()
    req.ParseFromString(body)
    for resource_metrics in req.resource_metrics:
        service = _attrs(resource_metrics.resource.attributes).get("service.name", "unknown")
        for scope_metrics in resource_metrics.scope_metrics:
            metrics = list(scope_metrics.metrics)
            TOTALS["metrics"] += len(metrics)
            print(f"\n📈 {len(metrics)} metric(s) from service={service}")
            for m in metrics:
                kind = m.WhichOneof("data")
                if kind == "histogram":
                    for dp in m.histogram.data_points:
                        avg = dp.sum / dp.count if dp.count else 0.0
                        keys = _attrs(dp.attributes)
                        label = keys.get("gen_ai.token.type") or keys.get("gen_ai.request.model", "")
                        print(f"   {m.name:<34} histogram  n={dp.count:<6} sum={dp.sum:<12.3f} "
                              f"avg={avg:<10.3f} {m.unit:<9} [{label}]")
                elif kind == "sum":
                    for dp in m.sum.data_points:
                        value = dp.as_double if dp.HasField("as_double") else dp.as_int
                        keys = _attrs(dp.attributes)
                        outcome = keys.get("app.outcome")
                        label = (f"{outcome}/{keys.get('app.cache', '?')}" if outcome
                                 else keys.get("gen_ai.request.model", ""))
                        print(f"   {m.name:<34} counter    total={value:<10.6g}{'':<22} "
                              f"{m.unit:<9} [{label}]")
                elif verbose:
                    print(f"   {m.name:<34} {kind}")


class Handler(BaseHTTPRequestHandler):
    verbose = False

    def do_POST(self):  # noqa: N802 (http.server's naming, not ours)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # The Python exporter gzips by default. Real collectors must handle both.
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        TOTALS["requests"] += 1

        try:
            if self.path.endswith("/v1/traces"):
                handle_traces(body, self.verbose)
                payload = ExportTraceServiceResponse().SerializeToString()
            elif self.path.endswith("/v1/metrics"):
                handle_metrics(body, self.verbose)
                payload = ExportMetricsServiceResponse().SerializeToString()
            else:
                self.send_error(404, f"no OTLP endpoint at {self.path}")
                return
        except Exception as exc:  # a malformed payload should not kill the server
            print(f"  ! could not decode {self.path}: {exc}")
            self.send_error(400, "bad OTLP payload")
            return

        # A partial-success response with an empty body is OTLP for "I took all of it".
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # the decoded telemetry above is the log we want, not one line per POST


def main() -> int:
    ap = argparse.ArgumentParser(description="A minimal OTLP/HTTP receiver.")
    ap.add_argument("--port", type=int, default=4318, help="OTLP/HTTP's conventional port")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--verbose", action="store_true", help="print every span, not the first few")
    args = ap.parse_args()

    Handler.verbose = args.verbose
    # Line-buffer stdout so `python hands_on/otel_collector.py > log` shows spans as
    # they land instead of whenever a 8KB buffer happens to fill. A tail -f of a
    # block-buffered pipe looks exactly like a receiver that is not receiving.
    sys.stdout.reconfigure(line_buffering=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OTLP/HTTP receiver listening on http://{args.host}:{args.port}")
    print("  POST /v1/traces    (gzipped protobuf)")
    print("  POST /v1/metrics   (gzipped protobuf)")
    print("\nSend it something:  python examples/09_otel_export.py --otlp")
    print("Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n\nReceived {TOTALS['requests']} OTLP request(s): "
              f"{TOTALS['spans']} span(s), {TOTALS['metrics']} metric stream(s).")
        print("Every byte of that was the real protocol. A vendor's endpoint takes")
        print("exactly the same payload; only the URL and an auth header change.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
