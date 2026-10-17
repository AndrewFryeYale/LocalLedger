"""State gaming revenue: Indiana Gaming Commission (IGC) monthly revenue reports, property by property.

Access (verified Oct 2026, see SOURCES.md):
- https://www.in.gov/igc/publications/monthly-revenue/                    this year's monthly reports (PDF + XLSX)
- https://www.in.gov/igc/publications/archived-monthly-revenue-reports/   one page per earlier year
- each monthly PDF (/igc/files/reports/2026/2026-09Revenue.pdf, /dA/<id>/2025-09-Revenue.pdf?language_id=1, ...)
  prints, per casino: win, free play, other adjustments and taxable AGR; table and EGD win; EGD units, coin-in, win.
www.in.gov robots.txt allows all agents. Colorado's Division of Gaming site blocks automated requests (CloudFront
403) and the Illinois Gaming Board publishes property data only through a form with no per-month file to cite, so
neither is used (see BLOCKERS.md). Properties are mapped to tickers in config/gaming_map.yaml.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup
from sqlmodel import col, select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.http import FetchError, RobotsDisallowed, read_document_text
from localledger.models import Document
from localledger.signals.scoring import move_significance, pct_change
from localledger.signals.store import SignalDraft

BASE = "https://www.in.gov"
CURRENT_PAGE = f"{BASE}/igc/publications/monthly-revenue/"
ARCHIVE_PAGE = f"{BASE}/igc/publications/archived-monthly-revenue-reports/"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
REPORT_PDF = re.compile(r"(\d{4})-(\d{2})-?Revenue\.pdf", re.IGNORECASE)
# pdfplumber sometimes splits words in the title ("reported fo rJanuary 2026"), so spacing is loose.
TITLE = re.compile(r"Summary of Wagering and Supplemental Tax\s*-\s*reported\s*f\s*o\s*r\s*([A-Z][a-z]+)\s*(\d{4})")
MONEY = r"\(?\$[\d,]+(?:\.\d+)?\)?"
# Report sections we read, keyed by the start of the header line that opens them (as pdfplumber extracts it).
SECTIONS = {
    "agr": "Win Free Play Other * Taxable AGR",
    "wager": "WAGERING TAX No. of Table Games",
    "egd": "SUMMARY OF EGD ACTIVITY",
}
AGR_ROW = re.compile(
    rf"^(?P<name>.+?)\**\s+(?P<win>{MONEY})\s+(?P<fp>{MONEY})\s+(?P<other>{MONEY})\s+(?P<agr>{MONEY})$"
)
WAGER_ROW = re.compile(
    rf"^(?P<name>.+?)\**\s+(?P<tables>[\d,]+)\s+(?P<tw>{MONEY})\s+(?P<egds>[\d,]+)\s+(?P<ew>{MONEY})\s+(?P<agr>{MONEY})$"
)
EGD_ROW = re.compile(rf"^(?P<name>.+?)\**\s+(?P<units>[\d,]+)\s+(?P<coin>{MONEY})\s+(?P<win>{MONEY})$")
ROWS = {"agr": AGR_ROW, "wager": WAGER_ROW, "egd": EGD_ROW}
CHALLENGE_MARKERS = (b"challenges.cloudflare.com", b"cf-turnstile", b"captcha", b"Request blocked")


def money(text: str) -> float:
    """'$3,606,652' -> 3606652.0; '($1,500,000)' -> -1500000.0."""
    neg = text.startswith("(")
    value = float(text.strip("()$").replace(",", "").replace("$", ""))
    return -value if neg else value


def ym(year: int, month: int) -> int:
    return year * 12 + month - 1


def ym_label(m: int, short: bool = True) -> str:
    name = MONTHS[m % 12]
    return f"{name[:3] if short else name} {m // 12}"


def month_end(m: int) -> date:
    year, month = (m + 1) // 12, (m + 1) % 12 + 1
    return date(year, month, 1) - timedelta(days=1)


def is_challenge(body: bytes) -> bool:
    head = body[:20_000].lower()
    return any(marker.lower() in head for marker in CHALLENGE_MARKERS)


def report_links(html: str | bytes, page_url: str) -> dict[int, str]:
    """{month int: absolute PDF url} for every monthly revenue report linked from an IGC listing page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        m = REPORT_PDF.search(a["href"])
        if m:
            out.setdefault(ym(int(m.group(1)), int(m.group(2))), urljoin(page_url, a["href"]))
    return out


