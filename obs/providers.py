"""
obs/providers.py — the ONLY provider-specific seam.
===================================================

Same keystone idea as every sibling repo: hide the one provider-specific call
behind a tiny function so the rest of the code is provider-agnostic. This repo
barely needs a live model at all — its whole job is analyzing *logs* — so the
seam here is small. Two capabilities live behind it:

  embed(text)              -> a vector, for measuring input drift by *meaning*
  score_answer(q, answer)  -> 0..1 quality, for the sampled LLM-as-judge

Three stacks, exactly like the siblings:

  PROVIDER=mock   ->  offline, deterministic. Embeddings are hashed from the
                      words (same words -> similar vectors), and the "judge" is a
                      transparent rule-based scorer. No key, no network, no cost.
                      This is what makes the entire repo runnable offline.
  PROVIDER=openai ->  real embeddings (text-embedding-3-small) + gpt-4o-mini judge
  PROVIDER=claude ->  claude-haiku-4-5 judge (embeddings still use OPENAI_API_KEY)

Nothing in the core path (reading logs, metrics, baselines, alerting, the
dashboard) touches this file. Only the two optional model-backed sections do —
and both work fully on the mock.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from functools import lru_cache

_OPENAI_JUDGE = "gpt-4o-mini"
_OPENAI_EMBED = "text-embedding-3-small"
_CLAUDE_JUDGE = "claude-haiku-4-5"

# The judge always needs OpenAI *or* Claude; embeddings, when live, always use
# OpenAI (so PROVIDER=claude still wants OPENAI_API_KEY for the drift section).
_KEYS = {
    "mock": [],
    "openai": ["OPENAI_API_KEY"],
    "claude": ["ANTHROPIC_API_KEY"],
}


def _configured_provider() -> str:
    return os.getenv("PROVIDER", "mock").strip().lower()


def _has_required_keys(p: str) -> bool:
    return all(os.getenv(k) for k in _KEYS.get(p, []))


_warned_fallback = False


def _warn_mock_fallback(p: str) -> None:
    global _warned_fallback
    if _warned_fallback:
        return
    _warned_fallback = True
    missing = ", ".join(_KEYS.get(p, []))
    print(
        f"\n⚠  PROVIDER={p} is set, but {missing} isn't on the environment — did you\n"
        f"   forget `secrun`? Falling back to the offline mock so this still runs.\n"
        f"   Real model:  secrun python <script>   |   Hard error instead:  PROVIDER_STRICT=1\n",
        file=sys.stderr,
    )


def provider_name() -> str:
    """The active stack. If a real provider is selected but its key isn't on the
    environment, degrade to the offline mock — loudly, once — unless
    PROVIDER_STRICT=1, which makes the missing key a hard error instead."""
    p = _configured_provider()
    if p in _KEYS and p != "mock" and not _has_required_keys(p):
        if os.getenv("PROVIDER_STRICT"):
            return p
        _warn_mock_fallback(p)
        return "mock"
    return p


def describe() -> str:
    configured = _configured_provider()
    p = provider_name()
    if p == "mock" and configured != "mock":
        return (
            f"mock  (FALLBACK: PROVIDER={configured} is set but its key isn't on the "
            f"environment — run under `secrun` for the real model)"
        )
    if p == "mock":
        return "mock  (offline, deterministic embeddings + rule-based judge, no key)"
    if p == "openai":
        return f"openai  (judge={_OPENAI_JUDGE}, embed={_OPENAI_EMBED})"
    if p == "claude":
        return f"claude  (judge={_CLAUDE_JUDGE}, embed={_OPENAI_EMBED} via OPENAI_API_KEY)"
    return f"unknown provider {p!r}"


def ensure_ready() -> None:
    """Fail fast with a friendly message if a real stack is missing its key.

    For PROVIDER=mock this never fails — that's the point.
    """
    p = provider_name()
    if p not in _KEYS:
        sys.exit(f"PROVIDER={p!r} is not recognized. Set PROVIDER=mock (default), openai, or claude.")
    missing = [k for k in _KEYS.get(p, []) if not os.getenv(k)]
    if missing:
        sys.exit(
            f"PROVIDER={p} needs {', '.join(missing)} in the environment. "
            f"Provide them via secrun (see ../SECRETS.md). "
            f"(Tip: PROVIDER=mock needs no key and runs everything offline.)"
        )


# ---------------------------------------------------------------------------
# Mock embeddings — deterministic, hash-based, offline
# ---------------------------------------------------------------------------
# The trick: give every word a fixed pseudo-random unit vector (seeded by a hash
# of the word), then embed a text as the normalized sum of its word vectors. Two
# questions that share words land near each other; a question full of NEW words
# (a topic the app has never seen) lands far from the baseline cloud. That is
# exactly the signal input-drift detection looks for — and here it's real, not
# faked: the vectors move because the words did.

_EMBED_DIM = 64
_WORD_RE = re.compile(r"[a-z0-9']+")


@lru_cache(maxsize=8192)
def _word_vec(word: str) -> tuple[float, ...]:
    # Seed a small PRNG from the word so the vector is stable across runs/processes.
    seed = int(hashlib.sha256(word.encode()).hexdigest(), 16)
    vec = []
    for i in range(_EMBED_DIM):
        seed = (1103515245 * seed + 12345 + i) & 0x7FFFFFFF
        vec.append((seed / 0x7FFFFFFF) * 2.0 - 1.0)  # in [-1, 1]
    return tuple(vec)


def _mock_embed(text: str) -> list[float]:
    words = _WORD_RE.findall(text.lower())
    acc = [0.0] * _EMBED_DIM
    for w in words:
        wv = _word_vec(w)
        for i in range(_EMBED_DIM):
            acc[i] += wv[i]
    norm = math.sqrt(sum(x * x for x in acc)) or 1.0
    return [x / norm for x in acc]


# --- Mock judge -------------------------------------------------------------
# A transparent rule-based stand-in for an LLM-as-judge. It rewards concrete,
# grounded help (steps, specific settings paths, numbers) and penalizes the
# hallmarks of a degraded answer (a bare refusal, empty text). It's crude on
# purpose — the point is that when the answers genuinely get worse (Section 5's
# injected quality regression makes them shorter and more evasive), a cheap proxy
# *sees* it. A real deployment swaps this for a real model via the same function.

_REFUSAL_MARKERS = (
    "i don't have information",
    "i do not have information",
    "i'm not sure",
    "cannot help",
    "can't help",
    "unable to help",
)
_CONCRETE_MARKERS = ("settings", "billing", "->", "click", "select", "step", "code")


def _mock_score(question: str, answer: str) -> float:
    a = answer.lower().strip()
    if not a:
        return 0.0
    score = 0.55
    if any(m in a for m in _REFUSAL_MARKERS):
        score -= 0.4
    score += 0.1 * sum(1 for m in _CONCRETE_MARKERS if m in a)
    if any(ch.isdigit() for ch in a):
        score += 0.1
    if len(a) < 40:  # a terse, likely-unhelpful answer
        score -= 0.2
    return max(0.0, min(1.0, score))


# --- Real clients (lazy, so importing this module never forces an SDK import) --


@lru_cache(maxsize=1)
def _openai_client():
    from openai import OpenAI

    return OpenAI()


@lru_cache(maxsize=1)
def _anthropic_client():
    import anthropic

    return anthropic.Anthropic()


def embed(text: str) -> list[float]:
    """Embed one string into a vector. Mock = hashed words; real = OpenAI."""
    if provider_name() == "mock":
        return _mock_embed(text)
    resp = _openai_client().embeddings.create(model=_OPENAI_EMBED, input=text)
    return resp.data[0].embedding


_JUDGE_SYSTEM = (
    "You are grading a customer-support answer. Output ONLY a number from 0.0 to "
    "1.0: 1.0 = a concrete, correct, helpful answer; 0.0 = useless, evasive, or "
    "empty. Judge helpfulness and specificity, not politeness."
)


def score_answer(question: str, answer: str) -> float:
    """Grade one answer's quality in [0, 1]. Mock = rule-based; real = an LLM judge.

    This is the per-item scorer; `obs/judge.py` decides *which* items to spend it
    on (you can't afford to judge every request) and aggregates the results.
    """
    p = provider_name()
    if p == "mock":
        return _mock_score(question, answer)
    prompt = f"Question:\n{question}\n\nAnswer:\n{answer}\n\nScore (0.0-1.0):"
    if p == "openai":
        resp = _openai_client().chat.completions.create(
            model=_OPENAI_JUDGE,
            max_tokens=8,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        return _parse_score(resp.choices[0].message.content or "")
    if p == "claude":
        resp = _anthropic_client().messages.create(
            model=_CLAUDE_JUDGE,
            max_tokens=8,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_score(text)
    raise ValueError(f"Unknown PROVIDER={p!r}.")


def _parse_score(text: str) -> float:
    m = re.search(r"[01](?:\.\d+)?", text)
    return max(0.0, min(1.0, float(m.group()))) if m else 0.0
