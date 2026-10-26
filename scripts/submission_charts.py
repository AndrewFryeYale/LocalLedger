"""Inline SVG charts for report.html and slides.html, drawn from the bundled demo results.

Each chart is a function returning an <svg> string; build_submission.py swaps <!--chart:name--> for it.
Colors were checked with the dataviz palette validator (green/blue pass the colorblind checks; the gray
comparison bars and the diverging red always carry direct value labels). Hovering a bar shows its value.
"""

import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "outputs"

INK, BODY, MUTED, GRID = "#14213d", "#1f2933", "#5b6876", "#e3e7ed"
GREEN, BLUE, GRAY, DOWN = "#1f7a55", "#4a7fd0", "#a9b2bd", "#b8442c"
FONT = 'font-family="Helvetica Neue, Helvetica, Arial, sans-serif"'


def _svg(w: int, h: int, body: str, label: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{escape(label)}" {FONT}>'
        f"{body}</svg>"
    )


def _text(x, y, s, size=13, fill=BODY, anchor="start", weight="400") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{escape(str(s))}</text>'
    )


def _bar(x, y, w, h, fill, tip, round_end="right") -> str:
    """A bar with a 4px rounded data end; square at the baseline."""
    r = min(4, abs(w) / 2, h / 2)
    if w <= 0:
        return ""
    if round_end == "right":
        d = f"M{x},{y} h{w - r} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r} a{r},{r} 0 0 1 {-r},{r} h{-(w - r)} z"
    else:  # rounded on the left (negative bars grow left from the baseline)
        d = f"M{x + w},{y} h{-(w - r)} a{r},{r} 0 0 0 {-r},{r} v{h - 2 * r} a{r},{r} 0 0 0 {r},{r} h{w - r} z"
    return f'<path d="{d}" fill="{fill}"><title>{escape(tip)}</title></path>'


def _results() -> dict:
    return json.loads((OUT / "backtest" / "results.json").read_text())


def _tokenomics() -> dict:
    return json.loads((OUT / "tokenomics" / "tokenomics.json").read_text())


