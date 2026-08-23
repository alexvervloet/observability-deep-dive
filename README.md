# Observability: A Guided Deep Dive

A hands-on playground for the question every other repo in this series answers only for
right now. Is my LLM app still good six weeks after it launched, and would I know before my
users tell me? You'll take one small app, the Acme Cloud support assistant, generate weeks
of its request logs, and build the machinery that watches a running system over time.
Operational metrics, input drift, quality drift, alerting that doesn't cry wolf, and the
loop that turns production failures back into eval cases. No framework, no SaaS dashboard,
no Grafana. Just enough code to see how each piece works. Then at the end you emit the same
telemetry as real OpenTelemetry over OTLP, in §11, so you can see exactly which part of
what you built the industry standard replaces and which part it doesn't.

Here is what makes this repo work. It runs completely offline on synthetic log history,
with no API key. Everything else in the series measures your app at a single point in time.
This repo needs history, so instead of a live model it ships a deterministic traffic
simulator that generates six weeks of realistic request logs with real incidents buried
inside: drift, a silent model regression, cost creep, a latency spike. Your job is to catch
them. Flip one env var and the optional model-backed bits, a sampled LLM-as-judge and real
embeddings, run against a real OpenAI or Claude model. The rest never needs one, because it
analyzes logs.

This is a bonus dive in the series, slotting in right after
[Production](https://github.com/alexvervloet/ai-in-production-deep-dive), #8. Production
taught you to operate one request end to end: traced, costed, guarded. This teaches you to
operate the next six weeks of them. Every log record here has the same shape Production's
`trace.summary()` emits, so this repo consumes exactly what a real traced app already
produces.

Like its siblings, walk through it rather than reading it. Each section ends with something
to run, all of it offline and free. And [EXERCISES.md](EXERCISES.md) has a predict-then-run
prompt for each section.

---

## 0. The one big idea

An eval, repo #5, tells you a change is better today, on the questions you have today.
Production, #8, tells you what one request did. Neither tells you that five weeks in, users
started asking about a feature you don't support, or that your provider swapped the model
under you and answers got worse without anyone saying so. Those failures don't throw
exceptions. Nothing turns red. The dashboards you already have stay green while quality
rots.

> **A prototype gets judged once. A production system gets judged continuously, so your
> quality has to be a trend you watch rather than a number you checked at launch.**

Everything below is one of the handles you have for watching that trend when your model is
a black box that takes free text and returns free text. Metrics from logs, drift in the
inputs, a sampled judge on the outputs, and alerting that tells the difference between a
bad Tuesday and a real incident.

---

## 1. Setup (5 minutes)

```bash
# 1. Create an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies (python-dotenv for the default offline stack, plus the
#    optional OpenTelemetry extras that §11 uses; everything else is stdlib)
pip install -r requirements.txt

# 3. Copy the env file: the default runs keyless (no API key needed)
cp .env.example .env
#    (Real judge/embeddings instead of the mock? Their key goes in your OS
#     keychain, not .env: see ../docs/SECRETS.md, then run scripts as `secrun python ...`.)

# 4. Confirm everything is wired up (makes no API call, costs nothing)
python check_setup.py
```

That's it, and no key is required. The default `PROVIDER=mock` gives you deterministic
offline embeddings and a rule-based judge, so the entire repo runs with no key and no cost.
Pick your stack with `PROVIDER` in `.env`.

| `PROVIDER` | What it changes | Keys needed | Cost |
|------------|-----------------|-------------|------|
| `mock` (default) | hashed embeddings + rule-based judge | **none** | **$0** |
| `openai` | real embeddings + `gpt-5.4-nano` judge | `OPENAI_API_KEY` | tiny |
| `claude` | `claude-haiku-4-5` judge (+ OpenAI embeddings) | `ANTHROPIC_API_KEY` (+ `OPENAI_API_KEY`) | tiny |

The provider only matters for two optional, model-backed sections: the sampled quality
judge in §6 and real embeddings in §5. Every other section, meaning reading logs,
baselines, alerting, mining, and the dashboard, is pure log analysis and runs identically
on all three. That is the point. Observability is something you build around whatever the
model happens to be.

> **Everything in this repo runs offline.** No key, no network, no cost. The traffic
> simulator generates the history and a deterministic mock stands in for the judge and the
> embeddings, so you can see the whole monitoring stack with a model that drifts, and
> recovers, exactly when we tell it to.

---

## 2. The traffic simulator, six weeks of history on demand

You cannot learn to spot drift in a single request. You need history. So this repo's
equivalent of Production's mock model is a deterministic generator of log history,
[obs/simulate.py](obs/simulate.py). `generate()` returns 42 days of realistic request logs
for the support assistant, and separately the ground-truth incident schedule it buried
inside them. A real system never has that answer key. We do, only so we can grade our
detectors.

The incidents get injected by genuinely changing the traffic, never by writing a hidden
flag into the logs.

- **Input drift.** Users start asking about a mobile app the KB can't answer.
- **Quality regression.** A silent model swap makes answers terser, then rolls back.
- **Cost creep.** A prompt change bloats the context, so tokens and cost climb.
- **Latency spike.** A one-day slowdown, to contrast a blip against a real trend.

```bash
python examples/00_generate_traffic.py
```

One thing to notice. A log record has the question, cost, latency, and whether it refused,
and no "was this answer good?" field. That label does not exist in production, which is the
entire challenge of everything that follows.

---

## 3. Metrics from logs, the numbers you actually watch

A log file is data. A metric is a decision aid. [obs/metrics.py](obs/metrics.py) reduces a
pile of records to the dozen numbers an on-call engineer reads: request volume, p50 and p95
latency (the average hides the tail, and the tail is what users feel), cost per request,
error and refusal rates, cache hit rate. The habit that matters is computing per window,
per day here, because a single number is noise and a trend is a story.

```bash
python examples/01_metrics_from_logs.py
```

---

## 4. Baselines and trends, because a number means nothing alone

"Cost per request is $0.00006" tells you nothing. "$0.00006, up from a $0.00003 baseline,
that's +120σ" is an incident. [obs/alerts.py](obs/alerts.py) learns what normal looked like
from a clean baseline window, then scores every new day as a z-score: how many baseline
standard deviations from normal. It's unitless, so the same "3σ is weird" rule works for
latency, cost, and refusals alike, with no hand-tuned threshold per metric.

```bash
python examples/02_baselines_trends.py
```

---

## 5. Input drift, where the questions changed and nothing errored

The most dangerous LLM-app failure is the one that doesn't throw. Users gradually start
asking things you were never good at, and the model dutifully answers them badly while
every ops dashboard stays green. [obs/drift.py](obs/drift.py) sees it three ways, cheapest
first. Novel-term rate counts questions using words your baseline never saw, pure string
counting. Embedding drift measures how far today's questions sit from the baseline's center
of mass in meaning. And PSI is the classic distribution-shift statistic, here on question
length.

```bash
python examples/03_input_drift.py        # offline (mock embeddings)
```

---

## 6. Quality drift, same questions and worse answers

Input drift is users changing. This is the provider changing under you. A silent model swap
makes answers terser and more evasive on identical questions, and latency, cost, and errors
never move. Quality is the one metric that isn't free to measure, so you sample a slice of
answers per day and score them with a judge, either the mock's rule-based scorer or a real
LLM-as-judge from the Evals dive. And because a mean over 20 sampled answers is a point
estimate, [obs/judge.py](obs/judge.py) reports it with a confidence interval, since a dip
inside the error bars is not a regression yet.

```bash
python examples/04_quality_drift.py      # offline (mock judge)
```

Watch the sampled score fall clear of its error bars during the regression and recover
after, while refusal rate, which is input drift's signal, does its own unrelated thing. Two
different failures, two different detectors. A single "quality" number would have conflated
them.

---

## 7. Alerting, so you get paged for incidents and not for Tuesdays

A dashboard nobody watches is useless. The point is to be told. But the naive alert, `page
if p95 > 300ms`, fires on normal days until you mute it, and the muted alert is the one
that misses the real outage. [obs/alerts.py](obs/alerts.py) builds a better detector from
three things: a baseline z-score, a direction (up is bad for latency, down is bad for a
quality score), and persistence, requiring the breach to hold N days before paging.
Persistence is the dial that tells a one-day spike apart from a sustained regression.

```bash
python examples/05_alerting.py
```

> **No setting gives you zero false alarms and zero misses.** Every value of
> `z_threshold` and `persistence` trades one against the other. Tighten to catch incidents
> faster and you page on noise. Loosen to stop the noise and you catch them later. The
> example makes that tradeoff visible, then picks an operating point where the transient
> latency blip does not page as a trend while the multi-week drifts do, with an honest
> detection lag as the price.

---

## 8. Mining traffic, because production is your best eval set

Monitoring isn't for admiring dashboards. It's for turning what production teaches you back
into fixes and tests. Every refusal, thumbs-down, and terse answer is a free,
real-user-labelled example of something you got wrong. [obs/mining.py](obs/mining.py) pulls
them out, clusters them by theme so "scattered failures" becomes "904 of them are the
mobile app you don't support", and emits them as candidate eval cases in the Evals dive's
JSONL shape, ready for a human to write the gold answer and drop into the regression
suite.

```bash
python examples/06_mining_traffic.py
```

The example ends on an honest caveat. Most failures are silent, with no thumbs at all, so
you cannot wait for feedback to find them. That is exactly why you monitor proxies like
refusals, drift, and judge samples in the first place.

---

## 9. The classic-MLOps vocabulary, and why half of it doesn't fit

Search "AI observability" and you'll get the classic MLOps curriculum: feature drift,
concept drift, PSI and KS tests, SHAP and LIME explainability. That curriculum is correct
for the model it grew up around, a tabular predictor with a fixed feature vector and labels
that eventually arrive. An LLM app mostly doesn't have those handles. The input is free
text, labels rarely arrive, and LLM interpretability is a research field rather than a prod
practice. Pretending otherwise is a trap.

```bash
python examples/07_classic_mlops_sidebar.py
```

The example maps each classic term to the LLM-app analog that actually works, then runs
PSI in its native habitat, a numeric feature, so you have seen the real thing. Learn the
vocabulary, because you'll be asked it. Don't buy a vendor's "LLM explainability". Attention
weights are not SHAP values.

---

## 10. The capstone: `watch.py`

Now the whole stack runs as one monitoring tool. [hands_on/watch.py](hands_on/watch.py)
ingests the full history, computes every metric series, runs the tuned detector suite, and
prints an operations dashboard: current health against baseline, a sparkline per metric, an
incident timeline of what fired when, and the thing that actually settles it, a detection
report grading the detectors against the ground-truth incidents. Did we catch each one, and
how many days late?

```bash
# The default 42-day history, dashboard to the terminal
python hands_on/watch.py

# A clean history with NO injected incidents (detectors should stay silent)
python hands_on/watch.py --healthy

# Also write a self-contained HTML dashboard you can open in a browser
python hands_on/watch.py --html report.html
```

On the default history it catches all four incidents: the latency spike at 0 days' lag,
input drift and cost creep at about 2 days, the quality regression at about 4, while the
latency regression detector correctly stays silent on the one-day spike. On `--healthy` it
fires nothing. That gap, catching real incidents while ignoring noise, is the entire craft,
and it is a tuning choice you can see and change.

---

## 11. Real OpenTelemetry, the same telemetry on the wire

Everything so far analyzed logs. This section emits them, in the format the industry
actually ships: real OpenTelemetry spans and metrics, over the real OTLP protocol, from the
same `LogRecord` you have been reading all along. [obs/otel.py](obs/otel.py) is the whole
integration, and it is smaller than any detector in this repo.

```bash
pip install -r requirements.txt          # the OTel extras are optional, and in there

python examples/09_otel_export.py        # offline: read a real span, no network
python examples/09_otel_export.py --console   # the SDK's raw console exporter
```

Three things the example makes concrete:

- **What a span actually contains.** The `gen_ai.*` attribute names are
  **semantic conventions**, so an LLM-aware backend renders a model call view
  without being told anything about your app. The `app.*` ones are yours. Cost
  lives there on purpose: it is priced per vendor, per model, per contract, so
  OTel declines to standardize it. Conventions for conventional things, your own
  prefix for the rest.
- **Spans are events; metrics are aggregates.** 300 requests produce 300 spans and
  8 metric points, and at 300 million requests it is still 8 metric points, because
  a metric point is one per *attribute combination*, not one per request. (It moves
  when a combination appears that had not occurred before, which is a bound, not a
  constant.) That ratio is why the rule of thumb is *alert on metrics, debug on
  traces*, and why
  metric attributes are deliberately a smaller set than span attributes: each
  distinct combination is its own time series, so `trace_id` is free on a span and
  a cardinality disaster on a metric.
- **Instrumenting a live call is three lines.** `otel.llm_span(...)` wraps the
  call you already make; timing, status, and exception recording come free. The
  sampled judge score from §6 rides along as `gen_ai.evaluation.*`, which is how a
  backend charts quality beside latency.

### Watch it cross a socket, without Docker

The usual way to see OTLP work is to `docker run` a collector, a backend, and a browser
tab. This repo runs offline, so instead
[hands_on/otel_collector.py](hands_on/otel_collector.py) implements the receiving end of
the protocol in about 200 lines. It accepts the protobuf the exporter posts, ungzips it
when the `Content-Encoding` header says to, and decodes it with the same generated classes
the exporter used to encode it. Two terminals:

```bash
python hands_on/otel_collector.py        # terminal 1: listens on localhost:4318
python examples/09_otel_export.py --otlp # terminal 2: sends real OTLP
```

Terminal 1 prints the spans and metric points that arrived. That is the actual
protocol, not a simulation of it. Point the exporter at a real backend instead and
nothing in the emitting code changes:

```bash
docker run -p 4318:4318 -p 16686:16686 jaegertracing/all-in-one
python examples/09_otel_export.py --otlp --endpoint http://localhost:4318
```

Only the URL and an auth header differ for a vendor. That interchangeability is
the whole reason the standard exists, and it is why "we emit OpenTelemetry" is a
decision you can make before you have picked a backend.

### Four things that will bite you

- **The batch queue outlives your intentions.** Spans are batched, not sent as
  they end. Python's SDK registers an `atexit` hook, so a *clean* exit flushes
  even if you never call `shutdown()`, which is worth verifying rather than
  believing (the exercises walk you through it). What no hook survives is an exit
  that skips `atexit`: `os._exit`, SIGKILL, an OOM kill, a container stopped past
  its grace period, a forked worker, or an SDK in another language without that
  default. Call `shutdown()` and the question stops mattering.
- **Old spans vanish.** Backends have look-back windows; backfilled history that
  is weeks old may be accepted and never shown. `replay()` slides the batch
  forward to now for exactly this reason, and says so.
- **Message content is a PII decision.** Question and answer text are off the span
  by default here, and gated behind a flag in every real GenAI instrumentation
  (OTel's own is `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`). Turning it
  on ships user text to a third-party store on someone else's retention schedule.
  Most teams enable it for a sampled slice, never for all traffic.
- **Compression is off by default.** This SDK sends uncompressed protobuf unless
  you ask otherwise; [obs/otel.py](obs/otel.py) asks (`compression=Compression.Gzip`),
  which is worth about 7x on this payload. Telemetry is usually billed on ingest
  volume, so this is one argument for a real discount, and the receiver has to
  check `Content-Encoding` rather than assume either way.

Instrumentation also rots in a way nothing else in this repo does. Rename an attribute and
no exception is raised, the spans keep flowing, and every dashboard and alert keyed to the
old name goes blank with nothing to explain it. That is what
[tests/test_otel.py](tests/test_otel.py) is for, and it is the one test in this repo worth
copying into your own.

```bash
python -m unittest discover -s tests
```

It pins the conventional attribute names, the low-cardinality span name, the error status
mapping, the PII default, and the metric shape. It has already paid for itself once, by
failing on a claim the prose in this section had made three times.

> **OTel is transport, not judgement.** Adopting it replaces §2 and §3 of this repo,
> writing telemetry down and computing metrics from it yourself. It does not replace §4
> through §10. A backend will happily store a million perfectly formed spans and never once
> tell you that quality drifted. Baselines, drift detection, the sampled judge, alert
> tuning, and mining failures back into evals are still yours to build or buy. That is the
> honest shape of every "just use the industry tool" upgrade in this series. You buy the
> plumbing, not the judgement.

---

## Going further: more observability concerns

The core arc ends at the capstone. These are the next ones you hit at scale. The first is
runnable here, and the rest are natural extensions of the code.

### Segmentation and cohorts, where the aggregate lies

An overall metric can look healthy while a segment is on fire: one tenant, one region, one
plan, a single prompt version. This is the most common incident there is, and a global
dashboard is blind to it by construction.

```bash
python examples/08_segmentation.py
```

The example runs an enterprise-only latency regression that is 15% of traffic. The global
p95 detector stays silent, because the outage hides inside normal noise, while the
enterprise cohort's own p95 triples and alerts. The fix is one line of discipline.
`metrics.daily_by_segment` computes every series per cohort and you run the same detectors
on each. There is an honest catch, which the example ends on. Smaller cohorts are noisier,
so slice on the few dimensions that carry different risk rather than every field you
log.

### Canary and staged rollouts, monitoring the change as well as the system
When you ship prompt v3 or a new model, don't flip everyone at once. Route a slice of
traffic to it and watch the canary's metrics against the control before ramping. This is
the online A/B eval from the Evals dive, run as an operational guardrail. Promote only if
the canary clears the control and no guardrail regressed on latency, cost, or refusals.

### SLOs, error budgets, and trace sampling, for monitoring at real volume
At scale you cannot judge or store everything. Define an SLO, something like "p95 under
800ms, 99% of the time", and an error budget you spend before you have to stop shipping.
Sample traces, with head or tail sampling, keeping all the slow and errored ones, to keep
storage sane. And remember your logs are a PII sink, as Production §3 says, so scrub before
they leave the process.

---

## Where to go next

You've built a small, complete monitoring stack. The road to production is swapping each
from-scratch layer for its industrial counterpart. The interfaces stay the same.

- **Metrics & tracing** → OpenTelemetry + a backend (Grafana/Tempo, Honeycomb,
  Datadog), or an LLM-native platform (Langfuse, Arize Phoenix, Braintrust) that
  captures traces, costs, and judge scores for you. **§11 already does the
  emitting half for real**, so this swap is a URL and an auth header away.
- **Drift detection** → Evidently, NannyML, or Arize for input/embedding drift with
  managed baselines and reports, instead of hand-rolled PSI.
- **Quality monitoring** → a continuous LLM-as-judge on sampled production traffic,
  wired to the same eval suite from the Evals dive, with human review of the
  disagreements.
- **Alerting** → Prometheus Alertmanager / Grafana alerts / PagerDuty, with the same
  z-score-and-persistence logic expressed as alert rules and on-call rotations.
- **The feedback loop** → thumbs and mined failures flowing into a labelling queue, your
  gold eval set, and fine-tuning data, closing Evals -> Production -> Observability ->
  Evals.

Every one of these sits on top of the idea you started with. Your quality is a trend you
watch, built from the handles you actually have.

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
  otel.py                   ← real OpenTelemetry: LogRecord → spans + metrics → OTLP
hands_on/
  watch.py                  ← capstone: dashboard + incident timeline + detection report
  obs_html.py               ← optional self-contained HTML dashboard (--html)
  otel_collector.py         ← a 200-line OTLP/HTTP receiver, so the wire works offline
tests/
  test_otel.py              ← pins the span/metric contract: names, status, PII default
examples/
  00_generate_traffic.py    ← the log history that makes it all runnable (no key)
  01_metrics_from_logs.py   ← logs → the numbers you watch (p50/p95, cost, rates)
  02_baselines_trends.py    ← why a number needs a baseline; the z-score
  03_input_drift.py         ← novel-term, embedding drift, PSI (mock embeddings)
  04_quality_drift.py       ← sampled LLM-as-judge with confidence intervals
  05_alerting.py            ← the false-alarm vs detection-lag tradeoff
  06_mining_traffic.py      ← failures → clusters → eval candidates (the feedback loop)
  07_classic_mlops_sidebar.py ← the tabular-MLOps vocabulary, and why it doesn't fit
  08_segmentation.py        ← slice by cohort: the incident a global average hides
  09_otel_export.py         ← real OTel spans + metrics, over real OTLP (optional deps)
```

---

## Troubleshooting

Run `python check_setup.py` first; it catches most problems. Then, by symptom:

| What you see | What it means / the fix |
|--------------|-------------------------|
| `ModuleNotFoundError: dotenv` | Dependencies aren't installed or the venv isn't active. `source .venv/bin/activate` then `pip install -r requirements.txt`. |
| `PROVIDER=... needs ... in the environment` | You switched to a real provider without a key. Load it from your keychain with `secrun` (see [../docs/SECRETS.md](../docs/SECRETS.md)), or go back to `PROVIDER=mock`. |
| The mock judge/embeddings "aren't a real model" | Correct: they're deterministic stand-ins so the repo runs offline. Flip `PROVIDER=openai` and run under `secrun` for the real thing; the drift/quality *stories* don't change, the exact numbers do. |
| A detector fires on a day I didn't expect | Baselines and z-scores are sensitive to the baseline window. Widen `--baseline-days`, or read the z-series with `obs.alerts.signed_z` to see why. |
| The judge z-score wobbles between runs | The judge *samples* (per-day, seeded), so a different `--per-day` changes the estimate. Bigger samples shrink the margin (§6). |
| `This section needs the OpenTelemetry SDK` | §11 only. `pip install -r requirements.txt` (or the two `opentelemetry-*` packages it lists). Sections 2 through 10 don't import them. |
| `--otlp` says nothing is listening on `:4318` | Start the receiver first, in another terminal: `python hands_on/otel_collector.py`. |
| Spans exported, but the backend shows nothing | Two usual causes: the process exited without flushing (call `shutdown()`), or the timestamps are older than the backend's look-back window (§11). |
| `SyntaxError` / odd type errors on startup | You're likely on Python 3.9 or older; this repo needs 3.10+. `check_setup.py` confirms your version. |

Still stuck? Every file is small and self-contained. Open it, read the docstring
at the top, and run it directly.

---

## The series

This is one of a set of standalone, hands-on deep dives into building with LLM APIs
eight core, plus the bonus dives listed below. Each one stands on its own, with its
own setup, examples, and capstone, and they all share the same house style:
provider-agnostic, built from scratch (no frameworks), offline-first examples, and
a real capstone.

**Core path (do these in order):**

1. [OpenAI API](https://github.com/alexvervloet/openai-api-deep-dive): the API from zero
2. [Claude API](https://github.com/alexvervloet/claude-api-deep-dive): the same ideas, the Anthropic way
3. [Prompt Engineering](https://github.com/alexvervloet/prompt-engineering-deep-dive): shape model behavior with better prompts
4. [RAG](https://github.com/alexvervloet/rag-deep-dive): answer questions over your own documents
5. [Evals](https://github.com/alexvervloet/evals-deep-dive): measure whether a change actually helps
6. [Agents](https://github.com/alexvervloet/agents-deep-dive): give a model tools and a loop so it can act
7. [Prompt Injection & Guardrails](https://github.com/alexvervloet/prompt-injection-deep-dive): attack and defend all of the above
8. [Production](https://github.com/alexvervloet/ai-in-production-deep-dive): operate one app end to end

**Bonus dives**, standalone and slotting in where they're most useful:

- [Context Engineering](https://github.com/alexvervloet/context-engineering-deep-dive): manage what's in the window, with memory, compaction, and assembly
- [AI Data Engineering](https://github.com/alexvervloet/ai-data-engineering-deep-dive): the corpus behind the index, with versions, lineage, ACLs, and deletes
- [Multimodal](https://github.com/alexvervloet/multimodal-deep-dive): images and audio as well as text
- [Fine-tuning](https://github.com/alexvervloet/fine-tuning-deep-dive): teach a model new behavior by example
- [MCP](https://github.com/alexvervloet/mcp-deep-dive): serve tools, data, and prompts to any LLM over a standard protocol
- [Local Models](https://github.com/alexvervloet/local-models-deep-dive): run open-weight models on your own machine
- [Agent Harnesses](https://github.com/alexvervloet/agent-harness-deep-dive): build on the loop, adding hooks, permissions, sandboxing, and subagents
- [Realtime Voice](https://github.com/alexvervloet/realtime-voice-deep-dive): low-latency speech-to-speech agents
- **Observability**: watch a running app over time, covering drift, quality, alerting, and the feedback loop
- [Architecture](https://github.com/alexvervloet/architecture-deep-dive): the seams between the components, each decision measured rather than asserted
- [GenAI Security](https://github.com/alexvervloet/genai-security-deep-dive): treat the model as an untrusted principal, and put identity, supply chain, isolation, budgets, and release gates around it
- [Inference Platform Engineering](https://github.com/alexvervloet/inference-platform-deep-dive): turn finite GPU memory and a request queue into latency, throughput, and a fleet size you can defend
- [Testing & Delivery](https://github.com/alexvervloet/testing-and-delivery-deep-dive): decide whether a build is fit to promote, using evidence, gates, staged rollout, and rollback
- [Professional Tools](https://github.com/alexvervloet/professional-tools-deep-dive): rebuild each hand-written piece with the tool professionals reach for, and measure both

And the whole series lands in one codebase in the
[capstone](https://github.com/alexvervloet/deep-dive-capstone): a codebase Q&A tool
built step by step, one tag per dive.

**You are here: Observability**, the bonus dive that pairs with Production (#8) and
Evals (#5). Production operates one request; this operates six weeks of them.
