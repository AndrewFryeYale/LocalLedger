"""Company history from SEC XBRL company facts: clean fiscal quarters, latest balance sheet, shares.

Quarters are derived from each fact's own start/end dates (SEC "frames" mis-label some fiscal quarters).
When a filer reports only year-to-date or annual figures, the missing quarter is derived:
Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlmodel import Session, select

from localledger.http import HttpClient, read_document_bytes
from localledger.models import Company, Document, utcnow

log = logging.getLogger(__name__)

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
REFRESH_DAYS = 7
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT", "20-F", "40-F"}

REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "OperatingLeasesIncomeStatementLeaseRevenue",
    "RealEstateRevenueNet",
    "OilAndGasRevenue",
    "SalesRevenueNet",
]
DURATION_TAGS = {
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "operating_income": ["OperatingIncomeLoss"],
    "dna": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    ],
    "interest_income": ["InterestAndDividendIncomeOperating"],
    "nii": ["InterestIncomeExpenseNet", "InterestIncomeExpenseAfterProvisionForLoanLoss"],
    "noninterest_income": ["NoninterestIncome"],
}
INSTANT_TAGS = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ],
    "debt": [
        "LongTermDebt",
        "DebtInstrumentCarryingAmount",
        "LongTermDebtNoncurrent",
        "NotesPayable",
        "SecuredDebt",
        "LongTermLineOfCredit",
        "SeniorNotes",
    ],
    "assets": ["Assets"],
    "loans": [
        "LoansAndLeasesReceivableNetReportedAmount",
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss",
    ],
    "deposits": ["Deposits"],
}


@dataclass
class Period:
    start: date
    end: date
    value: float
    accn: str
    filed: str
    derived: bool = False

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "value": self.value,
            "accn": self.accn,
            "filed": self.filed,
            "derived": self.derived,
        }


@dataclass
class History:
    ticker: str
    entity: str = ""
    revenue_tag: str | None = None
    quarters: dict[str, list[Period]] = field(default_factory=dict)  # metric -> quarterly periods, oldest first
    instants: dict[str, tuple[date, float, str]] = field(default_factory=dict)  # metric -> latest (end, value, accn)
    shares: float | None = None
    shares_as_of: str | None = None
    document_id: int | None = None
    source_url: str | None = None
    kind: str = "xbrl"  # xbrl | trust
    currency: str = "USD"

    def series(self, metric: str) -> list[Period]:
        return self.quarters.get(metric, [])


def _days(p: Period) -> int:
    return (p.end - p.start).days + 1


def _kind(days: int) -> str | None:
    if 80 <= days <= 100:
        return "Q"
    if 170 <= days <= 195:
        return "H1"
    if 260 <= days <= 285:
        return "9M"
    if 350 <= days <= 380:
        return "FY"
    return None


def _periods(rows: list[dict]) -> dict[tuple[date, date], Period]:
    """Duration facts keyed by (start, end); the latest filing wins (restatements replace originals)."""
    out: dict[tuple[date, date], Period] = {}
    for r in rows:
        if "start" not in r or r.get("form") not in FORMS:
            continue
        try:
            p = Period(
                date.fromisoformat(r["start"]),
                date.fromisoformat(r["end"]),
                float(r["val"]),
                r.get("accn", ""),
                r.get("filed", ""),
            )
        except (KeyError, ValueError):
            continue
        key = (p.start, p.end)
        if key not in out or p.filed >= out[key].filed:
            out[key] = p
    return out


def quarterly(rows: list[dict]) -> list[Period]:
    """Fiscal quarters from duration facts, deriving missing quarters from H1/9M/FY figures."""
    periods = _periods(rows)
    by_kind: dict[str, list[Period]] = {"Q": [], "H1": [], "9M": [], "FY": []}
    for p in periods.values():
        k = _kind(_days(p))
        if k:
            by_kind[k].append(p)
    quarters = {q.end: q for q in by_kind["Q"]}

    def q_ending(end: date) -> Period | None:
        return next((q for e, q in quarters.items() if abs((e - end).days) <= 5), None)

    def first_q(start: date) -> Period | None:
        return next((q for q in quarters.values() if abs((q.start - start).days) <= 5), None)

    for h1 in by_kind["H1"]:  # Q2 = H1 - Q1
        q1 = first_q(h1.start)
        if q1 and not q_ending(h1.end):
            quarters[h1.end] = Period(q1.end + timedelta(days=1), h1.end, h1.value - q1.value, h1.accn, h1.filed, True)
    for m9 in by_kind["9M"]:  # Q3 = 9M - H1
        h1 = next((h for h in by_kind["H1"] if abs((h.start - m9.start).days) <= 5), None)
        if h1 and not q_ending(m9.end):
            quarters[m9.end] = Period(h1.end + timedelta(days=1), m9.end, m9.value - h1.value, m9.accn, m9.filed, True)
    for fy in by_kind["FY"]:  # Q4 = FY - 9M
        if q_ending(fy.end):
            continue
        m9 = next((m for m in by_kind["9M"] if abs((m.start - fy.start).days) <= 5), None)
        if m9:
            quarters[fy.end] = Period(m9.end + timedelta(days=1), fy.end, fy.value - m9.value, fy.accn, fy.filed, True)
            continue
        inside = [q for q in quarters.values() if q.start >= fy.start - timedelta(days=5) and q.end < fy.end]
        if len(inside) == 3:
            last = max(inside, key=lambda q: q.end)
            quarters[fy.end] = Period(
                last.end + timedelta(days=1), fy.end, fy.value - sum(q.value for q in inside), fy.accn, fy.filed, True
            )
    return sorted(quarters.values(), key=lambda q: q.end)


def latest_instant(rows: list[dict]) -> tuple[date, float, str] | None:
    best = None
    for r in rows:
        if "start" in r or r.get("form") not in FORMS:
            continue
        try:
            item = (date.fromisoformat(r["end"]), float(r["val"]), r.get("accn", ""), r.get("filed", ""))
        except (KeyError, ValueError):
            continue
        if best is None or (item[0], item[3]) > (best[0], best[3]):
            best = item
    return best[:3] if best else None


IFRS_TAGS = {  # foreign private issuers report under IFRS; we only use USD-denominated facts
    "Revenues": "Revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "RevenueFromContractsWithCustomers",
    "NetIncomeLoss": "ProfitLoss",
    "StockholdersEquity": "Equity",
    "CashAndCashEquivalentsAtCarryingValue": "CashAndCashEquivalents",
    "LongTermDebt": "Borrowings",
    "OperatingIncomeLoss": "ProfitLossFromOperatingActivities",
    "DepreciationDepletionAndAmortization": "DepreciationAndAmortisationExpense",
    "Assets": "Assets",
}


def _usd(facts: dict, tag: str) -> list[dict]:
    rows = facts.get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])
    if not rows and tag in IFRS_TAGS:
        rows = facts.get("ifrs-full", {}).get(IFRS_TAGS[tag], {}).get("units", {}).get("USD", [])
    return rows


def annual_as_quarters(rows: list[dict]) -> list[Period]:
    """Annual-only reporters (e.g. foreign filers whose quarterly 6-Ks carry no XBRL): each FY split into FY/4."""
    years = sorted((p for p in _periods(rows).values() if _kind(_days(p)) == "FY"), key=lambda p: p.end)[-3:]
    out = []
    for fy in years:
        start = fy.start
        for i in range(4):
            end = fy.end if i == 3 else min(fy.end, start + timedelta(days=90))
            out.append(Period(start, end, fy.value / 4, fy.accn, fy.filed, derived=True))
            start = end + timedelta(days=1)
    return out


def _pick_series(facts: dict, tags: list[str]) -> tuple[str | None, list[Period]]:
    """The tag with the most recent quarter (ties -> most quarters). One tag only, so periods stay comparable."""
    best: tuple[str | None, list[Period]] = (None, [])
    for tag in tags:
        qs = quarterly(_usd(facts, tag)) or annual_as_quarters(_usd(facts, tag))
        if not qs:
            continue
        if not best[1] or (qs[-1].end, len(qs)) > (best[1][-1].end, len(best[1])):
            best = (tag, qs)
    return best


def _sum_by_end(a: list[Period], b: list[Period]) -> list[Period]:
    idx = {p.end: p for p in b}
    out = []
    for p in a:
        q = idx.get(p.end)
        if q:
            out.append(Period(p.start, p.end, p.value + q.value, p.accn, max(p.filed, q.filed), p.derived or q.derived))
    return out


def parse_companyfacts(ticker: str, data: dict, archetype: str = "") -> History:
    facts = data.get("facts", {})
    h = History(ticker=ticker, entity=data.get("entityName", ""))
    if archetype == "Community bank":
        _, nii = _pick_series(facts, DURATION_TAGS["nii"][:1])
        _, nonii = _pick_series(facts, DURATION_TAGS["noninterest_income"])
        options = [
            ("InterestIncomeExpenseNet+NoninterestIncome", _sum_by_end(nii, nonii)),
            _pick_series(facts, DURATION_TAGS["interest_income"]),
            _pick_series(facts, REVENUE_TAGS),
        ]
        options = [(t, q) for t, q in options if len(q) >= 4]
        if options:  # banks change tags over time: use the freshest series, preferring NII + fees on ties
            h.revenue_tag, h.quarters["revenue"] = max(options, key=lambda o: o[1][-1].end)
    if "revenue" not in h.quarters or not h.quarters["revenue"]:
        h.revenue_tag, h.quarters["revenue"] = _pick_series(facts, REVENUE_TAGS)
    for metric, tags in DURATION_TAGS.items():
        if metric in ("interest_income",):
            continue
        _, h.quarters[metric] = _pick_series(facts, tags)
    for metric, tags in INSTANT_TAGS.items():
        for tag in tags:
            got = latest_instant(_usd(facts, tag))
            if got:
                h.instants[metric] = got
                if tag == "LongTermDebtNoncurrent":  # add the current portion reported at the same date
                    cur = latest_instant(_usd(facts, "LongTermDebtCurrent"))
                    if cur and cur[0] == got[0]:
                        h.instants[metric] = (got[0], got[1] + cur[1], got[2])
                break
    if "equity" not in h.instants:
        for tax in ("ifrs-full", "us-gaap"):
            units = (
                facts.get(tax, {}).get("Equity" if tax == "ifrs-full" else "StockholdersEquity", {}).get("units", {})
            )
            if units and "USD" not in units:
                h.currency = next(iter(units))
                break
    shares = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if shares:
        latest_end = max(r.get("end", "") for r in shares)
        latest_filed = max(r.get("filed", "") for r in shares if r.get("end") == latest_end)
        classes = [r for r in shares if r.get("end") == latest_end and r.get("filed") == latest_filed]
        h.shares, h.shares_as_of = float(sum(r["val"] for r in classes)), latest_end  # multi-class: sum classes
    if not h.shares:
        diluted = (
            facts.get("us-gaap", {})
            .get("WeightedAverageNumberOfDilutedSharesOutstanding", {})
            .get("units", {})
            .get("shares", [])
        )
        q = quarterly(diluted)
        if q:
            h.shares, h.shares_as_of = q[-1].value, q[-1].end.isoformat()
    return h


def fetch_companyfacts(
    session: Session, company: Company, http: HttpClient | None, refresh_days: int = REFRESH_DAYS
) -> Document | None:
    """Stored raw company-facts JSON for a company; re-fetched when older than `refresh_days`."""
    if not company.cik:
        return None
    url = COMPANYFACTS_URL.format(cik=company.cik)
    doc = session.exec(select(Document).where(Document.url == url).order_by(Document.id.desc())).first()
    fresh = doc is not None and (utcnow() - _aware(doc.fetched_at)).days < refresh_days
    if fresh or http is None:
        return doc
    try:
        resp = http.get(url)
    except Exception as exc:  # network down: keep the last stored copy
        log.info("companyfacts fetch failed for %s: %s", company.ticker, exc)
        return doc
    if resp.status_code != 200:
        return doc
    from localledger.connectors.base import ensure_source
    from localledger.connectors.edgar import EdgarConnector
    from localledger.http import DocStore

    ensure_source(session, EdgarConnector)
    return DocStore(session).save(
        source_id="edgar",
        url=url,
        content=resp.content,
        content_type="application/json",
        ticker=company.ticker,
        title=f"XBRL company facts {company.ticker}",
        meta={"kind": "companyfacts"},
    )


def _aware(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_history(session: Session, company: Company, http: HttpClient | None) -> History | None:
    doc = fetch_companyfacts(session, company, http)
    if doc is None:
        return None
    data = json.loads(read_document_bytes(doc))
    h = parse_companyfacts(company.ticker, data, company.archetype)
    h.document_id, h.source_url = doc.id, doc.url
    return h
