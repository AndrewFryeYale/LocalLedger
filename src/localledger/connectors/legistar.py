"""Legistar Web API: council, board and commission agendas for the clients in config/legistar_clients.yaml.

Access (verified Oct 2026, see SOURCES.md): JSON at https://webapi.legistar.com/v1/{client}/..., no key for most
clients (a few answer 403 "Token is required"; those stay unavailable). webapi.legistar.com has no robots.txt.
- /events?$filter=EventDate ge datetime'YYYY-MM-DD' and EventDate lt datetime'YYYY-MM-DD'&$orderby=EventDate desc
- /events/{EventId}/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1
- /matters?$filter=MatterAgendaDate ge datetime'...' and MatterAgendaDate lt datetime'...'   (mode: matters, for
  clients whose /events answers HTTP 400)
Event items are rendered to plain text, one block per agenda item, stored with the raw JSON, and matched against
config/watchlists/. Citations go to the public InSite meeting page (EventInSiteURL) or legislation page. Agenda
PDFs live on {client}.legistar1.com, whose robots.txt disallows all agents: we record their URLs but never fetch.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlmodel import select

from localledger.connectors.base import Connector, ConnectorUnavailable
from localledger.connectors.watchlist import Watchlist, agenda_signal, classify_action, load_watchlists, match_items
from localledger.http import FetchError, read_document_bytes, read_document_text
from localledger.models import Document
from localledger.signals.store import SignalDraft

log = logging.getLogger(__name__)

API = "https://webapi.legistar.com/v1/{client}"
# InSite pages use their own IDs; the gateway redirects an API MatterId to the right LegislationDetail page.
INSITE_MATTER = "https://{client}.legistar.com/gateway.aspx?M=L&ID={id}"
LOOKBACK_DAYS = 60
LOOKAHEAD_DAYS = 30
REUSE_AFTER_DAYS = 14  # past events older than this, with unchanged timestamps, reuse the stored items
ITEM_PARAMS = {"AgendaNote": 1, "MinutesNote": 1, "Attachments": 1}


def load_clients(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text()) or {}).get("clients") or []


def odata_date(d: date) -> str:
    return f"datetime'{d.isoformat()}'"


def clean_body(name: str | None) -> str:
    """'Housing and Land Use Committee (2025-2027) ' -> 'Housing and Land Use Committee'."""
    return re.sub(r"\s*\(\d{4}\s*-\s*\d{4}\)\s*$", "", (name or "").strip()) or "Meeting"


def clean_text(value: Any) -> str | None:
    """API text as plain lines: CRLF -> LF, no blank lines inside a block, RTF blobs dropped."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    if text.lstrip().startswith("{\\rtf"):
        return None
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line.strip())
    return text.strip() or None


def _url(base: str, params: dict) -> str:
    return str(httpx.URL(base, params=params))


