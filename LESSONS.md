# Lessons

Things that did not go according to plan while building this repo, written down
when they happened.

## 2026-08-23: `find_spec` raises on a dotted name whose parent is missing

Expected: `importlib.util.find_spec("opentelemetry.sdk")` would return `None` when
OpenTelemetry is not installed, the way it does for a missing top-level module.

Actual: it raised `ModuleNotFoundError`, because looking inside `a.b` requires
importing the parent package `a` first, and that import is what failed. This made
`check_setup.py` crash with a traceback on precisely the machine it is meant to
help: one where the optional dependency is absent.

Next time: any probe for an optional submodule needs a `try/except (ImportError,
ValueError)` wrapper. Test the "not installed" path with a Python that really does
not have the package, not by reasoning about it.

## 2026-08-23: force_flush before shutdown exports the batch twice

Expected: flushing and then shutting down an OTel provider would be belt and
braces, with the second call finding an empty queue.

Actual: the receiver printed every metric stream twice per run. `force_flush()`
exports what is queued, and a provider's `shutdown()` exports again on its way
out, so the counters arrived doubled. In a real backend this shows up as inflated
totals with no obvious cause.

Next time: call `shutdown()` alone; it already flushes. Keep `force_flush()` for
the case where the process keeps running and you need the data out now.

## 2026-08-23: a block-buffered receiver looks like a broken receiver

Expected: running the OTLP receiver with its output redirected to a file
(`python hands_on/otel_collector.py > log`) would show spans as they arrived.

Actual: the log stayed empty while spans were in fact being received and decoded.
Python line-buffers stdout to a terminal but block-buffers it to a pipe or file,
so nothing appeared until 8KB had accumulated. Ten minutes went into suspecting
the exporter, the port, and the protobuf decoding, in that order.

Next time: any long-running printer that people might redirect should call
`sys.stdout.reconfigure(line_buffering=True)`. When a server "receives nothing,"
check that the output path is not simply buffered before debugging the protocol.
