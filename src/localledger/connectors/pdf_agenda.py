"""Agenda PDFs for towns that don't publish through Legistar (config/agenda_sources.yaml).

Access (verified Oct 2026, see SOURCES.md), per town:
- index_url + link_pattern: fetch a public meetings page, collect the agenda links matching the regex (relative
  links resolved), date each link from the pattern's m/d/y groups or from its text/aria-label/URL/table row.
- urls: explicit agenda URLs.
- platform civicclerk: the portal's public OData API, https://{tenant}.api.civicclerk.com/v1/Events (no key),
  agenda files at /v1/Meetings/GetMeetingFileStream(fileId=N,plainText=false).
Recent agendas (lookback_days back, 30 days ahead; at most max_docs per town, newest first) are downloaded and
stored (pdfplumber text is extracted on save), then matched against config/watchlists/. Citations are the agenda
file URLs. HttpClient enforces robots.txt and the declared User-Agent on every request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import yaml
from bs4 import BeautifulSoup
from sqlmodel import select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.connectors.watchlist import Watchlist, agenda_signal, load_watchlists, match_items
from localledger.http import FetchError, read_document_text
from localledger.models import Document
from localledger.signals.store import SignalDraft

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 75
LOOKAHEAD_DAYS = 30
MAX_DOCS = 6
REFRESH_DAYS = 7  # agendas for meetings in the last week (or upcoming) are re-downloaded: they get revised
MAX_PAGES = 5  # CivicClerk OData pages per run
CIVICCLERK_API = "https://{tenant}.api.civicclerk.com/v1"
CIVICCLERK_FILE = CIVICCLERK_API + "/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
CIVICCLERK_PAGE = "https://{tenant}.portal.civicclerk.com/event/{event_id}/files/agenda/{file_id}"

MONTHS = {
    m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
}
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), "mdy"),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), "ymd"),
    (re.compile(r"\b([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}),? (\d{4})\b"), "Mdy"),
    (re.compile(r"(?<!\d)(\d{1,2})_(\d{1,2})_(\d{4})(?!\d)"), "mdy"),
    (re.compile(r"_(\d{2})(\d{2})(\d{4})-\d+"), "mdy"),  # CivicPlus AgendaCenter: _MMDDYYYY-ID
]


def load_towns(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text()) or {}).get("towns") or []


def _mk_date(y: str, m: str | int, d: str) -> date | None:
    try:
        year = int(y) + (2000 if len(str(y)) == 2 else 0)
        month = m if isinstance(m, int) else int(m)
        return date(year, month, int(d))
    except (TypeError, ValueError):
        return None


def parse_date(text: str | None) -> date | None:
    """First date found in `text` (m/d/yyyy, yyyy-mm-dd, 'October 6, 2026', m_d_yyyy, CivicPlus _MMDDYYYY-id)."""
    if not text:
        return None
    for pattern, order in DATE_PATTERNS:
        for m in pattern.finditer(text):
            a, b, c = m.groups()
            if order == "mdy":
                found = _mk_date(c, a, b)
            elif order == "ymd":
                found = _mk_date(a, b, c)
            else:
                month = MONTHS.get(a.lower()[:3])
                found = _mk_date(c, month, b) if month else None
            if found:
                return found
    return None


def clean_url(href: str, base: str) -> str:
    """Absolute, percent-encoded URL without a redundant :443/:80."""
    url = urljoin(base, href.strip())
    url = quote(url, safe="/:?&=%#@!$'()*+,;~")
    parts = urlparse(url)
    if (parts.scheme, parts.port) in (("https", 443), ("http", 80)):
        url = parts._replace(netloc=parts.hostname or "").geturl()
    return url


def find_links(html: str | bytes, base_url: str, link_pattern: str) -> list[dict]:
    """Agenda links on an index page: [{url, text, date}], in page order, one entry per URL."""
    pattern = re.compile(link_pattern, re.IGNORECASE)
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        url = clean_url(a["href"], base_url)
        m = pattern.search(url) or pattern.search(unquote(url))
        if not m:
            continue
        text = a.get_text(" ", strip=True)
        row = a.find_parent("tr")
        link = links.setdefault(url, {"url": url, "text": "", "date": None})
        if text and len(text) > len(link["text"]):
            link["text"] = text
        groups = m.groupdict()
        found = None
        if groups.get("y") and groups.get("m") and groups.get("d"):
            found = _mk_date(groups["y"], groups["m"], groups["d"])
        for candidate in (text, a.get("aria-label"), unquote(url), row.get_text(" ", strip=True) if row else None):
            found = found or parse_date(candidate)
        link["date"] = link["date"] or found
    return list(links.values())


def select_recent(links: list[dict], today: date, lookback_days: int, max_docs: int) -> list[dict]:
    """Dated links in [today - lookback, today + 30d], newest first; undated pages fall back to the first N links."""
    dated = [x for x in links if x.get("date")]
    if not dated:
        return links[:max_docs]
    lo, hi = today - timedelta(days=lookback_days), today + timedelta(days=LOOKAHEAD_DAYS)
    recent = [x for x in dated if lo <= x["date"] <= hi]
    return sorted(recent, key=lambda x: x["date"], reverse=True)[:max_docs]


class PdfAgendaConnector(Connector):
    source_id = "pdf_agenda"
    name = "Municipal agenda PDFs (non-Legistar towns)"
    type = "agendas"
    access_method = (
        "Public meeting pages listed in config/agenda_sources.yaml: agenda links found by regex on each index "
        "page (or explicit URLs, or a CivicClerk portal's public OData API /v1/Events + "
        "GetMeetingFileStream), PDFs downloaded and text-extracted with pdfplumber, matched against "
        "config/watchlists/."
    )
    cadence = "weekly (agendas from the last ~75 days and the next ~30)"
    terms_note = (
        "Public meeting records from town/county websites. robots.txt is honored per host (towns whose agenda "
        "host disallows agents, e.g. Shakopee's Granicus, stay blocked). Declared User-Agent, 1 req/s per host."
    )

    def __init__(
        self,
        *args: Any,
        towns: list[dict] | None = None,
        watchlists: dict[str, Watchlist] | None = None,
        today: date | None = None,
        lookback_days: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.towns = towns if towns is not None else load_towns(self.settings.config_dir / "agenda_sources.yaml")
        self._by_id = {t["id"]: t for t in self.towns}
        self._watchlists = watchlists
        self.today = today or date.today()
        self.lookback_override = lookback_days

    @property
    def watchlists(self) -> dict[str, Watchlist]:
        if self._watchlists is None:
            self._watchlists = load_watchlists(self.settings.config_dir / "watchlists")
        return self._watchlists

    def _lookback(self, town: dict) -> int:
        return self.lookback_override or int(town.get("lookback_days", LOOKBACK_DAYS))

    # --- discover -------------------------------------------------------------------------------
    def discover(self) -> list[str]:
        universe = self.universe()
        enabled = [
            t["id"]
            for t in self.towns
            if t.get("status", "ok") == "ok" and any(x in universe and x in self.watchlists for x in t["tickers"])
        ]
        if not enabled:
            raise ConnectorUnavailable("no enabled agenda towns with watchlists for the selected tickers")
        return enabled

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: str) -> list[Document]:
        town = self._by_id[target]
        links = self._links(town)
        chosen = select_recent(links, self.today, self._lookback(town), int(town.get("max_docs", MAX_DOCS)))
        docs: list[Document] = []
        failed = 0
        for link in chosen:
            try:
                doc = self._download(town, link)
            except Exception as exc:  # one bad file shouldn't drop the town's other agendas
                failed += 1
                log.info("[pdf_agenda] %s: %s failed: %s", target, link["url"], exc)
                continue
            if doc is not None:
                docs.append(doc)
        if failed and not docs:
            raise FetchError(f"{target}: all {failed} agenda downloads failed")
        return docs

    def _links(self, town: dict) -> list[dict]:
        if town.get("platform") == "civicclerk":
            return self._civicclerk_links(town)
        if town.get("urls"):
            return [{"url": u, "text": "", "date": parse_date(unquote(u))} for u in town["urls"]]
        resp = self.http.get_ok(town["index_url"])
        return find_links(resp.content, str(resp.url), town["link_pattern"])

    def _civicclerk_links(self, town: dict) -> list[dict]:
        tenant = town["tenant"]
        lo = self.today - timedelta(days=self._lookback(town))
        hi = self.today + timedelta(days=LOOKAHEAD_DAYS)
        url: str | None = CIVICCLERK_API.format(tenant=tenant) + "/Events"
        params: dict | None = {
            "$filter": f"startDateTime ge {lo.isoformat()}T00:00:00Z and startDateTime le {hi.isoformat()}T23:59:59Z",
            "$orderby": "startDateTime desc",
        }
        bodies = [b.lower() for b in town.get("bodies") or []]
        types = {t.lower() for t in town.get("file_types") or ["Agenda"]}
        links: list[dict] = []
        for _ in range(MAX_PAGES):
            if not url:
                break
            data = self.http.get_ok(url, params=params).json() if params else self.http.get_ok(url).json()
            for ev in data.get("value") or []:
                category = ev.get("categoryName")
                body = (category if category and category != "General" else ev.get("eventName")) or "Meeting"
                if bodies and not any(b in body.lower() or b in (ev.get("eventName") or "").lower() for b in bodies):
                    continue
                # CivicClerk stores local wall-clock times with a Z suffix, so the date part is the meeting date.
                day = parse_date((ev.get("startDateTime") or "")[:10])
                for f in ev.get("publishedFiles") or []:
                    if str(f.get("type", "")).lower() not in types or not f.get("fileId"):
                        continue
                    links.append(
                        {
                            "url": CIVICCLERK_FILE.format(tenant=tenant, file_id=f["fileId"]),
                            "text": f.get("name") or "",
                            "date": day,
                            "body": body,
                            "meeting_page": CIVICCLERK_PAGE.format(
                                tenant=tenant, event_id=ev["id"], file_id=f["fileId"]
                            ),
                        }
                    )
            url, params = data.get("@odata.nextLink"), None
        return links

    def _download(self, town: dict, link: dict) -> Document | None:
        url = link["url"]
        known = self.session.exec(select(Document).where(Document.url == url).order_by(Document.id.desc())).first()
        fresh_cutoff = self.today - timedelta(days=REFRESH_DAYS)
        if known and link.get("date") and link["date"] < fresh_cutoff:
            return known
        if town.get("max_mb"):
            head = self.http.request("HEAD", url)
            size = int(head.headers.get("content-length") or 0) if head.status_code == 200 else 0
            if size > float(town["max_mb"]) * 1_000_000:
                log.info("[pdf_agenda] %s: skipping %s (%.0f MB > max_mb)", town["id"], url, size / 1e6)
                return None
        content = self.http.get_ok(url).content
        if content[:5] == b"%PDF-":
            ctype = "application/pdf"
        elif b"<html" in content[:2000].lower():
            ctype = "text/html"
        else:
            log.info("[pdf_agenda] %s: %s is neither PDF nor HTML; skipped", town["id"], url)
            return None
        day = link.get("date")
        body = link.get("body") or town.get("body") or "Meeting"
        meta = {
            "kind": "agenda",
            "town": town["id"],
            "place": town["name"],
            "body": body,
            "date": day.isoformat() if day else None,
            "link_text": link.get("text"),
            "index_url": town.get("index_url"),
            "meeting_page": link.get("meeting_page"),
        }
        return self.save(
            url=url,
            content=content,
            content_type=ctype,
            title=f"{town['name']} {body} agenda {day.isoformat() if day else ''}".strip(),
            meta=meta,
        )

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json or "{}")
        if meta.get("kind") != "agenda":
            return []
        text = read_document_text(doc, self.settings)
        if not text.strip():
            log.info("[pdf_agenda] %s has no text layer (scanned?); nothing to match", doc.url)
            return []
        town = self._by_id.get(meta["town"], {"id": meta["town"], "name": meta.get("place"), "tickers": []})
        day = meta.get("date") or (doc.fetched_at.date().isoformat() if doc.fetched_at else self.today.isoformat())
        key = meta.get("date") or hashlib.sha1(doc.url.encode()).hexdigest()[:10]
        return [
            {
                "document_id": doc.id,
                "url": doc.url,
                "town": town,
                "text": text,
                "date": day,
                "body": meta.get("body") or "Meeting",
                "period": f"{meta['town']}:{key}:{meta.get('body') or ''}",
                "detail": {
                    "town": meta["town"],
                    "agenda_url": doc.url,
                    "meeting_page": meta.get("meeting_page"),
                    "index_url": meta.get("index_url"),
                    "link_text": meta.get("link_text"),
                    "date_known": bool(meta.get("date")),
                },
            }
        ]

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        universe = self.universe()
        drafts: list[SignalDraft] = []
        for rec in records:
            for ticker in rec["town"].get("tickers") or []:
                wl = self.watchlists.get(ticker)
                if wl is None or ticker not in universe:
                    continue
                items = match_items(rec["text"], wl)
                if not items:
                    continue
                drafts.append(
                    agenda_signal(
                        ticker=ticker,
                        items=items,
                        text=rec["text"],
                        place=rec["town"]["name"],
                        body=rec["body"],
                        meeting_date=date.fromisoformat(rec["date"]),
                        citation_url=rec["url"],
                        document_id=rec["document_id"],
                        period=rec["period"],
                        detail={**rec["detail"], "why": rec["town"].get("why")},
                    )
                )
        return drafts
