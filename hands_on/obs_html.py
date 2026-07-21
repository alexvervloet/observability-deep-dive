"""
obs_html.py: render the capstone dashboard as a self-contained HTML file.

This is presentation, not teaching; the real capstone is the terminal dashboard
in watch.py and the detectors it drives. But "monitoring" clicks when you *see*
the lines move, so `--html` writes a standalone page: one small line chart per
metric, with the baseline band and the days that alerted marked on it, plus a
status tile row and the incident timeline.

No dependencies, no external assets: the whole thing is inline CSS + inline SVG,
so the file opens offline anywhere. Colors come from the data-viz skill's
validated default palette; each chart is a single series (so no legend is needed)
and status is shown with an icon + label, never color alone. The page is
theme-aware: it follows the browser's light/dark preference.
"""

from __future__ import annotations

import html

from obs import alerts

# --- validated palette (data-viz skill default) -----------------------------
_LINE = "#2a78d6"
_STATUS = {"ok": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
_ICON = {"ok": "✓", "warning": "▲", "serious": "▲", "critical": "●"}


def _status(z: float) -> str:
    az = abs(z)
    return "critical" if az >= 4 else "serious" if az >= 3 else "warning" if az >= 2 else "ok"


def _fmt(metric: str, v):
    if v is None:
        return "n/a"
    if metric == "cost_per_request_usd":
        return f"${v * 1e6:.1f}µ"
    if metric in ("refusal_rate", "judge_score", "embed_drift"):
        return f"{v:.3f}"
    return f"{v:.0f}"


def _chart(rows, metric, title, direction, alert_days):
    """One responsive SVG line chart: baseline band, series line, alert dots."""
    pts = [(r["day"], r[metric]) for r in rows if r.get(metric) is not None]
    if not pts:
        return ""
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    lo -= span * 0.1
    hi += span * 0.1
    span = hi - lo
    W, H, pad = 720, 150, 8
    n = len(pts)

    def x(i):
        return pad + i * (W - 2 * pad) / max(1, n - 1)

    def y(v):
        return H - pad - (v - lo) / span * (H - 2 * pad)

    # Baseline band = mean ± sd of the first 7 points.
    base_vals = vals[:7]
    mean, sd = alerts.baseline_stats(base_vals)
    band = f'<rect x="{pad}" y="{y(mean + sd):.1f}" width="{W - 2 * pad}" ' \
           f'height="{abs(y(mean - sd) - y(mean + sd)):.1f}" fill="var(--muted)" opacity="0.14"/>'
    baseline_line = f'<line x1="{pad}" y1="{y(mean):.1f}" x2="{W - pad}" y2="{y(mean):.1f}" ' \
                    f'stroke="var(--baseline)" stroke-dasharray="3 3" stroke-width="1"/>'
    line = "M" + " L".join(f"{x(i):.1f} {y(v):.1f}" for i, (_, v) in enumerate(pts))
    dots = ""
    for i, (day, v) in enumerate(pts):
        if day in alert_days:
            st = alert_days[day]
            dots += f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4.5" fill="{_STATUS[st]}" ' \
                    f'stroke="var(--surface)" stroke-width="2"><title>{html.escape(day)}: alert</title></circle>'
    last = pts[-1]
    return f"""
    <figure class="panel">
      <figcaption>{html.escape(title)}
        <span class="last">{_fmt(metric, last[1])}</span></figcaption>
      <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img"
           aria-label="{html.escape(title)} over {n} days">
        {band}{baseline_line}
        <path d="{line}" fill="none" stroke="{_LINE}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>
        {dots}
      </svg>
      <div class="axis"><span>{html.escape(pts[0][0])}</span><span>{html.escape(pts[-1][0])}</span></div>
    </figure>"""


def render(rows, fired, incidents, panels):
    days = [r["day"] for r in rows]
    total_req = sum(r["requests"] for r in rows)
    total_cost = sum(r["cost_usd_total"] for r in rows)

    # status tiles + charts
    tiles, charts = [], []
    # Build per-metric alert-day maps from the fired alerts' metric field.
    alert_days_by_metric: dict[str, dict] = {}
    for a in fired:
        m = a["metric"]
        alert_days_by_metric.setdefault(m, {})[a["fired_on"]] = _status(a["z"])

    for metric, title, direction in panels:
        det = alerts.Detector(metric, direction)
        z = alerts.signed_z(rows, det)
        cur_z = z[-1]["z"] if z else 0.0
        st = _status(cur_z)
        now = z[-1]["value"] if z else None
        tiles.append(f"""
        <div class="tile">
          <div class="tile-title">{html.escape(title)}</div>
          <div class="tile-value">{_fmt(metric, now)}</div>
          <div class="tile-status" style="color:{_STATUS[st]}">{_ICON[st]} {st} · z={cur_z:.1f}</div>
        </div>""")
        charts.append(_chart(rows, metric, title, direction, alert_days_by_metric.get(metric, {})))

    # incident timeline
    day_index = {d: i for i, d in enumerate(days)}
    rows_html = []
    for inc in incidents:
        matches = [a for a in fired if a.get("kind") == inc.kind]
        if matches:
            first = min(matches, key=lambda a: a["fired_on"])
            lag = day_index[first["fired_on"]] - inc.start_day
            verdict = f'<span style="color:{_STATUS["ok"]}">caught · {lag}d lag</span>'
        else:
            verdict = f'<span style="color:{_STATUS["critical"]}">missed</span>'
        rows_html.append(
            f"<tr><td>{html.escape(inc.kind)}</td><td>day {inc.start_day}</td>"
            f"<td>{html.escape(inc.description)}</td><td>{verdict}</td></tr>")
    timeline = "".join(rows_html) or '<tr><td colspan="4">no incidents injected: a healthy history</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Observability dashboard: Acme Cloud support</title>
<style>
  :root {{
    --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
    --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
      --muted:#898781; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,.10);
    }}
  }}
  :root[data-theme="light"] {{ --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,.10); }}
  :root[data-theme="dark"]  {{ --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,.10); }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--plane); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.5; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:24px 16px 64px; }}
  h1 {{ font-size:1.25rem; margin:0 0 2px; }}
  .sub {{ color:var(--ink2); font-size:.85rem; margin-bottom:20px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:24px; }}
  .tile {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }}
  .tile-title {{ color:var(--ink2); font-size:.78rem; }}
  .tile-value {{ font-size:1.5rem; font-weight:600; margin:2px 0; font-variant-numeric:tabular-nums; }}
  .tile-status {{ font-size:.78rem; font-weight:600; }}
  .panel {{ background:var(--surface); border:1px solid var(--border); border-radius:10px;
    padding:12px 14px; margin:0 0 14px; }}
  figcaption {{ font-size:.85rem; color:var(--ink2); display:flex; justify-content:space-between; margin-bottom:6px; }}
  .last {{ color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }}
  svg {{ width:100%; height:auto; display:block; }}
  .axis {{ display:flex; justify-content:space-between; color:var(--muted); font-size:.7rem; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; font-size:.85rem; margin-top:8px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; }}
  h2 {{ font-size:1rem; margin:28px 0 8px; }}
  .legend {{ color:var(--muted); font-size:.75rem; margin-top:6px; }}
</style></head>
<body><div class="wrap">
  <h1>Observability dashboard: Acme Cloud support</h1>
  <div class="sub">{days[0]} → {days[-1]} · {len(days)} days · {total_req:,} requests · ${total_cost:.4f} spend
    · baseline = first 7 days · dashed line = baseline mean, band = ±1σ, dots = alerts</div>
  <div class="tiles">{''.join(tiles)}</div>
  {''.join(charts)}
  <h2>Detection report</h2>
  <table><thead><tr><th>incident</th><th>started</th><th>what happened</th><th>detector verdict</th></tr></thead>
  <tbody>{timeline}</tbody></table>
  <div class="legend">Generated by hands_on/watch.py. All synthetic, all offline.</div>
</div></body></html>"""