def event_moves() -> str:
    """Median 20-day return vs IWM after each flagged event type (n >= 6 measured)."""
    summary = _results()["events"]["summary"]
    rows = [(k, v["median_ar20"], v["n_ar20"]) for k, v in summary.items() if not k.startswith("_") and v["n_ar20"] >= 6]
    rows.sort(key=lambda r: r[1])
    allr = summary["_all_risk"]
    rows.append(("All EDGAR risk events", allr["median_ar20"], allr["n_ar20"]))
    w, label_w, row_h = 760, 250, 30
    h = 30 + row_h * len(rows) + 26
    lo, hi = -0.14, 0.06
    px = lambda v: label_w + (v - lo) / (hi - lo) * (w - label_w - 70)  # noqa: E731
    zero = px(0)
    out = []
    for t in (-0.10, -0.05, 0.05):
        out.append(f'<line x1="{px(t):.1f}" x2="{px(t):.1f}" y1="22" y2="{h - 24}" stroke="{GRID}"/>')
        out.append(_text(px(t), h - 8, f"{t:+.0%}", 11, MUTED, "middle"))
    out.append(_text(zero, h - 8, "0%", 11, MUTED, "middle"))
    for i, (name, v, n) in enumerate(rows):
        y = 26 + i * row_h
        bold = name.startswith("All")
        if bold:
            out.append(f'<line x1="0" x2="{w}" y1="{y - 4}" y2="{y - 4}" stroke="{INK}" stroke-width="1"/>')
        out.append(_text(label_w - 10, y + 15, f"{name} (n={n})", 12.5, INK if bold else BODY, "end", "700" if bold else "400"))
        color = DOWN if v < 0 else BLUE
        bw = abs(px(v) - zero)
        x = zero - bw if v < 0 else zero
        out.append(_bar(x, y + 3, bw, 18, color, f"{name}: {v:+.1%} median, n={n}", "left" if v < 0 else "right"))
        tx = x - 6 if v < 0 else x + bw + 6
        out.append(_text(tx, y + 16, f"{v:+.1%}", 12.5, INK, "end" if v < 0 else "start", "700"))
    out.append(f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="20" y2="{h - 22}" stroke="{MUTED}" stroke-width="1.5"/>')
    out.append(_text(label_w, 13, "Median 20-trading-day return relative to IWM, from the close before the filing", 11.5, MUTED))
    return _svg(w, h, "".join(out), "Median 20-day return after each type of flagged event")


def agenda_lead() -> str:
    """Days from a town agenda item to the first SEC filing naming the same project (one dot per item)."""
    events = _results()["events"]["events"]
    lags = sorted(
        e["sec_mention_lag_days"] for e in events if e["kind"].startswith("Local agenda") and e.get("sec_mention_lag_days") is not None
    )
    med = _results()["events"]["summary"]["_agenda_lead"]["median_days_before_sec_mention"]
    w, h, left, right = 760, 190, 30, 30
    top, base = 28, 130
    span = 120
    px = lambda d: left + d / span * (w - left - right)  # noqa: E731
    out = [f'<line x1="{left}" x2="{w - right}" y1="{base}" y2="{base}" stroke="{MUTED}"/>']
    for d in range(0, span + 1, 15):
        out.append(f'<line x1="{px(d):.1f}" x2="{px(d):.1f}" y1="{base}" y2="{base + 5}" stroke="{MUTED}"/>')
        out.append(_text(px(d), base + 20, d, 11, MUTED, "middle"))
    out.append(_text((w) / 2, base + 40, "days from the town meeting to the company's first SEC filing that names the project", 11.5, MUTED, "middle"))
    # stack dots into 5-day bins
    bins: dict[int, int] = {}
    for d in lags:
        b = int(d // 5) * 5
        k = bins.get(b, 0)
        bins[b] = k + 1
        cx, cy = px(b + 2.5), base - 9 - k * 15
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="{GREEN}" stroke="#fff" stroke-width="2"><title>{d} days</title></circle>')
    out.append(f'<line x1="{px(med):.1f}" x2="{px(med):.1f}" y1="{top}" y2="{base}" stroke="{INK}" stroke-dasharray="4 3"/>')
    out.append(_text(px(med) + 6, top + 10, f"median {med} days", 12.5, INK, "start", "700"))
    out.append(_text(left, 14, f"{len(lags)} agenda items later named in an SEC filing (one dot each)", 11.5, MUTED))
    return _svg(w, h, "".join(out), f"Agenda items arrive a median {med} days before the SEC filing")


def forecast_error() -> str:
    s = _results()["forecast"]["summary"]
    rows = [
        ("Local Ledger model", s["median_ape_model"], GREEN),
        ("Last reported quarter", s["median_ape_last_quarter"], GRAY),
        ("Same quarter last year", s["median_ape_seasonal_naive"], GRAY),
    ]
    w, label_w, row_h = 760, 190, 34
    h = 30 + row_h * len(rows) + 6
    px = lambda v: v / 0.13 * (w - label_w - 70)  # noqa: E731
    out = [_text(label_w, 13, f"Median absolute revenue forecast error, {s['company_quarters']} company-quarters, point in time (lower is better)", 11.5, MUTED)]
    for i, (name, v, c) in enumerate(rows):
        y = 24 + i * row_h
        out.append(_text(label_w - 10, y + 17, name, 13, INK if c == GREEN else BODY, "end", "700" if c == GREEN else "400"))
        out.append(_bar(label_w, y + 4, px(v), 20, c, f"{name}: {v:.1%}"))
        out.append(_text(label_w + px(v) + 6, y + 19, f"{v:.1%}", 13, INK, "start", "700"))
    out.append(f'<line x1="{label_w}" x2="{label_w}" y1="22" y2="{h - 4}" stroke="{MUTED}"/>')
    return _svg(w, h, "".join(out), "Forecast error: our model against two naive forecasts")


def tokens_by_effort() -> str:
    """Output tokens per benchmark workload, split into hidden reasoning and visible output, by effort setting."""
    runs = _tokenomics()["bench_runs"]

    def mean(labels, key):
        vals = [runs[x]["totals"][key] for x in labels if x in runs]
        return sum(vals) / len(vals)

    groups = [
        ("Default effort", ["a_baseline", "a_baseline_r2"], "8/8 notes pass"),
        ('"low" everywhere', ["b_low"], "7/8 pass"),
        ('"none" everywhere', ["c_none"], "6/8 pass, rejected"),
        ("Tuned per agent (shipped)", ["d_tuned", "d_tuned_r2"], "7/8 pass"),
    ]
    w, label_w, row_h = 760, 200, 40
    h = 46 + row_h * len(groups)
    px = lambda v: v / 46000 * (w - label_w - 150)  # noqa: E731
    out = [
        f'<rect x="{label_w}" y="6" width="12" height="12" rx="2" fill="{BLUE}"/>',
        _text(label_w + 18, 16, "hidden reasoning", 12, BODY),
        f'<rect x="{label_w + 140}" y="6" width="12" height="12" rx="2" fill="{GREEN}"/>',
        _text(label_w + 158, 16, "visible output", 12, BODY),
        _text(w, 16, "output tokens per workload (mean of runs)", 11.5, MUTED, "end"),
    ]
    for i, (name, labels, note) in enumerate(groups):
        y = 32 + i * row_h
        reasoning = mean(labels, "reasoning_tokens")
        visible = mean(labels, "output_tokens") - reasoning
        cost = mean(labels, "cost_usd")
        shipped = "shipped" in name
        out.append(_text(label_w - 10, y + 15, name, 13, INK, "end", "700" if shipped else "400"))
        out.append(_text(label_w - 10, y + 30, note, 11, MUTED, "end"))
        rw, vw = px(reasoning), px(visible)
        if rw > 0:
            out.append(f'<rect x="{label_w}" y="{y + 4}" width="{max(rw - 2, 0):.1f}" height="20" fill="{BLUE}"><title>{name}: {reasoning:,.0f} reasoning tokens</title></rect>')
        out.append(_bar(label_w + rw, y + 4, vw, 20, GREEN, f"{name}: {visible:,.0f} visible output tokens"))
        out.append(_text(label_w + rw + vw + 8, y + 19, f"{reasoning + visible:,.0f} tok · ${cost:.4f}", 12.5, INK, "start", "700"))
    return _svg(w, h, "".join(out), "Output tokens per workload by reasoning effort")


def warroom_cost() -> str:
    """Where one live war room's $0.0075 went (war room #4, measured per call)."""
    rows = [
        ("Reader", 8650, 1505), ("Model", 2725, 1130), ("Thesis", 1834, 1011), ("Read-across", 2026, 308),
        ("Bull", 2949, 245), ("Bear", 2949, 233), ("Moderator", 2037, 990), ("Note writer", 11234, 1456),
        ("Fact-check", 3271, 534),
    ]
    p = _tokenomics()["prices_per_mtok"]
    cost = [(n, (i * p["input"] + o * p["output"]) / 1e6, i, o) for n, i, o in rows]
    total = sum(c for _, c, _, _ in cost)
    w, label_w, row_h = 760, 120, 24
    h = 28 + row_h * len(cost) + 4
    px = lambda v: v / 0.0021 * (w - label_w - 190)  # noqa: E731
    out = [_text(label_w, 13, f"Cost per agent, one live PCYO war room: ${total:.4f} total, 43 seconds", 11.5, MUTED)]
    for i, (name, c, ti, to) in enumerate(cost):
        y = 22 + i * row_h
        out.append(_text(label_w - 10, y + 14, name, 12.5, BODY, "end"))
        out.append(_bar(label_w, y + 3, px(c), 15, GREEN, f"{name}: ${c:.4f} ({ti:,} in / {to:,} out)"))
        out.append(_text(label_w + px(c) + 6, y + 15, f"${c:.4f} · {ti:,} in / {to:,} out", 11.5, MUTED))
    return _svg(w, h, "".join(out), "War room cost by agent")


def reading_hours() -> str:
    """Hours to read every document vs hours to read Local Ledger's flagged notes."""
    wl = _results()["workload"]
    full = wl["reading_hours"]
    flagged = wl["flagged"]
    notes_h = flagged * 2 / 60
    rows = [
        (f"Read all {wl['documents_total']:,} documents", full, GRAY, f"{full:,.0f} hours"),
        (f"Read the {flagged} flagged notes", notes_h, GREEN, f"{notes_h:.1f} hours"),
    ]
    w, label_w, row_h = 760, 230, 40
    h = 26 + row_h * 2 + 22
    px = lambda v: v / full * (w - label_w - 110)  # noqa: E731
    out = [_text(label_w, 13, "Hours to keep up with 237 microcaps for 12 months", 11.5, MUTED)]
    for i, (name, v, c, lab) in enumerate(rows):
        y = 22 + i * row_h
        out.append(_text(label_w - 10, y + 20, name, 13, INK, "end", "700" if c == GREEN else "400"))
        out.append(_bar(label_w, y + 6, max(px(v), 4), 22, c, f"{name}: {lab}"))
        out.append(_text(label_w + max(px(v), 4) + 8, y + 22, lab, 13, INK, "start", "700"))
    out.append(_text(label_w, h - 6, "Reading times: 5 min per 8-K, 10 per agenda or call report, 30 per 10-Q, 60 per 10-K; 2 min per note.", 11, MUTED))
    return _svg(w, h, "".join(out), "Reading hours: everything vs flagged notes")


CHARTS = {
    "event_moves": event_moves,
    "agenda_lead": agenda_lead,
    "forecast_error": forecast_error,
    "tokens_by_effort": tokens_by_effort,
    "warroom_cost": warroom_cost,
    "reading_hours": reading_hours,
}
