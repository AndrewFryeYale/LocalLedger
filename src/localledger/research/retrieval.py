"""Find the 10-K/10-Q passages relevant to a signal (BM25 over paragraph chunks) and excerpt source documents."""

from __future__ import annotations

import json
import math
import re
from collections import Counter

from sqlmodel import Session, select

from localledger.http import read_document_text
from localledger.models import Company, Document, Signal

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(
    "the a an and or of to in for on by with from at as is are was were be been this that these those its it "
    "our we us their which such any all may other than not no".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def chunk_text(text: str, size: int = 1400) -> list[str]:
    """Greedy paragraph packing into ~size-char chunks (paragraph boundaries preserved for verbatim quotes)."""
    chunks, buf = [], ""
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) > size and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.avgdl = sum(len(d) for d in docs) / max(1, len(docs))
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def score(self, query: list[str]) -> list[float]:
        scores = []
        for d, tf in zip(self.docs, self.tf, strict=True):
            s = 0.0
            for t in query:
                if t not in tf:
                    continue
                f = tf[t]
                s += (
                    self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
                )
            scores.append(s)
        return scores


def latest_filings(session: Session, ticker: str, kinds: tuple[str, ...] = ("10-k", "10-q")) -> list[Document]:
    docs = session.exec(
        select(Document).where(Document.ticker == ticker, Document.source_id == "edgar").order_by(Document.id.desc())
    ).all()
    out, seen = [], set()
    for d in docs:
        kind = json.loads(d.meta_json or "{}").get("kind")
        if kind in kinds and kind not in seen:
            out.append(d)
            seen.add(kind)
    return out


def filing_passages(session: Session, ticker: str, query: str, k: int = 3, size: int = 1400) -> list[dict]:
    """Top-k BM25 chunks across the latest 10-K and 10-Q, each as a citable document dict."""
    candidates = []
    for doc in latest_filings(session, ticker):
        text = read_document_text(doc)
        for chunk in chunk_text(text, size):
            if len(chunk) > 200:
                candidates.append((doc, chunk))
    if not candidates:
        return []
    bm = BM25([tokenize(c) for _, c in candidates])
    scores = bm.score(tokenize(query))
    ranked = sorted(zip(scores, range(len(candidates)), strict=True), reverse=True)[:k]
    return [
        {
            "document_id": candidates[i][0].id,
            "url": candidates[i][0].url,
            "title": candidates[i][0].title,
            "text": candidates[i][1],
        }
        for s, i in ranked
        if s > 0
    ]


def _json_object_around(raw: str, needle: str) -> str | None:
    idx = raw.find(needle)
    if idx == -1:
        return None
    start = raw.rfind("{", 0, idx)
    end = raw.find("}", idx)
    return raw[start : end + 1] if start != -1 and end != -1 else None


def signal_excerpt(session: Session, signal: Signal, doc: Document | None = None, max_chars: int = 6000) -> str:
    """The part of a signal's source document that the signal was computed from (verbatim)."""
    doc = doc or session.get(Document, signal.document_id)
    detail = json.loads(signal.detail_json or "{}")
    if detail.get("excerpt"):
        return detail["excerpt"]
    text = read_document_text(doc)
    meta = json.loads(doc.meta_json or "{}")
    if meta.get("kind") == "frame" and detail.get("accn"):
        row = _json_object_around(text, f'"accn":"{detail["accn"]}"')
        return (
            f"XBRL frame {meta.get('tag')} {meta.get('period')} (USD). Row for this filer:\n{row}"
            if row
            else text[:800]
        )
    if meta.get("kind") == "submissions":
        accession = signal.period or ""
        row = _filing_row(text, accession)
        return row or text[:800]
    if len(text) <= max_chars:
        return text
    company = session.get(Company, signal.ticker)
    keys = [w for w in tokenize(f"{signal.metric} {company.name if company else ''}") if len(w) > 3]
    head = text[: max_chars // 2]
    windows = []
    for key in keys[:4]:
        i = text.lower().find(key, max_chars // 2)
        if i != -1:
            windows.append(text[max(0, i - 600) : i + 900])
    return (head + "\n...\n" + "\n...\n".join(windows))[:max_chars]


def _filing_row(raw: str, accession: str) -> str | None:
    """Render one filing from a submissions JSON as 'field: value' lines (values verbatim from the JSON)."""
    try:
        sub = json.loads(raw)
    except json.JSONDecodeError:
        return None
    recent = sub.get("filings", {}).get("recent", {})
    accs = recent.get("accessionNumber", [])
    if accession not in accs:
        return None
    i = accs.index(accession)
    fields = ["form", "filingDate", "reportDate", "items", "accessionNumber", "primaryDocDescription"]
    lines = [f"{sub.get('name')} (CIK {sub.get('cik')}) EDGAR filing record:"]
    lines += [f'"{f}": "{recent[f][i]}"' for f in fields if f in recent and recent[f][i]]
    return "\n".join(lines)
