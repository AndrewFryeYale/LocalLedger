"""SEC EDGAR: the backbone connector.

Access (verified Oct 2026, see SOURCES.md):
- https://www.sec.gov/files/company_tickers.json            ticker -> CIK
- https://data.sec.gov/submissions/CIK##########.json        filing history (forms, items, primary docs)
- https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{acc-with-dashes}-index.htm   exhibit list (EX-99.1)
- https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{yyyy}Q{q}.json          one concept, all filers
SEC requires a declared User-Agent and <=10 requests/second.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup
from sqlmodel import select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.http import read_document_bytes
from localledger.models import Company, Document
from localledger.signals.scoring import move_significance, pct_change
from localledger.signals.store import SignalDraft

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{period}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "InterestAndDividendIncomeOperating",
]
INCOME_TAGS = ["NetIncomeLoss"]

# 8-K items worth a signal, with rules-based significance.
ITEM_SIGNIFICANCE = {
    "1.01": (0.5, "entered into a material definitive agreement"),
    "1.02": (0.5, "terminated a material definitive agreement"),
    "1.03": (0.95, "disclosed a bankruptcy or receivership"),
    "2.01": (0.6, "completed an acquisition or disposition of assets"),
    "2.05": (0.5, "disclosed exit or disposal costs"),
    "2.06": (0.6, "disclosed a material impairment"),
    "3.01": (0.8, "received a delisting or listing-standard notice"),
    "4.01": (0.6, "changed its certifying accountant"),
    "4.02": (0.9, "disclosed non-reliance on prior financial statements"),
    "5.01": (0.8, "disclosed a change in control"),
    "5.02": (0.4, "disclosed a director or officer change"),
}
FORM_SIGNIFICANCE = {
    "NT 10-K": (0.7, "filed a notice of late 10-K filing"),
    "NT 10-Q": (0.65, "filed a notice of late 10-Q filing"),
    "SCHEDULE 13D": (0.5, "had a new Schedule 13D (activist-style) ownership filing"),
    "DEFC14A": (0.7, "had a definitive contested proxy filing"),
    "PREC14A": (0.65, "had a preliminary contested proxy filing"),
}
EVENT_WINDOW_DAYS = 75


def acc_nodash(accession: str) -> str:
    return accession.replace("-", "")


def filing_index_url(cik: str | int, accession: str) -> str:
    return f"{ARCHIVE.format(cik=int(cik), acc=acc_nodash(accession))}/{accession}-index.htm"


def filing_doc_url(cik: str | int, accession: str, filename: str) -> str:
    return f"{ARCHIVE.format(cik=int(cik), acc=acc_nodash(accession))}/{filename}"


def fmt_musd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value) / 1e6:,.1f}M"


def quarter_label(period: str) -> str:
    """CY2026Q2 -> 'Q2 2026' (calendar-aligned per the SEC frames API)."""
    m = re.fullmatch(r"CY(\d{4})Q(\d)", period)
    return f"Q{m.group(2)} {m.group(1)}" if m else period


def recent_quarters(today: date, n: int = 2) -> list[tuple[str, str]]:
    """Latest `n` calendar quarters that have ended, each paired with the same quarter a year earlier."""
    q = (today.month - 1) // 3  # quarters fully ended this year
    year = today.year
    if q == 0:
        year, q = year - 1, 4
    out = []
    for _ in range(n):
        out.append((f"CY{year}Q{q}", f"CY{year - 1}Q{q}"))
        q -= 1
        if q == 0:
            year, q = year - 1, 4
    return out


def parse_exhibit_index(html: str | bytes) -> list[dict]:
    """Rows of a filing -index.htm page: seq, description, document, type, url."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        link = tr.find("a", href=True)
        href = link["href"] if link else ""
        if href.startswith("/ix?doc="):
            href = href[len("/ix?doc=") :]
        rows.append(
            {
                "seq": cells[0].get_text(strip=True),
                "description": cells[1].get_text(strip=True),
                "document": cells[2].get_text(" ", strip=True).split(" ")[0],
                "type": cells[3].get_text(strip=True),
                "url": f"https://www.sec.gov{href}" if href.startswith("/") else href,
            }
        )
    return rows


