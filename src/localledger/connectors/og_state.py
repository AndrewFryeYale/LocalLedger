"""State oil & gas production: New Mexico Oil Conservation Division (OCD) C-115 data, via NM Tech's GO-TECH site.

Access (verified Oct 2026, see SOURCES.md):
- https://octane.nmt.edu/gotech/Petroleum_Data/county/{County}{yyyy}.zip   one CSV per county and year: every
  well's monthly oil (bbl), gas (Mcf), water and producing days (Oil1..Oil12, Gas1..Gas12, ...) with its operator of
  record (OgridName), county and pool. Built by the NM Tech Petroleum Recovery Research Center from OCD data and
  refreshed nightly; production months lag ~45 days. No login; octane.nmt.edu has no robots.txt (404 = allow).
- The OCD's own report pages (wwwapps.emnrd.nm.gov/OCD/OCDPermitting/Reporting/Production/...) answer with a
  Cloudflare Turnstile "human verification" page, so we don't use them (see BLOCKERS.md).
Operators and counties are mapped to tickers in config/og_map.yaml. For royalty trusts the connector also reads the
last declared distribution from the 8-K Ex. 99.1 the EDGAR connector stored and states the direction the next one
is implied to take from volumes alone (prices and costs are not modeled).
"""

from __future__ import annotations

import csv
import heapq
import io
import json
import logging
import re
import statistics
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import yaml
from sqlmodel import col, select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.http import FetchError, RobotsDisallowed, read_document_bytes, read_document_text
from localledger.models import Document, utcnow
from localledger.signals.scoring import move_significance, pct_change, scale_by_volume
from localledger.signals.store import SignalDraft

log = logging.getLogger(__name__)

BASE = "https://octane.nmt.edu/gotech/Petroleum_Data"
COUNTY_PAGE = f"{BASE}/county.aspx"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
UNITS = {"gas": "Mcf", "oil": "bbl"}
TOP_ROWS = 3  # largest operator rows per month quoted verbatim in excerpts
COMPLETE_SHARE = 0.9  # a month counts as reported once >=90% of the usual number of rows report volumes
REUSE_DAYS = 31  # closed-year files change little; reuse a recent stored copy instead of re-downloading
FLAT_BAND = 0.03  # volume moves within +/-3% read as "about flat" for the implied-distribution call
MIN_MONTHLY = {"gas": 50_000, "oil": 5_000}  # below this monthly volume, damp significance (tiny bases)
OUTLIER_X = 3.0  # a month >=3x (or <=1/3) its trailing 12-month median is flagged in the summary
CHALLENGE_MARKERS = (b"challenges.cloudflare.com", b"Human Verification Required", b"cf-turnstile", b"captcha")


def county_url(county: str, year: int) -> str:
    return f"{BASE}/county/{quote(county)}{year}.zip"


# Months are integers (year * 12 + month - 1) so windows are plain ranges.
def ym(year: int, month: int) -> int:
    return year * 12 + month - 1


def ym_year(m: int) -> int:
    return m // 12


def ym_label(m: int) -> str:
    return f"{MONTHS[m % 12]} {m // 12}"


def ym_iso(m: int) -> str:
    return f"{m // 12}-{m % 12 + 1:02d}"


def span_label(months: range) -> str:
    first, last = months[0], months[-1]
    if first == last:
        return ym_label(last)
    if ym_year(first) == ym_year(last):
        return f"{MONTHS[first % 12]}-{ym_label(last)}"
    return f"{ym_label(first)}-{ym_label(last)}"


def month_end(m: int) -> date:
    nxt = date(ym_year(m + 1), (m + 1) % 12 + 1, 1)
    return nxt - timedelta(days=1)


def parse_month(text: str | None) -> int | None:
    """'July 2026' -> month int."""
    if not text:
        return None
    try:
        d = datetime.strptime(text.strip(), "%B %Y")
    except ValueError:
        return None
    return ym(d.year, d.month)


def years_needed(today: date) -> list[int]:
    """Years covering the latest reported month (~2-3 months back) and the 14 months before it."""
    now = ym(today.year, today.month)
    return list(range(ym_year(now - 17), today.year + 1))


def is_challenge(body: bytes) -> bool:
    head = body[:20_000].lower()
    return any(marker.lower() in head for marker in CHALLENGE_MARKERS)


