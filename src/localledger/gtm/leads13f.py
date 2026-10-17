"""Fund target list from the SEC Form 13F data sets: funds holding >= 2 universe names, ranked by overlap.

Access (verified Oct 2026): https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets lists quarterly
zips (SUBMISSION, COVERPAGE, SUMMARYPAGE, INFOTABLE .tsv). INFOTABLE rows carry CUSIPs, not tickers, so we map
universe tickers to CUSIPs by issuer-name matching and verify each one against OpenFIGI (public, no key) before
any holding is used in outreach. The map is cached in data/cusip_map.csv; rows marked manual=1 are never overwritten.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz
from sqlmodel import Session, select

from localledger.config import get_settings
from localledger.connectors.base import Connector
from localledger.http import HttpClient, read_document_bytes
from localledger.models import Company, Document, Lead, Source, utcnow

log = logging.getLogger(__name__)

INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
FIGI_URL = "https://api.openfigi.com/v3/mapping"
MEGA_VALUE_USD = 100e9
BROAD_OVERLAP = 40  # filers holding this many universe names are index/quant-style, not research buyers
MEANINGFUL_USD, MEANINGFUL_PCT = 250_000, 0.0025
# Index giants, wirehouses and market makers: real holders, but not newsletter/pilot buyers.
NON_TARGET = re.compile(
    r"\b(vanguard|blackrock|state street|geode|northern trust|schwab|fidelity|morgan stanley|goldman|jpmorgan|"
    r"j\.p\. morgan|ubs|wells fargo|bank of america|merrill|citigroup|citadel|susquehanna|jane street|virtu|"
    r"two sigma|renaissance technologies|millennium|hrt|tower research|optiver|invesco|bank of new york|"
    r"bny|nuveen|tiaa|ameriprise|raymond james|lpl financial|royal bank|td |barclays|deutsche bank|"
    r"credit suisse|nomura|mufg|legal & general|norges|qube|xtx|squarepoint|jump|hudson river|"
    r"envestnet|osaic|commonwealth equity|edward jones|stifel|ameritas|principal financial|retirement|pension|"
    r"teachers|public employees|state of|county of|endowment|etf|etfs|index)\b",
    re.IGNORECASE,
)
_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|lp|llc|holdings?|hldgs?|group|grp|the|"
    r"bancorp(oration)?|bancshares|bankshares|financial|finl|trust|tr|reit|com|new|del|de|cl a|class a)\b"
)


def norm_name(name: str) -> str:
    name = name.lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(_SUFFIX.sub(" ", name).split())


@dataclass
class LeadsResult:
    zip_url: str
    report_period: str = ""
    mapped: int = 0
    verified: int = 0
    funds_with_overlap: int = 0
    leads: int = 0
    path: Path | None = None
    notes: list[str] = field(default_factory=list)


class _ThirteenFSource(Connector):
    """Only used to register the 13F source and store the raw zip; it doesn't emit signals."""

    source_id = "sec_13f"
    name = "SEC Form 13F data sets"
    type = "holdings"
    access_method = "Quarterly zips from sec.gov/data-research/sec-markets-data/form-13f-data-sets (TSV tables)."
    cadence = "quarterly"
    terms_note = "SEC fair access (declared UA, <=10 req/s). Holdings are as of quarter end, filed up to 45 days later."

    def discover(self):  # pragma: no cover - not used
        return []

    def fetch(self, target):  # pragma: no cover
        return []

    def parse(self, doc):  # pragma: no cover
        return []

    def to_signals(self, records):  # pragma: no cover
        return []


def latest_zip_url(http: HttpClient) -> str:
    html = http.get_ok(INDEX_URL).text
    links = re.findall(r'href="([^"]*form-13f-data-sets/[^"]*_form13f\.zip)"', html)
    if not links:
        raise RuntimeError("no 13F data set links found on the SEC page")
    return "https://www.sec.gov" + links[0] if links[0].startswith("/") else links[0]


def _tsv(zf: zipfile.ZipFile, name: str):
    member = next(n for n in zf.namelist() if n.upper().endswith(name))
    with zf.open(member) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t")


