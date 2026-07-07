"""The from-scratch observability stack for the deep dive.

Each module is small and teaches one idea:

    simulate.py   — the traffic generator: weeks of synthetic request logs, with
                    incidents (drift, cost creep, latency spikes) injected on cue
    logs.py       — the LogRecord shape + JSONL load/save (what a real system emits)
    metrics.py    — operational metrics from a batch of records (latency, cost, rates)
    drift.py      — input drift (topic + embedding) and output/quality drift signals
    judge.py      — score a *sample* of answers (mock rule-based, or a real LLM)
    alerts.py     — turn a metric time series into alerts, without paging on noise
    mining.py     — surface real failures from traffic as new eval cases (the flywheel)
    providers.py  — the ONLY provider-specific seam (optional real judge + embeddings)
"""