def unzip_csv(content: bytes) -> tuple[str, str]:
    """(member name, CSV text) of a GO-TECH county-year zip; line endings normalized to \\n."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("zip has no CSV member")
        return names[0], z.read(names[0]).decode("utf-8", errors="replace").replace("\r\n", "\n")


def _num(value: str) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def parse_rows(text: str) -> tuple[str, dict[str, int], list[tuple[str, list[str]]]]:
    """(header line, column index, [(verbatim line, parsed row)]) of a GO-TECH CSV (one well-pool per line)."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("empty GO-TECH CSV")
    header = lines[0]
    cols = next(csv.reader([header]))
    idx = {name.strip(): i for i, name in enumerate(cols)}
    needed = ["OgridName", "CountyName", "Year"] + [f"{p}{m}" for p in ("Oil", "Gas") for m in range(1, 13)]
    missing = [c for c in needed if c not in idx]
    if missing:
        raise ValueError(f"unexpected GO-TECH CSV header (missing {', '.join(missing[:4])})")
    rows = list(zip(lines[1:], csv.reader(lines[1:]), strict=True))
    return header, idx, [(line, row) for line, row in rows if len(row) == len(cols)]


def aggregate(rows: list[tuple[str, list[str]]], idx: dict[str, int], operators: list[str], primary: str, year: int):
    """Monthly oil/gas totals for one operator set in one county-year file, plus its largest rows (verbatim)."""
    ops = {o.strip().upper() for o in operators}
    oil, gas, reporting = [0.0] * 12, [0.0] * 12, [0] * 12
    top: list[list[tuple[float, int, str]]] = [[] for _ in range(12)]
    n_rows = 0
    for n, (line, row) in enumerate(rows):
        if row[idx["OgridName"]].strip().upper() not in ops or row[idx["Year"]].strip() != str(year):
            continue
        n_rows += 1
        for m in range(12):
            o, g = _num(row[idx[f"Oil{m + 1}"]]), _num(row[idx[f"Gas{m + 1}"]])
            oil[m] += o
            gas[m] += g
            if o or g:
                reporting[m] += 1
            vol = g if primary == "gas" else o
            if vol > 0:
                item = (vol, -n, line)
                if len(top[m]) < TOP_ROWS:
                    heapq.heappush(top[m], item)
                elif item > top[m][0]:
                    heapq.heapreplace(top[m], item)
    return {
        "oil": oil,
        "gas": gas,
        "reporting": reporting,
        "rows": n_rows,
        "top": [[line for _, _, line in sorted(t, reverse=True)] for t in top],
    }


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (or line) of `text` around [start, end), verbatim."""
    dot, nl = text.rfind(". ", 0, start), text.rfind("\n", 0, start)
    lo = max(dot + 2 if dot != -1 else 0, nl + 1)
    stops = [text.find("\n", end)]
    if (dot_end := text.find(". ", end)) != -1:
        stops.append(dot_end + 1)
    stops = [i for i in stops if i != -1]
    return text[lo : min(stops) if stops else len(text)].strip()


def parse_declaration(text: str) -> dict[str, Any]:
    """A royalty trust's monthly distribution release (8-K Ex. 99.1): what was declared and what it rested on.

    amount: $ per unit (0.0 when the release declares no distribution; None when neither can be read).
    month / production_month: the distribution month and the production month it relates to (month ints).
    sentences: the verbatim sentences the fields were read from, for excerpts.
    """
    out: dict[str, Any] = {
        "amount": None,
        "month": None,
        "production_month": None,
        "excess_costs_gross": None,
        "trust_gas": None,
        "sentences": [],
    }
    spans: list[tuple[int, int]] = []
    head = text[:3000]
    if m := re.search(r"[^\n]*Distribution for\s+(?:the month of\s+)?([A-Z][a-z]+ \d{4})", head):
        out["month"] = parse_month(m.group(1))
        spans.append(m.span())
    if m := re.search(r"\$\s?(\d*\.\d{3,})\s+per\s+unit", text, re.IGNORECASE):
        out["amount"] = float(m.group(1))
        spans.append(m.span())
    elif m := re.search(r"\b(?:no|not declare a)\s+(?:monthly\s+)?(?:cash\s+)?distribution", head, re.IGNORECASE):
        out["amount"] = 0.0
        spans.append(m.span())
    if m := re.search(r"excess production costs is currently approximately \$([\d,]+) gross", text, re.IGNORECASE):
        out["excess_costs_gross"] = int(m.group(1).replace(",", ""))
        spans.append(m.span())
    if m := re.search(r"production month of ([A-Z][a-z]+ \d{4})", text):
        out["production_month"] = parse_month(m.group(1))
        spans.append(m.span())
    pattern = (
        r"gas volumes for the Subject Interests for ([A-Z][a-z]+ \d{4}) totaled ([\d,]+) Mcf"
        r".{0,80}?as compared to ([\d,]+) Mcf.{0,80}?for ([A-Z][a-z]+ \d{4})"
    )
    if m := re.search(pattern, text, re.DOTALL):
        out["trust_gas"] = {
            "month": parse_month(m.group(1)),
            "mcf": int(m.group(2).replace(",", "")),
            "prior_month": parse_month(m.group(4)),
            "prior_mcf": int(m.group(3).replace(",", "")),
        }
        out["production_month"] = out["production_month"] or out["trust_gas"]["month"]
        spans.append(m.span())
    seen: list[str] = []
    for start, end in sorted(spans):
        s = _sentence(text, start, end)
        if s and s not in seen:
            seen.append(s)
    out["sentences"] = seen
    return out


class Series:
    """One ticker's monthly operator totals across its mapped counties, built from per county-year records."""

    def __init__(self, entry: dict, recs: dict[tuple[str, int], dict]) -> None:
        self.counties = list(entry["counties"])
        self.primary = entry.get("primary", "gas")
        self.recs = recs

    def has(self, m: int) -> bool:
        return all((c, ym_year(m)) in self.recs for c in self.counties)

    def get(self, m: int, product: str | None = None) -> float | None:
        if not self.has(m):
            return None
        product = product or self.primary
        return sum(self.recs[(c, ym_year(m))][product][m % 12] for c in self.counties)

    def total(self, months: range, product: str | None = None) -> float | None:
        vals = [self.get(m, product) for m in months]
        return None if any(v is None for v in vals) else sum(vals)

    def reporting(self, m: int) -> int:
        return sum(self.recs[(c, ym_year(m))]["reporting"][m % 12] for c in self.counties) if self.has(m) else 0

    def latest_complete(self) -> int | None:
        """Latest month reported about as fully as the six before it (the newest month fills in late).

        Complete = at least 90% of the usual number of rows reporting, or at least 90% of the usual volume (a few
        shut-in wells at a small operator shouldn't hide an otherwise fully reported month).
        """
        months = sorted({ym(y, mo + 1) for (_, y) in self.recs for mo in range(12)})
        for m in reversed([m for m in months if self.reporting(m) and self.get(m)]):
            prev = [p for p in range(m - 6, m) if self.reporting(p)]
            if not prev:
                continue
            usual_rows = statistics.median(self.reporting(p) for p in prev)
            usual_vol = statistics.median(self.get(p) or 0.0 for p in prev)
            if self.reporting(m) >= COMPLETE_SHARE * usual_rows or self.get(m) >= COMPLETE_SHARE * usual_vol:
                return m
        return None

    def outliers(self, months: range, product: str) -> list[str]:
        """Months whose volume is >=3x or <=1/3 of the trailing 12-month median (often a C-115 amendment)."""
        notes = []
        for m in months:
            value = self.get(m, product)
            hist = [v for p in range(m - 12, m) if (v := self.get(p, product))]
            if not value or len(hist) < 6:
                continue
            ratio = value / statistics.median(hist)
            if ratio >= OUTLIER_X or ratio <= 1 / OUTLIER_X:
                notes.append(
                    f"{ym_label(m)} {product} ({_fmt(value)} {UNITS[product]}) is {ratio:.1f}x its trailing "
                    "12-month median"
                )
        return notes

    def main_county(self, m: int) -> str:
        return max(self.counties, key=lambda c: self.recs[(c, ym_year(m))][self.primary][m % 12])

    def record(self, m: int, county: str) -> dict:
        return self.recs[(county, ym_year(m))]

    def by_county(self, months: range) -> dict[str, dict[str, int]]:
        return {
            c: {p: round(sum(self.recs[(c, ym_year(m))][p][m % 12] for m in months)) for p in ("gas", "oil")}
            for c in self.counties
        }