def load_cusip_map(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return {r["ticker"]: r for r in csv.DictReader(fh)}


def save_cusip_map(path: Path, rows: dict[str, dict]) -> None:
    fields = ["ticker", "cusip", "issuer_name", "score", "status", "figi_ticker", "manual"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in sorted(rows):
            w.writerow({k: rows[t].get(k, "") for k in fields})


def match_cusips(companies: list[Company], issuers: dict[str, Counter]) -> dict[str, dict]:
    """Best CUSIP per ticker by normalized issuer-name similarity, preferring the most widely held CUSIP."""
    index: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for cusip, names in issuers.items():
        name, _ = names.most_common(1)[0]
        index[norm_name(name)].append((cusip, sum(names.values())))
    keys = list(index)
    out = {}
    for c in companies:
        variants = {norm_name(n) for n in (c.name, c.sec_name) if n}
        best = None
        for v in variants:
            if not v:
                continue
            for key in keys:
                if key[:1] != v[:1]:
                    continue
                score = fuzz.token_sort_ratio(v, key)
                if score >= 90 and (best is None or (score, max(n for _, n in index[key])) > best[:2]):
                    cusip, holders = max(index[key], key=lambda x: x[1])
                    best = (score, holders, cusip, key)
        if best:
            out[c.ticker] = {
                "ticker": c.ticker,
                "cusip": best[2],
                "issuer_name": best[3],
                "score": best[0],
                "status": "fuzzy",
                "figi_ticker": "",
                "manual": "0",
            }
    return out


def verify_with_figi(http: HttpClient, rows: dict[str, dict]) -> None:
    pending = [r for r in rows.values() if r["status"] in ("fuzzy", "")]
    for i in range(0, len(pending), 10):  # 10 jobs per request without an API key
        batch = pending[i : i + 10]
        payload = [{"idType": "ID_CUSIP", "idValue": r["cusip"]} for r in batch]
        try:
            resp = http.request("POST", FIGI_URL, json_body=payload, interval=2.6)
            results = resp.json() if resp.status_code == 200 else []
        except Exception as exc:  # FIGI down: leave as fuzzy (excluded from outreach)
            log.warning("OpenFIGI unavailable: %s", exc)
            return
        for row, res in zip(batch, results, strict=False):
            tickers = {d.get("ticker", "").upper() for d in (res.get("data") or [])}
            row["figi_ticker"] = ",".join(sorted(tickers))[:60]
            row["status"] = (
                "verified" if row["ticker"] in tickers or row["ticker"].replace(".", "/") in tickers else "mismatch"
            )


def build_leads(session: Session, http: HttpClient | None = None, zip_path: Path | None = None) -> LeadsResult:
    settings = get_settings()
    http = http or HttpClient()
    src = _ThirteenFSource(session, http=http)
    if zip_path is None:
        url = latest_zip_url(http)
        doc = session.exec(select(Document).where(Document.url == url)).first()
        if doc is None:
            resp = http.get_ok(url)
            doc = src.save(url=url, content=resp.content, content_type="application/zip", title=url.rsplit("/", 1)[-1])
            session.commit()
        data = read_document_bytes(doc)
    else:
        url, data = str(zip_path), zip_path.read_bytes()
    result = LeadsResult(zip_url=url)
    zf = zipfile.ZipFile(io.BytesIO(data))

    # 1. The latest original 13F-HR per filer, plus cover-page details.
    subs = {}
    for r in _tsv(zf, "SUBMISSION.TSV"):
        if r.get("SUBMISSIONTYPE") not in ("13F-HR", "13F-HR/A"):
            continue
        subs[r["ACCESSION_NUMBER"]] = r
    cover = {r["ACCESSION_NUMBER"]: r for r in _tsv(zf, "COVERPAGE.TSV") if r["ACCESSION_NUMBER"] in subs}
    totals = {r["ACCESSION_NUMBER"]: r for r in _tsv(zf, "SUMMARYPAGE.TSV") if r["ACCESSION_NUMBER"] in subs}
    latest_by_cik: dict[str, str] = {}
    for acc, r in subs.items():
        cp = cover.get(acc, {})
        if cp.get("ISAMENDMENT") == "Y" and cp.get("AMENDMENTTYPE") != "RESTATEMENT":
            continue  # "new holdings" amendments are partial
        prev = latest_by_cik.get(r["CIK"])
        if prev is None or (r.get("PERIODOFREPORT", ""), r.get("FILING_DATE", "")) > (
            subs[prev].get("PERIODOFREPORT", ""),
            subs[prev].get("FILING_DATE", ""),
        ):
            latest_by_cik[r["CIK"]] = acc
    keep = set(latest_by_cik.values())

    # 2. CUSIP map (cached, FIGI-verified).
    map_path = settings.data_dir / "cusip_map.csv"
    cmap = load_cusip_map(map_path)
    companies = session.exec(select(Company)).all()
    missing = [c for c in companies if c.ticker not in cmap]
    if missing:
        issuers: dict[str, Counter] = defaultdict(Counter)
        for r in _tsv(zf, "INFOTABLE.TSV"):
            if r["ACCESSION_NUMBER"] in keep and not r.get("PUTCALL"):
                issuers[r["CUSIP"].upper()][r["NAMEOFISSUER"].strip()] += 1
        for t, row in match_cusips(missing, issuers).items():
            cmap.setdefault(t, row)
        verify_with_figi(http, cmap)
        save_cusip_map(map_path, cmap)
    usable = {r["cusip"].upper(): t for t, r in cmap.items() if r["status"] == "verified" or r.get("manual") == "1"}
    result.mapped, result.verified = len(cmap), len(usable)

    # 3. Overlap per filer.
    holdings: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in _tsv(zf, "INFOTABLE.TSV"):
        acc = r["ACCESSION_NUMBER"]
        if acc not in keep or r.get("PUTCALL"):
            continue
        ticker = usable.get(r["CUSIP"].upper())
        if ticker:
            holdings[acc][ticker] += float(r.get("VALUE") or 0)
    rows = []
    for acc, pos in holdings.items():
        if len(pos) < 2:
            continue
        s, cp = subs[acc], cover.get(acc, {})
        name = cp.get("FILINGMANAGER_NAME") or s.get("CIK")
        total = float(totals.get(acc, {}).get("TABLEVALUETOTAL") or 0)
        meaningful = [t for t, v in pos.items() if v >= MEANINGFUL_USD and (not total or v / total >= MEANINGFUL_PCT)]
        overlap_value = sum(pos.values())
        rows.append(
            {
                "fund_name": name,
                "cik": s["CIK"].lstrip("0"),
                "city": cp.get("FILINGMANAGER_CITY", "").title(),
                "state": cp.get("FILINGMANAGER_STATEORCOUNTRY", ""),
                "report_period": s.get("PERIODOFREPORT", ""),
                "overlap_count": len(pos),
                "overlap_value_usd": round(overlap_value),
                "meaningful_count": len(meaningful),
                "meaningful_tickers": " ".join(sorted(meaningful, key=pos.get, reverse=True)),
                "overlap_pct_of_13f": round(overlap_value / total, 4) if total else None,
                "is_broad": len(pos) >= BROAD_OVERLAP,
                "overlap_tickers": " ".join(sorted(pos, key=pos.get, reverse=True)),
                "total_13f_value_usd": round(total),
                "is_mega": bool(total >= MEGA_VALUE_USD or NON_TARGET.search(name or "")),
                "filing_url": f"https://www.sec.gov/Archives/edgar/data/{s['CIK'].lstrip('0')}/"
                f"{acc.replace('-', '')}/{acc}-index.htm",
            }
        )
    # Research buyers first: not index/mega/asset-owner, then by meaningful positions, concentration, overlap value.
    rows.sort(
        key=lambda r: (
            r["is_mega"],
            r["is_broad"],
            r["meaningful_count"] < 2,
            -r["meaningful_count"],
            -(r["overlap_pct_of_13f"] or 0),
            -r["overlap_value_usd"],
        )
    )
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    result.funds_with_overlap = len(rows)
    result.report_period = rows[0]["report_period"] if rows else ""

    out = settings.out_dir / "leads.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "fund_name",
        "cik",
        "city",
        "state",
        "report_period",
        "meaningful_count",
        "meaningful_tickers",
        "overlap_count",
        "overlap_value_usd",
        "overlap_pct_of_13f",
        "overlap_tickers",
        "total_13f_value_usd",
        "is_mega",
        "is_broad",
        "filing_url",
    ]
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r[k] for k in fields} for r in rows)
    result.path = out

    existing = {lead.cik: lead for lead in session.exec(select(Lead)).all()}
    for r in rows:
        lead = existing.get(r["cik"]) or Lead(cik=r["cik"], fund_name=r["fund_name"])
        for k in (
            "fund_name",
            "city",
            "state",
            "report_period",
            "overlap_count",
            "overlap_value_usd",
            "total_13f_value_usd",
            "rank",
        ):
            setattr(lead, k, r[k])
        lead.overlap_tickers = r["meaningful_tickers"] or r["overlap_tickers"]
        lead.is_mega = r["is_mega"] or r["is_broad"]
        lead.updated_at = utcnow()
        session.add(lead)
    source = session.get(Source, src.source_id)
    source.status, source.last_run_at, source.last_error = "available", utcnow(), None
    session.add(source)
    session.commit()
    result.leads = len(rows)
    return result
