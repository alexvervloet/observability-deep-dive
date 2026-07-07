"""
obs/mining.py — production traffic is your best eval set.
=========================================================

The Evals dive made the point that the best test cases come from real failures
you find later and add back so they never regress. Monitoring is where you *find*
them. Every refusal, every thumbs-down, every terse answer is a labelled example
of something your system got wrong — and it arrived for free, from a real user,
which is worth more than any case you'd invent.

This module closes the loop (the "feedback flywheel"):

  1. **Surface** the failures — refusals, negative feedback, suspiciously short
     answers — out of the traffic.
  2. **Cluster** them by what they're about, so "47 scattered failures" becomes
     "31 of them are the mobile app you don't support" — a fixable finding, and
     the exact signal input-drift detection saw from the other side.
  3. **Emit** them as candidate eval cases in the Evals dive's JSONL shape, ready
     for a human to write the gold answer and drop into the regression suite.

That's the whole point of observability, really: not to admire dashboards, but to
turn what production teaches you back into tests and fixes.
"""

from __future__ import annotations

from collections import Counter

from obs.logs import LogRecord

_STOPWORDS = {
    "how", "do", "i", "the", "a", "an", "to", "my", "is", "in", "on", "for", "of",
    "can", "you", "me", "it", "does", "what", "where", "when", "why", "there",
    "keep", "get", "set", "up", "and", "or", "if", "new", "back", "some", "all",
}


def salient_terms(question: str) -> list[str]:
    """Content words of a question, stopwords removed — the 'aboutness' tokens we
    cluster on."""
    words = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in question).split()
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def failures(records: list[LogRecord]) -> list[LogRecord]:
    """Requests that look like the system fell short: an explicit refusal, a
    thumbs-down, or a terse answer to a real question (the degraded-answer shape).
    These are candidate eval cases and candidate incidents."""
    out = []
    for r in records:
        is_refusal = r.outcome == "refused"
        is_negative = r.feedback == -1
        is_terse = r.outcome == "answered" and 0 < r.answer_chars < 40
        if is_refusal or is_negative or is_terse:
            out.append(r)
    return out


def cluster(failing: list[LogRecord], all_records: list[LogRecord], top: int = 5) -> list[dict]:
    """Group failing questions by their most *diagnostic* term and return the
    biggest clusters: {term, count, examples}.

    Cheap lexical clustering with one refinement that matters: we key each question
    on the word most *over-represented among failures* (a word whose uses are mostly
    failing), not just its first content word. That pushes distinctive topic words
    ("app", "iphone") to the top over generic verbs ("enable", "get") that appear
    everywhere — so a real theme like the unsupported mobile app surfaces instead of
    fragmenting. Enough to turn a pile of failures into a ranked list of what to fix.
    """
    fail_tf: Counter[str] = Counter()
    all_tf: Counter[str] = Counter()
    for r in failing:
        fail_tf.update(set(salient_terms(r.question)))
    for r in all_records:
        all_tf.update(set(salient_terms(r.question)))
    # salience = fraction of a term's total uses that were failures (0..1).
    salience = {t: fail_tf[t] / all_tf[t] for t in fail_tf if all_tf[t]}

    by_term: dict[str, list[LogRecord]] = {}
    for r in failing:
        terms = salient_terms(r.question)
        if not terms:
            continue
        key = max(terms, key=lambda t: (salience.get(t, 0.0), fail_tf[t]))
        by_term.setdefault(key, []).append(r)
    ranked = sorted(by_term.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [{"term": term, "count": len(recs), "examples": [r.question for r in recs[:3]]}
            for term, recs in ranked[:top]]


def gold_candidates(records: list[LogRecord], limit: int = 10) -> list[dict]:
    """Turn failing requests into candidate eval cases in the Evals dive's JSONL
    shape: {input, expected, note}. `expected` is left blank on purpose — a human
    writes the right answer; the machine only found the question worth asking."""
    seen: set[str] = set()
    cases = []
    for r in failures(records):
        q = r.question.strip()
        if q in seen:
            continue
        seen.add(q)
        why = ("refused" if r.outcome == "refused"
               else "thumbs-down" if r.feedback == -1 else "terse-answer")
        cases.append({"input": q, "expected": "", "note": f"from production ({why})"})
        if len(cases) >= limit:
            break
    return cases


def unrated_but_failing(records: list[LogRecord]) -> int:
    """How many failures had NO user feedback at all — a reminder that thumbs are
    sparse, so you can't wait for them: most failures are silent."""
    return sum(1 for r in failures(records) if r.feedback is None)
