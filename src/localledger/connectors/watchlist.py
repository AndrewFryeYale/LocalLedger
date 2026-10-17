"""Keyword watchlists shared by the agenda connectors (Legistar, PDF agendas).

config/watchlists/{TICKER}.yaml lists the names a company's local-government exposure shows up under:
  company   names and aliases                    (default weight 0.8)
  projects  project / community names            (default weight 1.0)
  parcels   parcel numbers, addresses, streets   (default weight 0.9)
  policy    jurisdiction-wide rules that bite    (default weight 0.6), e.g. "rent stabilization"
  generic   place names used only as context     (default weight 0.3); generic hits never trigger a signal alone
  exclude   phrases that are never a hit (a term inside "Canterbury Road" doesn't count)
  exclude_items  phrases that disqualify a whole agenda item (e.g. "Special Event Liquor License": a gala held at
            the company's office is not news)
A term is a string or a mapping {term, weight, near: [...], window, exclude: [...], note}. `near` requires one of
the words within `window` characters of the hit; a term-level `exclude` drops only that term's hits.

Matching is whole-word and case-insensitive, ignores accents and okina/apostrophes between letters
(Kāʻanapali == Kaanapali == Ka'anapali), lets a phrase wrap across lines (PDF text), and treats street suffixes
as their abbreviations (Street == St.). Every snippet and item text is an exact substring of the matched text,
so a fact-checker can find the quote verbatim.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from localledger.signals.store import SignalDraft

CATEGORY_WEIGHTS = {"project": 1.0, "parcel": 0.9, "company": 0.8, "policy": 0.6, "generic": 0.3}
SECTIONS = {"projects": "project", "parcels": "parcel", "company": "company", "policy": "policy", "generic": "generic"}
SNIPPET_CHARS = 300
EXCERPT_MAX = 4000

_APOS = "'‘’ʻʼ`´"
_APOS_OPT = f"[{_APOS}]?"
_GAP = r"[\s\-–—]+"  # between words of a phrase: spaces, line breaks, hyphens
_STREET = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "drive": "dr",
    "boulevard": "blvd",
    "place": "pl",
    "lane": "ln",
    "court": "ct",
    "highway": "hwy",
    "terrace": "ter",
    "parkway": "pkwy",
}
# Start of an agenda item line: "A.", "12.", "IV.", "(b)", "Item 4", "5047 - ...", "2026-21 Ordinance", "[Item 3]".
ITEM_START = re.compile(
    r"^\s*(?:\[(?:Item|Matter)\b|(?:[IVXLC]{1,6}|[A-Za-z]|\d{1,3})[.)]\s|\(\w{1,3}\)\s|Item\s+\d|"
    r"\d{3,6}\s*[-–]\s|\d{4}-\d{1,4}\s)",
)

# Agenda-item type -> how much a match in it matters. First matching rule with the highest factor wins.
ACTION_RULES: list[tuple[str, float, re.Pattern]] = [
    (label, factor, re.compile(pattern, re.IGNORECASE))
    for label, factor, pattern in [
        ("public hearing", 1.0, r"\bpublic hearing|\bhearing to consider\b|\bsecond reading\b"),
        (
            "rezoning",
            1.0,
            r"\brezon\w*|\bchange (?:in|of) zon\w*|\bzone change|\bzoning (?:map|amendment|change|district|bill)|"
            r"\bconditional zoning|\bproject district\b",
        ),
        (
            "entitlement",
            1.0,
            r"\bentitlement|\bsubdivision|\bplat\b|\bsite plan|\bspecial (?:use|permit)|\bconditional use|\bvariance|"
            r"\bplanned (?:unit|development)|\bannex\w*|\bgeneral plan amendment|\bspecific plan|"
            r"\bdevelopment agreement|\benvironmental impact|\bSEQRA\b|\bCEQA\b|\bfindings statement|\bredevelopment",
        ),
        ("ordinance", 0.95, r"\bordinance\b|\bbill\s+\d|\blocal law\b|\bbill\b"),
        ("approval", 0.9, r"\bapprov\w*|\badopt\w*|\bauthoriz\w*|\baward\w*|\bvote\b|\baction\b|\bresolution\b"),
        ("permit", 0.85, r"\bpermit\b|\blicen[cs]e|\bwater use\b|\bwater rights?\b|\btax increment|\bTIF\b|\bbond\w*"),
        ("contract", 0.8, r"\bcontract\b|\bagreement\b|\blease\b|\bpurchase\b|\bacquisition\b|\bsale of\b|\bsale\b"),
        ("discussion", 0.6, r"\bdiscussion\b|\bupdate\b|\breport\b|\breview\b|\bstudy session\b|\bwork session\b"),
        (
            "presentation",
            0.45,
            r"\bpresentation\b|\bminutes\b|\bcorrespondence\b|\bcommunications?\b|\binformational\b|"
            r"\breceive and file\b|\bproclamation\b",
        ),
    ]
]
DEFAULT_ACTION = ("agenda item", 0.7)


# --- text folding ---------------------------------------------------------------------------------
def fold(text: str) -> tuple[str, list[int]]:
    """Accent-stripped copy of `text` plus, for each folded char, the index of the original char."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isascii():
            out.append(ch)
            index.append(i)
            continue
        for base in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(base):
                continue
            out.append(base)
            index.append(i)
    return "".join(out), index


