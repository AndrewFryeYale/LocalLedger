"""Weekly issue builder: top 5-8 fact-checked signals, one deeper company note, a "new to the universe" item and a
methodology footer. Writes Markdown + email-safe HTML to ./out/newsletter/. Never sends anything.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import markdown as md
from sqlmodel import Session, select

from localledger.compliance import PublishBlocked, check_publish, disclosures, lint_impersonal
from localledger.config import get_settings
from localledger.http import read_document_text
from localledger.llm.client import LLM
from localledger.llm.mock import register_mock
from localledger.models import Company, Document, NewsletterIssue, Note, Signal, utcnow
from localledger.research.analyst import MIN_CLAIMS_TO_PASS, _verbatim_span, render_note_md, run_research
from localledger.research.factcheck import deterministic_check, fact_check, validate_claims
from localledger.research.retrieval import filing_passages, signal_excerpt
from localledger.signals.store import CitationError

log = logging.getLogger(__name__)
MAX_ITEMS, MIN_ITEMS, MAX_PER_TICKER, MAX_PER_SOURCE = 8, 5, 1, 3


@dataclass
class IssueResult:
    week: str
    path_md: Path
    path_html: Path
    items: int
    factcheck_passed: bool
    excluded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_week(week: str | None) -> tuple[str, date, date]:
    if not week:
        iso = date.today().isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
    m = re.fullmatch(r"(\d{4})-?W?(\d{1,2})", week.strip())
    if not m:
        raise ValueError("week must look like 2026-W42 or 2026-42")
    year, wk = int(m.group(1)), int(m.group(2))
    start = date.fromisocalendar(year, wk, 1)
    return f"{year}-W{wk:02d}", start, start + timedelta(days=6)


def _featured_before(session: Session, week: str) -> tuple[set[int], set[str]]:
    sig_ids: set[int] = set()
    tickers: set[str] = set()
    for issue in session.exec(select(NewsletterIssue).where(NewsletterIssue.week != week)).all():
        sig_ids |= {int(x) for x in issue.signal_ids.split(",") if x}
        meta = json.loads(issue.note_ids or "{}") if issue.note_ids.startswith("{") else {}
        tickers |= set(meta.get("new_to_universe", []))
    return sig_ids, tickers


def select_items(session: Session, exclude_ids: set[int]) -> list[tuple[Signal, Note, Company]]:
    rows = session.exec(
        select(Signal, Note, Company)
        .join(Note, Note.signal_id == Signal.id)
        .join(Company, Company.ticker == Signal.ticker)
        .where(Note.factcheck_passed == True, Note.kind == "signal")  # noqa: E712
        .order_by(Signal.rank_score.desc())
    ).all()
    per_ticker: dict[str, int] = defaultdict(int)
    per_source: dict[str, int] = defaultdict(int)
    out = []
    for sig, note, company in rows:
        if (
            sig.id in exclude_ids
            or per_ticker[sig.ticker] >= MAX_PER_TICKER
            or per_source[sig.source_id] >= MAX_PER_SOURCE
        ):
            continue
        per_ticker[sig.ticker] += 1
        per_source[sig.source_id] += 1
        out.append((sig, note, company))
        if len(out) >= MAX_ITEMS:
            break
    return out


def deep_note(session: Session, company: Company, signals: list[Signal], llm: LLM) -> Note | None:
    settings = get_settings()
    docs, corpora, computed = [], {}, []
    for s in signals[:4]:
        doc = session.get(Document, s.document_id)
        if doc.id not in corpora:
            excerpt = signal_excerpt(session, s, doc)
            docs.append({"document_id": doc.id, "url": s.citation_url, "title": doc.title, "text": excerpt})
            corpora[doc.id] = read_document_text(doc) + "\n" + excerpt
        computed += [v for v in (s.value, s.prior_value, s.delta) if v is not None]
        pct = json.loads(s.detail_json or "{}").get("pct_change")
        if isinstance(pct, (int, float)):
            computed.append(pct)
    query = f"{company.local_gov_driver} {company.note or ''} " + " ".join(s.summary for s in signals[:4])
    for p in filing_passages(session, company.ticker, query, k=4):
        if p["document_id"] not in corpora:
            corpora[p["document_id"]] = read_document_text(session.get(Document, p["document_id"]))
        docs.append(p)
    if not docs:
        return None
    variables = {
        "brand": settings.brand,
        "company": company.model_dump(),
        "documents": docs,
        "signals": [
            {
                "metric": s.metric,
                "as_of": str(s.as_of),
                "summary": s.summary,
                "value": s.value,
                "prior_value": s.prior_value,
                "delta": s.delta,
                "document_id": s.document_id,
            }
            for s in signals[:4]
        ],
    }
    res = llm.json("company_deep_note", variables)
    claims = res.data.get("claims") or []
    try:
        validate_claims(claims, {d["document_id"]: d["url"] for d in docs})
    except CitationError as exc:
        log.warning("deep note for %s failed citation enforcement: %s", company.ticker, exc)
        return None
    kept, dropped = fact_check(claims, corpora, computed, llm, settings.brand)
    note = Note(
        kind="company",
        ticker=company.ticker,
        title=str(res.data.get("headline") or company.name)[:200],
        body_md=render_note_md(kept, docs) if kept else "(all claims rejected by fact-check)",
        claims_json=json.dumps(kept),
        factcheck_log=json.dumps(dropped),
        factcheck_passed=len(kept) >= MIN_CLAIMS_TO_PASS,
        model=res.model,
    )
    session.add(note)
    session.flush()
    return note


def new_to_universe(session: Session, exclude: set[str], used: set[str]) -> tuple[Company, dict | None] | None:
    """Highest-priority company not featured before, with one verbatim, cited line from its latest 10-K."""
    companies = session.exec(select(Company).order_by(Company.priority.desc(), Company.sheet_rank)).all()
    for c in companies:
        if c.ticker in exclude or c.ticker in used:
            continue
        passages = filing_passages(
            session, c.ticker, f"business overview operations {c.name} {c.local_gov_driver}", k=3
        )
        for p in passages:
            span = _verbatim_span(p["text"], max_words=40)
            if not span:
                continue
            claim = {"text": span, "citation": {"document_id": p["document_id"], "url": p["url"], "quote": span}}
            corpus = read_document_text(session.get(Document, p["document_id"]))
            if deterministic_check(claim, corpus, []) is None:
                return c, {"quote": span, "url": p["url"], "title": p["title"]}
    return None


def _item_md(i: int, sig: Signal, note: Note, company: Company) -> str:
    return (
        f"### {i}. {company.ticker}: {note.title}\n"
        f"*{company.name} · {company.archetype}*\n\n"
        f"{note.body_md}\n\n"
        f"Signal: {sig.summary} Significance {sig.significance:.2f}. [Source document]({sig.citation_url})\n"
    )


def methodology_md(settings) -> str:
    return (
        f"**Methodology.** {settings.brand} ingests public records (SEC filings, building permits, municipal "
        "agendas, bank call reports, state production and gaming data), maps them to 237 under-covered US "
        "microcaps, and ranks signals by rule-based significance blended with company priority. Analyst agents "
        "write each item; a fact-check pass drops any claim not supported verbatim or numerically by its cited "
        "document. Full method and source register: /methodology."
    )


def build_issue(session: Session, week: str | None, llm: LLM, ensure_notes: bool = True) -> IssueResult:
    settings = get_settings()
    week, start, end = parse_week(week)
    used_signals, used_new = _featured_before(session, week)
    warnings: list[str] = []
    items = select_items(session, used_signals)
    if ensure_notes and len(items) < MAX_ITEMS:
        summary = run_research(session, llm, limit=max(0, MAX_ITEMS + 2 - len(items)))
        warnings += summary.errors[:3]
        items = select_items(session, used_signals)
    if len(items) < MIN_ITEMS:
        warnings.append(f"only {len(items)} fact-checked signals available (target {MIN_ITEMS}-{MAX_ITEMS})")

    excluded = []
    kept_items = []
    for sig, note, company in items:
        try:
            check_publish([company.ticker], disclosure_flag=note.disclosure_flag)
        except PublishBlocked as exc:
            excluded.append(f"{company.ticker}: {exc}")
            continue
        kept_items.append((sig, note, company))

    # Deeper note: the company with the most weight in this week's items.
    weight: dict[str, float] = defaultdict(float)
    for sig, _, company in kept_items:
        weight[company.ticker] += sig.rank_score
    deep = None
    for ticker in sorted(weight, key=weight.get, reverse=True):
        company = session.get(Company, ticker)
        signals = session.exec(select(Signal).where(Signal.ticker == ticker).order_by(Signal.rank_score.desc())).all()
        deep = deep_note(session, company, signals, llm)
        if deep and deep.factcheck_passed:
            break
    nu = new_to_universe(session, used_new, {c.ticker for _, _, c in kept_items})

    title = f"{settings.brand} · Week {int(week[-2:])}, {week[:4]}"
    parts = [
        f"# {title}",
        f"*Independent microcap research from public records. Signals through {end:%B %d, %Y}.*",
        "",
        "## This week's signals",
        "",
    ]
    for i, (sig, note, company) in enumerate(kept_items, 1):
        parts.append(_item_md(i, sig, note, company))
    if deep and deep.factcheck_passed:
        c = session.get(Company, deep.ticker)
        parts += [f"## Company note: {c.ticker} ({c.name})", f"**{deep.title}**", "", deep.body_md, ""]
    elif deep:
        warnings.append(f"deep note for {deep.ticker} failed fact-check; omitted")
    if nu:
        company, line = nu
        parts += [
            f"## New to the universe: {company.ticker} ({company.name})",
            f"*{company.archetype} · ${company.mkt_cap_musd or 0:,.0f}M market cap · analyst coverage: "
            f"{company.analyst_coverage}*",
            "",
            f"Why it's here: {company.local_gov_driver}. Public data we track: {company.public_data}.",
            "",
        ]
        if line:
            parts += [f'From its latest annual report: "{line["quote"]}" [{line["title"]}]({line["url"]})', ""]
    parts += ["---", methodology_md(settings), "", "**Disclosures**", ""]
    parts += [f"- {d}" for d in disclosures()]
    text_md = "\n".join(parts).strip() + "\n"

    lint = lint_impersonal(text_md)
    if lint:
        warnings.append(f"impersonal-language lint flagged: {', '.join(lint)}")
    passed = (
        bool(kept_items)
        and all(n.factcheck_passed for _, n, _ in kept_items)
        and not lint
        and (deep is None or deep.factcheck_passed)
    )

    out_dir = settings.out_dir / "newsletter"
    out_dir.mkdir(parents=True, exist_ok=True)
    path_md, path_html = out_dir / f"{week}.md", out_dir / f"{week}.html"
    path_md.write_text(text_md)
    path_html.write_text(email_html(title, text_md))

    issue = session.exec(select(NewsletterIssue).where(NewsletterIssue.week == week)).first() or NewsletterIssue(
        week=week, title=title, path_md="", path_html=""
    )
    issue.title, issue.path_md, issue.path_html = title, str(path_md), str(path_html)
    issue.signal_ids = ",".join(str(s.id) for s, _, _ in kept_items)
    issue.note_ids = json.dumps(
        {
            "signal_notes": [n.id for _, n, _ in kept_items],
            "company_note": deep.id if deep else None,
            "new_to_universe": [nu[0].ticker] if nu else [],
        }
    )
    issue.factcheck_passed = passed
    issue.created_at = utcnow()
    session.add(issue)
    session.commit()
    return IssueResult(week, path_md, path_html, len(kept_items), passed, excluded, warnings)


# --- email-safe HTML ------------------------------------------------------------------------------
_STYLES = {
    "h1": "font-size:22px;line-height:28px;margin:0 0 6px 0;color:#0d3326;font-weight:600",
    "h2": "font-size:16px;line-height:22px;margin:26px 0 8px 0;color:#0d3326;font-weight:600;"
    "border-top:1px solid #e2e8f0;padding-top:14px",
    "h3": "font-size:15px;line-height:21px;margin:18px 0 4px 0;color:#0f172a;font-weight:600",
    "p": "font-size:14px;line-height:21px;margin:0 0 10px 0;color:#1e293b",
    "li": "font-size:12px;line-height:18px;margin:0 0 6px 0;color:#475569",
    "a": "color:#1f6f54;text-decoration:underline",
    "hr": "border:0;border-top:1px solid #cbd5e1;margin:24px 0",
    "em": "color:#64748b",
}


def email_html(title: str, text_md: str) -> str:
    body = md.markdown(html.escape(text_md, quote=False))
    for tag, style in _STYLES.items():
        body = re.sub(rf"<{tag}(?=[\s>])", f'<{tag} style="{style}"', body)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f1f5f9">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0">'
        '<tr><td style="padding:28px 28px 20px 28px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        f'sans-serif">{body}</td></tr></table></td></tr></table></body></html>'
    )


# --- mock -----------------------------------------------------------------------------------------
@register_mock("company_deep_note")
def _mock_deep(variables: dict) -> dict:
    claims = []
    signals = {s["document_id"]: s for s in variables["signals"]}
    for d in variables["documents"]:
        if len(claims) >= 5:
            break
        if d["document_id"] in signals:
            lines = [ln for ln in d["text"].splitlines() if any(ch.isdigit() for ch in ln) and len(ln.split()) >= 3]
            if lines:
                claims.append(
                    {
                        "text": signals[d["document_id"]]["summary"],
                        "citation": {
                            "document_id": d["document_id"],
                            "url": d["url"],
                            "quote": " ".join(lines[-1].split()[:60]),
                        },
                    }
                )
            continue
        span = _verbatim_span(d["text"])
        if span:
            claims.append(
                {
                    "text": f'{d["title"]} states: "{span}"',
                    "citation": {"document_id": d["document_id"], "url": d["url"], "quote": span},
                }
            )
    return {"headline": f"{variables['company']['ticker']}: what the public record shows this week", "claims": claims}
