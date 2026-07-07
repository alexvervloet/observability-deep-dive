# Observability — A Guided Deep Dive

A hands-on playground for the question every other repo in this series answers
only for *right now*: **is my LLM app still good six weeks after it launched — and
would I know before my users tell me?** You'll take one small app — the Acme Cloud
support assistant — generate weeks of its request logs, and build the machinery
that watches a *running* system over time: operational metrics, input drift,
quality drift, alerting that doesn't cry wolf, and the flywheel that turns
production failures back into eval cases. No framework, no SaaS dashboard, no
Grafana — just enough code to *see* how each piece works.

The twist that makes this repo work: it runs **completely offline on synthetic
log history**, with no API key. Everything else in the series measures your app at
a single point in time — this repo needs *history*, so instead of a live model it
ships a deterministic **traffic simulator** that generates six weeks of realistic
request logs with real incidents (drift, a silent model regression, cost creep, a
latency spike) buried inside. Your job is to catch them. Flip one env var and the
optional model-backed bits (a sampled LLM-as-judge, real embeddings) run against a
real OpenAI or Claude model; the rest never needs one, because it analyzes *logs*.

This is a **bonus dive** in the series, slotting in right after
[Production](https://github.com/alexvervloet/ai-in-production-deep-dive) (#8).
Production taught you to operate one request end to end — traced, costed, guarded.
This teaches you to operate *the next six weeks of them*. Every log record here has
the same shape Production's `trace.summary()` emits, so this repo consumes exactly
what a real traced app already produces.

Like its siblings, it's meant to be *walked through*, not just read. Each section
ends with something to run — all of it offline and free. And
[EXERCISES.md](EXERCISES.md) has a predict-then-run prompt for each section.

---

## 0. The one big idea

An eval (repo #5) tells you a change is better *today*, on the questions you have
*today*. Production (#8) tells you what *one* request did. Neither tells you that,
five weeks in, users started asking about a feature you don't support, or that your
provider silently swapped the model under you and answers quietly got worse. Those
failures don't throw exceptions. Nothing turns red. The dashboards you already have
stay green while quality rots.

> **A prototype is judged once. A production system is judged continuously — so
> your quality has to be a *trend you watch*, not a number you checked at launch.**

Everything below is one of the handles you have for watching that trend when your
"model" is a black box that takes free text and returns free text: metrics from
logs, drift in the inputs, a sampled judge on the outputs, and alerting that tells
the difference between a bad Tuesday and a real incident.

---

## 1. Setup (5 minutes)

```bash
# 1. Create an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies (just python-dotenv for the default offline stack)
pip install -r requirements.txt

# 3. Copy the env file — the default runs keyless (no API key needed)
cp .env.example .env
#    (Real judge/embeddings instead of the mock? Their key goes in your OS
#     keychain, not .env — see ../SECRETS.md — then run scripts as `secrun python ...`.)

# 4. Confirm everything is wired up (makes no API call, costs nothing)
python check_setup.py
```

That's it — no key required. The default `PROVIDER=mock` gives you deterministic
offline embeddings and a rule-based judge, so the *entire* repo runs with no key
and no cost. Pick your stack with `PROVIDER` in `.env`:

| `PROVIDER` | What it changes | Keys needed | Cost |
|------------|-----------------|-------------|------|
| `mock` (default) | hashed embeddings + rule-based judge | **none** | **$0** |
| `openai` | real embeddings + `gpt-4o-mini` judge | `OPENAI_API_KEY` | tiny |
| `claude` | `claude-haiku-4-5` judge (+ OpenAI embeddings) | `ANTHROPIC_API_KEY` (+ `OPENAI_API_KEY`) | tiny |

The provider only matters for two optional, model-backed sections (the sampled
quality judge in §6, real embeddings in §5). **Every other section — reading logs,
baselines, alerting, mining, the dashboard — is pure log analysis and runs
identically on all three.** That's the point: observability is something *you*
build around whatever the model is.

> 💡 **Everything in this repo runs offline.** No key, no network, no cost — the
> traffic simulator generates the history, and a deterministic mock stands in for
> the judge and embeddings, so you can see the whole monitoring stack with a model
> that drifts (and recovers) exactly when we tell it to.

---

## 2. The traffic simulator — six weeks of history, on demand

You cannot learn to spot drift in a single request; you need *history*. So this
repo's equivalent of Production's mock model is a deterministic generator of **log
history** ([obs/simulate.py](obs/simulate.py)). `generate()` returns 42 days of
realistic request logs for the support assistant — and, separately, the
ground-truth **incident schedule** it buried inside them. A real system never has
that answer key; we do, only so we can grade our detectors.

The incidents are injected by genuinely changing the traffic, never by writing a
hidden flag into the logs:

- **input drift** — users start asking about a mobile app the KB can't answer
- **quality regression** — a silent model swap makes answers terser, then rolls back
- **cost creep** — a prompt change bloats the context, so tokens and cost climb
- **latency spike** — a one-day slowdown, to contrast a blip against a real trend

```bash
python examples/00_generate_traffic.py
```

The one thing to notice: a log record has the question, cost, latency, and whether
it refused — but **no "was this answer good?" field.** That label doesn't exist in
production, which is the entire challenge of everything that follows.

---

## 3. Metrics from logs — the numbers you actually watch

A log file is data; a metric is a decision aid. [obs/metrics.py](obs/metrics.py)
reduces a pile of records to the dozen numbers an on-call engineer reads: request
volume, **p50 vs p95 latency** (the average hides the tail; the tail is what users
feel), cost per request, error and refusal rates, cache hit rate. The key habit:
compute per *window* (per day here), because a single number is noise and a trend
is a story.

```bash
python examples/01_metrics_from_logs.py
```

---

## 4. Baselines & trends — a number means nothing alone

"Cost per request is $0.00006" tells you nothing; "$0.00006, up from a $0.00003
baseline — that's +120σ" is an incident. [obs/alerts.py](obs/alerts.py) learns
what *normal* looked like from a clean baseline window, then scores every new day
as a **z-score**: how many baseline standard deviations from normal. It's unitless,
so the same "3σ is weird" rule works for latency, cost, and refusals alike — no
hand-tuned threshold per metric.

```bash
python examples/02_baselines_trends.py
```

---

## 5. Input drift — the questions changed, and nothing errored

The most dangerous LLM-app failure is the one that doesn't throw: users gradually
start asking things you were never good at, and the model dutifully answers them
badly while every ops dashboard stays green. [obs/drift.py](obs/drift.py) sees it
three ways, cheapest first: **novel-term rate** (questions using words your baseline
never saw — pure string counting), **embedding drift** (how far today's questions
sit from the baseline's center of mass *in meaning*), and **PSI** (the classic
distribution-shift statistic, here on question length).

```bash
python examples/03_input_drift.py        # offline (mock embeddings)
```

---

## 6. Quality drift — same questions, worse answers

Input drift is users changing; this is the provider changing under you. A silent
model swap makes answers terser and more evasive with identical questions — latency,
cost, and errors never move. Quality is the one metric that isn't free to measure,
so you **sample** a slice of answers per day and score them with a judge (the mock's
rule-based scorer, or a real LLM-as-judge from the Evals dive). And because a mean
over 20 sampled answers is a point estimate, [obs/judge.py](obs/judge.py) reports it
**with a confidence interval** — a dip inside the error bars isn't a regression yet.

```bash
python examples/04_quality_drift.py      # offline (mock judge)
```

Watch the sampled score fall clear of its error bars during the regression and
recover after — while refusal rate (input drift's signal) does its own unrelated
thing. Two different failures, two different detectors; a single "quality" number
would have conflated them.

---

## 7. Alerting — page me for incidents, not for Tuesdays

A dashboard nobody watches is useless; the point is to be *told*. But the naive
alert (`page if p95 > 300ms`) fires on normal days until you mute it — and the muted
alert is the one that misses the real outage. [obs/alerts.py](obs/alerts.py) builds
a better detector from a **baseline z-score**, a **direction** (up is bad for
latency; down is bad for a quality score), and **persistence** — require the breach
to hold N days before paging. Persistence is the dial that tells a one-day spike
apart from a sustained regression.

```bash
python examples/05_alerting.py
```

> ⚠️ **There is no setting that gives zero false alarms *and* zero misses.** Every
> value of `z_threshold` and `persistence` trades one against the other; tighten to
> catch incidents faster and you page on noise, loosen to stop the noise and you
> catch them later. The example makes that tradeoff visible, then picks an operating
> point where the transient latency blip does *not* page as a trend while the
> multi-week drifts do — with an honest detection lag as the price.

---

## 8. Mining traffic — production is your best eval set

Monitoring isn't for admiring dashboards; it's for turning what production teaches
you back into fixes and tests. Every refusal, thumbs-down, and terse answer is a
free, real-user-labelled example of something you got wrong. [obs/mining.py](obs/mining.py)
surfaces them, **clusters** them by theme (so "scattered failures" becomes "904 of
them are the mobile app you don't support"), and emits them as candidate eval cases
in the Evals dive's JSONL shape — ready for a human to write the gold answer and
drop into the regression suite.

```bash
python examples/06_mining_traffic.py
```

The honest caveat the example ends on: most failures are **silent** (no thumbs at
all), so you can't wait for feedback to find them — which is exactly why you monitor
proxies (refusals, drift, judge samples) in the first place.

---

## 9. The classic-MLOps sidebar — the vocabulary, and why half of it doesn't fit

Search "AI observability" and you'll get the classic MLOps curriculum: feature
drift, concept drift, PSI/KS tests, SHAP/LIME explainability. That curriculum is
correct — for the model it grew up around: a **tabular predictor** with a fixed
feature vector and labels that eventually arrive. An LLM app mostly doesn't have
those handles (the input is free text, labels rarely arrive, and LLM
"interpretability" is a research field, not a prod practice), and pretending it does
is a trap.

```bash
python examples/07_classic_mlops_sidebar.py
```

The example maps each classic term to the LLM-app analog that actually works, then
runs PSI in its *native* habitat (a numeric feature) so you've seen the real thing.
Learn the vocabulary — you'll be asked it — but don't buy a vendor's "LLM
explainability": attention weights are not SHAP values.

---

## 10. The capstone: `watch.py`

Now the whole stack runs as one monitoring tool. [hands_on/watch.py](hands_on/watch.py)
ingests the full history, computes every metric series, runs the tuned detector
suite, and prints an operations dashboard — current health vs baseline, a sparkline
per metric, an incident timeline of what fired when, and the honest payoff: a
**detection report** that grades the detectors against the ground-truth incidents.
Did we catch each one, and how many days late?

```bash
# The default 42-day history, dashboard to the terminal
python hands_on/watch.py

# A clean history with NO injected incidents (detectors should stay silent)
python hands_on/watch.py --healthy

# Also write a self-contained HTML dashboard you can open in a browser
python hands_on/watch.py --html report.html
```

On the default history it catches all four incidents (latency spike at 0 days'
lag, input drift and cost creep at ~2 days, the quality regression at ~4) — while
the latency *regression* detector correctly stays silent on the one-day spike. On
`--healthy` it fires nothing. That gap — catches real incidents, ignores noise — is
the entire craft, and it's a tuning choice you can see and change.

---

## Going further — more observability concerns

The core arc ends at the capstone. These are the next ones you hit at scale; the
first is runnable here, the rest are natural extensions of the code.

### Segmentation & cohorts — the aggregate lies

An overall metric can look healthy while a *segment* is on fire: one tenant, one
region, one plan, a single prompt version. This is the most common incident there
is, and a global dashboard is blind to it by construction.

```bash
python examples/08_segmentation.py
```

The example runs an enterprise-only latency regression that's just 15% of traffic:
the **global** p95 detector stays silent (the outage hides inside normal noise)
while the **enterprise** cohort's own p95 triples and alerts. The fix is one line
of discipline — `metrics.daily_by_segment` computes every series per cohort, and
you run the same detectors on each — plus the honest catch it ends on: smaller
cohorts are noisier, so you slice on the few dimensions that carry different risk,
not every field you log.

### Canary & staged rollouts — monitor the change, not just the system
When you ship prompt v3 or a new model, don't flip everyone at once: route a slice
of traffic to it and watch the canary's metrics *against the control* before ramping.
This is the online A/B eval from the Evals dive, run as an operational guardrail —
promote only if the canary clears the control and no guardrail (latency, cost,
refusals) regressed.

### SLOs, error budgets & trace sampling — monitoring at real volume
At scale you can't judge or store everything. Define an **SLO** (e.g. "p95 < 800ms,
99% of the time") and an **error budget** you spend before you must stop shipping;
**sample** traces (head vs tail sampling — keep all the slow/errored ones) to keep
storage sane; and remember your logs are a **PII sink** (Production §3) — scrub
before they leave the process.

---

## Where to go next

You've built a small, complete monitoring stack. The road to production is swapping
each from-scratch layer for its industrial counterpart — the interfaces stay the
same:

- **Metrics & tracing** → OpenTelemetry + a backend (Grafana/Tempo, Honeycomb,
  Datadog), or an LLM-native platform (Langfuse, Arize Phoenix, Braintrust) that
  captures traces, costs, and judge scores for you.
- **Drift detection** → Evidently, NannyML, or Arize for input/embedding drift with
  managed baselines and reports, instead of hand-rolled PSI.
- **Quality monitoring** → a continuous LLM-as-judge on sampled production traffic,
  wired to the same eval suite from the Evals dive, with human review of the
  disagreements.
- **Alerting** → Prometheus Alertmanager / Grafana alerts / PagerDuty, with the same
  z-score-and-persistence logic expressed as alert rules and on-call rotations.
- **The flywheel** → thumbs and mined failures flowing into a labelling queue, your
  gold eval set, and fine-tuning data — closing Evals → Production → Observability →
  Evals.

Each slots on top of the idea you started with: your quality is a trend you watch,
built from the handles you actually have.

---

## File map

```
check_setup.py              ← run first: verifies Python, packages, provider
README.md                   ← this guide
EXERCISES.md                ← predict-then-run prompts, one per section
obs/                        ← the from-scratch observability stack (read it!)
  simulate.py               ← the traffic generator: weeks of logs + injected incidents
  logs.py                   ← the LogRecord shape + JSONL load/save
  metrics.py                ← operational metrics from records (latency, cost, rates)
  drift.py                  ← input drift: novel-term, embedding drift, PSI
  judge.py                  ← sampled quality scoring, with confidence intervals
  alerts.py                 ← baselines, z-scores, persistence, EWMA → alerts
  mining.py                 ← surface + cluster failures into eval candidates
  providers.py              ← the ONLY provider seam: mock (default) + openai + claude
hands_on/
  watch.py                  ← capstone: dashboard + incident timeline + detection report
  obs_html.py               ← optional self-contained HTML dashboard (--html)
examples/
  00_generate_traffic.py    ← the log history that makes it all runnable (no key)
  01_metrics_from_logs.py   ← logs → the numbers you watch (p50/p95, cost, rates)
  02_baselines_trends.py    ← why a number needs a baseline; the z-score
  03_input_drift.py         ← novel-term, embedding drift, PSI (mock embeddings)
  04_quality_drift.py       ← sampled LLM-as-judge with confidence intervals
  05_alerting.py            ← the false-alarm vs detection-lag tradeoff
  06_mining_traffic.py      ← failures → clusters → eval candidates (the flywheel)
  07_classic_mlops_sidebar.py ← the tabular-MLOps vocabulary, and why it doesn't fit
  08_segmentation.py        ← slice by cohort: the incident a global average hides
```

---

## Troubleshooting

Run `python check_setup.py` first — it catches most problems. Then, by symptom:

| What you see | What it means / the fix |
|--------------|-------------------------|
| `ModuleNotFoundError: dotenv` | Dependencies aren't installed or the venv isn't active. `source .venv/bin/activate` then `pip install -r requirements.txt`. |
| `PROVIDER=... needs ... in the environment` | You switched to a real provider without a key. Load it from your keychain with `secrun` (see [../SECRETS.md](../SECRETS.md)), or go back to `PROVIDER=mock`. |
| The mock judge/embeddings "aren't a real model" | Correct — they're deterministic stand-ins so the repo runs offline. Flip `PROVIDER=openai` and run under `secrun` for the real thing; the drift/quality *stories* don't change, the exact numbers do. |
| A detector fires on a day I didn't expect | Baselines and z-scores are sensitive to the baseline window. Widen `--baseline-days`, or read the z-series with `obs.alerts.signed_z` to see why. |
| The judge z-score wobbles between runs | The judge *samples* (per-day, seeded), so a different `--per-day` changes the estimate. Bigger samples shrink the margin (§6). |
| `SyntaxError` / odd type errors on startup | You're likely on Python 3.9 or older; this repo needs 3.10+. `check_setup.py` confirms your version. |

Still stuck? Every file is small and self-contained — open it, read the docstring
at the top, and run it directly.

---

## The series

This is one of a set of standalone, hands-on deep dives into building with LLM APIs
— eight core, plus the bonus dives listed below. Each one stands on its own — its
own setup, examples, and capstone — and they all share the same house style:
provider-agnostic, built from scratch (no frameworks), offline-first examples, and
a real capstone.

**Core path (do these in order):**

1. [OpenAI API](https://github.com/alexvervloet/openai-api-deep-dive) — the API from zero
2. [Claude API](https://github.com/alexvervloet/claude-api-deep-dive) — the same ideas, the Anthropic way
3. [Prompt Engineering](https://github.com/alexvervloet/prompt-engineering-deep-dive) — shape model behavior with better prompts
4. [RAG](https://github.com/alexvervloet/rag-deep-dive) — answer questions over your own documents
5. [Evals](https://github.com/alexvervloet/evals-deep-dive) — measure whether a change actually helps
6. [Agents](https://github.com/alexvervloet/agents-deep-dive) — give a model tools and a loop so it can act
7. [Prompt Injection & Guardrails](https://github.com/alexvervloet/prompt-injection-deep-dive) — attack and defend all of the above
8. [Production](https://github.com/alexvervloet/ai-in-production-deep-dive) — operate one app end to end

**Bonus dives** — standalone, slotting in where they're most useful:

- [Context Engineering](https://github.com/alexvervloet/context-engineering-deep-dive) — manage what's in the window: memory, compaction, assembly
- [Multimodal](https://github.com/alexvervloet/multimodal-deep-dive) — images & audio, not just text
- [Fine-tuning](https://github.com/alexvervloet/fine-tuning-deep-dive) — teach a model new behavior by example
- [MCP](https://github.com/alexvervloet/mcp-deep-dive) — serve tools, data & prompts to any LLM over a standard protocol
- [Local Models](https://github.com/alexvervloet/local-models-deep-dive) — run open-weight models on your own machine
- [Agent Harnesses](https://github.com/alexvervloet/agent-harness-deep-dive) — build on the loop: hooks, permissions, sandboxing, subagents
- [Realtime Voice](https://github.com/alexvervloet/realtime-voice-deep-dive) — low-latency speech-to-speech agents
- **Observability** — watch a running app over time: drift, quality, alerting, the flywheel

**You are here: Observability** — the bonus dive that pairs with Production (#8) and
Evals (#5). Production operates one request; this operates six weeks of them.
