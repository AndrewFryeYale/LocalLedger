"""Small server-rendered SVG charts (no JS, print-friendly, readable in screenshots)."""

from __future__ import annotations

from html import escape

from markupsafe import Markup

INK, MUTED, GRID, ACCENT, ACCENT_SOFT, RED = "#0f172a", "#64748b", "#e2e8f0", "#1f6f54", "#cfe5da", "#b42318"


def _m(v: float) -> str:
    return f"${v / 1e9:.1f}B" if abs(v) >= 1e9 else f"${v / 1e6:.1f}M" if abs(v) >= 1e6 else f"${v / 1e3:.0f}K"


def revenue_chart(model, width: int = 560, height: int = 190) -> Markup:
    """Reported quarterly revenue (bars) and the forecast P50 with its P10-P90 band."""
    hist = [h for h in (model.history or []) if h.get("revenue") is not None][-8:]
    fc = [q for q in (model.forecast or []) if q.status in ("unreported", "current", "next")]
    if not hist or not fc:
        return Markup("")
    pts = [("A", h["label"], h["revenue"], None, None) for h in hist] + [
        ("F", q.label, q.p50, q.p10, q.p90) for q in fc
    ]
    top = max(max(p[2] or 0, p[4] or 0) for p in pts) * 1.08 or 1
    left, right, tpad, bpad = 52, 8, 10, 40
    w, h = width - left - right, height - tpad - bpad
    step = w / len(pts)
    bw = step * 0.56

    def y(v: float) -> float:
        return tpad + h - (v / top) * h

    out = [
        f'<svg viewBox="0 0 {width} {height}" class="w-full h-auto" role="img" '
        f'aria-label="Quarterly revenue: reported and forecast" font-family="Inter, ui-sans-serif" font-size="10">'
    ]
    for frac in (0, 0.5, 1):
        gy = y(top * frac / 1.08)
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{GRID}"/>')
        out.append(
            f'<text x="{left - 6}" y="{gy + 3:.1f}" text-anchor="end" fill="{MUTED}">{_m(top * frac / 1.08)}</text>'
        )
    first_f = next(i for i, p in enumerate(pts) if p[0] == "F")
    sx = left + first_f * step
    out.append(f'<rect x="{sx:.1f}" y="{tpad}" width="{w - first_f * step:.1f}" height="{h}" fill="#f8fafc"/>')
    out.append(f'<text x="{sx + 4:.1f}" y="{tpad + 10}" fill="{MUTED}">forecast</text>')
    band, line = [], []
    for i, (kind, label, v, lo, hi) in enumerate(pts):
        cx = left + i * step + step / 2
        if kind == "A":
            out.append(
                f'<rect x="{cx - bw / 2:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{tpad + h - y(v):.1f}" '
                f'fill="{ACCENT}" rx="1.5"><title>{escape(label)}: {_m(v)} reported</title></rect>'
            )
        else:
            band.append((cx, lo, hi))
            line.append((cx, v))
            out.append(
                f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(hi):.1f}" y2="{y(lo):.1f}" stroke="{ACCENT}" '
                f'stroke-width="{bw:.1f}" stroke-opacity="0.18"><title>{escape(label)}: P10 {_m(lo)} · P50 {_m(v)} '
                f"· P90 {_m(hi)}</title></line>"
            )
            out.append(
                f'<rect x="{cx - bw / 2:.1f}" y="{y(v) - 1.5:.1f}" width="{bw:.1f}" height="3" fill="{ACCENT}"/>'
            )
        short = label.replace("Q ending ", "").replace(" 20", " '")
        out.append(f'<text x="{cx:.1f}" y="{height - 24}" text-anchor="middle" fill="{MUTED}">{escape(short)}</text>')
    out.append(
        f'<g transform="translate({left},{height - 2})" fill="{MUTED}"><rect width="8" height="8" y="-8" '
        f'fill="{ACCENT}"/><text x="12" y="-1">reported</text><rect x="70" width="14" height="8" y="-8" '
        f'fill="{ACCENT}" fill-opacity="0.18"/><text x="88" y="-1">P10-P90</text><rect x="140" y="-5.5" width="14" '
        f'height="3" fill="{ACCENT}"/><text x="158" y="-1">P50 forecast</text></g>'
    )
    out.append("</svg>")
    return Markup("".join(out))


def hbar_chart(items: list[tuple[str, float, str]], width: int = 560, fmt: str = "{:+.1%}") -> Markup:
    """Horizontal bars around zero. items = [(label, value, sublabel)]."""
    if not items:
        return Markup("")
    row, label_w, pad = 26, 230, 60
    height = row * len(items) + 8
    span = max(abs(v) for _, v, _ in items) or 1
    w = width - label_w - pad * 2
    zero = label_w + pad + (w / 2)
    out = [
        f'<svg viewBox="0 0 {width} {height}" class="w-full h-auto" role="img" font-family="Inter, ui-sans-serif" '
        f'font-size="11">',
        f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="0" y2="{height}" stroke="{GRID}"/>',
    ]
    for i, (label, v, sub) in enumerate(items):
        ty = i * row + 17
        bl = (abs(v) / span) * (w / 2)
        x = zero if v >= 0 else zero - bl
        color = ACCENT if v >= 0 else RED
        out.append(f'<text x="0" y="{ty}" fill="{INK}">{escape(label)}</text>')
        if sub:
            out.append(f'<text x="{label_w - 4}" y="{ty}" text-anchor="end" fill="{MUTED}">{escape(sub)}</text>')
        out.append(f'<rect x="{x:.1f}" y="{ty - 11}" width="{max(bl, 1):.1f}" height="14" rx="2" fill="{color}"/>')
        tx = zero + bl + 6 if v >= 0 else zero - bl - 6
        anchor = "start" if v >= 0 else "end"
        out.append(f'<text x="{tx:.1f}" y="{ty}" text-anchor="{anchor}" fill="{INK}">{fmt.format(v)}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def compare_bars(groups: list[tuple[str, list[tuple[str, float]]]], width: int = 560, fmt: str = "{:.1%}") -> Markup:
    """Grouped horizontal bars from zero (e.g. error by method). groups = [(group, [(series, value)])]."""
    colors = [ACCENT, "#94a3b8", "#cbd5e1", "#a16207"]
    rows = sum(len(s) for _, s in groups) + len(groups)
    row, label_w = 18, 170
    height = rows * row + 6
    top = max(v for _, s in groups for _, v in s) or 1
    w = width - label_w - 60
    out = [
        f'<svg viewBox="0 0 {width} {height}" class="w-full h-auto" font-family="Inter, ui-sans-serif" font-size="11">'
    ]
    yy = 4
    for g, series in groups:
        out.append(f'<text x="0" y="{yy + 12}" fill="{INK}" font-weight="600">{escape(g)}</text>')
        yy += row
        for i, (name, v) in enumerate(series):
            bl = v / top * w
            out.append(f'<text x="{label_w - 6}" y="{yy + 11}" text-anchor="end" fill="{MUTED}">{escape(name)}</text>')
            out.append(
                f'<rect x="{label_w}" y="{yy + 2}" width="{max(bl, 1):.1f}" height="12" rx="2" '
                f'fill="{colors[i % len(colors)]}"/>'
            )
            out.append(f'<text x="{label_w + bl + 6:.1f}" y="{yy + 12}" fill="{INK}">{fmt.format(v)}</text>')
            yy += row
    out.append("</svg>")
    return Markup("".join(out))
