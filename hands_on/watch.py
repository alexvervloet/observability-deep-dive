#!/usr/bin/env python3
"""
watch.py — the capstone: monitor six weeks of traffic and catch the incidents.
==============================================================================

Everything in the repo comes together here. `watch.py` ingests the whole
generated log history, computes the daily metric series, runs the tuned detector
suite (latency spike + regression, input drift, cost creep, quality drop), and
prints an operations dashboard: current health vs baseline, a sparkline per
metric, an incident timeline of what fired when, and — the honest payoff — a
**detection report** that grades the detectors against the ground-truth incidents
the simulator buried in the history: did we catch each one, and how many days
late?

It runs fully offline on the mock provider. Flip PROVIDER (mock | openai | claude)
in .env to score the sampled quality slice with a real LLM-as-judge; everything
else is pure log analysis and never touches a model.

Examples
--------
  # The default 42-day history, dashboard to the terminal
  python hands_on/watch.py

  # A longer/quieter history, bigger judge sample
  python hands_on/watch.py --days 60 --per-day 30

  # A clean history with NO injected incidents (detectors should stay silent)
  python hands_on/watch.py --healthy

  # Also write a self-contained HTML dashboard you can open in a browser
  python hands_on/watch.py --html report.html
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for the HTML helper

from dotenv import load_dotenv

from obs import alerts, drift, judge, metrics, mining, providers, simulate
from obs.logs import by_day

_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
_BLOCKS = "▁▂▃▄▅▆▇█"

# label, the incident kind this detector is meant to catch (None = guardrail that
# should stay silent), and the detector config.
DETECTORS = [
    ("latency spike",      "latency_spike",      alerts.Detector("p95_latency_ms", "up", 4.0, 1, 7)),
    ("latency regression", None,                 alerts.Detector("p95_latency_ms", "up", 3.0, 3, 7)),
    ("input drift",        "input_drift",        alerts.Detector("refusal_rate", "up", 3.0, 3, 7)),
    ("input drift (embed)", "input_drift",       alerts.Detector("embed_drift", "up", 3.0, 3, 7)),
    ("cost creep",         "cost_creep",         alerts.Detector("cost_per_request_usd", "up", 3.0, 3, 7)),
    ("quality drop",       "quality_regression", alerts.Detector("judge_score", "down", 3.0, 3, 7)),
]
# The metrics we draw as sparklines / charts, with display direction of "bad".
PANELS = [
    ("p95_latency_ms", "p95 latency (ms)", "up"),
    ("refusal_rate", "refusal rate", "up"),
    ("cost_per_request_usd", "cost / request ($)", "up"),
    ("embed_drift", "input embedding drift", "up"),
    ("judge_score", "sampled judge score", "down"),
]


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def build_rows(records, per_day, baseline_days):
    """The daily metric series, enriched with the model-backed signals."""
    rows = metrics.daily(records)
    drift_by_day = {r["day"]: r["drift"] for r in drift.embedding_drift_by_day(records, baseline_days)}
    judge_by_day = {j["day"]: j for j in judge.judge_by_day(records, per_day=per_day)}
    days = by_day(records)
    vocab = drift.baseline_vocabulary([r for d in list(days)[:baseline_days] for r in days[d]])
    for row in rows:
        d = row["day"]
        row["embed_drift"] = drift_by_day.get(d, 0.0)
        row["judge_score"] = judge_by_day[d]["judge_score"]
        row["judge_margin"] = judge_by_day[d]["margin"]
        row["novel_term_rate"] = drift.novel_term_rate(days[d], vocab)
    return rows


def sparkline(values):
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(_BLOCKS[min(7, int((v - lo) / span * 7))] for v in values)


def status_of(z, direction_ok=True):
    """Map a z-score to a status label + ANSI color + icon (never color alone)."""
    az = abs(z)
    if az >= 4:
        return "critical", "1;31", "●"
    if az >= 3:
        return "serious", "31", "▲"
    if az >= 2:
        return "warning", "33", "▲"
    return "ok", "32", "✓"


def run_alerts(rows):
    fired = []
    for label, kind, det in DETECTORS:
        for a in alerts.detect(rows, det):
            fired.append({**a, "label": label, "kind": kind})
    fired.sort(key=lambda a: a["fired_on"])
    return fired


def print_dashboard(records, rows, incidents, fired, baseline_days):
    days = [r["day"] for r in rows]
    total_cost = sum(r["cost_usd_total"] for r in rows)
    total_req = sum(r["requests"] for r in rows)
    print(_c("\n══ Acme Cloud support — observability dashboard ═══════════════════", "1"))
    print(f"provider={providers.describe()}")
    print(f"window: {days[0]} → {days[-1]}  ({len(days)} days)   "
          f"requests: {total_req:,}   spend: ${total_cost:.4f}   "
          f"baseline: first {baseline_days} days")

    # --- current health: last 3 days vs baseline, per metric ----------------
    print(_c("\nCURRENT HEALTH  (last 3 days vs baseline)", "1"))
    print(f"  {'metric':<24}{'baseline':>12}{'now':>12}{'z':>7}   status")
    print("  " + "-" * 63)
    for metric, title, direction in PANELS:
        det = alerts.Detector(metric, direction, baseline_days=baseline_days)
        z = alerts.signed_z(rows, det)
        if not z:
            continue
        base_vals = [zr["value"] for zr in z[:baseline_days]]
        base = sum(base_vals) / len(base_vals)
        now = sum(zr["value"] for zr in z[-3:]) / 3
        cur_z = z[-1]["z"]
        _, color, icon = status_of(cur_z)
        print(f"  {title:<24}{_fmt(metric, base):>12}{_fmt(metric, now):>12}"
              f"{cur_z:>7.1f}   {_c(icon + ' ' + status_of(cur_z)[0], color)}")

    # --- sparklines ---------------------------------------------------------
    print(_c("\nTRENDS  (each spark spans the full window, left=oldest)", "1"))
    for metric, title, _ in PANELS:
        vals = metrics.series(rows, metric)
        if vals:
            print(f"  {title:<24} {sparkline(vals)}  {_fmt(metric, vals[-1])}")

    # --- incident timeline --------------------------------------------------
    print(_c("\nINCIDENT TIMELINE  (alerts fired)", "1"))
    if not fired:
        print("  (no alerts — all clear)")
    for a in fired:
        _, color, icon = status_of(a["z"])
        print(f"  {_c(icon, color)} {a['fired_on']}  {a['label']:<20}"
              f"z={a['z']:>5.1f}  (breach began {a['breach_started']})")

    # --- detection report: grade detectors vs ground truth ------------------
    print(_c("\nDETECTION REPORT  (detectors vs the ground-truth incidents)", "1"))
    day_index = {d: i for i, d in enumerate(days)}
    for inc in incidents:
        matches = [a for a in fired if a["kind"] == inc.kind]
        if matches:
            first = min(matches, key=lambda a: a["fired_on"])
            lag = day_index[first["fired_on"]] - inc.start_day
            verdict = _c(f"CAUGHT (lag {lag}d via {first['label']})", "32")
        else:
            verdict = _c("MISSED", "1;31")
        print(f"  {inc.kind:<20} started day {inc.start_day:<3} → {verdict}")
    # The trend guardrail should have stayed silent on the transient spike.
    trend_fired = any(a["label"] == "latency regression" for a in fired)
    note = _c("correctly silent", "32") if not trend_fired else _c("FALSE ALARM", "1;31")
    print(f"  {'(latency regression)':<20} guardrail on the 1-day spike → {note}")

    # --- the flywheel --------------------------------------------------------
    fails = mining.failures(records)
    top = mining.cluster(fails, records, top=1)
    print(_c("\nTOP FAILURE CLUSTER TO FIX  (mine → eval → fix)", "1"))
    if top:
        print(f"  {top[0]['count']}× “{top[0]['term']}”  e.g. {top[0]['examples'][0]}")
        print("  → turn into eval cases with examples/06_mining_traffic.py")
    print()


def _fmt(metric, v):
    if v is None:
        return "  n/a"
    if metric == "cost_per_request_usd":
        return f"${v*1e6:.1f}µ"
    if metric in ("refusal_rate", "judge_score", "embed_drift"):
        return f"{v:.3f}"
    return f"{v:.0f}"


# ---------------------------------------------------------------------------
# Optional self-contained HTML dashboard
# ---------------------------------------------------------------------------

def write_html(rows, fired, incidents, path):
    from obs_html import render  # local helper kept out of the teaching path
    html = render(rows, fired, incidents, PANELS)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description="Monitor the support assistant's traffic history.")
    ap.add_argument("--days", type=int, default=42, help="length of the generated history")
    ap.add_argument("--seed", type=int, default=7, help="simulator seed (changes the exact history)")
    ap.add_argument("--per-day", type=int, default=20, help="judge sample size per day")
    ap.add_argument("--baseline-days", type=int, default=7, help="leading days that define 'normal'")
    ap.add_argument("--healthy", action="store_true", help="generate a clean history with no incidents")
    ap.add_argument("--html", metavar="PATH", help="also write a self-contained HTML dashboard")
    args = ap.parse_args()

    load_dotenv()
    providers.ensure_ready()  # no-op on the mock

    incidents = [] if args.healthy else None
    records, incidents = simulate.generate(args.days, seed=args.seed, incidents=incidents)
    rows = build_rows(records, args.per_day, args.baseline_days)
    for _, _, det in DETECTORS:
        det.baseline_days = args.baseline_days
    fired = run_alerts(rows)
    print_dashboard(records, rows, incidents, fired, args.baseline_days)

    if args.html:
        write_html(rows, fired, incidents, args.html)
        print(f"Wrote HTML dashboard → {args.html}  (open it in a browser)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