def phrase_regex(phrase: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern for a phrase over folded text (see module docstring)."""
    folded, _ = fold(phrase.strip())
    words = []
    ends_in_word = folded[-1:].isalnum()
    for word in folded.split():
        suffix = _STREET.get(word.lower().rstrip("."))
        if suffix:
            words.append(rf"(?:{re.escape(word.rstrip('.'))}|{suffix}\.?)")
            ends_in_word = True
            continue
        chars = []
        for j, ch in enumerate(word):
            if ch in _APOS:
                chars.append(_APOS_OPT)
                continue
            chars.append(re.escape(ch))
            if j + 1 < len(word) and ch.isalpha() and word[j + 1].isalpha():
                chars.append(_APOS_OPT)
        words.append("".join(chars))
        ends_in_word = word[-1:].isalnum()
    body = _GAP.join(words)
    # Whole word: nothing word-like glued on either side (a phrase ending in "." is its own boundary).
    tail = r"(?!\w)" if ends_in_word else ""
    return re.compile(rf"(?<!\w){body}{tail}", re.IGNORECASE)


# --- watchlists -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Term:
    text: str
    category: str
    weight: float
    near: tuple[str, ...] = ()
    window: int = 120
    exclude: tuple[str, ...] = ()
    note: str | None = None
    pattern: re.Pattern = field(compare=False, repr=False, default=None)
    near_patterns: tuple[re.Pattern, ...] = field(compare=False, repr=False, default=())
    exclude_patterns: tuple[re.Pattern, ...] = field(compare=False, repr=False, default=())


def make_term(raw: str | dict, category: str) -> Term:
    spec = {"term": raw} if isinstance(raw, str) else dict(raw)
    text = str(spec["term"]).strip()
    near = tuple(str(n) for n in spec.get("near") or ())
    exclude = tuple(str(x) for x in spec.get("exclude") or ())
    return Term(
        text=text,
        category=category,
        weight=float(spec.get("weight", CATEGORY_WEIGHTS[category])),
        near=near,
        window=int(spec.get("window", 120)),
        exclude=exclude,
        note=spec.get("note"),
        pattern=phrase_regex(text),
        near_patterns=tuple(phrase_regex(n) for n in near),
        exclude_patterns=tuple(phrase_regex(x) for x in exclude),
    )


@dataclass
class Watchlist:
    ticker: str
    terms: list[Term]
    exclude: list[str] = field(default_factory=list)
    exclude_patterns: list[re.Pattern] = field(default_factory=list, repr=False)
    exclude_items: list[str] = field(default_factory=list)
    exclude_item_patterns: list[re.Pattern] = field(default_factory=list, repr=False)

    def item_excluded(self, text: str) -> bool:
        folded = fold(text)[0]
        return any(p.search(folded) for p in self.exclude_item_patterns)

    @classmethod
    def from_dict(cls, ticker: str, data: dict[str, Any]) -> Watchlist:
        terms = []
        for section, category in SECTIONS.items():
            for raw in data.get(section) or []:
                terms.append(make_term(raw, category))
        exclude = [str(x) for x in data.get("exclude") or []]
        exclude_items = [str(x) for x in data.get("exclude_items") or []]
        return cls(
            ticker=str(data.get("ticker") or ticker).upper(),
            terms=terms,
            exclude=exclude,
            exclude_patterns=[phrase_regex(x) for x in exclude],
            exclude_items=exclude_items,
            exclude_item_patterns=[phrase_regex(x) for x in exclude_items],
        )


def load_watchlist(path: Path) -> Watchlist:
    return Watchlist.from_dict(path.stem, yaml.safe_load(path.read_text()) or {})


def load_watchlists(directory: Path, tickers: set[str] | list[str] | None = None) -> dict[str, Watchlist]:
    """All config/watchlists/*.yaml (optionally only `tickers`), keyed by ticker."""
    wanted = {t.upper() for t in tickers} if tickers else None
    out: dict[str, Watchlist] = {}
    for path in sorted(directory.glob("*.yaml")) if directory.exists() else []:
        if wanted is not None and path.stem.upper() not in wanted:
            continue
        wl = load_watchlist(path)
        out[wl.ticker] = wl
    return out


# --- matching -------------------------------------------------------------------------------------
@dataclass
class Match:
    ticker: str
    term: str
    category: str
    weight: float
    start: int  # offsets into the original text
    end: int
    matched: str  # text[start:end]
    snippet: str  # text[snippet_start:snippet_end]
    snippet_start: int
    snippet_end: int

    @property
    def triggers(self) -> bool:
        return self.category != "generic"


def _spans(patterns: list[re.Pattern] | tuple[re.Pattern, ...], folded: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for p in patterns for m in p.finditer(folded)]


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < e and s < span[1] for s, e in spans)


def match(text: str, watchlist: Watchlist) -> list[Match]:
    """Every watchlist hit in `text`, overlapping hits resolved to the longest (then heaviest) term."""
    if not text:
        return []
    folded, index = fold(text)
    excluded = _spans(watchlist.exclude_patterns, folded)
    hits: list[tuple[int, int, Term]] = []
    for term in watchlist.terms:
        term_excluded = _spans(term.exclude_patterns, folded)
        for m in term.pattern.finditer(folded):
            span = (m.start(), m.end())
            if _overlaps(span, excluded) or _overlaps(span, term_excluded):
                continue
            if term.near:
                lo, hi = max(0, span[0] - term.window), min(len(folded), span[1] + term.window)
                window = folded[lo:hi]
                if not any(p.search(window) for p in term.near_patterns):
                    continue
            hits.append((span[0], span[1], term))
    hits.sort(key=lambda h: (-(h[1] - h[0]), -h[2].weight, h[0]))
    kept: list[tuple[int, int, Term]] = []
    for hit in hits:
        if not _overlaps((hit[0], hit[1]), [(k[0], k[1]) for k in kept]):
            kept.append(hit)
    out = []
    for fs, fe, term in sorted(kept, key=lambda h: h[0]):
        start, end = index[fs], index[fe - 1] + 1
        s0, s1 = snippet_bounds(text, start, end)
        out.append(
            Match(
                ticker=watchlist.ticker,
                term=term.text,
                category=term.category,
                weight=term.weight,
                start=start,
                end=end,
                matched=text[start:end],
                snippet=text[s0:s1],
                snippet_start=s0,
                snippet_end=s1,
            )
        )
    return out


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return start, len(text) if end == -1 else end


def snippet_bounds(text: str, start: int, end: int, chars: int = SNIPPET_CHARS) -> tuple[int, int]:
    """The agenda item's line(s) around a hit: back to the item's first line, forward to the next item or blank
    line, never more than `chars` beyond the hit on either side."""
    s0, line_end = _line_bounds(text, start)
    while not ITEM_START.match(text[s0:line_end]) and s0 > 0:
        prev_start, prev_end = _line_bounds(text, s0 - 1)
        if not text[prev_start:prev_end].strip() or start - prev_start > chars:
            break
        s0, line_end = prev_start, prev_end
    _, s1 = _line_bounds(text, max(start, end - 1))
    while s1 < len(text):
        nxt_start, nxt_end = _line_bounds(text, s1 + 1)
        line = text[nxt_start:nxt_end]
        if not line.strip() or ITEM_START.match(line) or nxt_end - end > chars:
            break
        s1 = nxt_end
    if start - s0 > chars:  # one very long line: clip at a word boundary
        s0 = start - chars
        s0 = next((i + 1 for i in range(s0, start) if text[i].isspace()), s0)
    if s1 - end > chars:
        s1 = end + chars
        s1 = next((i for i in range(s1, end, -1) if text[i - 1].isspace()), s1)
    while s0 < start and text[s0].isspace():
        s0 += 1
    while s1 > end and text[s1 - 1].isspace():
        s1 -= 1
    return s0, s1


# --- agenda items -----------------------------------------------------------------------------------
_MARKER = re.compile(r"^\s*(?:[IVXLC]{1,6}|[A-Z]|\d{1,2})[.)]\s+")


def section_heading(text: str, pos: int, lookback: int = 1500) -> str | None:
    """Nearest short ALL-CAPS line before `pos` ("I. PUBLIC HEARINGS/DECISIONS:", "CONSENT AGENDA")."""
    lo = max(0, pos - lookback)
    for line in reversed(text[lo:pos].split("\n")[:-1] or []):
        stripped = _MARKER.sub("", line.strip())
        letters = [c for c in stripped if c.isalpha()]
        if 5 <= len(letters) and len(stripped) <= 60 and sum(c.isupper() for c in letters) >= 0.8 * len(letters):
            return line.strip()
    return None


def classify_action(text: str) -> tuple[str, float]:
    """(label, factor) for an agenda item: hearings, rezonings and approvals outrank presentations/minutes."""
    best: tuple[str, float] | None = None
    for label, factor, pattern in ACTION_RULES:
        if pattern.search(text) and (best is None or factor > best[1]):
            best = (label, factor)
    return best or DEFAULT_ACTION


@dataclass
class ItemMatch:
    """One agenda item (a run of overlapping snippets) and the watchlist hits inside it."""

    ticker: str
    start: int
    end: int
    text: str
    matches: list[Match]
    label: str = DEFAULT_ACTION[0]
    factor: float = DEFAULT_ACTION[1]

    @property
    def triggers(self) -> bool:
        return any(m.triggers for m in self.matches)

    @property
    def terms(self) -> list[str]:
        """Distinct configured terms, triggering ones first."""
        ordered = sorted(self.matches, key=lambda m: (not m.triggers, -m.weight, m.start))
        return list(dict.fromkeys(m.term for m in ordered))

    @property
    def strength(self) -> float:
        weights = [m.weight for m in self.matches if m.triggers]
        if not weights:
            return 0.0
        bonus = 0.05 * (len(self.terms) - 1)
        return round(min(1.0, max(weights) + bonus) * self.factor, 4)


def group_items(text: str, matches: list[Match], classify: bool = True) -> list[ItemMatch]:
    """Merge hits whose snippets overlap into items (adjacent items stay separate); item text = text[start:end]."""
    items: list[ItemMatch] = []
    for m in sorted(matches, key=lambda m: m.snippet_start):
        if items and items[-1].ticker == m.ticker and m.snippet_start < items[-1].end:
            item = items[-1]
            item.end = max(item.end, m.snippet_end)
            item.matches.append(m)
        else:
            items.append(ItemMatch(m.ticker, m.snippet_start, m.snippet_end, "", [m]))
    for item in items:
        item.text = text[item.start : item.end]
        if classify:
            # The section an item sits under ("PUBLIC HEARINGS") says what kind of item it is.
            heading = section_heading(text, item.start)
            item.label, item.factor = classify_action(f"{heading}\n{item.text}" if heading else item.text)
    return items


def match_items(text: str, watchlist: Watchlist) -> list[ItemMatch]:
    """Agenda items in `text` that trigger for this watchlist (generic-only and excluded items are dropped)."""
    items = group_items(text, match(text, watchlist))
    return [i for i in items if i.triggers and not watchlist.item_excluded(i.text)]


def event_significance(items: list[ItemMatch]) -> float:
    """0.15 floor plus 0.8 x the noisy-OR of item strengths; capped at 0.95 (one vote on a project ~ 0.95)."""
    miss = 1.0
    for item in items:
        miss *= 1.0 - item.strength
    return round(min(0.95, 0.15 + 0.8 * (1.0 - miss)), 4)


def excerpt_for(text: str, items: list[ItemMatch]) -> str:
    """Verbatim excerpt: the span covering every matched item if short enough, else the strongest item."""
    start, end = min(i.start for i in items), max(i.end for i in items)
    if end - start <= EXCERPT_MAX:
        return text[start:end]
    return max(items, key=lambda i: i.strength).text


def agenda_signal(
    *,
    ticker: str,
    items: list[ItemMatch],
    text: str,
    place: str,
    body: str,
    meeting_date: date,
    citation_url: str,
    document_id: int,
    period: str,
    detail: dict[str, Any] | None = None,
) -> SignalDraft:
    """The `agenda_match` signal for one (ticker, meeting): value = number of matching agenda items."""
    terms = list(dict.fromkeys(t for item in items for t in item.terms if t))
    trigger_terms = list(dict.fromkeys(m.term for item in items for m in item.matches if m.triggers))
    labels = list(dict.fromkeys(i.label for i in sorted(items, key=lambda i: -i.factor)))
    n = len(items)
    noun = "agenda item mentions" if n == 1 else "agenda items mention"
    summary = (
        f"{body}, {place} ({meeting_date.isoformat()}): {n} {noun} "
        f"{' / '.join(trigger_terms[:3])} ({'; '.join(labels[:3])})."
    )
    payload = {
        "excerpt": excerpt_for(text, items),
        "excerpts": [i.text for i in items],
        "keywords": terms,
        "meeting_date": meeting_date.isoformat(),
        "body": body,
        "place": place,
        "items": [
            {
                "label": i.label,
                "strength": i.strength,
                "terms": i.terms,
                "matched": [{"term": m.term, "category": m.category, "text": m.matched} for m in i.matches],
            }
            for i in items
        ],
    }
    payload.update(detail or {})
    return SignalDraft(
        ticker=ticker,
        metric="agenda_match",
        as_of=meeting_date,
        period=period,
        value=float(n),
        unit="agenda items",
        significance=event_significance(items),
        citation_url=citation_url,
        document_id=document_id,
        summary=summary,
        detail=payload,
    )
