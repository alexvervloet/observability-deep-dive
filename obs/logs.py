"""
obs/logs.py: the record shape, and reading/writing it.

Observability starts with one humble object: the **log record** for a single
request. Everything else in this repo (every metric, every drift detector, the
alerting, the dashboard) is a function of a *pile of these*.

The shape here is deliberately the same one the Production deep dive emits from
`trace.summary()`: a flat dict with a `trace_id`, timings, token counts, cost,
and a handful of outcome flags. That's the point: this repo consumes exactly
what a real traced app already produces. We store one JSON object per line
(**JSONL**), which is what every log pipeline in the world speaks.

The crucial honesty of this file: **a real production log does not contain a
ground-truth "was this answer good?" label.** If it did, monitoring would be
trivial. So a record here carries only what a running system actually knows at
request time: the question, what it cost, how long it took, whether it refused.
Whether quality *drifted* has to be *inferred* later from these proxies (that's
Sections 6–7), never read off a field. The simulator keeps its own private truth
so the examples can grade the detectors; the logs you analyze do not.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class LogRecord:
    """One request's worth of telemetry: the honest subset a real app logs.

    Note what is *absent*: no "correct" flag, no gold answer, no clean topic
    label. Those don't exist in production. `feedback` (a thumbs up/down) is the
    one quality signal you sometimes get, and it's sparse; most users never rate.
    """

    ts: float  # unix seconds, when the request finished
    trace_id: str
    question: str
    prompt_version: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    duration_ms: float  # total request latency (what the user felt)
    cache: str  # "hit" | "miss"
    outcome: str  # "answered" | "refused" | "blocked" | "error"
    answer_chars: int = 0
    feedback: int | None = None  # +1 / -1 from the user, or None (the common case)
    # A cohort dimension: here the customer's plan ("free"/"pro"/"enterprise"). Real
    # apps log several (tenant, locale, channel, prompt version). Slicing metrics by
    # a segment is how you catch a problem that a global average hides (Section 11).
    segment: str = ""
    # The answer text. Cheap metrics (above) are logged for EVERY request; capturing
    # full input/output is a choice with a cost: it's a PII sink (Production §3) and
    # at scale you sample a few percent to a separate store rather than log all of it.
    # We keep it here so the sampled LLM-as-judge (Section 6) has text to grade.
    answer: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def day(self) -> str:
        """The UTC calendar day, 'YYYY-MM-DD': the unit we bucket trends by."""
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def parse(line: str) -> LogRecord:
    """Parse one JSONL line into a LogRecord, ignoring unknown fields."""
    raw = json.loads(line)
    known = {f: raw[f] for f in LogRecord.__dataclass_fields__ if f in raw}
    return LogRecord(**known)


def load(path: str) -> list[LogRecord]:
    """Read a whole JSONL log file into a list of records (sorted by time)."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(parse(line))
    records.sort(key=lambda r: r.ts)
    return records


def save(path: str, records: list[LogRecord]) -> None:
    """Write records as JSONL: one object per line, the log-pipeline lingua franca."""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r.to_json() + "\n")


def by_day(records: list[LogRecord]) -> dict[str, list[LogRecord]]:
    """Group records into calendar days, in chronological order.

    Daily buckets are the workhorse window for trends: coarse enough to be
    stable, fine enough to see an incident within a day of it starting.
    """
    buckets: dict[str, list[LogRecord]] = {}
    for r in records:
        buckets.setdefault(r.day, []).append(r)
    return dict(sorted(buckets.items()))
