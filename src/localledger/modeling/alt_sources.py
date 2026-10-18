"""Models for companies without usable XBRL: royalty trusts (monthly distribution 8-Ks) and banks whose holding
company isn't an SEC filer (FFIEC call report extracts)."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, timedelta

from sqlmodel import Session, select

from localledger.http import HttpClient, read_document_bytes, read_document_text
from localledger.modeling.engine import CompanyModel, month_end
from localledger.modeling.history import History, Period
from localledger.models import Company, Document, Signal

log = logging.getLogger(__name__)

TRUST_NAME = re.compile(r"\b(trust|royalty)\b", re.IGNORECASE)
PER_UNIT = [
    re.compile(r"distribution[^.$]{0,80}?\$\s?(\d*\.\d+)\s*(?:per|/)\s*unit", re.IGNORECASE),
    re.compile(r"\$\s?(\d*\.\d+)\s*per\s*(?:trust\s*)?unit", re.IGNORECASE),
]
NO_DIST = re.compile(
    r"\b(no (?:monthly |cash |quarterly )?distribution|not (?:make|declare) a distribution|"
    r"distribution (?:will|would) not be (?:made|paid))",
    re.IGNORECASE,
)
UNITS = re.compile(r"(\d{1,3}(?:,\d{3}){2,})\s+(?:units|trust units|units of beneficial interest)", re.IGNORECASE)
MONTHS_BACK = 15


def is_trust(company: Company) -> bool:
    return company.archetype in ("Oil & gas producer / royalty", "Midstream / energy infrastructure") and bool(
        TRUST_NAME.search(company.name or "")
    )


def parse_distribution(text: str) -> float | None:
    """Per-unit distribution declared in a trust press release; 0.0 if it says no distribution."""
    head = text[:6000]
    for rx in PER_UNIT:
        m = rx.search(head)
        if m:
            return float(m.group(1))
    if NO_DIST.search(head):
        return 0.0
    return None


def _units_from_10k(session: Session, ticker: str) -> float | None:
    docs = session.exec(
        select(Document).where(Document.ticker == ticker, Document.source_id == "edgar").order_by(Document.id.desc())
    ).all()
    for d in docs:
        if json.loads(d.meta_json or "{}").get("kind") in ("10-k", "10-q"):
            text = read_document_text(d)[:40000]
            for m in UNITS.finditer(text):
                ctx = text[max(0, m.start() - 250) : m.end() + 250].lower()
                if "outstanding" in ctx:
                    return float(m.group(1).replace(",", ""))
    return None


def trust_history(session: Session, company: Company, http: HttpClient | None, today: date) -> History | None:
    """Quarterly distributable income (= per-unit distributions x units) from the trust's monthly 8-K releases."""
    if not company.cik:
        return None
    sub_doc = session.exec(
        select(Document)
        .where(
            Document.ticker == company.ticker, Document.source_id == "edgar", Document.title.like("EDGAR submissions%")
        )
        .order_by(Document.id.desc())
    ).first()
    if sub_doc is None:
        return None
    recent = json.loads(read_document_bytes(sub_doc)).get("filings", {}).get("recent", {})
    filings = [dict(zip(recent.keys(), v, strict=False)) for v in zip(*recent.values(), strict=False)]
    cutoff = (today - timedelta(days=31 * MONTHS_BACK)).isoformat()
    eightks = [f for f in filings if f["form"] == "8-K" and f["filingDate"] >= cutoff]
    from localledger.connectors.edgar import EdgarConnector

    conn = EdgarConnector(session, http=http, frames=False, full_text=False) if http else None
    monthly: dict[str, tuple[float, str, str]] = {}  # yyyy-mm -> (per unit, accession, filed)
    for f in eightks:
        docs = []
        if conn is not None:
            try:
                docs = conn._fetch_exhibit_991(company, f)
            except Exception as exc:  # one missing exhibit never stops the model
                log.info("trust exhibit fetch failed %s %s: %s", company.ticker, f["accessionNumber"], exc)
        else:
            docs = [
                d
                for d in session.exec(select(Document).where(Document.ticker == company.ticker)).all()
                if json.loads(d.meta_json or "{}").get("accession") == f["accessionNumber"]
            ]
        for d in docs:
            per = parse_distribution(read_document_text(d))
            month = f["filingDate"][:7]
            if per is not None and month not in monthly:
                monthly[month] = (per, f["accessionNumber"], f["filingDate"])
    session.commit()
    units = _units_from_10k(session, company.ticker)
    if not monthly or not units:
        return None
    quarters: dict[date, list[tuple[float, str, str]]] = defaultdict(list)
    for month, item in monthly.items():
        y, mo = int(month[:4]), int(month[5:])
        quarters[month_end(y, ((mo - 1) // 3 + 1) * 3)].append(item)
    h = History(ticker=company.ticker, entity=company.name, revenue_tag="Monthly distributions x units (8-K Ex. 99.1)")
    periods = []
    for end, items in sorted(quarters.items()):
        if end >= month_end(today.year, ((today.month - 1) // 3 + 1) * 3):
            continue  # the current calendar quarter is still being declared
        start = month_end(end.year, end.month - 3) + timedelta(days=1) if end.month > 3 else date(end.year, 1, 1)
        periods.append(
            Period(start, end, sum(i[0] for i in items) * units, items[-1][1], items[-1][2], derived=len(items) < 3)
        )
    h.quarters["revenue"] = periods
    h.shares, h.shares_as_of = units, today.isoformat()
    h.kind = "trust"
    h.source_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company.cik}&type=8-K"
    return h if len(periods) >= 4 else None


def call_report_model(session: Session, company: Company, today: date) -> CompanyModel | None:
    """Banks whose holding company isn't an SEC filer: Tier 1 capital and assets from the latest FFIEC extract."""
    sig = session.exec(
        select(Signal).where(Signal.ticker == company.ticker, Signal.source_id == "ffiec").order_by(Signal.as_of.desc())
    ).first()
    if sig is None:
        return None
    doc = session.get(Document, sig.document_id)
    text = read_document_text(doc)

    def latest(item: str) -> float | None:
        m = re.search(rf"\t{re.escape(item)}\t(\d+)", text)
        return float(m.group(1)) * 1000 if m else None

    tier1, assets = latest("Tier 1 capital"), latest("Total assets")
    if not tier1:
        return None
    m = CompanyModel(
        ticker=company.ticker,
        name=company.name,
        archetype=company.archetype,
        status="call_report_only",
        as_of=today.isoformat(),
    )
    m.balance = {
        "tbv": tier1,
        "equity": tier1,
        "assets": assets,
        "mkt_cap": (company.mkt_cap_musd or 0) * 1e6,
        "shares": None,
        "price": None,
        "balance_date": str(sig.as_of),
    }
    m.sources.append({"document_id": doc.id, "url": sig.citation_url, "title": doc.title})
    m.overlay_note = (
        "Holding company isn't an SEC filer: modeled from the bank's FFIEC call report (balance sheet only)."
    )
    return m
