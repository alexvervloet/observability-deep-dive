"""
obs/simulate.py: the traffic generator that makes this repo runnable offline.

The Production deep dive shipped a mock *model* so you could operate one request
with no key. This repo needs something different: **history**. You can't learn to
spot drift in one request; you need weeks of them, with real incidents buried
inside. So instead of a live model, this repo ships a deterministic generator of
*log history* for the Acme Cloud support assistant.

`generate()` returns two things:

  1. `records`: a list of `LogRecord`s, realistic request-by-request telemetry,
     containing only what a running app actually logs (no ground-truth "good?"
     label; see obs/logs.py).
  2. `incidents`: the *ground truth* of what was wrong and when. A real system
     never has this; we do, only so the examples can grade a detector ("you
     flagged input drift on day 16; it actually started on day 14, a 2-day lag").

The incidents are injected by genuinely changing the generated traffic, never by
writing a hidden flag into the logs:

  - **input drift**: users start asking about the mobile app (a topic the KB has
    no answer for), so out-of-KB refusals rise and the questions' *wording* moves.
  - **quality regression**: a silent provider model swap makes answers terser and
    more evasive. The `model` string never changes; you infer the drop from
    behavior (refusals up, answers shorter, judge scores down).
  - **cost creep**: someone stuffs more context into the prompt, so prompt tokens
    (and the bill, and latency) climb without any user-visible change.
  - **latency spike**: a short, transient slowdown, to contrast a one-day blip
    against a sustained regression (you alert on them differently).

Everything is seeded, so the same call always yields the same history, which is
exactly what lets an exercise say "the alert fires on day 16" and be right.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from obs.logs import LogRecord

# USD per 1M tokens (input, output): the gpt-4o-mini rate, same as the Production
# dive prices its mock at, so the dollar figures look like a real small model.
_PRICE_IN, _PRICE_OUT = 0.15, 0.60
_MODEL = "acme-support-1"  # the "model" name in the logs; stays fixed even across a silent swap

# --- The knowledge base the assistant can actually answer from --------------
# Each in-KB topic has a set of naturally-worded questions and one grounded answer.
# The wording per topic shares vocabulary (so questions cluster in embedding space)
# but varies enough to be realistic.
_TOPICS: dict[str, dict] = {
    "reset_password": {
        "questions": [
            "How do I reset my password?",
            "I forgot my password, how can I set a new one?",
            "Where do I change my password?",
            "My password isn't working, how do I reset it?",
        ],
        "answer": (
            "To reset your password, open Settings -> Security -> Reset password, "
            "then follow the emailed link. It expires in 30 minutes."
        ),
    },
    "two_factor": {
        "questions": [
            "How do I turn on two-factor authentication?",
            "How do I enable 2FA on my account?",
            "I lost my 2FA device, how do I get back in?",
            "Where are my two-factor backup codes?",
        ],
        "answer": (
            "Turn on two-factor auth under Settings -> Security -> Two-factor. If you "
            "lose your device, use a backup code you saved when enabling it."
        ),
    },
    "refund": {
        "questions": [
            "How do I get a refund?",
            "Can I get my money back for last month's charge?",
            "Where do I request a refund for a payment?",
            "I was billed by mistake, how do I get a refund?",
        ],
        "answer": (
            "Refunds are available within 30 days. Go to Billing -> History, find the "
            "charge, and choose Request refund. It posts in 5-10 days."
        ),
    },
    "cancel": {
        "questions": [
            "How do I cancel my subscription?",
            "Where do I cancel my plan?",
            "I want to stop my subscription, what do I do?",
            "How do I cancel before the next billing date?",
        ],
        "answer": (
            "You can cancel anytime under Billing -> Plan -> Cancel. Your plan stays "
            "active until the end of the current billing period."
        ),
    },
    "export_data": {
        "questions": [
            "How do I export my data?",
            "Where can I download an archive of my data?",
            "Can I get a copy of all my data?",
            "How do I download everything in my account?",
        ],
        "answer": (
            "Export your data under Settings -> Data -> Export. We build a downloadable "
            "archive and email you a link, usually within an hour."
        ),
    },
    "pricing": {
        "questions": [
            "What plans do you offer?",
            "How much does the Pro plan cost?",
            "What are your pricing tiers?",
            "Is there a free plan?",
        ],
        "answer": (
            "Acme Cloud has three plans: Free (1 project), Pro ($12/mo, unlimited "
            "projects), and Team ($29/user/mo, with shared workspaces and SSO)."
        ),
    },
}

# The out-of-KB topic that arrives with the input-drift incident. Its vocabulary is
# deliberately disjoint from every in-KB topic, so it both refuses (no KB answer)
# AND lands far away in embedding space.
_DRIFT_TOPIC = "mobile_app"
_DRIFT_QUESTIONS = [
    "Is there a mobile app for iphone?",
    "How do I install the android app on my phone?",
    "Does the mobile app support push notifications?",
    "Why does the iphone app keep logging me out?",
    "When will the android tablet app be released?",
    "How do I enable dark mode in the phone app?",
]

# The customer cohorts every request belongs to, and their share of traffic. A
# segment-scoped incident (segment_outage) hits only one of these: and because the
# affected cohort is a minority, it can stay hidden in the global average.
_SEGMENTS = [("free", 0.55), ("pro", 0.30), ("enterprise", 0.15)]

_REFUSALS = [
    "I don't have information about that in the Acme Cloud help center. Please contact support@acme.example.",
    "I'm not sure about that one — I'd recommend reaching out to support@acme.example for help.",
]
# Degraded answers during a quality regression: technically on-topic, but terse and
# unhelpful: the shape a weaker model regresses toward.
_DEGRADED = [
    "Check your account settings.",
    "That should be in the settings somewhere.",
    "Please try again or contact support.",
    "You can do that from your account page.",
]


@dataclass
class Incident:
    """Ground truth about one injected problem. NOT present in the logs."""

    kind: str  # input_drift | quality_regression | cost_creep | latency_spike | segment_outage
    start_day: int  # 0-based day index into the history
    end_day: int  # inclusive; the last day it's active
    metric: str  # the operational signal it shows up in, for grading detectors
    description: str
    segment: str = ""  # for segment_outage: which cohort is affected (Section 11)

    def active_on(self, day_index: int) -> bool:
        return self.start_day <= day_index <= self.end_day

    def ramp(self, day_index: int) -> float:
        """0..1 severity. Incidents ramp in over ~a week, not switch on instantly,
        which is what makes them hard to catch early."""
        if not self.active_on(day_index):
            return 0.0
        span = max(1, min(self.end_day, self.start_day + 6) - self.start_day)
        return min(1.0, (day_index - self.start_day + 1) / (span + 1))


def default_incidents() -> list[Incident]:
    """The three-incident story the capstone and examples are built around,
    for the default 42-day history."""
    return [
        Incident("latency_spike", 9, 9, "p95_latency_ms",
                 "One-day provider slowdown (transient; should NOT page as a trend)."),
        Incident("input_drift", 14, 41, "refusal_rate",
                 "Users start asking about a mobile app the KB can't answer."),
        Incident("cost_creep", 21, 41, "cost_per_request_usd",
                 "A prompt change bloats context; tokens and cost climb silently."),
        Incident("quality_regression", 28, 35, "judge_score",
                 "Silent model swap: answers get terse and evasive, then it's rolled back."),
    ]


def _price(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * _PRICE_IN + completion_tokens * _PRICE_OUT) / 1_000_000


def generate(
    days: int = 42,
    *,
    seed: int = 7,
    start: str = "2026-04-01",
    requests_per_day: int = 120,
    incidents: list[Incident] | None = None,
) -> tuple[list[LogRecord], list[Incident]]:
    """Generate a deterministic log history plus the ground-truth incident schedule.

    Same arguments -> same history, every time. Pass your own `incidents` to tell a
    different story; pass `[]` for a clean, healthy baseline with no problems.
    """
    if incidents is None:
        incidents = default_incidents()
    rng = random.Random(seed)
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    in_kb_topics = list(_TOPICS)
    records: list[LogRecord] = []
    counter = 0

    for d in range(days):
        day_dt = start_dt + timedelta(days=d)
        # Weekends are quieter; add a little day-to-day noise.
        weekday = day_dt.weekday()
        volume = int(requests_per_day * (0.6 if weekday >= 5 else 1.0) * rng.uniform(0.9, 1.1))

        active = {i.kind: i for i in incidents if i.active_on(d)}
        drift = active.get("input_drift")
        regression = active.get("quality_regression")
        creep = active.get("cost_creep")
        spike = active.get("latency_spike")
        outage = active.get("segment_outage")

        drift_frac = 0.35 * drift.ramp(d) if drift else 0.0  # up to 35% mobile-app questions
        regress_p = 0.55 * regression.ramp(d) if regression else 0.0  # chance a good answer degrades
        creep_tokens = int(280 * creep.ramp(d)) if creep else 0  # extra prompt tokens
        latency_mult = 3.2 if spike else 1.0

        for _ in range(volume):
            counter += 1
            ts = (day_dt + timedelta(seconds=rng.randint(0, 86_399))).timestamp()
            trace_id = f"{seed:02d}{counter:07d}"
            segment = _pick_segment(rng)

            # A segment-scoped outage: only the affected cohort's requests slow down
            # (a mis-provisioned backend for that plan). Because the cohort is a
            # minority, the *global* p95 barely moves while the cohort's own p95
            # screams, which is the whole lesson of Section 11.
            seg_hit = outage is not None and segment == outage.segment
            seg_lat_mult = (1.0 + 2.4 * outage.ramp(d)) if (outage is not None and seg_hit) else 1.0

            # --- pick a topic and question -------------------------------------
            if drift and rng.random() < drift_frac:
                topic, question, in_kb = _DRIFT_TOPIC, rng.choice(_DRIFT_QUESTIONS), False
            else:
                topic = rng.choice(in_kb_topics)
                question = rng.choice(_TOPICS[topic]["questions"])
                in_kb = True

            # --- a repeat question can be a cache hit (fast, free) -------------
            # Cache hits are local, so a backend outage doesn't touch them.
            if rng.random() < 0.28:
                answer = _TOPICS[topic]["answer"] if in_kb else rng.choice(_REFUSALS)
                rec = _record(ts, trace_id, question, answer,
                              outcome="answered" if in_kb else "refused", cache="hit",
                              prompt_tokens=0, latency_mult=latency_mult, segment=segment, rng=rng)
                records.append(_maybe_feedback(rec, good=in_kb, rng=rng))
                continue

            # --- otherwise it's a live model call ------------------------------
            if not in_kb:
                answer, outcome = rng.choice(_REFUSALS), "refused"
            elif rng.random() < regress_p:
                answer, outcome = rng.choice(_DEGRADED), "answered"  # degraded but not a refusal
            elif rng.random() < 0.02:
                answer, outcome = rng.choice(_REFUSALS), "refused"  # baseline: odd phrasings still refuse
            elif rng.random() < 0.01:
                answer, outcome = "", "error"  # a rare upstream error
            else:
                answer, outcome = _TOPICS[topic]["answer"], "answered"

            base_prompt_tokens = rng.randint(58, 92) + creep_tokens
            rec = _record(ts, trace_id, question, answer, outcome=outcome, cache="miss",
                          prompt_tokens=base_prompt_tokens, latency_mult=latency_mult * seg_lat_mult,
                          segment=segment, rng=rng)
            good = outcome == "answered" and answer not in _DEGRADED
            records.append(_maybe_feedback(rec, good=good, rng=rng))

    return records, incidents


def _pick_segment(rng: random.Random) -> str:
    r = rng.random()
    cume = 0.0
    for name, share in _SEGMENTS:
        cume += share
        if r <= cume:
            return name
    return _SEGMENTS[-1][0]


def _record(ts, trace_id, question, answer, *, outcome, cache, prompt_tokens, latency_mult, segment, rng) -> LogRecord:
    completion_tokens = 0 if not answer else max(1, len(answer) // 4)
    if cache == "hit":
        cost = 0.0
        latency = rng.uniform(2, 12)  # a cache hit is a dictionary lookup
    else:
        cost = _price(prompt_tokens, completion_tokens)
        # A right-skewed latency: most calls quick, a few slow (so p95 > p50).
        latency = rng.gauss(140, 40)
        if rng.random() < 0.08:
            latency += rng.uniform(200, 700)  # the tail
        latency = max(20.0, latency) * latency_mult
    return LogRecord(
        ts=ts, trace_id=trace_id, question=question, prompt_version="v2",
        model=_MODEL, provider="mock", prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, cost_usd=round(cost, 8),
        duration_ms=round(latency, 1), cache=cache, outcome=outcome,
        answer_chars=len(answer), answer=answer, segment=segment,
    )


def _maybe_feedback(rec: LogRecord, *, good: bool, rng: random.Random) -> LogRecord:
    """Sparse, noisy thumbs. Most requests get none; when they do, it correlates
    with quality but isn't a clean label, exactly like real feedback."""
    if rng.random() < 0.08:  # only ~8% of users ever rate
        if good:
            rec.feedback = 1 if rng.random() < 0.85 else -1
        else:
            rec.feedback = -1 if rng.random() < 0.80 else 1
    return rec
