"""FFIEC Call Reports: loan growth, deposit mix, CRE concentration and asset quality for the community banks.

Access (verified Oct 2026, see SOURCES.md):
- https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx   FFIEC Central Data Repository (CDR) public bulk data,
  "Call Reports -- Single Period", tab-delimited zip (~6 MB, every Call Report filer). It's an ASP.NET form: GET the
  page, post back the product choice to list report dates, then post the download. Public; no login or key.
- https://cdr.ffiec.gov/public/ViewFacsimileDirect.aspx?ds=call&idType=ID_RSSD&id={rssd}&date={MMDDYYYY}
  opens that bank's Call Report facsimile (PDF) for the quarter. This is the citation a human clicks.
- https://api.fdic.gov/banks/institutions   FDIC BankFind active institutions (no key). Used only to map tickers to
  banks (holding-company name NAMEHCR, state, assets). The result is kept in config/bank_map.yaml.
cdr.ffiec.gov/robots.txt redirects to an HTML page with no rules (allow); api.fdic.gov/robots.txt is 404 (allow).

Each mapped bank gets one stored text extract: the Call Report items we use, copied verbatim from the bulk files, for
the latest quarter, the prior quarter and the year-ago quarter. Signals are computed from that extract and quote it.
The extract records the sha256 of each raw zip it came from, and the zips are stored under ./raw/ffiec/ too.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from sqlmodel import select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.http import FetchError, RobotsDisallowed, read_document_bytes, read_document_text
from localledger.models import Document
from localledger.signals.scoring import move_significance, pct_change
from localledger.signals.store import SignalDraft

CDR_BULK_URL = "https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx"
FACSIMILE_URL = "https://cdr.ffiec.gov/public/ViewFacsimileDirect.aspx?ds=call&idType=ID_RSSD&id={rssd}&date={period}"
FDIC_INSTITUTIONS_URL = "https://api.fdic.gov/banks/institutions"
FDIC_FIELDS = "CERT,FED_RSSD,NAME,CITY,STALP,NAMEHCR,RSSDHCR,CITYHCR,STALPHCR,ASSET,REPDTE,ACTIVE"
ARCHETYPE = "Community bank"

# ASP.NET control names on the CDR bulk download page.
PRODUCT = "ReportingSeriesSinglePeriod"
F_PRODUCT = "ctl00$MainContentHolder$ListBox1"
F_DATES = "ctl00$MainContentHolder$DatesDropDownList"
F_FORMAT = "ctl00$MainContentHolder$FormatType"
F_DOWNLOAD = "ctl00$MainContentHolder$TabStrip1$Download_0"

REPORT_LAG_DAYS = 45  # Call Reports are due 30 days after quarter end (35 for some); CDR needs ~2 more weeks
CRE_GUIDELINE = 3.0  # 2006 interagency CRE guidance: total CRE >= 300% of total risk-based capital
CONSTRUCTION_GUIDELINE = 1.0  # ...or construction & land development >= 100%
FUZZY_MIN, FUZZY_MARGIN = 93, 5


@dataclass(frozen=True)
class Item:
    key: str
    schedule: str
    codes: tuple[str, ...]  # MDRM alternatives: consolidated (RCFD/RCFA, FFIEC 031 filers) before domestic (RCON/RCOA)
    label: str


# Extract sections, in document order. A signal's excerpt is one whole section, so each holds what its metric needs.
SECTIONS: dict[str, tuple[Item, ...]] = {
    "loans": (
        Item(
            "loans_hfi", "RC", ("RCFDB528", "RCONB528"), "Loans and leases held for investment, net of unearned income"
        ),
        Item("acl", "RC", ("RCFD3123", "RCON3123"), "Allowance for credit losses on loans and leases"),
        Item("net_loans", "RC", ("RCFDB529", "RCONB529"), "Loans and leases, net of unearned income and allowance"),
        Item("loans_hfs", "RC", ("RCFD5369", "RCON5369"), "Loans and leases held for sale"),
        Item(
            "total_loans",
            "RCCI",
            ("RCFD2122", "RCON2122"),
            "Total loans and leases, net of unearned income (held for investment and for sale)",
        ),
    ),
    "deposits": (
        Item("deposits", "RC", ("RCON2200",), "Total deposits (domestic offices)"),
        Item("deposits_nib", "RC", ("RCON6631",), "Noninterest-bearing deposits (domestic offices)"),
        Item("deposits_ib", "RC", ("RCON6636",), "Interest-bearing deposits (domestic offices)"),
        Item("brokered", "RCE", ("RCON2365",), "Total brokered deposits"),
    ),
    "cre": (
        Item("cons_1to4", "RCCI", ("RCFDF158", "RCONF158"), "1-4 family residential construction loans"),
        Item(
            "cons_other",
            "RCCI",
            ("RCFDF159", "RCONF159"),
            "Other construction loans and all land development and other land loans",
        ),
        Item(
            "multifamily", "RCCI", ("RCFD1460", "RCON1460"), "Loans secured by multifamily (5+) residential properties"
        ),
        Item(
            "nfnr_other",
            "RCCI",
            ("RCFDF161", "RCONF161"),
            "Loans secured by other (non-owner-occupied) nonfarm nonresidential properties",
        ),
        Item(
            "cre_unsecured",
            "RCCI",
            ("RCFD2746", "RCON2746"),
            "Loans to finance CRE, construction and land development, not secured by real estate",
        ),
        Item(
            "nfnr_owner",
            "RCCI",
            ("RCFDF160", "RCONF160"),
            "Loans secured by owner-occupied nonfarm nonresidential properties (not in CRE total)",
        ),
        Item("total_capital", "RCRI", ("RCFA3792", "RCOA3792"), "Total risk-based capital"),
        Item("tier1", "RCRI", ("RCFA8274", "RCOA8274"), "Tier 1 capital"),
        Item("acl", "RC", ("RCFD3123", "RCON3123"), "Allowance for credit losses on loans and leases"),
        Item("cblr", "RCRI", ("RCOALE74",), "Community bank leverage ratio framework elected (1 = yes)"),
    ),
    "asset_quality": (
        Item("pd_30_89", "RCN", ("RCFD1406", "RCON1406"), "Loans and leases past due 30-89 days, still accruing"),
        Item("pd_90", "RCN", ("RCFD1407", "RCON1407"), "Loans and leases past due 90 days or more, still accruing"),
        Item("nonaccrual", "RCN", ("RCFD1403", "RCON1403"), "Nonaccrual loans and leases"),
        Item("oreo", "RC", ("RCFD2150", "RCON2150"), "Other real estate owned"),
        Item(
            "total_loans",
            "RCCI",
            ("RCFD2122", "RCON2122"),
            "Total loans and leases, net of unearned income (held for investment and for sale)",
        ),
        Item("total_assets", "RC", ("RCFD2170", "RCON2170"), "Total assets"),
    ),
}
CODES_BY_SCHEDULE: dict[str, set[str]] = defaultdict(set)
for _items in SECTIONS.values():
    for _item in _items:
        CODES_BY_SCHEDULE[_item.schedule].update(_item.codes)
ITEM_BY_CODE = {code: item for items in SECTIONS.values() for item in items for code in item.codes}

SCHEDULE_FILE = re.compile(r"FFIEC CDR Call Schedule (\w+) (\d{8})(?:\((\d+) of (\d+)\))?\.txt$")
POR_FILE = re.compile(r"FFIEC CDR Call Bulk POR (\d{8})\.txt$")
EXTRACT_HEAD = re.compile(r"^FFIEC Call Report extract: (.+) \(IDRSSD (\d+), FDIC cert (\d+)\), (.*)$")


# --- periods ------------------------------------------------------------------------------------------------------
def period_date(period: str) -> date:
    """'06302026' (MMDDYYYY, the CDR's own format) -> date(2026, 6, 30)."""
    return date(int(period[4:]), int(period[:2]), int(period[2:4]))


def period_str(d: date) -> str:
    return f"{d.month:02d}{d.day:02d}{d.year}"


def period_label(period: str) -> str:
    return f"{period[:2]}/{period[2:4]}/{period[4:]}"


def short_date(period: str) -> str:
    d = period_date(period)
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def quarter_end_before(d: date, quarters: int = 1) -> date:
    month = d.month - 3 * quarters
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def choose_periods(available: list[str], today: date, lag_days: int = REPORT_LAG_DAYS) -> tuple[str, str, str | None]:
    """Latest quarter old enough to be complete, the quarter before it, and the same quarter a year earlier."""
    ready = sorted((p for p in available if period_date(p) + timedelta(days=lag_days) <= today), key=period_date)
    if not ready:
        raise ConnectorUnavailable("the CDR lists no Call Report quarter old enough to be complete")
    latest = ready[-1]
    prior = period_str(quarter_end_before(period_date(latest)))
    yoy = period_str(quarter_end_before(period_date(latest), 4))
    if prior not in available:
        raise ConnectorUnavailable(f"the CDR lists {period_label(latest)} but not the prior quarter {prior}")
    return latest, prior, yoy if yoy in available else None


# --- CDR bulk page and files ----------------------------------------------------------------------------------------
def hidden_fields(html: str | bytes) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    return {i["name"]: i.get("value", "") for i in soup.find_all("input", type="hidden") if i.get("name")}


def period_options(html: str | bytes) -> dict[str, str]:
    """Report dates listed in the page's dropdown after the product postback: {'06302026': '152', ...}."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for opt in soup.select("#DatesDropDownList option"):
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", opt.get_text(strip=True))
        if m and opt.get("value"):
            out[f"{m.group(1)}{m.group(2)}{m.group(3)}"] = opt["value"]
    return out


def zip_doc_url(period: str) -> str:
    """Stable identifier for a stored bulk zip (the download is a form POST; the fragment names the selection)."""
    return f"{CDR_BULK_URL}#{PRODUCT}-TSV-{period}"


def extract_doc_url(period: str, rssd: str) -> str:
    return f"{CDR_BULK_URL}#{PRODUCT}-TSV-{period}-IDRSSD-{rssd}"


def facsimile_url(rssd: str | int, period: str) -> str:
    return FACSIMILE_URL.format(rssd=rssd, period=period)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _split(line: str) -> list[str]:
    return [c.strip().strip('"').strip() for c in line.rstrip("\r\n").split("\t")]


def read_bulk_zip(content: bytes, rssds: set[str] | None = None) -> dict[str, Any]:
    """Pull the items in SECTIONS (plus the POR panel of reporters) out of a single-period bulk zip.

    Returns {'period', 'por': {rssd: {...}}, 'rows': {rssd: {mdrm: value-as-reported}}, 'files': {schedule: [names]},
    'lines': {rssd: {file: line_no}}}. Values stay strings exactly as in the file ('' = not reported).
    """
    zf = zipfile.ZipFile(io.BytesIO(content))
    period = None
    por: dict[str, dict] = {}
    rows: dict[str, dict[str, str]] = defaultdict(dict)
    lines_at: dict[str, dict[str, int]] = defaultdict(dict)
    files: dict[str, list[str]] = defaultdict(list)
    for name in sorted(zf.namelist()):
        base = name.rsplit("/", 1)[-1]
        m_por, m_sched = POR_FILE.search(base), SCHEDULE_FILE.search(base)
        if m_por:
            period = m_por.group(1)
            lines = _decode(zf.read(name)).splitlines()
            header = _split(lines[0])
            for line in lines[1:]:
                cells = dict(zip(header, _split(line), strict=False))
                rssd = cells.get("IDRSSD", "")
                if rssd and (rssds is None or rssd in rssds):
                    por[rssd] = {
                        "rssd": rssd,
                        "cert": cells.get("FDIC Certificate Number", ""),
                        "name": cells.get("Financial Institution Name", ""),
                        "city": cells.get("Financial Institution City", ""),
                        "state": cells.get("Financial Institution State", ""),
                        "filing_type": cells.get("Financial Institution Filing Type", ""),
                        "updated": cells.get("Last Date/Time Submission Updated On", ""),
                    }
        elif m_sched and m_sched.group(1) in CODES_BY_SCHEDULE:
            schedule = m_sched.group(1)
            wanted = CODES_BY_SCHEDULE[schedule]
            files[schedule].append(base)
            lines = _decode(zf.read(name)).splitlines()
            header = _split(lines[0])
            cols = [(i, code) for i, code in enumerate(header) if code in wanted]
            for n, line in enumerate(lines[2:], start=3):  # line 2 holds the item descriptions
                cells = _split(line)
                rssd = cells[0] if cells else ""
                if not rssd or (rssds is not None and rssd not in rssds):
                    continue
                for i, code in cols:
                    rows[rssd][code] = cells[i] if i < len(cells) else ""
                lines_at[rssd][base] = n
    if period is None:
        raise ValueError("not a CDR single-period bulk zip (no POR file)")
    missing = {"RC", "RCCI", "RCN", "RCRI"} - set(files)
    if missing:
        raise ValueError(f"bulk zip {period} is missing schedules {sorted(missing)}")
    return {"period": period, "por": por, "rows": dict(rows), "files": dict(files), "lines": dict(lines_at)}


def zip_manifest(content: bytes) -> str:
    """Short text layer for a stored zip: the CDR readme and the file list (the data itself is in the extracts)."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    readme = next((_decode(zf.read(n)).strip() for n in zf.namelist() if n.lower().endswith("readme.txt")), "")
    listing = "\n".join(f"{i.file_size:>10}  {i.filename}" for i in zf.infolist())
    return f"{readme}\n\nFiles:\n{listing}".strip()


# --- per-bank extract -----------------------------------------------------------------------------------------------
def _pick_code(item: Item, rows: dict[str, dict[str, str]]) -> str:
    for code in item.codes:
        if any((r or {}).get(code, "") != "" for r in rows.values()):
            return code
    return item.codes[-1]


def render_extract(bank: dict, periods: list[str], rows: dict[str, dict[str, str]], sources: dict[str, dict]) -> str:
    """The stored text a bank's signals are computed from. Values are copied verbatim from the bulk files."""
    who = f"{bank['name']} (IDRSSD {bank['rssd']}, FDIC cert {bank['cert']})"
    out = [
        f"FFIEC Call Report extract: {who}, {bank.get('city', '')}, {bank.get('state', '')}",
        f'Source: FFIEC Central Data Repository public bulk data, "Call Reports -- Single Period" (tab-delimited), '
        f"{CDR_BULK_URL}",
        "Amounts in thousands of US dollars, exactly as reported in the bulk files. Blank = not reported.",
    ]
    for p in periods:
        src = sources.get(p) or {}
        out.append(f"{period_label(p)}: {src.get('filename', '')}, sha256 {src.get('sha256', '')}")
    for section, items in SECTIONS.items():
        out += [
            "",
            f"[{section}] {bank['name']} (IDRSSD {bank['rssd']}), Call Report items in thousands of USD",
            "\t".join(["Schedule", "MDRM", "Item", *(period_label(p) for p in periods)]),
        ]
        for item in items:
            code = _pick_code(item, rows)
            vals = [(rows.get(p) or {}).get(code, "") for p in periods]
            out.append("\t".join([item.schedule, code, item.label, *vals]))
    return "\n".join(out) + "\n"


def _num(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_extract(text: str) -> dict[str, Any]:
    """Inverse of render_extract: bank identity, periods, {item key: {period: value}} and each section's text."""
    lines = text.splitlines()
    m = EXTRACT_HEAD.match(lines[0]) if lines else None
    if not m:
        raise ValueError("not an FFIEC Call Report extract")
    bank = {"name": m.group(1), "rssd": m.group(2), "cert": m.group(3)}
    items: dict[str, dict[str, float | None]] = {}
    codes: dict[str, str] = {}
    sections: dict[str, str] = {}
    periods: list[str] = []
    i = 0
    while i < len(lines):
        sm = re.match(r"^\[(\w+)\] ", lines[i])
        if not sm:
            i += 1
            continue
        start = i
        cols = lines[i + 1].split("\t")
        periods = [c.replace("/", "") for c in cols[3:]]
        i += 2
        while i < len(lines) and lines[i].strip():
            cells = lines[i].split("\t")
            item = ITEM_BY_CODE.get(cells[1])
            if item:
                codes[item.key] = cells[1]
                items[item.key] = {p: _num(v) for p, v in zip(periods, cells[3:], strict=False)}
            i += 1
        sections[sm.group(1)] = "\n".join(lines[start:i])
    return {"bank": bank, "periods": periods, "items": items, "codes": codes, "sections": sections}


# --- metrics --------------------------------------------------------------------------------------------------------
def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


def bank_metrics(items: dict[str, dict[str, float | None]], period: str | None) -> dict[str, Any]:
    """Ratios for one quarter. CRE uses the 2006 interagency guidance definition; see cre_basis for the denominator."""
    if period is None:
        return {}

    def g(key: str) -> float | None:
        return (items.get(key) or {}).get(period)

    construction = _sum([g("cons_1to4"), g("cons_other")])
    cre = _sum([construction, g("multifamily"), g("nfnr_other"), g("cre_unsecured")])
    if g("total_capital"):
        capital, basis = g("total_capital"), "total risk-based capital"
    elif g("tier1") is not None:
        # CBLR banks don't report total risk-based capital; Tier 1 + ACL is the usual stand-in for the guideline.
        capital, basis = g("tier1") + (g("acl") or 0.0), "Tier 1 capital + ACL"
    else:
        capital, basis = None, None
    noncurrent = _sum([g("pd_90"), g("nonaccrual")])
    npa = _sum([noncurrent, g("oreo")])
    return {
        "net_loans": g("net_loans"),
        "loans_hfs": g("loans_hfs"),
        "deposits": g("deposits"),
        "deposits_nib": g("deposits_nib"),
        "brokered": g("brokered"),
        "nib_share": _ratio(g("deposits_nib"), g("deposits")),
        "brokered_share": _ratio(g("brokered"), g("deposits")),
        "cre": cre,
        "construction": construction,
        "capital": capital,
        "cre_basis": basis,
        "cre_ratio": _ratio(cre, capital),
        "construction_ratio": _ratio(construction, capital),
        "noncurrent": noncurrent,
        "total_loans": g("total_loans"),
        "noncurrent_ratio": _ratio(noncurrent, g("total_loans")),
        "npa": npa,
        "npa_assets": _ratio(npa, g("total_assets")),
        "total_assets": g("total_assets"),
    }


def fmt_usd_k(thousands: float | None) -> str:
    if thousands is None:
        return "n/a"
    v = thousands * 1000
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v / 1e3:,.0f}K"


def _pct(x: float | None) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def _share(x: float | None, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None else "n/a"


def _pts(cur: float | None, prior: float | None, digits: int = 1) -> str:
    if cur is None or prior is None:
        return "n/a"
    d = (cur - prior) * 100
    return "flat" if round(d, digits) == 0 else f"{d:+.{digits}f} pts"


def _delta(cur: float | None, prior: float | None) -> float | None:
    return None if cur is None or prior is None else cur - prior


# --- ticker -> bank map ---------------------------------------------------------------------------------------------
_ABBR = {
    "BCORP": "BANCORP",
    "BANCORPORATION": "BANCORP",
    "BANKCORP": "BANCORP",
    "BSHS": "BANCSHARES",
    "BSHRS": "BANCSHARES",
    "FINL": "FINANCIAL",
    "FNCL": "FINANCIAL",
    "CMTY": "COMMUNITY",
    "CMNTY": "COMMUNITY",
    "CNTY": "COUNTY",
    "NATL": "NATIONAL",
    "BK": "BANK",
    "HLDGS": "HOLDINGS",
    "HLDG": "HOLDINGS",
    "HOLDING": "HOLDINGS",
    "SVCS": "SERVICES",
    "SVC": "SERVICES",
    "GRP": "GROUP",
    "FED": "FEDERAL",
    "SVGS": "SAVINGS",
    "CORPORATION": "CORP",
    "INCORPORATED": "INC",
    "COMPANY": "CO",
    "AND": "&",
}
_DROP = {"INC", "CORP", "CO", "LTD", "LLC", "THE", "OF", "MHC", "NA", "N", "A", "SSB", "FSB"}
_CORP_SUFFIX = {"INC", "CORP", "CORPORATION", "CO", "LTD"}
# fmt: off
_STATES = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS", "CA": "CALIFORNIA", "CO": "COLORADO",
    "CT": "CONNECTICUT", "DE": "DELAWARE", "DC": "DISTRICT OF COLUMBIA", "FL": "FLORIDA", "GA": "GEORGIA",
    "HI": "HAWAII", "ID": "IDAHO", "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY",
    "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND", "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA",
    "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA", "NH": "NEW HAMPSHIRE",
    "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK", "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA",
    "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON", "PA": "PENNSYLVANIA", "PR": "PUERTO RICO", "RI": "RHODE ISLAND",
    "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA", "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT",
    "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA", "WI": "WISCONSIN", "WY": "WYOMING",
}
# fmt: on


def normalize_name(name: str | None) -> str:
    """Comparable core of a bank/holding-company name: FDIC abbreviations expanded, corporate suffixes dropped.

    'BANK OF THE JAMES FINANCIAL GROUP INC' and 'BK JAMES FINL GROUP INC' -> 'BANK JAMES FINANCIAL GROUP';
    'PEOPLES BCORP OF NC INC' -> 'PEOPLES BANCORP NORTH CAROLINA'; 'FIRST UNITED CORP/MD/' -> 'FIRST UNITED'.
    """
    n = (name or "").upper()
    n = re.sub(r"\s*/[A-Z]{2}/?\s*$", "", n)  # EDGAR state-of-incorporation tags: 'CORP/MD/', 'INC /PA/'
    n = n.replace("&", " & ")
    toks = re.findall(r"[A-Z0-9&]+", n)
    if len(toks) > 2 and toks[-1] in _STATES and toks[-2] in _CORP_SUFFIX:
        toks = toks[:-1]  # 'BROADWAY FINANCIAL CORP DE'
    out: list[str] = []
    for i, t in enumerate(toks):
        if i > 0 and toks[i - 1] == "OF" and t in _STATES:
            out.extend(_STATES[t].split())  # FDIC 'OF LA' / 'OF NC' -> state name
        else:
            out.append(_ABBR.get(t, t))
    return " ".join(t for t in out if t not in _DROP)


def holder_candidates(institutions: list[dict]) -> list[dict]:
    """One candidate per top-tier holding company (or per bank with no holding company), with its largest bank."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in institutions:
        if str(r.get("ACTIVE", 1)) not in ("1", "1.0"):
            continue
        key = f"hc:{r['RSSDHCR']}" if r.get("RSSDHCR") else f"bank:{r['CERT']}"
        groups[key].append(r)
    out = []
    for key, banks in groups.items():
        top = max(banks, key=lambda b: float(b.get("ASSET") or 0))
        is_hc = key.startswith("hc:")
        name = top.get("NAMEHCR") if is_hc else top.get("NAME")
        out.append(
            {
                "kind": "holding_company" if is_hc else "bank",
                "name": name,
                "norm": normalize_name(name),
                "state": (top.get("STALPHCR") or top.get("STALP")) if is_hc else top.get("STALP"),
                "bank_state": top.get("STALP"),
                "top": top,
                "n_banks": len(banks),
            }
        )
    return out


def _entry(c: dict, method: str, confidence: str, score: float, matched_on: str) -> dict:
    top = c["top"]
    return {
        "cert": int(top["CERT"]),
        "rssd": int(top["FED_RSSD"]),
        "bank": top.get("NAME"),
        "city": top.get("CITY"),
        "state": top.get("STALP"),
        "holding_company": top.get("NAMEHCR") or None,
        "holding_rssd": int(top["RSSDHCR"]) if top.get("RSSDHCR") else None,
        "banks_in_holding": c["n_banks"],
        "bank_assets_k": int(float(top.get("ASSET") or 0)),
        "fdic_repdte": top.get("REPDTE"),
        "match": method,
        "confidence": confidence,
        "score": round(float(score), 1),
        "matched_on": matched_on,
    }


def match_company(names: list[str | None], state: str | None, candidates: list[dict]) -> dict:
    """Match a holding company to an FDIC candidate. Returns a map entry, or {'reason': ...} when not confident."""
    queries = {normalize_name(n): n for n in names if n and normalize_name(n)}
    if not queries:
        return {"reason": "no company name to match"}

    def in_state(c: dict) -> bool:
        return bool(state) and state in (c["state"], c["bank_state"])

    exact = [c for c in candidates if c["norm"] in queries]
    if len(exact) == 1:
        c = exact[0]
        if not state or in_state(c):
            return _entry(c, "name_exact" + ("+state" if state else ""), "high", 100, queries[c["norm"]])
        return _entry(c, "name_exact_unique (state differs)", "medium", 100, queries[c["norm"]])
    if exact:
        states = sorted({c["state"] or "?" for c in exact})
        local = [c for c in exact if in_state(c)]
        if len(local) == 1:
            return _entry(local[0], "name_exact+state", "high", 100, queries[local[0]["norm"]])
        return {
            "reason": f"ambiguous: {len(exact)} FDIC holders named {exact[0]['name']!r} (states {', '.join(states)}) "
            f"and {'no unique one in ' + state if state else 'no state on file'}; add an override"
        }
    scored = sorted(
        ((max(fuzz.ratio(q, c["norm"]) for q in queries), c) for c in candidates), key=lambda x: x[0], reverse=True
    )
    if not scored:
        return {"reason": "no FDIC institutions to match against"}
    (s1, c1), (s2, _c2) = scored[0], scored[1] if len(scored) > 1 else (0.0, None)
    if s1 >= FUZZY_MIN and in_state(c1) and s1 - s2 >= FUZZY_MARGIN:
        return _entry(c1, "name_fuzzy+state", "medium", s1, next(iter(queries.values())))
    why = "state differs" if s1 >= FUZZY_MIN and not in_state(c1) else f"score below {FUZZY_MIN} or not unique"
    return {
        "reason": f"no confident match: best FDIC candidate {c1['name']!r} ({c1['state']}) scored {s1:.0f} ({why}); "
        "add an override if this is the right bank"
    }


def build_bank_map(
    companies: dict[str, dict], institutions: list[dict], existing: dict | None = None, today: date | None = None
) -> dict:
    """Map each ticker to its bank. Overrides win; existing auto matches are kept; everything else is (re)matched.

    `companies` is {ticker: {'name', 'sec_name', 'state'}}. Returns the full map, ready for save_bank_map().
    """
    data = {k: dict(v or {}) for k, v in (existing or {}).items() if k in ("overrides", "auto", "unmapped")}
    overrides, auto, unmapped = (data.setdefault(k, {}) for k in ("overrides", "auto", "unmapped"))
    active_certs = {int(r["CERT"]) for r in institutions if str(r.get("ACTIVE", 1)) in ("1", "1.0")}
    candidates = holder_candidates(institutions)
    for ticker, co in sorted(companies.items()):
        if ticker in overrides:
            auto.pop(ticker, None)
            unmapped.pop(ticker, None)
            continue
        if ticker in auto and (not active_certs or int(auto[ticker]["cert"]) in active_certs):
            continue
        auto.pop(ticker, None)
        result = match_company([co.get("sec_name"), co.get("name")], co.get("state"), candidates)
        if "reason" in result:
            unmapped[ticker] = {"name": co.get("sec_name") or co.get("name"), "reason": result["reason"]}
        else:
            auto[ticker] = result
            unmapped.pop(ticker, None)
    data["generated"] = (today or date.today()).isoformat()
    data["source"] = f"{FDIC_INSTITUTIONS_URL} (active institutions; NAMEHCR, STALPHCR, ASSET)"
    return {
        "generated": data["generated"],
        "source": data["source"],
        "overrides": dict(sorted(overrides.items())),
        "auto": dict(sorted(auto.items())),
        "unmapped": dict(sorted(unmapped.items())),
    }


def resolve_bank_map(data: dict) -> dict[str, dict]:
    """{ticker: entry} with manual overrides taking precedence over auto matches."""
    out = {t: {**e, "source": "auto"} for t, e in (data.get("auto") or {}).items()}
    out.update({t: {**e, "source": "override"} for t, e in (data.get("overrides") or {}).items()})
    return out


BANK_MAP_HEADER = """\
# Ticker -> FDIC-insured bank for the FFIEC Call Report connector (src/localledger/connectors/ffiec.py).
#
# overrides: set by hand; they always win. Give cert (FDIC certificate), rssd (Federal Reserve RSSD ID), bank, and a
#            basis saying where you verified it (e.g. the 10-K's description of the bank subsidiary).
# auto:      written by build_bank_map() on each run for tickers not yet mapped. It matches the holding company's EDGAR
#            name (and the universe name) and business state against active FDIC BankFind institutions (holding-company
#            name NAMEHCR, state STALPHCR/STALP). When a holding company owns several banks, the largest by total
#            assets is used. Exact name matches (after expanding FDIC abbreviations like BCORP/FINL) are accepted;
#            fuzzy matches only with the same state and a clear margin.
# unmapped:  tickers with no confident match, and why. Low-confidence matches are never accepted. Fix with an override.
#
# Delete an auto entry to force a re-match on the next run.
"""


def load_bank_map(path: Path) -> dict:
    if not path.exists():
        return {"overrides": {}, "auto": {}, "unmapped": {}}
    data = yaml.safe_load(path.read_text()) or {}
    for k in ("overrides", "auto", "unmapped"):
        data[k] = data.get(k) or {}
    return data


def save_bank_map(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=200, default_flow_style=None)
    path.write_text(BANK_MAP_HEADER + "\n" + body)


# --- connector ------------------------------------------------------------------------------------------------------
class FfiecConnector(Connector):
    source_id = "ffiec"
    name = "FFIEC Call Reports (CDR bulk data)"
    type = "bank_financials"
    access_method = (
        "FFIEC CDR public bulk data (cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx): 'Call Reports -- Single Period' "
        "tab-delimited zip per quarter via the page's ASP.NET form postbacks (no login). Ticker -> bank map from the "
        "FDIC BankFind institutions API (api.fdic.gov/banks/institutions, no key) in config/bank_map.yaml. Citations "
        "open the bank's Call Report facsimile on cdr.ffiec.gov (ViewFacsimileDirect.aspx)."
    )
    cadence = "quarterly (Call Reports due 30 days after quarter end; used ~45 days after)"
    terms_note = (
        "Public US bank regulatory data (FFIEC CDR Public Data Distribution); no login, key or registration. "
        "Declared User-Agent, 1 request/second, 3 requests per quarter downloaded. FDIC BankFind API is public."
    )

    def __init__(
        self,
        *args: Any,
        bank_map_path: Path | None = None,
        today: date | None = None,
        periods: tuple[str, str, str | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.bank_map_path = bank_map_path or self.settings.config_dir / "bank_map.yaml"
        self.today = today or date.today()
        self.periods = periods
        self.bank_map: dict[str, dict] = {}
        self._form: dict[str, str] = {}
        self._options: dict[str, str] = {}
        self._bulk: dict[str, tuple[Document, dict]] = {}

    # --- discover -------------------------------------------------------------------------------
    def _banks_in_scope(self) -> dict[str, dict]:
        return {
            t: {"name": c.name, "sec_name": c.sec_name, "state": c.state}
            for t, c in self.universe().items()
            if c.archetype == ARCHETYPE
        }

    def discover(self) -> list[tuple[str, str]]:
        companies = self._banks_in_scope()
        if not companies:
            raise ConnectorUnavailable(f"no '{ARCHETYPE}' companies in the selected universe")
        self.bank_map = self.refresh_bank_map(companies)
        if not self.bank_map:
            raise ConnectorUnavailable("no community bank is mapped to an FDIC bank; see config/bank_map.yaml")
        self._list_periods()
        if self.periods is None:
            self.periods = choose_periods(list(self._options), self.today)
        # Commit now: run() rolls back after a failed target, which would otherwise also discard the (only flushed)
        # Source row and the FDIC institutions document saved above.
        self.session.commit()
        targets = [("period", p) for p in self.periods if p]
        return targets + [("bank", t) for t in sorted(self.bank_map)]

    def refresh_bank_map(self, companies: dict[str, dict]) -> dict[str, dict]:
        """Load config/bank_map.yaml, auto-match any unmapped tickers against FDIC BankFind, and persist."""
        data = load_bank_map(self.bank_map_path)
        known = set(data["overrides"]) | set(data["auto"])
        if set(companies) - known:
            try:
                institutions = self._fetch_institutions()
            except (FetchError, RobotsDisallowed, ValueError, KeyError) as exc:
                if not known & set(companies):
                    raise ConnectorUnavailable(f"FDIC BankFind institutions API unavailable: {exc}") from exc
                institutions = None  # keep going with the banks already mapped
            if institutions:
                data = build_bank_map(companies, institutions, data, self.today)
                save_bank_map(self.bank_map_path, data)
        resolved = resolve_bank_map(data)
        return {t: e for t, e in resolved.items() if t in companies}

    def _fetch_institutions(self) -> list[dict]:
        params = {
            "filters": "ACTIVE:1",
            "fields": FDIC_FIELDS,
            "sort_by": "CERT",
            "sort_order": "ASC",
            "limit": 10000,
        }
        url = str(httpx.URL(FDIC_INSTITUTIONS_URL, params=params))
        resp = self.http.get_ok(url)
        payload = resp.json()
        rows = [r["data"] for r in payload.get("data", [])]
        total = (payload.get("meta") or {}).get("total")
        if not rows or (total and total > len(rows)):
            raise ValueError(f"FDIC institutions response has {len(rows)} of {total} rows")
        self.save(
            url=url,
            content=resp.content,
            content_type="application/json",
            title="FDIC BankFind active institutions",
            meta={"kind": "institutions", "rows": len(rows)},
        )
        return rows

    def _list_periods(self) -> None:
        """GET the bulk page, post back the product choice, and keep the form state plus the listed report dates."""
        try:
            page = self.http.get(CDR_BULK_URL)
            self._check_page(page)
            form = hidden_fields(page.text)
            if "__VIEWSTATE" not in form:
                raise ConnectorUnavailable("CDR bulk page has no __VIEWSTATE (layout changed?)")
            data = {**form, "__EVENTTARGET": F_PRODUCT, "__EVENTARGUMENT": "", F_PRODUCT: PRODUCT}
            resp = self.http.post(CDR_BULK_URL, data=data)
            self._check_page(resp)
        except RobotsDisallowed as exc:
            raise ConnectorUnavailable(f"robots.txt disallows {exc}") from exc
        except FetchError as exc:
            raise ConnectorUnavailable(f"FFIEC CDR unreachable: {exc}") from exc
        self._form = hidden_fields(resp.text)
        self._options = period_options(resp.text)
        if not self._options:
            raise ConnectorUnavailable("CDR bulk page listed no Call Report dates for 'Call Reports -- Single Period'")

    @staticmethod
    def _check_page(resp: httpx.Response) -> None:
        if resp.status_code != 200:
            raise ConnectorUnavailable(
                f"FFIEC CDR returned HTTP {resp.status_code} for {CDR_BULK_URL}"
                + (" (looks like a bot challenge; not bypassed)" if resp.status_code in (202, 403, 429) else "")
            )
        if "text/html" not in resp.headers.get("content-type", "text/html"):
            raise ConnectorUnavailable(f"FFIEC CDR returned {resp.headers.get('content-type')} instead of the page")

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: tuple[str, str]) -> list[Document]:
        kind, value = target
        if kind == "period":
            return [self._fetch_period(value)]
        return self._build_extract(value)

    def _fetch_period(self, period: str) -> Document:
        url = zip_doc_url(period)
        existing = self.session.exec(select(Document).where(Document.url == url).order_by(Document.id.desc())).first()
        if existing is not None and period != self.periods[0]:
            return existing  # closed quarters rarely change; re-download only the latest each run
        if period not in self._options:
            raise ValueError(f"the CDR does not list {period_label(period)}")
        data = {
            **self._form,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            F_PRODUCT: PRODUCT,
            F_DATES: self._options[period],
            F_FORMAT: "TSVRadioButton",
            F_DOWNLOAD: "Download",
        }
        resp = self.http.post(CDR_BULK_URL, data=data)
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} downloading the {period_label(period)} bulk zip")
        if not zipfile.is_zipfile(io.BytesIO(resp.content)):
            raise FetchError(
                f"CDR returned {resp.headers.get('content-type')} instead of a zip for {period_label(period)} "
                "(expired form state or a challenge page; not bypassed)"
            )
        m = re.search(r'filename="?([^";]+)"?', resp.headers.get("content-disposition", ""))
        filename = m.group(1) if m else f"FFIEC CDR Call Bulk All Schedules {period}.zip"
        return self.save(
            url=url,
            content=resp.content,
            content_type="application/zip",
            title=filename,
            meta={"kind": "bulk_zip", "period": period, "filename": filename, "product": PRODUCT, "format": "TSV"},
            text=zip_manifest(resp.content),
        )

    def _build_extract(self, ticker: str) -> list[Document]:
        entry = self.bank_map[ticker]
        rssd = str(entry["rssd"])
        latest = self.periods[0]
        periods = [p for p in self.periods if p]
        if latest not in self._bulk:
            raise RuntimeError(f"bulk data for {period_label(latest)} was not loaded")
        rows = {p: self._bulk[p][1]["rows"].get(rssd) for p in periods if p in self._bulk}
        if not rows.get(latest):
            raise ValueError(f"IDRSSD {rssd} ({entry.get('bank')}) is not in the {period_label(latest)} bulk file")
        bank = dict(self._bulk[latest][1]["por"].get(rssd) or {"rssd": rssd, "cert": entry.get("cert"), "name": ""})
        if str(bank.get("cert")) != str(entry.get("cert")):
            raise ValueError(f"bank_map cert {entry.get('cert')} != CDR cert {bank.get('cert')} for IDRSSD {rssd}")
        sources = {}
        for p in periods:
            if p in self._bulk:
                doc, parsed = self._bulk[p]
                sources[p] = {
                    "document_id": doc.id,
                    "filename": json.loads(doc.meta_json or "{}").get("filename") or doc.title,
                    "sha256": doc.sha256,
                    "lines": parsed["lines"].get(rssd, {}),
                }
        text = render_extract(bank, periods, rows, sources)
        doc = self.save(
            url=extract_doc_url(latest, rssd),
            content=text.encode("utf-8"),
            content_type="text/plain",
            ticker=ticker,
            title=f"FFIEC Call Report extract {ticker} {entry.get('bank')} {period_label(latest)}",
            meta={
                "kind": "extract",
                "rssd": rssd,
                "cert": bank.get("cert"),
                "periods": periods,
                "sources": sources,
                "facsimile_url": facsimile_url(rssd, latest),
            },
            text=text,
        )
        return [doc]

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json or "{}")
        if meta.get("kind") == "bulk_zip":
            rssds = {str(e["rssd"]) for e in self.bank_map.values()}
            self._bulk[meta["period"]] = (doc, read_bulk_zip(read_document_bytes(doc, self.settings), rssds))
            return []
        if meta.get("kind") != "extract":
            return []
        parsed = parse_extract(read_document_text(doc, self.settings))
        return [
            {
                "kind": "bank",
                "ticker": doc.ticker,
                "document_id": doc.id,
                "rssd": parsed["bank"]["rssd"],
                "cert": parsed["bank"]["cert"],
                "periods": meta.get("periods") or parsed["periods"],
                "items": parsed["items"],
                "sections": parsed["sections"],
                "sources": meta.get("sources") or {},
            }
        ]

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        drafts: list[SignalDraft] = []
        companies = self.universe()
        for r in records:
            if r.get("kind") != "bank":
                continue
            periods = r["periods"] + [None] * (3 - len(r["periods"]))
            cur_p, pri_p, yoy_p = periods[:3]
            cur, pri, yoy = (bank_metrics(r["items"], p) for p in (cur_p, pri_p, yoy_p))
            if not pri:
                continue
            entry = self.bank_map.get(r["ticker"]) or {}
            company = companies.get(r["ticker"])
            holder = company.name if company else r["ticker"]
            bank = " ".join((entry.get("bank") or "").split())  # FDIC names sometimes carry double spaces
            who = holder if not bank or normalize_name(bank) == normalize_name(holder) else f"{holder} ({bank})"
            ctx = {
                "r": r,
                "who": who,
                "cur": cur,
                "pri": pri,
                "yoy": yoy,
                "periods": (cur_p, pri_p, yoy_p),
                "base": {
                    "rssd": r["rssd"],
                    "cert": r["cert"],
                    "bank": bank,
                    "holding_company": entry.get("holding_company"),
                    "map_source": entry.get("source"),
                    "period": cur_p,
                    "prior_period": pri_p,
                    "yoy_period": yoy_p,
                    "prior_document_id": r["document_id"],  # prior-quarter values are columns of the same extract
                    "prior_url": facsimile_url(r["rssd"], pri_p),
                    "source_documents": {p: s.get("document_id") for p, s in r["sources"].items()},
                },
            }
            for build in (self._loans, self._deposits, self._cre, self._asset_quality):
                draft = build(ctx)
                if draft is not None:
                    drafts.append(draft)
        return drafts

    def _draft(self, ctx: dict, metric: str, section: str, value, prior, unit, sig, summary, extra) -> SignalDraft:
        r, (cur_p, _pri_p, _yoy_p) = ctx["r"], ctx["periods"]
        return SignalDraft(
            ticker=r["ticker"],
            metric=metric,
            as_of=period_date(cur_p),
            period=period_date(cur_p).isoformat(),
            value=value,
            prior_value=prior,
            unit=unit,
            significance=round(max(0.0, min(1.0, sig)), 4),
            citation_url=facsimile_url(r["rssd"], cur_p),
            document_id=r["document_id"],
            summary=summary,
            detail={
                **ctx["base"],
                "pct_change": pct_change(value, prior),
                **extra,
                "excerpt": r["sections"][section],
            },
        )

    def _loans(self, ctx: dict) -> SignalDraft | None:
        cur, pri, yoy = ctx["cur"], ctx["pri"], ctx["yoy"]
        if cur.get("net_loans") is None or pri.get("net_loans") is None:
            return None
        qoq = pct_change(cur["net_loans"], pri["net_loans"])
        yoy_pct = pct_change(cur["net_loans"], yoy.get("net_loans")) if yoy else None
        # Net loans exclude loans held for sale, so a reclass between HFS and HFI looks like growth. When HFS is
        # material, score the move on total loans (HFI + HFS) instead and say so.
        total_qoq = pct_change(cur.get("total_loans"), pri.get("total_loans"))
        hfs_share = max(
            _ratio(cur.get("loans_hfs"), cur.get("total_loans")) or 0.0,
            _ratio(pri.get("loans_hfs"), pri.get("total_loans")) or 0.0,
        )
        hfs_material = hfs_share >= 0.05 and total_qoq is not None
        sig = move_significance(total_qoq if hfs_material else qoq, floor=0.02, full=0.15)
        if yoy_pct is not None and not hfs_material:
            sig = max(sig, move_significance(yoy_pct, floor=0.08, full=0.4) * 0.8)
        cur_p = ctx["periods"][0]
        hfs_note = (
            f"; total loans incl. held for sale {_pct(total_qoq)} q/q"
            if hfs_material and abs(total_qoq - (qoq or 0)) >= 0.01
            else ""
        )
        summary = (
            f"{ctx['who']}: net loans & leases {fmt_usd_k(cur['net_loans'])} at {short_date(cur_p)}, "
            f"{_pct(qoq)} q/q" + (f", {_pct(yoy_pct)} y/y" if yoy_pct is not None else "") + hfs_note + "."
        )
        return self._draft(
            ctx,
            "net_loans_qoq",
            "loans",
            cur["net_loans"] * 1000,
            pri["net_loans"] * 1000,
            "USD",
            sig,
            summary,
            {
                "yoy_pct_change": yoy_pct,
                "yoy_value": yoy["net_loans"] * 1000 if yoy and yoy.get("net_loans") is not None else None,
                "total_loans_pct_change": total_qoq,
                "held_for_sale_share": hfs_share,
            },
        )

    def _deposits(self, ctx: dict) -> SignalDraft | None:
        cur, pri, yoy = ctx["cur"], ctx["pri"], ctx["yoy"]
        if cur.get("nib_share") is None or pri.get("nib_share") is None:
            return None
        dep_qoq = pct_change(cur["deposits"], pri["deposits"])
        nib_d, brk_d = _delta(cur["nib_share"], pri["nib_share"]), _delta(cur["brokered_share"], pri["brokered_share"])
        sig = max(
            move_significance(nib_d, floor=0.01, full=0.08),
            move_significance(dep_qoq, floor=0.03, full=0.25) * 0.9,
            move_significance(brk_d, floor=0.01, full=0.08),
        )
        if (cur.get("brokered_share") or 0) >= 0.15:
            sig = max(sig, 0.45)  # heavy reliance on brokered funding
        cur_p = ctx["periods"][0]
        brokered = (
            f"; brokered {_share(cur['brokered_share'])} ({_pts(cur['brokered_share'], pri['brokered_share'])})"
            if cur.get("brokered_share") is not None
            else ""
        )
        summary = (
            f"{ctx['who']}: noninterest-bearing deposits {_share(cur['nib_share'])} of total deposits at "
            f"{short_date(cur_p)} ({_pts(cur['nib_share'], pri['nib_share'])} q/q){brokered}; total deposits "
            f"{fmt_usd_k(cur['deposits'])}, {_pct(dep_qoq)} q/q."
        )
        return self._draft(
            ctx,
            "deposit_mix",
            "deposits",
            cur["nib_share"],
            pri["nib_share"],
            "share of deposits",
            sig,
            summary,
            {
                "change_pts": nib_d * 100 if nib_d is not None else None,
                "brokered_share": cur.get("brokered_share"),
                "prior_brokered_share": pri.get("brokered_share"),
                "deposits_usd": cur["deposits"] * 1000 if cur.get("deposits") is not None else None,
                "deposits_pct_change": dep_qoq,
                "yoy_nib_share": yoy.get("nib_share") if yoy else None,
            },
        )

    def _cre(self, ctx: dict) -> SignalDraft | None:
        cur, pri, yoy = ctx["cur"], ctx["pri"], ctx["yoy"]
        if cur.get("cre_ratio") is None or pri.get("cre_ratio") is None:
            return None
        v, p = cur["cre_ratio"], pri["cre_ratio"]
        d = v - p
        sig = move_significance(d, floor=0.05, full=0.5) * 0.8
        flags = []
        if v >= CRE_GUIDELINE:
            sig = max(sig, 0.5)
            if p < CRE_GUIDELINE:
                sig, flags = max(sig, 0.8), [*flags, "crossed above the 300% guideline"]
            else:
                flags.append("above the 300% guideline")
        elif p >= CRE_GUIDELINE:
            sig, flags = max(sig, 0.55), [*flags, "fell below the 300% guideline"]
        cv, cp = cur.get("construction_ratio"), pri.get("construction_ratio")
        if cv is not None and cv >= CONSTRUCTION_GUIDELINE:
            sig = max(sig, 0.7 if (cp or 0) < CONSTRUCTION_GUIDELINE else 0.5)
            flags.append("construction above the 100% guideline")
        basis_note = "" if cur["cre_basis"] == pri["cre_basis"] else f"; prior quarter used {pri['cre_basis']}"
        cur_p = ctx["periods"][0]
        summary = (
            f"{ctx['who']}: CRE loans {v:.0%} of {cur['cre_basis']} at {short_date(cur_p)} "
            f"({d * 100:+.0f} pts q/q{', ' + ', '.join(flags) if flags else ''}); construction & land "
            f"{_share(cv, 0)}{basis_note}."
        )
        return self._draft(
            ctx,
            "cre_concentration",
            "cre",
            v,
            p,
            "ratio to capital",
            sig,
            summary,
            {
                "change_pts": d * 100,
                "cre_basis": cur["cre_basis"],
                "prior_cre_basis": pri["cre_basis"],
                "cre_definition": "construction & land development + multifamily + non-owner-occupied nonfarm "
                "nonresidential + CRE loans not secured by real estate (2006 interagency CRE guidance)",
                "cre_usd": cur["cre"] * 1000 if cur.get("cre") is not None else None,
                "cre_pct_change": pct_change(cur.get("cre"), pri.get("cre")),
                "cre_yoy_pct_change": pct_change(cur.get("cre"), yoy.get("cre")) if yoy else None,
                "construction_ratio": cv,
                "prior_construction_ratio": cp,
                "yoy_cre_ratio": yoy.get("cre_ratio") if yoy else None,
                "guideline": CRE_GUIDELINE,
            },
        )

    def _asset_quality(self, ctx: dict) -> SignalDraft | None:
        cur, pri, yoy = ctx["cur"], ctx["pri"], ctx["yoy"]
        if cur.get("noncurrent_ratio") is None or pri.get("noncurrent_ratio") is None:
            return None
        v, p = cur["noncurrent_ratio"], pri["noncurrent_ratio"]
        d = v - p
        nc_pct = pct_change(cur["noncurrent"], pri["noncurrent"])
        sig = move_significance(nc_pct, floor=0.2, full=2.0) * 0.7
        if max(v, p) < 0.005:
            sig *= 0.6  # big % moves on a tiny noncurrent book
        if d >= 0.005:
            sig = max(sig, 0.75)
        elif d >= 0.0025 and (nc_pct or 0) >= 1.0:
            sig = max(sig, 0.65)
        if v >= 0.02:
            sig = max(sig, 0.7)
        elif v >= 0.01:
            sig = max(sig, 0.5)
        cur_p, pri_p = ctx["periods"][:2]
        summary = (
            f"{ctx['who']}: noncurrent loans {_share(v, 2)} of loans at {short_date(cur_p)} vs {_share(p, 2)} at "
            f"{short_date(pri_p)} ({fmt_usd_k(cur['noncurrent'])} vs {fmt_usd_k(pri['noncurrent'])}); NPAs incl. "
            f"OREO {_share(cur.get('npa_assets'), 2)} of assets."
        )
        return self._draft(
            ctx,
            "noncurrent_loans",
            "asset_quality",
            v,
            p,
            "share of loans",
            sig,
            summary,
            {
                "change_pts": d * 100,
                "noncurrent_usd": cur["noncurrent"] * 1000 if cur.get("noncurrent") is not None else None,
                "noncurrent_pct_change": nc_pct,
                "npa_assets": cur.get("npa_assets"),
                "prior_npa_assets": pri.get("npa_assets"),
                "yoy_noncurrent_ratio": yoy.get("noncurrent_ratio") if yoy else None,
            },
        )