class EdgarConnector(Connector):
    source_id = "edgar"
    name = "SEC EDGAR"
    type = "filings"
    access_method = (
        "Public JSON APIs (company_tickers.json, data.sec.gov submissions, XBRL frames) and the "
        "EDGAR Archives (filing index pages, 8-K Ex. 99.1, 10-K/10-Q primary documents). No key."
    )
    cadence = "daily"
    terms_note = (
        "SEC fair-access policy: declared User-Agent with contact email, <=10 requests/second. "
        "Public filings; no login."
    )

    def __init__(self, *args: Any, full_text: bool = True, frames: bool = True, today: date | None = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.full_text = full_text
        self.frames = frames
        self.today = today or date.today()
        self._cik_to_ticker: dict[int, str] = {}

    # --- discover -------------------------------------------------------------------------------
    def discover(self) -> list[tuple[str, Any]]:
        resp = self.http.get_ok(TICKERS_URL)
        self.save(url=TICKERS_URL, content=resp.content, content_type="application/json",
                  title="SEC company_tickers.json")
        mapping = {row["ticker"].upper(): row for row in resp.json().values()}
        targets: list[tuple[str, Any]] = []
        for ticker, company in sorted(self.universe().items(), key=lambda kv: kv[1].sheet_rank or 999):
            row = mapping.get(ticker) or mapping.get(ticker.replace(".", "-"))
            if not row:
                continue
            company.cik = str(row["cik_str"])
            company.sec_name = row["title"]
            self.session.add(company)
            self._cik_to_ticker[int(row["cik_str"])] = ticker
            targets.append(("company", ticker))
        self.session.flush()
        if not targets:
            raise ConnectorUnavailable("no universe tickers found in company_tickers.json")
        if self.frames:
            for current, prior in recent_quarters(self.today):
                for tag in REVENUE_TAGS + INCOME_TAGS:
                    targets.append(("frame", (tag, current)))
                    targets.append(("frame", (tag, prior)))
        return targets

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: tuple[str, Any]) -> list[Document]:
        kind, value = target
        if kind == "frame":
            return self._fetch_frame(*value)
        return self._fetch_company(value)

    def _fetch_frame(self, tag: str, period: str) -> list[Document]:
        url = FRAMES_URL.format(tag=tag, period=period)
        existing = self.session.exec(select(Document).where(Document.url == url)).first()
        if existing and period != recent_quarters(self.today, 1)[0][0]:
            return [existing]  # closed historical frames don't change much; reuse
        resp = self.http.get(url)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise RuntimeError(f"frames HTTP {resp.status_code} for {url}")
        doc = self.save(url=url, content=resp.content, content_type="application/json",
                        title=f"XBRL frame {tag} {period}", meta={"kind": "frame", "tag": tag, "period": period})
        return [doc]

    def _fetch_company(self, ticker: str) -> list[Document]:
        company = self.session.get(Company, ticker)
        url = SUBMISSIONS_URL.format(cik=company.cik)
        resp = self.http.get_ok(url)
        sub = resp.json()
        docs = [self.save(url=url, content=resp.content, content_type="application/json", ticker=ticker,
                          title=f"EDGAR submissions {ticker}", meta={"kind": "submissions"})]
        addr = (sub.get("addresses") or {}).get("business") or {}
        company.state = addr.get("stateOrCountry") or company.state
        company.sic = sub.get("sic") or company.sic
        self.session.add(company)

        recent = sub.get("filings", {}).get("recent", {})
        filings = [dict(zip(recent.keys(), vals, strict=False)) for vals in zip(*recent.values(), strict=False)]
        cutoff = (self.today - timedelta(days=EVENT_WINDOW_DAYS)).isoformat()

        earnings = next((f for f in filings if f["form"] in ("8-K", "8-K/A") and "2.02" in (f.get("items") or "")),
                        None)
        if earnings:
            docs += self._fetch_exhibit_991(company, earnings)
        for f in filings:
            if f["filingDate"] < cutoff:
                break
            if f["form"] == "8-K" and f is not earnings:
                items = (f.get("items") or "").split(",")
                if any(i in ITEM_SIGNIFICANCE and ITEM_SIGNIFICANCE[i][0] >= 0.4 for i in items):
                    docs += self._fetch_primary(company, f, kind="8k")
        if self.full_text:
            for form in ("10-K", "10-Q"):
                latest = next((f for f in filings if f["form"] == form), None)
                if latest:
                    docs += self._fetch_primary(company, latest, kind=form.lower())
        return docs

    def _known(self, url: str) -> Document | None:
        return self.session.exec(select(Document).where(Document.url == url)).first()

    def _fetch_primary(self, company: Company, filing: dict, kind: str) -> list[Document]:
        url = filing_doc_url(company.cik, filing["accessionNumber"], filing["primaryDocument"])
        known = self._known(url)
        if known:
            return [known]
        resp = self.http.get_ok(url)
        meta = {"kind": kind, "form": filing["form"], "accession": filing["accessionNumber"],
                "filing_date": filing["filingDate"], "report_date": filing.get("reportDate"),
                "items": filing.get("items"), "index_url": filing_index_url(company.cik, filing["accessionNumber"])}
        return [self.save(url=url, content=resp.content, content_type="text/html", ticker=company.ticker,
                          title=f"{company.ticker} {filing['form']} {filing['filingDate']}", meta=meta)]

    def _fetch_exhibit_991(self, company: Company, filing: dict) -> list[Document]:
        index_url = filing_index_url(company.cik, filing["accessionNumber"])
        meta = {"kind": "ex99", "form": filing["form"], "accession": filing["accessionNumber"],
                "filing_date": filing["filingDate"], "report_date": filing.get("reportDate"),
                "items": filing.get("items"), "index_url": index_url}
        existing = self.session.exec(
            select(Document).where(Document.ticker == company.ticker, Document.source_id == self.source_id)
        ).all()
        for doc in existing:
            if doc.meta_json and json.loads(doc.meta_json).get("accession") == filing["accessionNumber"] \
                    and json.loads(doc.meta_json).get("kind") == "ex99":
                return [doc]
        index = self.http.get_ok(index_url)
        rows = parse_exhibit_index(index.content)
        ex = next((r for r in rows if r["type"].upper() in ("EX-99.1", "EX-99")), None) or next(
            (r for r in rows if r["type"].upper().startswith("EX-99")), None)
        if not ex or not ex["url"]:
            return []
        resp = self.http.get_ok(ex["url"])
        ctype = "application/pdf" if ex["url"].lower().endswith(".pdf") else "text/html"
        return [self.save(url=ex["url"], content=resp.content, content_type=ctype, ticker=company.ticker,
                          title=f"{company.ticker} 8-K Ex. 99.1 {filing['filingDate']}", meta=meta)]

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json) if doc.meta_json else {}
        kind = meta.get("kind")
        if kind == "frame":
            return self._parse_frame(doc, meta)
        if kind == "submissions":
            return self._parse_submissions(doc)
        if kind in ("ex99", "8k"):
            return [{"kind": kind, "ticker": doc.ticker, "document_id": doc.id, "url": doc.url, **meta}]
        return []

    def _parse_frame(self, doc: Document, meta: dict) -> list[dict]:
        if not self._cik_to_ticker:
            for c in self.session.exec(select(Company).where(Company.cik.is_not(None))).all():
                self._cik_to_ticker[int(c.cik)] = c.ticker
        data = json.loads(read_document_bytes(doc, self.settings))
        out = []
        for row in data.get("data", []):
            ticker = self._cik_to_ticker.get(int(row["cik"]))
            if not ticker:
                continue
            out.append({"kind": "frame", "ticker": ticker, "tag": meta["tag"], "period": meta["period"],
                        "val": float(row["val"]), "accn": row["accn"], "end": row["end"], "cik": row["cik"],
                        "document_id": doc.id})
        return out

    def _parse_submissions(self, doc: Document) -> list[dict]:
        sub = json.loads(read_document_bytes(doc, self.settings))
        recent = sub.get("filings", {}).get("recent", {})
        filings = [dict(zip(recent.keys(), vals, strict=False)) for vals in zip(*recent.values(), strict=False)]
        cutoff = (self.today - timedelta(days=EVENT_WINDOW_DAYS)).isoformat()
        out = []
        for f in filings:
            if f["filingDate"] < cutoff:
                break
            if f["form"] in FORM_SIGNIFICANCE:
                out.append({"kind": "form", "ticker": doc.ticker, "form": f["form"],
                            "filing_date": f["filingDate"], "accession": f["accessionNumber"],
                            "cik": sub.get("cik"), "document_id": doc.id})
        return out

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        drafts: list[SignalDraft] = []
        drafts += self._event_signals(records)
        drafts += self._xbrl_signals([r for r in records if r["kind"] == "frame"])
        return drafts

    def _event_signals(self, records: list[dict]) -> list[SignalDraft]:
        drafts = []
        cutoff = (self.today - timedelta(days=EVENT_WINDOW_DAYS)).isoformat()
        for r in records:
            if r["kind"] == "ex99" and r["filing_date"] >= cutoff:
                period = r.get("report_date") or r["filing_date"]
                drafts.append(SignalDraft(
                    ticker=r["ticker"], metric="earnings_release", as_of=date.fromisoformat(r["filing_date"]),
                    period=r["accession"], significance=0.45, citation_url=r["url"], document_id=r["document_id"],
                    summary=f"{r['ticker']} filed an earnings release (8-K Item 2.02, Ex. 99.1) on "
                            f"{r['filing_date']} (event date {period}).",
                    detail={"index_url": r["index_url"], "items": r.get("items")}))
            elif r["kind"] == "8k":
                items = [i for i in (r.get("items") or "").split(",") if i in ITEM_SIGNIFICANCE]
                if not items:
                    continue
                top = max(items, key=lambda i: ITEM_SIGNIFICANCE[i][0])
                sig, phrase = ITEM_SIGNIFICANCE[top]
                drafts.append(SignalDraft(
                    ticker=r["ticker"], metric=f"8k_item_{top}", as_of=date.fromisoformat(r["filing_date"]),
                    period=r["accession"], significance=sig, citation_url=r["url"], document_id=r["document_id"],
                    summary=f"{r['ticker']} {phrase} (8-K Item {top}, filed {r['filing_date']}).",
                    detail={"items": r.get("items"), "index_url": r["index_url"]}))
            elif r["kind"] == "form":
                sig, phrase = FORM_SIGNIFICANCE[r["form"]]
                drafts.append(SignalDraft(
                    ticker=r["ticker"], metric=f"form_{r['form'].replace(' ', '_').lower()}",
                    as_of=date.fromisoformat(r["filing_date"]), period=r["accession"], significance=sig,
                    citation_url=filing_index_url(r["cik"], r["accession"]), document_id=r["document_id"],
                    summary=f"{r['ticker']} {phrase} ({r['form']}, filed {r['filing_date']})."))
        return drafts

    def _xbrl_signals(self, rows: list[dict]) -> list[SignalDraft]:
        by_key: dict[tuple[str, str, str], dict] = {}
        for r in rows:
            by_key[(r["ticker"], r["tag"], r["period"])] = r
        drafts = []
        tickers = {r["ticker"] for r in rows}
        for ticker in tickers:
            for metric, tags in (("revenue_yoy", REVENUE_TAGS), ("net_income_yoy", INCOME_TAGS)):
                for current, prior in recent_quarters(self.today):
                    pair = next(((by_key[(ticker, t, current)], by_key[(ticker, t, prior)]) for t in tags
                                 if (ticker, t, current) in by_key and (ticker, t, prior) in by_key), None)
                    if not pair:
                        continue
                    cur, pri = pair
                    pct = pct_change(cur["val"], pri["val"])
                    if metric == "revenue_yoy":
                        sig = move_significance(pct, floor=0.05, full=0.4)
                    else:
                        sig = move_significance(pct, floor=0.15, full=1.0) * 0.8
                    if (cur["val"] < 0) != (pri["val"] < 0):
                        sig = max(sig, 0.6)  # swung between profit and loss
                    if metric == "revenue_yoy":
                        label = "total interest income" if cur["tag"] == "InterestAndDividendIncomeOperating" \
                            else "revenue"
                    else:
                        label = "net income"
                    if (cur["val"] < 0) != (pri["val"] < 0) or pri["val"] < 0:
                        move_txt = "sign change; % not meaningful" if (cur["val"] < 0) != (pri["val"] < 0) \
                            else f"loss {'narrowed' if cur['val'] > pri['val'] else 'widened'}"
                        pct = None if (cur["val"] < 0) != (pri["val"] < 0) else pct
                    else:
                        move_txt = f"{pct:+.1%} y/y" if pct is not None else "n/m"
                    drafts.append(SignalDraft(
                        ticker=ticker, metric=metric, as_of=date.fromisoformat(cur["end"]), period=current,
                        value=cur["val"], prior_value=pri["val"], unit="USD", significance=round(sig, 4),
                        citation_url=filing_index_url(cur["cik"], cur["accn"]), document_id=cur["document_id"],
                        summary=f"{quarter_label(current)} {label} {fmt_musd(cur['val'])} vs "
                                f"{fmt_musd(pri['val'])} a year earlier ({move_txt}), XBRL tag {cur['tag']}.",
                        detail={"tag": cur["tag"], "accn": cur["accn"], "prior_accn": pri["accn"],
                                "prior_document_id": pri["document_id"], "pct_change": pct}))
                    break  # most recent available quarter only
        return drafts
