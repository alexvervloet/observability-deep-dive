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
exports what is queued, and a provider's `shutdown()` exports again on its way out.

The first version of this entry said the counters "arrived doubled." They did not,
and the correction is the more useful lesson. The default temporality for the OTLP
metric exporter is **cumulative**: every export carries the running total, so two
exports of the same batch carry the same number at the same timestamp, and the
backend overwrites rather than adds. Reading duplicated *lines* as doubled *values*
is an easy mistake to make when your receiver prints a stream it does not aggregate.

Next time: call `shutdown()` alone; it already flushes, and the duplicate is pure
waste even when it is harmless. But check the temporality before deciding how
harmless: under **delta** temporality, which several vendors request, each export
carries only what happened since the last one, and the duplicate really is counted
twice. And when a printed stream looks wrong, compare the values, not the line
count.

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

## 2026-08-23: three claims in the OpenTelemetry section that a probe disproved

Expected: the section's factual claims about the SDK were safe, since they are the
ones repeated everywhere in OTel write-ups.

Actual: a review probed them against the real socket and the real SDK, and three
were wrong.

- "The Python exporter gzips by default." It does not; `Compression.NoCompression`
  is the default, and the receiver's decompression branch was dead code. Fixed by
  making it true (`compression=Compression.Gzip`), which is worth about 7x on this
  payload and is a knob worth teaching anyway.
- "force_flush plus shutdown doubles your counters." It duplicates the *point*, not
  the value, because the default temporality is cumulative. The advice survived;
  the reason did not.
- "Delete `shutdown()` and nothing arrives." Everything arrives: both providers
  default to `shutdown_on_exit=True` and register an `atexit` hook. The batch is
  lost only on an exit that skips `atexit` (`os._exit`, SIGKILL, an OOM kill, a
  container stopped past its grace period). An exercise asked learners to run this
  and printed the opposite of what they would see.

Next time: when writing about a library's behaviour, run the probe rather than
repeating the folklore, especially for the claims that sound too familiar to check.
Print the header, check the magic bytes, compare the values instead of the line
count. A teaching repo that is confidently wrong is worse than one that says less.

## 2026-08-23: the first test written found a claim printed in three places

Expected: `tests/test_otel.py` would be a formality, pinning names that were
already correct.

Actual: its first run failed on `assertEqual(small_points, big_points)`. Metric
points are bounded by the number of distinct *attribute combinations*, not fixed:
a rarer outcome that never occurred at 30 requests/day appears at 300 and adds a
point. The README, the example's printed output, and an exercise answer all said
the count "does not move."

Next time: write the assertion for a claim the prose already makes. The prose had
been read several times by then and the error survived every reading, because
prose cannot fail.
