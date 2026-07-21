# Exercises: make the learning stick

Reading code teaches you less than *predicting* what it will do and then checking.
This file turns each section of the [README](README.md) into a few quick
active-recall prompts: a thing to predict, a thing to change, and a question to
answer from memory.

How to use it: work the section first, then come back. **Commit to an answer
before you run or reveal.** The prediction is where the learning happens, even
(especially) when you're wrong. Answers are hidden behind ▸ toggles.

> Everything here is **offline**: the default mock provider needs no key and
> costs nothing. Run as much as you like.

---

## Section 2: The traffic simulator

**Predict, then run.** Run `examples/00_generate_traffic.py` twice. Will the log
records differ between the two runs? Why does the answer matter for an exercise
that says "the alert fires on day 16"?

<details><summary>▸ Answer</summary>

No: `generate()` is seeded, so the same call yields byte-identical history every
time. That determinism is the whole reason an exercise can predict a specific
outcome and be right: a cache hit is provably identical, a detector fires on a
knowable day, and the incident schedule is fixed. Change the `seed` and you get a
different (but equally reproducible) history.
</details>

**Recall.** The simulator knows exactly which days are "input drift" and which are
"quality regression." Why is that information deliberately kept *out* of the log
records the rest of the repo reads?

<details><summary>▸ Answer</summary>

Because a real production log doesn't have it. There's no field that says "this
answer was bad". If there were, monitoring would be trivial. The whole skill is
*inferring* trouble from proxies (latency, refusals, a sampled judge). The schedule
exists only as an answer key to grade our detectors against, exactly what you don't
get in real life.
</details>

---

## Section 3: Metrics from logs

**Predict, then run.** In `examples/01_metrics_from_logs.py`, will p95 latency be
closer to p50, or much higher? What would it mean if p95 were *equal* to p50?

<details><summary>▸ Answer</summary>

Much higher. The simulator gives most calls a moderate latency and a minority a
long tail, so p95 sits well above p50. If p95 equalled p50 your latency would have
no tail at all (every request nearly identical), which almost never happens in a
real system calling a model over a network. Watching the *gap* is watching how bad
the slow experiences are.
</details>

**Change it.** `window_metrics` counts `cost_per_request_usd` over cache *misses*
only. Predict what happens to that number if you include cache hits, then change it
and check.

<details><summary>▸ Answer</summary>

It drops, because cache hits cost $0 and would dilute the average. That's exactly
why misses-only is the right denominator for "is a call getting more expensive?" 
you want the price of the work, undiluted by how often you skipped the work.
</details>

---

## Section 4: Baselines & trends

**Predict, then run.** `examples/02_baselines_trends.py` prints a z-score for cost
per request. During the healthy weeks it hovers near zero; once cost creep starts it
climbs past 100. Why so *huge*, and is a z of 120 a bug?

<details><summary>▸ Answer</summary>

Not a bug. Cost per request is an extremely *stable* baseline metric, and its day-to-day
spread is tiny, so even a modest absolute rise is an enormous number of standard
deviations. A big z just means "this moved far relative to how much it normally
wobbles." A noisy metric (like p95) needs a much bigger absolute change to reach the
same z. That's the point of standardizing: it's comparable across metrics.
</details>

---

## Section 5: Input drift

**Predict, then run.** In `examples/03_input_drift.py`, novel-term rate and
embedding drift both start near their baseline and rise in the back half. Which one
would you expect to react *first* to a brand-new topic, and why?

<details><summary>▸ Answer</summary>

Novel-term rate. It fires the moment a question contains a word your baseline never
saw ("iphone", "android"), which is instant and needs no model. Embedding drift is
subtler (it can catch drift even in ordinary words) but moves more gradually. Cheap
lexical checks are your early smoke alarm; embeddings are the deeper confirmation.
</details>

**Recall.** Input drift moved *no* error, latency, or cost metric. So what actually
goes wrong for users, and why is that the scariest kind of failure?

<details><summary>▸ Answer</summary>

The model answers questions it can't answer well: here, refusing (or worse,
confidently guessing) about a mobile app that isn't in the KB. Nothing errors, so
every ops dashboard stays green while answer quality quietly rots. A failure that
throws an exception gets caught in minutes; a silent quality failure can run for
weeks until users leave.
</details>

---

## Section 6: Quality drift

**Predict, then run.** `examples/04_quality_drift.py` reports the sampled judge
score with a ± margin. On a day in the regression window the score is 0.615 ±0.089.
Is that a real regression from a 0.79 baseline, or could it be noise?

<details><summary>▸ Answer</summary>

Real. 0.615 + 0.089 = 0.704, still well below the 0.79 baseline, so the drop clears
its own error bar. Compare a day at 0.685 ±0.077: 0.685 + 0.077 = 0.762, which does
*not* clear the bar, so that day alone isn't yet conclusive. Reporting the margin is
what stops you from declaring a regression on a wobble.
</details>

**Recall.** Why does the judge sample only ~20 answers per day instead of scoring
every request, and what does the sample size trade off?

<details><summary>▸ Answer</summary>

Because grading isn't free. A real LLM-as-judge costs a call per answer, and you
have thousands a day. Sampling keeps the cost bounded. The trade: a smaller sample
is cheaper but has a wider confidence interval, so it takes a bigger true drop to
detect. More samples, tighter margin, higher cost: you pick the point.
</details>