def archive_pages(html: str | bytes, page_url: str) -> dict[int, str]:
    """{year: url} of the per-year archive pages linked from the archive index."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"archived-monthly-revenue-reports/[\w-]*?(\d{4})/?$", a["href"])
        if m:
            out.setdefault(int(m.group(1)), urljoin(page_url, a["href"]))
    return out


def parse_report(text: str) -> dict[str, Any]:
    """Property rows of one IGC monthly revenue report (PDF text). Every value keeps its verbatim source line."""
    month = None
    if m := TITLE.search(text):
        month = ym(int(m.group(2)), MONTHS.index(m.group(1)) + 1)
    props: dict[str, dict[str, Any]] = {}
    section: str | None = None
    headers: dict[str, str] = {}
    title_line = m.group(0) if m else None
    for raw in text.splitlines():
        line = raw.strip()
        opened = next((s for s, h in SECTIONS.items() if line.startswith(h)), None)
        if opened:
            section, headers[opened] = opened, line
            continue
        if section in ("agr", "wager") and line.startswith("TOTAL"):
            section = None
            continue
        if section == "egd" and (line.startswith("Last updated") or line.startswith("INDIANA GAMING COMMISSION")):
            section = None
            continue
        if section == "egd" and "UNITS* COIN IN WIN" in line:
            headers["egd"] = line  # licensee-group sub-header carries the column names
            continue
        row = ROWS[section].match(line) if section else None
        if row is None:
            continue
        p = props.setdefault(row.group("name").strip(" *"), {"lines": {}})
        if section in p["lines"]:
            continue  # first row per section wins
        if section == "agr":
            p.update(
                win=money(row["win"]), free_play=money(row["fp"]), other=money(row["other"]), agr=money(row["agr"])
            )
        elif section == "wager":
            p.update(
                tables=int(row["tables"].replace(",", "")),
                table_win=money(row["tw"]),
                egds=int(row["egds"].replace(",", "")),
                egd_win=money(row["ew"]),
            )
        else:
            p.update(egd_units=int(row["units"].replace(",", "")), coin_in=money(row["coin"]))
        p["lines"][section] = line
        p["lines"][f"{section}_header"] = headers[section]
    return {"month": month, "title_line": title_line, "properties": props}


def _usd(value: float) -> str:
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _pct_txt(pct: float | None) -> str:
    return f"{pct:+.1%}" if pct is not None else "n/a"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class GamingStateConnector(Connector):
    source_id = "gaming_state"
    name = "Indiana Gaming Commission monthly revenue reports"
    type = "gaming_revenue"
    access_method = (
        "Monthly revenue report PDFs linked from www.in.gov/igc/publications/monthly-revenue/ (current year) and "
        "the per-year pages under /igc/publications/archived-monthly-revenue-reports/. Property rows (taxable AGR, "
        "table/EGD win, EGD coin-in) read from the PDF text; properties mapped in config/gaming_map.yaml."
    )
    cadence = "monthly (posted ~10th-14th of the following month; IGC publishes the posting calendar)"
    terms_note = (
        "Public state regulator reports; no login or key. www.in.gov robots.txt allows all agents. Taxable AGR is "
        "after the free-play deduction (racinos also deduct 12%); figures are as reported, before audit."
    )

    def __init__(self, *args: Any, gaming_map: list[dict] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entries = gaming_map if gaming_map is not None else self._load_map()
        self.latest: int | None = None

    def _load_map(self) -> list[dict]:
        path = self.settings.config_dir / "gaming_map.yaml"
        return (yaml.safe_load(path.read_text()) or {}).get("properties", [])

    def _entries_in_scope(self) -> list[dict]:
        universe = self.universe()
        return [e for e in self.entries if e.get("state") == "IN" and e["ticker"] in universe]

    # --- discover -------------------------------------------------------------------------------
    def _page(self, url: str) -> str:
        try:
            resp = self.http.get_ok(url)
        except RobotsDisallowed:
            raise ConnectorUnavailable(f"robots.txt disallows {url}") from None
        except FetchError as exc:
            raise ConnectorUnavailable(f"IGC listing unreachable: {exc}") from None
        if is_challenge(resp.content):
            raise ConnectorUnavailable(f"{url} answered with a bot-verification page; not bypassing it")
        return resp.text

    def discover(self) -> list[tuple[int, str]]:
        if not self._entries_in_scope():
            raise ConnectorUnavailable("gaming_map.yaml has no Indiana properties for the selected tickers")
        links = report_links(self._page(CURRENT_PAGE), CURRENT_PAGE)
        if not links:
            raise ConnectorUnavailable(f"no monthly revenue report links found on {CURRENT_PAGE}")
        latest = max(links)
        year = latest // 12
        # Year-to-date in both years: January through the latest month, this year and last.
        wanted = [ym(y, mo) for y in (year - 1, year) for mo in range(1, latest % 12 + 2)]
        missing_years = sorted({m // 12 for m in wanted if m not in links})
        if missing_years:
            pages = archive_pages(self._page(ARCHIVE_PAGE), ARCHIVE_PAGE)
            for y in missing_years:
                if y in pages:
                    for m, url in report_links(self._page(pages[y]), pages[y]).items():
                        links.setdefault(m, url)
        self.latest = latest
        return [(m, links[m]) for m in wanted if m in links]

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: tuple[int, str]) -> list[Document]:
        month, url = target
        if month != self.latest:
            # Closed months: reuse the stored copy (the newest report is the one that gets corrected).
            existing = self.session.exec(
                select(Document).where(Document.url == url).order_by(col(Document.fetched_at).desc())
            ).first()
            if existing is not None:
                return [existing]
        resp = self.http.get_ok(url)
        if not resp.content.startswith(b"%PDF"):
            why = " (bot-verification page; not bypassing)" if is_challenge(resp.content) else ""
            raise FetchError(f"{url} did not return a PDF{why}")
        return [
            self.save(
                url=url,
                content=resp.content,
                content_type="application/pdf",
                title=f"IGC monthly revenue report {ym_label(month, short=False)}",
                meta={"kind": "igc_month", "month": f"{month // 12}-{month % 12 + 1:02d}"},
            )
        ]

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json or "{}")
        report = parse_report(read_document_text(doc, self.settings))
        month = report["month"]
        if month is None:
            raise ValueError(f"no report month in {doc.url}")
        if meta.get("month") and meta["month"] != f"{month // 12}-{month % 12 + 1:02d}":
            raise ValueError(f"{doc.url} reports {ym_label(month)}, not {meta['month']}")
        out = []
        for entry in self._entries_in_scope():
            row = report["properties"].get(entry["property"])
            if row is None or "agr" not in row:
                continue
            out.append(
                {
                    **row,
                    "ticker": entry["ticker"],
                    "property": entry["property"],
                    "month": month,
                    "title_line": report["title_line"],
                    "document_id": doc.id,
                    "url": doc.url,
                }
            )
        return out

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        index: dict[tuple[str, str], dict[int, dict]] = {}
        for r in records:
            index.setdefault((r["ticker"], r["property"]), {})[r["month"]] = r
        latest = self.latest if self.latest is not None else max((r["month"] for r in records), default=None)
        drafts: list[SignalDraft] = []
        for entry in self._entries_in_scope():
            months = index.get((entry["ticker"], entry["property"]), {})
            if latest is None or latest not in months or latest - 12 not in months:
                continue
            cur, prior = months[latest], months[latest - 12]
            drafts.append(self._month_signal(entry, cur, prior))
            if cur.get("coin_in") is not None and prior.get("coin_in") is not None:
                drafts.append(self._handle_signal(entry, cur, prior))
            ytd = [latest - i for i in range(latest % 12 + 1)][::-1]
            if all(m in months and m - 12 in months for m in ytd):
                drafts.append(self._ytd_signal(entry, [months[m] for m in ytd], [months[m - 12] for m in ytd]))
        return drafts

    def _who(self, entry: dict) -> str:
        where = f"{entry['location']}; " if entry.get("location") else ""
        licensee = f"licensee {entry['licensee']}" if entry.get("licensee") else "Indiana"
        return f"{entry['property']} ({where}{licensee})"

    @staticmethod
    def _lines(rec: dict, keys: tuple[str, ...]) -> list[str]:
        """Verbatim report lines: title, then each section's header and the property's row."""
        lines = [rec["title_line"]] if rec.get("title_line") else []
        for key in keys:
            if key in rec["lines"]:
                lines += [rec["lines"][f"{key}_header"], rec["lines"][key]]
        return lines

    def _excerpt(self, cur: dict, prior: dict, keys: tuple[str, ...]) -> str:
        return (
            "\n".join(self._lines(cur, keys))
            + f"\n\n[prior-year report {prior['url']}]\n"
            + "\n".join(self._lines(prior, keys))
        )

    def _draft(self, entry: dict, metric: str, cur: dict, prior: dict, **kw: Any) -> SignalDraft:
        detail = {
            "state": "IN",
            "property": entry["property"],
            "licensee": entry.get("licensee"),
            "why": entry.get("why"),
            "prior_document_id": prior["document_id"],
            "prior_url": prior["url"],
            **kw.pop("detail"),
        }
        return SignalDraft(
            ticker=entry["ticker"],
            metric=metric,
            as_of=month_end(cur["month"]),
            period=f"in:{_slug(entry['property'])}:{cur['month'] // 12}-{cur['month'] % 12 + 1:02d}",
            citation_url=cur["url"],
            document_id=cur["document_id"],
            detail=detail,
            **kw,
        )

    def _month_signal(self, entry: dict, cur: dict, prior: dict) -> SignalDraft:
        pct = pct_change(cur["agr"], prior["agr"])
        sig = move_significance(pct, floor=0.04, full=0.35) * 0.85
        now, then = ym_label(cur["month"]), ym_label(prior["month"])
        summary = (
            f"{self._who(entry)} reported taxable AGR of {_usd(cur['agr'])} for {now} vs {_usd(prior['agr'])} in "
            f"{then} ({_pct_txt(pct)} y/y)"
        )
        if "table_win" in cur and "table_win" in prior:
            summary += (
                f"; table win {_usd(cur['table_win'])} vs {_usd(prior['table_win'])}, EGD win "
                f"{_usd(cur['egd_win'])} vs {_usd(prior['egd_win'])}"
            )
        summary += ". Indiana Gaming Commission monthly revenue report, as reported (unaudited)."
        detail = {
            "pct_change": pct,
            "win": cur["win"],
            "free_play": cur["free_play"],
            "prior_win": prior["win"],
            "prior_free_play": prior["free_play"],
            "table_win": cur.get("table_win"),
            "prior_table_win": prior.get("table_win"),
            "egd_win": cur.get("egd_win"),
            "prior_egd_win": prior.get("egd_win"),
            "excerpt": self._excerpt(cur, prior, ("agr", "wager")),
        }
        return self._draft(
            entry,
            "gaming_agr_month_yoy",
            cur,
            prior,
            value=cur["agr"],
            prior_value=prior["agr"],
            unit="USD",
            significance=round(sig, 4),
            summary=summary,
            detail=detail,
        )

    def _handle_signal(self, entry: dict, cur: dict, prior: dict) -> SignalDraft:
        pct = pct_change(cur["coin_in"], prior["coin_in"])
        sig = move_significance(pct, floor=0.04, full=0.35) * 0.6
        summary = (
            f"{self._who(entry)} EGD coin-in (slot handle) {_usd(cur['coin_in'])} in {ym_label(cur['month'])} vs "
            f"{_usd(prior['coin_in'])} in {ym_label(prior['month'])} ({_pct_txt(pct)} y/y) on {cur['egd_units']:,} "
            f"vs {prior['egd_units']:,} machines. Indiana Gaming Commission monthly revenue report (no admissions "
            "count is published)."
        )
        detail = {
            "pct_change": pct,
            "egd_units": cur["egd_units"],
            "prior_egd_units": prior["egd_units"],
            "egd_win": cur.get("egd_win"),
            "prior_egd_win": prior.get("egd_win"),
            "excerpt": self._excerpt(cur, prior, ("egd",)),
        }
        return self._draft(
            entry,
            "gaming_handle_month_yoy",
            cur,
            prior,
            value=cur["coin_in"],
            prior_value=prior["coin_in"],
            unit="USD",
            significance=round(sig, 4),
            summary=summary,
            detail=detail,
        )

    def _ytd_signal(self, entry: dict, cur: list[dict], prior: list[dict]) -> SignalDraft:
        value, base = sum(r["agr"] for r in cur), sum(r["agr"] for r in prior)
        pct = pct_change(value, base)
        sig = move_significance(pct, floor=0.02, full=0.2)
        span = f"Jan-{ym_label(cur[-1]['month'])}" if len(cur) > 1 else ym_label(cur[-1]["month"])
        prior_span = f"Jan-{ym_label(prior[-1]['month'])}" if len(prior) > 1 else ym_label(prior[-1]["month"])
        summary = (
            f"{self._who(entry)} taxable AGR {_usd(value)} in {span} vs {_usd(base)} in {prior_span} "
            f"({_pct_txt(pct)} y/y), summed from the {len(cur)} monthly Indiana Gaming Commission reports of each year."
        )
        lines = self._lines(cur[-1], ("agr",))
        for r in cur[-2::-1] + prior[::-1]:
            lines += ["", f"[{ym_label(r['month'])} report {r['url']}]", r["title_line"] or "", r["lines"]["agr"]]
        detail = {
            "pct_change": pct,
            "months": [{"month": ym_label(r["month"]), "agr": r["agr"], "document_id": r["document_id"]} for r in cur],
            "prior_months": [
                {"month": ym_label(r["month"]), "agr": r["agr"], "document_id": r["document_id"]} for r in prior
            ],
            "excerpt": "\n".join(lines),
        }
        return self._draft(
            entry,
            "gaming_agr_ytd_yoy",
            cur[-1],
            prior[-1],
            value=value,
            prior_value=base,
            unit="USD",
            significance=round(sig, 4),
            summary=summary,
            detail=detail,
        )
