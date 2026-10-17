"""Polite HTTP: declared User-Agent, per-host rate limits, robots.txt, retries, and a raw document store.

Every fetched item that a signal can cite is saved under ./raw/ with its sha256 so claims are reproducible.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import threading
import time
import urllib.robotparser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session, select

from localledger.config import Settings, get_settings
from localledger.models import Document

log = logging.getLogger(__name__)

# Seconds between requests per host. SEC fair-access policy is 10 req/s; stay under it.
HOST_INTERVALS = {
    "www.sec.gov": 0.13,
    "data.sec.gov": 0.13,
    "efts.sec.gov": 0.2,
}
DEFAULT_INTERVAL = 1.0
TEXT_VERSION = 2  # bump when html_to_text/pdf_to_text change; stale text is re-derived from raw bytes


class RobotsDisallowed(Exception):
    pass


class FetchError(Exception):
    pass


class RateLimiter:
    def __init__(self) -> None:
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            ready = self._next.get(host, now)
            sleep_for = max(0.0, ready - now)
            self._next[host] = max(now, ready) + interval
        if sleep_for:
            time.sleep(sleep_for)


def load_robots_permissions(path: Path | None = None) -> list[dict]:
    path = path or get_settings().config_dir / "robots_permissions.yaml"
    if not path.exists():
        return []
    import yaml

    entries = (yaml.safe_load(path.read_text()) or {}).get("permissions") or []
    return [e for e in entries if e.get("host") and str(e.get("basis", "")).strip()]


class RobotsCache:
    """RFC 9309: 4xx on robots.txt -> allow all; 5xx/unreachable -> disallow all.

    A host/path listed in config/robots_permissions.yaml with a written basis is allowed even if robots.txt
    blocks generic agents.
    """

    def __init__(self, client: httpx.Client, user_agent: str, permissions: list[dict] | None = None) -> None:
        self._client = client
        self._ua = user_agent
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | bool] = {}
        self._permissions = load_robots_permissions() if permissions is None else permissions

    def permitted(self, url: str) -> bool:
        parts = urlparse(url)
        for entry in self._permissions:
            if entry["host"] == parts.netloc and any(parts.path.startswith(p) for p in entry.get("paths") or ["/"]):
                return True
        return False

    def allowed(self, url: str) -> bool:
        if self.permitted(url):
            return True
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._parsers:
            self._parsers[base] = self._load(base)
        parser = self._parsers[base]
        if isinstance(parser, bool):
            return parser
        return parser.can_fetch(self._ua, url)

    def _load(self, base: str) -> urllib.robotparser.RobotFileParser | bool:
        try:
            resp = self._client.get(f"{base}/robots.txt", timeout=20)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallow", base, exc)
            return False
        if 400 <= resp.status_code < 500:
            return True
        if resp.status_code >= 500:
            return False
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


class HttpClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            headers={"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=True,
        )
        self._limiter = RateLimiter()
        self._robots = RobotsCache(self._client, self.settings.user_agent)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        interval: float | None = None,
        check_robots: bool = True,
        retries: int = 3,
    ) -> httpx.Response:
        if self.settings.offline:
            raise FetchError(f"offline mode: refusing to fetch {url}")
        if check_robots and not self._robots.allowed(url):
            raise RobotsDisallowed(url)
        host = urlparse(url).netloc
        wait = interval if interval is not None else HOST_INTERVALS.get(host, DEFAULT_INTERVAL)
        last_exc: Exception | None = None
        for attempt in range(retries):
            self._limiter.wait(host, wait)
            try:
                resp = self._client.request(method, url, params=params, data=data, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = FetchError(f"{resp.status_code} from {url}")
                time.sleep(3 * (attempt + 1))
                continue
            return resp
        raise FetchError(str(last_exc))

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def get_ok(self, url: str, **kwargs: Any) -> httpx.Response:
        resp = self.get(url, **kwargs)
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        return resp

    def close(self) -> None:
        self._client.close()


_EXT = {
    "application/json": "json",
    "text/html": "html",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/zip": "zip",
    "application/xml": "xml",
}


_BLOCK_TAGS = ["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "title"]


def _tidy_line(line: str) -> str:
    line = " ".join(line.split())
    line = re.sub(r"(\|\s*)+\|", "|", line)  # empty cells
    line = re.sub(r"\$\s*\|\s*", "$", line)  # "$ | 2,900" -> "$2,900"
    line = re.sub(r"\s*\|\s*(\)|%)", r"\1", line)  # "(12 | )" -> "(12)", "31 | %" -> "31%"
    return line.strip(" |")


def html_to_text(html: str | bytes) -> str:
    """Readable text: newlines at block elements only (so sentences survive inline tags), cells joined by |."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for tag in soup.find_all(style=lambda s: s and "display:none" in s.replace(" ", "").lower()):
        tag.decompose()
    for tag in soup.find_all(["ix:header"]):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with(" | ".join(c for c in cells if c) + "\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    text = soup.get_text("").replace("​", "").replace("\xa0", " ")
    lines = [_tidy_line(line) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


class DocStore:
    """Writes raw bytes to ./raw/<source>/<sha[:2]>/<sha>.<ext>[.gz] and registers a Document row."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def save(
        self,
        *,
        source_id: str,
        url: str,
        content: bytes,
        content_type: str,
        ticker: str | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        origin: str = "real",
        text: str | None = None,
    ) -> Document:
        sha = hashlib.sha256(content).hexdigest()
        existing = self.session.exec(select(Document).where(Document.url == url, Document.sha256 == sha)).first()
        if existing:
            if ticker and not existing.ticker:
                existing.ticker = ticker
            return existing
        base_type = content_type.split(";")[0].strip().lower()
        ext = _EXT.get(base_type, "bin")
        rel_dir = Path(source_id) / sha[:2]
        abs_dir = self.settings.raw_dir / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        compress = ext in ("html", "json", "txt", "csv", "xml") and len(content) > 64_000
        name = f"{sha}.{ext}" + (".gz" if compress else "")
        abs_path = abs_dir / name
        if not abs_path.exists():
            abs_path.write_bytes(gzip.compress(content) if compress else content)
        text_rel = None
        if text is None and ext == "html":
            text = html_to_text(content)
        elif text is None and ext == "pdf":
            from localledger.pdftext import pdf_to_text

            text = pdf_to_text(content)
        if text is not None:
            text_rel = str(rel_dir / f"{sha}.v{TEXT_VERSION}.txt.gz")
            (self.settings.raw_dir / text_rel).write_bytes(gzip.compress(text.encode("utf-8")))
        doc = Document(
            source_id=source_id,
            ticker=ticker,
            url=url,
            title=title,
            sha256=sha,
            path=str(rel_dir / name),
            text_path=text_rel,
            content_type=base_type,
            origin=origin,
            meta_json=json.dumps(meta) if meta else None,
        )
        self.session.add(doc)
        self.session.flush()
        return doc


def read_document_bytes(doc: Document, settings: Settings | None = None) -> bytes:
    settings = settings or get_settings()
    path = settings.raw_dir / doc.path
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def read_document_text(doc: Document, settings: Settings | None = None) -> str:
    """Plain text of a stored document (cached extraction for HTML/PDF; decoded bytes otherwise)."""
    settings = settings or get_settings()
    # Text cache path is derived from the sha, so reading never writes to the DB (no lock contention).
    rel = str(Path(doc.path).parent / f"{doc.sha256}.v{TEXT_VERSION}.txt.gz")
    path = settings.raw_dir / rel
    if path.exists():
        return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    raw = read_document_bytes(doc, settings)
    if doc.content_type == "application/pdf":
        from localledger.pdftext import pdf_to_text

        text = pdf_to_text(raw)
    elif doc.content_type == "text/html":
        text = html_to_text(raw)
    else:
        return raw.decode("utf-8", errors="replace")
    # Extraction improved since this doc was stored: write a new versioned text file (raw bytes untouched).
    path.write_bytes(gzip.compress(text.encode("utf-8")))
    return text