def render_event(meta: dict, items: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    """Plain-text agenda for one event: a header, then one block per item. Returns (text, [(start, end, item)])."""
    ev = meta.get("event") or {}
    header = [
        f"{meta.get('place')}: {clean_body(ev.get('EventBodyName'))}",
        f"Meeting date: {(ev.get('EventDate') or '')[:10]} {ev.get('EventTime') or ''}".rstrip(),
    ]
    if ev.get("EventInSiteURL"):
        header.append(f"Meeting page: {ev['EventInSiteURL']}")
    if ev.get("EventAgendaFile"):
        header.append(f"Agenda: {ev['EventAgendaFile']}")
    parts = ["\n".join(header)]
    spans: list[tuple[int, int, dict]] = []
    pos = len(parts[0])
    ordered = sorted(items, key=lambda i: (i.get("EventItemAgendaSequence") is None, i.get("EventItemAgendaSequence")))
    for item in ordered:
        title = clean_text(item.get("EventItemTitle"))
        matter = clean_text(item.get("EventItemMatterFile"))
        if not title and not matter:
            continue
        head = [f"[Item {item.get('EventItemAgendaSequence') or '-'}]"]
        tags = [t for t in (matter, clean_text(item.get("EventItemMatterType"))) if t]
        if tags:
            head.append(" | ".join(tags))
        lines = [" ".join(head)]
        if title:
            lines.append(title)
        name = clean_text(item.get("EventItemMatterName"))
        if name and name != title:
            lines.append(f"Matter name: {name}")
        for label, key in (("Agenda note", "EventItemAgendaNote"), ("Minutes note", "EventItemMinutesNote")):
            note = clean_text(item.get(key))
            if note:
                lines.append(f"{label}: {note}")
        action = clean_text(item.get("EventItemActionName"))
        if action:
            passed = clean_text(item.get("EventItemPassedFlagName"))
            lines.append(f"Action: {action}" + (f" ({passed})" if passed else ""))
        attachments = [
            clean_text(a.get("MatterAttachmentName"))
            for a in item.get("EventItemMatterAttachments") or []
            if a.get("MatterAttachmentShowOnInternetPage", True)
        ]
        attachments = [a for a in attachments if a]
        if attachments:
            lines.append("Attachments: " + "; ".join(attachments))
        block = "\n".join(lines)
        start = pos + 2
        parts.append(block)
        spans.append((start, start + len(block), item))
        pos = start + len(block)
    return "\n\n".join(parts), spans


def render_matters(place: str, matters: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    """Plain text for /matters results grouped into meetings by (body, agenda date)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in matters:
        key = ((m.get("MatterAgendaDate") or "")[:10], clean_body(m.get("MatterBodyName")))
        groups.setdefault(key, []).append(m)
    parts: list[str] = []
    spans: list[tuple[int, int, dict]] = []
    pos = -2
    for (day, body), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True):
        header = f"{place}: {body}\nAgenda date: {day}"
        pos += 2
        parts.append(header)
        pos += len(header)
        for m in sorted(rows, key=lambda r: str(r.get("MatterFile") or "")):
            tags = [f"[Matter {clean_text(m.get('MatterFile')) or m.get('MatterId')}]"]
            tags += [t for t in (clean_text(m.get("MatterTypeName")), clean_text(m.get("MatterStatusName"))) if t]
            lines = [" | ".join(tags)]
            title = clean_text(m.get("MatterTitle"))
            if title:
                lines.append(title)
            name = clean_text(m.get("MatterName"))
            if name and name != title:
                lines.append(f"Matter name: {name}")
            block = "\n".join(lines)
            start = pos + 2
            parts.append(block)
            spans.append((start, start + len(block), {**m, "_group": (day, body)}))
            pos = start + len(block)
    return "\n\n".join(parts), spans


class LegistarConnector(Connector):
    source_id = "legistar"
    name = "Legistar Web API (municipal agendas)"
    type = "agendas"
    access_method = (
        "Legistar Web API JSON, no key: webapi.legistar.com/v1/{client}/events (OData $filter on EventDate), "
        "/events/{id}/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1, and /matters filtered on "
        "MatterAgendaDate for clients whose /events errors. Clients in config/legistar_clients.yaml; keyword "
        "watchlists in config/watchlists/."
    )
    cadence = "weekly (meetings in the last ~60 days and the next ~30)"
    terms_note = (
        "Public meeting records. webapi.legistar.com has no robots.txt (4xx = allow). Token-gated clients "
        "(HTTP 403 'Token is required', e.g. nyc) are not used. Agenda PDFs on *.legistar1.com are robots-"
        "disallowed, so citations point to the public InSite meeting pages instead."
    )

    def __init__(
        self,
        *args: Any,
        clients: list[dict] | None = None,
        watchlists: dict[str, Watchlist] | None = None,
        today: date | None = None,
        lookback_days: int = LOOKBACK_DAYS,
        lookahead_days: int = LOOKAHEAD_DAYS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.clients = (
            clients if clients is not None else load_clients(self.settings.config_dir / "legistar_clients.yaml")
        )
        self._watchlists = watchlists
        self.today = today or date.today()
        self.window = (self.today - timedelta(days=lookback_days), self.today + timedelta(days=lookahead_days))
        self._by_client = {c["client"]: c for c in self.clients}
        self._matched: dict[tuple[int, str], list] = {}

    @property
    def watchlists(self) -> dict[str, Watchlist]:
        if self._watchlists is None:
            self._watchlists = load_watchlists(self.settings.config_dir / "watchlists")
        return self._watchlists

    # --- discover -------------------------------------------------------------------------------
    def discover(self) -> list[str]:
        universe = self.universe()
        enabled = [
            c["client"]
            for c in self.clients
            if c.get("status", "ok") == "ok" and any(t in universe and t in self.watchlists for t in c["tickers"])
        ]
        if not enabled:
            raise ConnectorUnavailable("no enabled Legistar clients with watchlists for the selected tickers")
        return enabled

    # --- fetch ----------------------------------------------------------------------------------
    def fetch(self, target: str) -> list[Document]:
        client = self._by_client[target]
        if client.get("mode") == "matters":
            return [self._fetch_matters(client)]
        lo, hi = self.window
        params = {
            "$filter": f"EventDate ge {odata_date(lo)} and EventDate lt {odata_date(hi)}",
            "$orderby": "EventDate desc",
        }
        resp = self.http.get(API.format(client=target) + "/events", params=params)
        if resp.status_code == 403:
            raise ConnectorUnavailable(f"{target}: HTTP 403 (token required)")
        if resp.status_code != 200:
            raise FetchError(f"{target}: HTTP {resp.status_code} on /events: {resp.text[:160]}")
        docs: list[Document] = []
        failed = 0
        events = resp.json()
        for ev in events:
            if not self._wanted_body(client, ev.get("EventBodyName")):
                continue
            day = date.fromisoformat(ev["EventDate"][:10])
            if day > self.today and not ev.get("EventAgendaFile"):
                continue  # upcoming meeting with no agenda posted yet
            try:
                docs.append(self._fetch_items(client, ev, day))
            except Exception as exc:  # one bad event shouldn't drop the client's other meetings
                failed += 1
                log.info("[legistar] %s event %s failed: %s", target, ev.get("EventId"), exc)
        if failed and not docs:
            raise FetchError(f"{target}: all {failed} event item requests failed")
        return docs

    @staticmethod
    def _wanted_body(client: dict, body: str | None) -> bool:
        wanted = client.get("bodies")
        return not wanted or any(w.lower() in (body or "").lower() for w in wanted)

    def _fetch_items(self, client: dict, ev: dict, day: date) -> Document:
        base = API.format(client=client["client"]) + f"/events/{ev['EventId']}/eventitems"
        url = _url(base, ITEM_PARAMS)
        stamp = [
            ev.get("EventLastModifiedUtc"),
            ev.get("EventAgendaLastPublishedUTC"),
            ev.get("EventMinutesLastPublishedUTC"),
        ]
        if day < self.today - timedelta(days=REUSE_AFTER_DAYS):
            known = self.session.exec(select(Document).where(Document.url == url).order_by(Document.id.desc())).first()
            if known and json.loads(known.meta_json or "{}").get("stamp") == stamp:
                return known
        resp = self.http.get_ok(base, params=ITEM_PARAMS)
        meta = {
            "kind": "eventitems",
            "client": client["client"],
            "place": client["name"],
            "stamp": stamp,
            "event": {k: v for k, v in ev.items() if k != "EventItems"},
        }
        text, _ = render_event(meta, resp.json())
        return self.save(
            url=url,
            content=resp.content,
            content_type="application/json",
            title=f"{client['name']} {clean_body(ev.get('EventBodyName'))} {day.isoformat()} agenda items",
            meta=meta,
            text=text,
        )

    def _fetch_matters(self, client: dict) -> Document:
        lo, hi = self.window
        base = API.format(client=client["client"]) + "/matters"
        params = {"$filter": f"MatterAgendaDate ge {odata_date(lo)} and MatterAgendaDate lt {odata_date(hi)}"}
        resp = self.http.get(base, params=params)
        if resp.status_code == 403:
            raise ConnectorUnavailable(f"{client['client']}: HTTP 403 (token required)")
        if resp.status_code != 200:
            raise FetchError(f"{client['client']}: HTTP {resp.status_code} on /matters: {resp.text[:160]}")
        meta = {
            "kind": "matters",
            "client": client["client"],
            "place": client["name"],
            "window": [lo.isoformat(), hi.isoformat()],
        }
        text, _ = render_matters(client["name"], resp.json())
        return self.save(
            url=_url(base, params),
            content=resp.content,
            content_type="application/json",
            title=f"{client['name']} Legistar matters on agendas {lo.isoformat()}..{hi.isoformat()}",
            meta=meta,
            text=text,
        )

    # --- parse ----------------------------------------------------------------------------------
    def parse(self, doc: Document) -> list[dict]:
        meta = json.loads(doc.meta_json or "{}")
        if meta.get("kind") not in ("eventitems", "matters"):
            return []
        text = read_document_text(doc, self.settings)
        raw = json.loads(read_document_bytes(doc, self.settings))
        client = self._by_client.get(
            meta["client"], {"client": meta["client"], "name": meta.get("place"), "tickers": []}
        )
        if meta["kind"] == "eventitems":
            rendered, spans = render_event(meta, raw)
            if rendered != text:  # renderer changed since this doc was stored: match the stored text as one block
                spans = [(0, len(text), {})]
            ev = meta["event"]
            return [
                {
                    "kind": "event",
                    "document_id": doc.id,
                    "client": client,
                    "text": text,
                    "range": (0, len(text)),
                    "spans": spans,
                    "date": ev["EventDate"][:10],
                    "body": clean_body(ev.get("EventBodyName")),
                    "period": f"{meta['client']}:event:{ev['EventId']}",
                    "citation_url": ev.get("EventInSiteURL") or ev.get("EventAgendaFile"),
                    "detail": {
                        "client": meta["client"],
                        "event_id": ev["EventId"],
                        "meeting_page": ev.get("EventInSiteURL"),
                        "agenda_url": ev.get("EventAgendaFile"),
                        "minutes_url": ev.get("EventMinutesFile"),
                        "api_url": doc.url,
                    },
                }
            ]
        rendered, spans = render_matters(meta.get("place") or client["name"], raw)
        if rendered != text:
            spans = []
        groups: dict[tuple[str, str], list[tuple[int, int, dict]]] = {}
        for span in spans:
            groups.setdefault(span[2]["_group"], []).append(span)
        out = []
        for (day, body), group in groups.items():
            if not day:
                continue
            start = text.rfind("\n\n", 0, group[0][0] - 1)
            out.append(
                {
                    "kind": "matters",
                    "document_id": doc.id,
                    "client": client,
                    "text": text,
                    "range": (max(0, start), group[-1][1]),
                    "spans": group,
                    "date": day,
                    "body": body,
                    "period": f"{meta['client']}:matters:{day}:{body}",
                    "citation_url": None,  # the first matched matter's legislation page (to_signals)
                    "detail": {"client": meta["client"], "api_url": doc.url},
                }
            )
        return out

    # --- signals --------------------------------------------------------------------------------
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        universe = self.universe()
        drafts: list[SignalDraft] = []
        for rec in records:
            for ticker in rec["client"].get("tickers") or []:
                wl = self.watchlists.get(ticker)
                if wl is None or ticker not in universe:
                    continue
                items = self._items(rec, wl)
                if not items:
                    continue
                matched = [self._span_of(rec, item) for item in items]
                citation = rec["citation_url"]
                detail = dict(rec["detail"])
                if rec["kind"] == "matters":
                    first = next((s[2] for s in matched if s and s[2].get("MatterId")), None)
                    if first is None:
                        continue
                    citation = INSITE_MATTER.format(client=rec["client"]["client"], id=first["MatterId"])
                    detail["matters"] = [s[2].get("MatterFile") for s in matched if s]
                else:
                    detail["event_item_ids"] = [s[2].get("EventItemId") for s in matched if s and s[2]]
                detail["why"] = rec["client"].get("why")
                drafts.append(
                    agenda_signal(
                        ticker=ticker,
                        items=items,
                        text=rec["text"],
                        place=rec["client"]["name"],
                        body=rec["body"],
                        meeting_date=date.fromisoformat(rec["date"]),
                        citation_url=citation,
                        document_id=rec["document_id"],
                        period=rec["period"],
                        detail=detail,
                    )
                )
        return drafts

    def _items(self, rec: dict, wl: Watchlist) -> list:
        """Triggering items inside the record's range, labelled by the whole agenda-item block they sit in."""
        key = (rec["document_id"], wl.ticker)
        if key not in self._matched:
            self._matched[key] = match_items(rec["text"], wl)
        lo, hi = rec["range"]
        out = []
        for item in self._matched[key]:
            if not lo <= item.start < hi:
                continue
            span = self._span_of(rec, item)
            if span and span[2]:
                block = rec["text"][span[0] : span[1]]
                if wl.item_excluded(block):
                    continue
                item.label, item.factor = classify_action(block)
            out.append(item)
        return out

    @staticmethod
    def _span_of(rec: dict, item: Any) -> tuple[int, int, dict] | None:
        return next((s for s in rec["spans"] if s[0] <= item.start < s[1]), None)