def _fmt(value: float) -> str:
    return f"{round(value):,}"


def _pct_txt(pct: float | None) -> str:
    return f"{pct:+.1%}" if pct is not None else "n/a"


def _county_txt(counties: list[str]) -> str:
    if len(counties) == 1:
        return f"{counties[0]} County, NM"
    return f"{', '.join(counties[:-1])} and {counties[-1]} counties, NM"


def _rows_excerpt(rec: dict, months: list[int], with_header: bool = True) -> list[str]:
    lines = [rec["header"]] if with_header else []
    for m in months:
        lines += [line for line in rec["top"][m % 12] if line not in lines]
    return lines


class OgStateConnector(Connector):
    source_id = "og_state"
    name = "New Mexico oil & gas production (OCD C-115 via NM Tech GO-TECH)"
    type = "production"
    access_method = (
        "Per-well monthly production CSVs (zipped, one per county and year) at octane.nmt.edu/gotech/Petroleum_Data/"
        "county/{County}{yyyy}.zip, published by the NM Tech Petroleum Recovery Research Center (GO-TECH) from NM "
        "OCD C-115 reports. Summed by operator of record (OgridName) x county per config/og_map.yaml. Trust "
        "distributions read from the 8-K Ex. 99.1 stored by the EDGAR connector."
    )
    cadence = "monthly (C-115 production lags ~45 days; GO-TECH files refresh nightly)"
    terms_note = (
        "Public state production data; no login or key. octane.nmt.edu has no robots.txt (redirects to a 404 = "
        "allow). The OCD's own production report pages sit behind a Cloudflare Turnstile check and are not used. "
        "C-115 volumes are operator-reported and get amended; operator totals are a proxy for a trust's interest."
    )

    def __init__(
        self,
        *args: Any,
        og_map: list[dict] | None = None,
        today: date | None = None,
        years: list[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.entries = og_map if og_map is not None else self._load_map()
        self.today = today or date.today()
        self.years = years

    def _load_map(self) -> list[dict]:
        path = self.settings.config_dir / "og_map.yaml"
        return (yaml.safe_load(path.read_text()) or {}).get("entries", [])

    def _entries_in_scope(self) -> list[dict]:
        universe = self.universe()
        return [e for e in self.entries if e.get("state", "NM") == "NM" and e["ticker"] in universe]

    # --- discover -------------------------------------------------------------------------------
    def discover(self) -> list[tuple[str, int]]:
        entries = self._entries_in_scope()
        if not entries:
            raise ConnectorUnavailable("og_map.yaml has no New Mexico entries for the selected tickers")
        try:
            page = self.http.get_ok(COUNTY_PAGE)
        except RobotsDisallowed:
            raise ConnectorUnavailable("octane.nmt.edu robots.txt disallows the GO-TECH county files") from None
        except FetchError as exc:
            raise ConnectorUnavailable(f"GO-TECH unreachable: {exc}") from None
        if is_challenge(page.content):
            raise ConnectorUnavailable("GO-TECH answered with a bot-verification page; not bypassing it")
        counties = sorted({c for e in entries for c in e["counties"]})
        years = self.years or years_needed(self.today)
        return [(county, year) for county in counties for year in years]

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: tuple[str, int]) -> list[Document]:
        county, year = target
        url = county_url(county, year)
        if year < self.today.year and (recent := self._recent_copy(url)) is not None:
            return [recent]
        resp = self.http.get(url)
        if resp.status_code == 404:
            return []  # county-year not published yet (e.g. January, before the first month is in)
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        if not resp.content.startswith(b"PK"):
            why = " (bot-verification page; not bypassing)" if is_challenge(resp.content) else ""
            raise FetchError(f"{url} did not return a zip{why}")
        member, text = unzip_csv(resp.content)
        return [
            self.save(
                url=url,
                content=resp.content,
                content_type="application/zip",
                title=f"GO-TECH NM production {county} County {year}",
                meta={"kind": "county_year", "county": county, "year": year, "member": member},
                text=text,
            )
        ]

    def _recent_copy(self, url: str) -> Document | None:
        doc = self.session.exec(
            select(Document).where(Document.url == url).order_by(col(Document.fetched_at).desc())
        ).first()
        if doc is None:
            return None
        fetched = doc.fetched_at if doc.fetched_at.tzinfo else doc.fetched_at.replace(tzinfo=UTC)
        return doc if utcnow() - fetched <= timedelta(days=REUSE_DAYS) else None

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json or "{}")
        county, year = meta.get("county"), meta.get("year")
        entries = [e for e in self._entries_in_scope() if county in e["counties"]]
        if not entries:
            return []
        header, idx, rows = parse_rows(self._csv_text(doc))
        out = []
        for entry in entries:
            agg = aggregate(rows, idx, entry["operators"], entry.get("primary", "gas"), year)
            out.append(
                {
                    **agg,
                    "ticker": entry["ticker"],
                    "county": county,
                    "year": year,
                    "header": header,
                    "document_id": doc.id,
                    "url": doc.url,
                }
            )
        return out

    def _csv_text(self, doc: Document) -> str:
        text = read_document_text(doc, self.settings)
        if "OgridName" in text[:2000]:
            return text
        return unzip_csv(read_document_bytes(doc, self.settings))[1]  # text cache missing: re-extract from raw

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        by_ticker: dict[str, dict[tuple[str, int], dict]] = defaultdict(dict)
        for r in records:
            by_ticker[r["ticker"]][(r["county"], r["year"])] = r
        drafts: list[SignalDraft] = []
        for entry in self._entries_in_scope():
            if entry["ticker"] not in by_ticker:
                continue
            series = Series(entry, by_ticker[entry["ticker"]])
            latest = series.latest_complete()
            if latest is None:
                log.info("[og_state] %s: no completely reported month yet", entry["ticker"])
                continue
            drafts += self._production_signals(entry, series, latest)
            if entry.get("distribution"):
                draft = self._distribution_signal(entry, series, latest)
                if draft is not None:
                    drafts.append(draft)
        return drafts

    def _who(self, entry: dict) -> str:
        ops = " / ".join(entry["operators"])
        basin = f", {entry['basin']}" if entry.get("basin") else ""
        return f"{ops}-operated wells in {_county_txt(entry['counties'])}{basin},"

    def _production_signals(self, entry: dict, s: Series, latest: int) -> list[SignalDraft]:
        ticker, primary = entry["ticker"], s.primary
        other = "oil" if primary == "gas" else "gas"
        windows = [
            ("og_production_3m_vs_prior", range(latest - 2, latest + 1), range(latest - 5, latest - 2)),
            ("og_production_3m_yoy", range(latest - 2, latest + 1), range(latest - 14, latest - 11)),
            ("og_production_month_yoy", range(latest, latest + 1), range(latest - 12, latest - 11)),
        ]
        main = s.main_county(latest)
        drafts = []
        for metric, cur, prior in windows:
            value, base = s.total(cur), s.total(prior)
            if value is None or base is None:
                continue
            pct = pct_change(value, base)
            o_val, o_base = s.total(cur, other), s.total(prior, other)
            o_pct = pct_change(o_val, o_base)
            if metric == "og_production_3m_vs_prior":
                sig = move_significance(pct, floor=0.03, full=0.25) * 0.85
            elif metric == "og_production_3m_yoy":
                sig = move_significance(pct, floor=0.05, full=0.35)
            else:
                sig = move_significance(pct, floor=0.05, full=0.4) * 0.8
            sig = scale_by_volume(sig, max(value, base) / len(cur), MIN_MONTHLY[primary])
            flags = {p: s.outliers(cur, p) + s.outliers(prior, p) for p in (primary, other)}
            if flags[primary]:
                sig *= 0.5  # a one-month spike or gap in the headline product is more likely a filing artifact
            unit = UNITS[primary]
            summary = (
                f"{self._who(entry)} produced {_fmt(value)} {unit} of {primary} in {span_label(cur)} vs "
                f"{_fmt(base)} {unit} in {span_label(prior)} ({_pct_txt(pct)}); {other} {_fmt(o_val)} "
                f"{UNITS[other]} vs {_fmt(o_base)} {UNITS[other]} ({_pct_txt(o_pct)}). Operator-wide NM OCD C-115 "
                f"volumes, not {ticker}'s net share; latest fully reported month {ym_label(latest)}."
            )
            if flags[primary] or flags[other]:
                summary += f" Check: {'; '.join(flags[primary] + flags[other])} (possible C-115 amendment)."
            cur_rec, prior_rec = s.record(cur[-1], main), s.record(prior[-1], main)
            lines = _rows_excerpt(cur_rec, [cur[-1]])
            if prior_rec["document_id"] == cur_rec["document_id"]:
                lines += [line for line in _rows_excerpt(prior_rec, [prior[-1]], False) if line not in lines]
                excerpt = "\n".join(lines)
            else:
                excerpt = "\n".join(lines) + (
                    f"\n\n[prior-period file {prior_rec['url']}]\n" + "\n".join(_rows_excerpt(prior_rec, [prior[-1]]))
                )
            detail = {
                "state": "NM",
                "operators": entry["operators"],
                "counties": entry["counties"],
                "why": entry.get("why"),
                "primary": primary,
                "pct_change": pct,
                "months": [ym_iso(m) for m in cur],
                "prior_months": [ym_iso(m) for m in prior],
                other: o_val,
                f"prior_{other}": o_base,
                f"{other}_pct_change": o_pct,
                "by_county": s.by_county(cur),
                "prior_by_county": s.by_county(prior),
                "documents": sorted({s.record(m, c)["url"] for m in (*cur, *prior) for c in s.counties}),
                "method": (
                    f"Sum of Oil1..12 / Gas1..12 over every row with OgridName in {entry['operators']} in the "
                    f"GO-TECH county-year files; excerpt = CSV header plus the largest rows in {main} County for "
                    "the last month of each window (verbatim)."
                ),
                "outliers": flags[primary] + flags[other],
                "excerpt": excerpt,
            }
            if prior_rec["document_id"] != cur_rec["document_id"]:
                detail["prior_document_id"] = prior_rec["document_id"]
                detail["prior_url"] = prior_rec["url"]
            drafts.append(
                SignalDraft(
                    ticker=ticker,
                    metric=metric,
                    as_of=month_end(latest),
                    period=f"nm:{ym_iso(latest)}",
                    value=float(round(value)),
                    prior_value=float(round(base)),
                    unit=unit,
                    significance=round(sig, 4),
                    citation_url=cur_rec["url"],
                    document_id=cur_rec["document_id"],
                    summary=summary,
                    detail=detail,
                )
            )
        return drafts

    def _last_declaration(self, ticker: str) -> Document | None:
        docs = self.session.exec(select(Document).where(Document.source_id == "edgar", Document.ticker == ticker))
        ex99 = []
        for doc in docs.all():
            meta = json.loads(doc.meta_json or "{}")
            if meta.get("kind") == "ex99":
                ex99.append((meta.get("filing_date") or "", doc.id, doc))
        return max(ex99, key=lambda x: x[:2])[2] if ex99 else None

    def _distribution_signal(self, entry: dict, s: Series, latest: int) -> SignalDraft | None:
        ticker, primary = entry["ticker"], s.primary
        doc = self._last_declaration(ticker)
        if doc is None:
            log.info("[og_state] %s: no stored 8-K Ex. 99.1; run the EDGAR connector first", ticker)
            return None
        text = read_document_text(doc, self.settings)
        decl = parse_declaration(text)
        if decl["amount"] is None:
            log.info("[og_state] %s: could not read the declared distribution from %s", ticker, doc.url)
            return None
        lag = int((entry.get("distribution") or {}).get("lag_months", 2))
        filed = (json.loads(doc.meta_json or "{}").get("filing_date") or "")[:10]
        decl_month = decl["month"]
        if decl_month is None and filed:
            decl_month = ym(int(filed[:4]), int(filed[5:7]))
        if decl_month is None:
            return None
        base_month = decl["production_month"] or decl_month - lag
        feeding = list(range(base_month + 1, latest + 1))[:2]
        base = s.get(base_month)
        vals = [s.get(m) for m in feeding]
        if not feeding or not base or any(v is None for v in vals):
            return None
        avg = sum(vals) / len(vals)
        pct = pct_change(avg, base)
        amount = decl["amount"]
        if not amount:
            if decl["excess_costs_gross"]:
                direction = "unchanged (no distribution)"
                sig = 0.3
            elif pct is not None and pct > FLAT_BAND:
                direction = "possible resumption"
                sig = 0.6
            else:
                direction = "unchanged (no distribution)"
                sig = 0.3
        else:
            direction = "higher" if pct > FLAT_BAND else "lower" if pct < -FLAT_BAND else "about flat"
            sig = move_significance(pct, floor=FLAT_BAND, full=0.25) * 0.9
        unit = UNITS[primary]
        feeds = " and ".join(ym_label(m + lag) for m in feeding)
        if len(feeding) == 1:
            produced = (
                f"produced {_fmt(avg)} {unit} of {primary} in {ym_label(feeding[0])} (feeds the {feeds} declaration)"
            )
        else:
            produced = (
                f"averaged {_fmt(avg)} {unit}/month of {primary} in {span_label(range(feeding[0], feeding[-1] + 1))} "
                f"(feeds the {feeds} declarations)"
            )
        last_txt = "no distribution" if not amount else f"${amount:g} per unit"
        summary = (
            f"Implied next-distribution direction: {direction}. {self._who(entry)} {produced} vs {_fmt(base)} "
            f"{unit} in {ym_label(base_month)}, the production month behind the {ym_label(decl_month)} declaration "
            f"of {last_txt} ({_pct_txt(pct)})."
        )
        if not amount and decl["excess_costs_gross"]:
            summary += (
                f" That release reports cumulative excess production costs of ${decl['excess_costs_gross']:,} gross, "
                "which must be repaid before distributions resume."
            )
        summary += (
            f" Assumptions: {lag}-month production-to-declaration lag; operator-wide volumes as a proxy for the "
            "trust's interest; prices and costs not modeled."
        )
        proxy = None
        tg = decl["trust_gas"]
        if tg and primary == "gas" and s.get(tg["month"]) is not None and s.get(tg["prior_month"]):
            trust_pct = pct_change(tg["mcf"], tg["prior_mcf"])
            op_pct = pct_change(s.get(tg["month"]), s.get(tg["prior_month"]))
            proxy = {
                "months": [ym_iso(tg["prior_month"]), ym_iso(tg["month"])],
                "trust_reported_mcf": [tg["prior_mcf"], tg["mcf"]],
                "trust_pct_change": trust_pct,
                "operator_mcf": [round(s.get(tg["prior_month"])), round(s.get(tg["month"]))],
                "operator_pct_change": op_pct,
            }
            summary += (
                f" Proxy check: trust-reported gas {ym_label(tg['month'])} vs {ym_label(tg['prior_month'])} "
                f"{_pct_txt(trust_pct)}; operator series {_pct_txt(op_pct)}."
            )
        main = s.main_county(feeding[-1])
        rec = s.record(feeding[-1], main)
        lines = _rows_excerpt(rec, feeding)
        base_rec = s.record(base_month, main)
        if base_rec["document_id"] == rec["document_id"]:
            lines += [line for line in _rows_excerpt(base_rec, [base_month], False) if line not in lines]
            excerpt = "\n".join(lines)
        else:
            base_lines = "\n".join(_rows_excerpt(base_rec, [base_month]))
            excerpt = "\n".join(lines) + f"\n\n[base-month file {base_rec['url']}]\n{base_lines}"
        excerpt += f"\n\n[last declared distribution: {doc.url}]\n" + "\n".join(decl["sentences"])
        detail = {
            "state": "NM",
            "operators": entry["operators"],
            "counties": entry["counties"],
            "why": entry.get("why"),
            "direction": direction,
            "pct_change": pct,
            "feeding_months": [ym_iso(m) for m in feeding],
            "base_month": ym_iso(base_month),
            "declaration_month": ym_iso(decl_month),
            "declaration_filed": filed or None,
            "last_distribution_per_unit": amount,
            "excess_production_costs_gross": decl["excess_costs_gross"],
            "lag_months": lag,
            "price_modeled": False,
            "proxy_check": proxy,
            "by_county": s.by_county(range(feeding[0], feeding[-1] + 1)),
            "prior_document_id": doc.id,
            "prior_url": doc.url,
            "excerpt": excerpt,
        }
        return SignalDraft(
            ticker=ticker,
            metric="og_implied_distribution_direction",
            as_of=month_end(feeding[-1]),
            period=f"nm:{ym_iso(feeding[-1])}:after:{ym_iso(decl_month)}",
            value=float(round(avg)),
            prior_value=float(round(base)),
            unit=f"{unit}/month",
            significance=round(sig, 4),
            citation_url=rec["url"],
            document_id=rec["document_id"],
            summary=summary,
            detail=detail,
        )
