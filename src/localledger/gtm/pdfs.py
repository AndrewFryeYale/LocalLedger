"""PDFs from the same templates as the site and newsletter: the fund one-pager and the sample issue."""

from __future__ import annotations

import io
from collections import Counter
from pathlib import Path

from sqlmodel import Session, func, select

from localledger.config import get_settings
from localledger.models import Company, NewsletterIssue, Note, Signal, Source


def _to_pdf(html: str, path: Path) -> Path:
    from xhtml2pdf import pisa

    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    if status.err:
        raise RuntimeError(f"PDF render failed for {path.name}")
    path.write_bytes(buf.getvalue())
    return path


def one_pager_context(session: Session) -> dict:
    rows = session.exec(
        select(Signal, Company, Note)
        .join(Company, Company.ticker == Signal.ticker)
        .join(Note, Note.signal_id == Signal.id)
        .where(Note.factcheck_passed == True)  # noqa: E712
        .order_by(Signal.rank_score.desc())
    ).all()
    seen, signals = set(), []
    for s, c, _ in rows:
        if s.ticker not in seen:
            seen.add(s.ticker)
            signals.append((s, c))
        if len(signals) >= 6:
            break
    companies = session.exec(select(Company)).all()
    return {
        "stats": {
            "companies": len(companies),
            "uncovered": sum(1 for c in companies if c.analyst_coverage == "0 (checked)"),
            "signals": session.exec(select(func.count()).select_from(Signal)).one(),
            "sources": session.exec(
                select(func.count()).select_from(Source).where(Source.status.in_(["available", "degraded"]))
            ).one(),
        },
        "signals": signals,
        "archetypes": Counter(c.archetype for c in companies).most_common(),
    }


def render_one_pager_html(session: Session) -> str:
    from localledger.web.deps import TEMPLATES

    return TEMPLATES.get_template("pdf/one_pager.html").render(**one_pager_context(session))


def build_pdfs(session: Session) -> list[Path]:
    out = get_settings().out_dir / "pdf"
    paths = [_to_pdf(render_one_pager_html(session), out / "fund_one_pager.pdf")]
    issue = session.exec(select(NewsletterIssue).order_by(NewsletterIssue.created_at.desc())).first()
    if issue and Path(issue.path_html).exists():
        paths.append(_to_pdf(Path(issue.path_html).read_text(), out / "sample_issue.pdf"))
    return paths