---

## Section 7: Alerting

**Predict, then run.** `examples/05_alerting.py` runs the latency detector at
persistence 1, 2, and 3. Predict which persistence catches the one-day spike, and
which stays silent on it. Then run it.

<details><summary>▸ Answer</summary>

persistence=1 catches the spike (one breach day is enough to fire); persistence=3
rides over it (a single day never reaches three consecutive breaches). That's the
dial that separates a transient blip from a sustained regression. Neither is
"correct": a one-day spike *is* worth a glance but not a 3am page, which is why you
run a spike detector (persistence 1) and a trend detector (persistence 3) side by
side.
</details>

**Recall.** The naive rule "page if p95 > 300ms" fires on most normal days here.
Why, and what's the failure mode of shipping it anyway?

<details><summary>▸ Answer</summary>

Because normal p95 is already ~400ms, so the static threshold is below the healthy
baseline, so it fires constantly. Ship it and you get alert fatigue: the team mutes
it, and the muted alert is precisely the one that misses the real outage. A z-score
against the actual baseline is "weird relative to normal," which no magic constant
can be.
</details>

---

## Section 8: Mining traffic

**Predict, then run.** `examples/06_mining_traffic.py` clusters failing questions.
Predict the single largest cluster before running. Also: what fraction of failures
had a thumbs-down attached?

<details><summary>▸ Answer</summary>

The largest cluster is the mobile app ("app", ~900 failures), the input-drift topic
the KB can't answer, seen from the other side. And the vast majority of failures had
*no* feedback at all (≈900 of ~1100). Thumbs are sparse, so you cannot wait for a
thumbs-down to find failures. You mine proxies (refusals, terse answers) instead.
</details>

**Recall.** How does this section close the loop with the Evals dive?

<details><summary>▸ Answer</summary>

The mined failures become candidate eval cases in the exact JSONL shape the Evals
dive uses. A human writes the gold answer, they drop into the regression suite, and
now that failure can never silently return. Production → Observability → Evals →
Production: the feedback flywheel.
</details>

---

## Section 9: The classic-MLOps sidebar

**Recall.** A vendor pitches you "explainability for your LLM support bot,"
promising SHAP-style attributions. Why be skeptical?

<details><summary>▸ Answer</summary>

SHAP/LIME explain a *tabular* model's prediction in terms of its input features, and an
LLM has no fixed feature vector, just free text. LLM interpretability (probing,
attention, mechanistic interp) is a research field, not a production practice, and
attention weights are not feature attributions. The genuinely useful "why did it say
that?" handles for an LLM app are its *citations* and the retrieved context (the RAG
dive), not a borrowed tabular technique.
</details>

---

## Section 10: The capstone

**Predict, then run.** Run `python hands_on/watch.py`, then
`python hands_on/watch.py --healthy`. Predict what the detection report shows in
each case, especially for the "latency regression" guardrail.

<details><summary>▸ Answer</summary>

On the default history: all four incidents CAUGHT (with a few days' detection lag),
and the latency *regression* detector "correctly silent": it didn't mistake the
one-day spike for a trend. On `--healthy`: no alerts at all. Catching real incidents
while staying quiet on a clean history is the whole game, and it's the same detector
tuning in both runs.
</details>

**Change it.** Lower every detector's `z_threshold` (in `hands_on/watch.py`'s
`DETECTORS`) from 3 to 1.5 and rerun `--healthy`. Predict what happens.

<details><summary>▸ Answer</summary>

You start getting alerts on the *clean* history, false alarms, because a 1.5σ bar
is crossed by normal daily noise. That's the tradeoff made tangible: you bought
faster detection on real incidents at the cost of paging on nothing. There's no
threshold that gives you both; you choose where on the curve to sit.
</details>

---

## Going further: Segmentation

**Predict, then run.** `examples/08_segmentation.py` slows down one cohort
(enterprise, ~15% of traffic) starting day 21. Before running, predict: does the
*global* p95 latency detector fire? Does the *enterprise* one?

<details><summary>▸ Answer</summary>

The global detector stays silent. The enterprise slowdown, diluted across the 85%
of traffic that's fine, keeps the overall p95 inside its normal noise band (it never
reaches a persistent 3σ). The enterprise cohort's *own* p95 roughly triples and
alerts loudly. Same metric, same detector; the only difference is what you grouped
by. That gap is the whole point: an aggregate dashboard is necessary but not
sufficient.
</details>

**Recall.** Why not just slice by *every* field you log (tenant × region × plan ×
prompt version) and monitor all of them?

<details><summary>▸ Answer</summary>

Two costs. First, noise: each extra split shrinks the sample per series, so small
cohorts get noisy metrics that either miss real problems or cry wolf (which is why
the example loosens persistence for per-segment detection). Second, sheer volume:
the cross-product is thousands of series nobody can watch or alert on sanely. So you
slice on the few dimensions that actually carry *different risk*, the ones where
one group's experience really can diverge from another's, not every field available.
</details>

---

**Done?** You've operated six weeks of one app's traffic, caught its drifts,
graded its answers, and turned its failures into tests. The "Where to go next"
section of the README maps each from-scratch layer here to its industrial
counterpart (OpenTelemetry, Langfuse, Arize, Evidently, PagerDuty), same
interfaces, bigger machinery.
