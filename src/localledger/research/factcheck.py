"""Citation enforcement and the fact-check gate. Credibility is the product, so this is strict.

1. validate_claims(): every claim must cite a provided document (id + url) with a non-empty quote, or the whole
   note fails with CitationError.
2. Deterministic pass: the quote must appear verbatim (after whitespace/quote normalization) in the cited
   document, and every number in the claim must appear in the quote/document or be a computed signal field.
3. LLM pass (fast model): re-reads the cited text around each quote and rejects claims it doesn't support.
Rejected claims are dropped and logged; they never reach a reader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from localledger.llm.client import LLM
from localledger.llm.mock import register_mock
from localledger.signals.store import CitationError

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-", "\xa0": " ", "​": ""})
_NUM = re.compile(
    r"(?<![\w.])(-?\$?\d[\d,]*(?:\.\d+)?)\s*(%|percent|million|billion|thousand|mm|bn|[mbk](?![a-z]))?",
    re.IGNORECASE,
)
_SCALE = {"million": 1e6, "mm": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3}
# Recommendation language (not ordinary business verbs: a homebuilder "buys lots", a utility "sells water").
RECOMMENDATION = re.compile(
    r"\b(?:(?:we|i|(?:investors|readers|subscribers|holders|shareholders|everyone) should|you should) (?:would )?"
    r"(?:buy|sell|short|accumulate|own|dump)|(?:investors|readers|subscribers|shareholders) should (?:consider )?"
    r"(?:buying|selling|owning)|"
    r"(?:buy|sell|short|accumulate) (?:the )?(?:stock|shares|name)|(?:buy|sell|hold) (?:rating|recommendation)|"
    r"rated? (?:a )?(?:buy|sell)|strong buy|price target|target price|overweight|underweight|"
    r"outperform|underperform|you should|we recommend|undervalued|overvalued)\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    return " ".join(text.translate(_QUOTES).split()).lower()


@dataclass(frozen=True)
class Num:
    value: float
    decimals: int
    scale: float
    is_pct: bool
    raw: str


def extract_numbers(text: str) -> list[Num]:
    out = []
    for m in _NUM.finditer(text.translate(_QUOTES)):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        digits = raw.replace("$", "").replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append(Num(abs(value), decimals, _SCALE.get(unit, 1.0), unit in ("%", "percent"), raw))
    return out


def _is_context_number(n: Num) -> bool:
    """Years, quarter/fiscal labels and tiny counters are context, not claims."""
    if n.is_pct or n.scale != 1.0 or n.decimals:
        return False
    return 1990 <= n.value <= 2100 or n.value <= 4


def numbers_supported(claim: str, corpus: str, computed: list[float]) -> list[str]:
    """Return the claim numbers that can't be found in the cited text or the computed signal fields."""
    doc_nums = extract_numbers(corpus)
    plain = {n.value * n.scale for n in doc_nums if not n.is_pct}
    # Financial tables state "(In thousands)" / "(in millions)"; scale only table-style values in such documents.
    tabular = {
        n.value
        for n in doc_nums
        if not n.is_pct and n.scale == 1.0 and ("," in n.raw or "$" in n.raw or n.value >= 100 or n.decimals)
    }
    candidates = set(plain)
    if re.search(r"in thousands", corpus, re.IGNORECASE):
        candidates |= {v * 1e3 for v in tabular}
    if re.search(r"in millions", corpus, re.IGNORECASE):
        candidates |= {v * 1e6 for v in tabular}
    pcts = {n.value for n in doc_nums if n.is_pct}
    comp = {abs(c) for c in computed if c is not None}
    comp_pct = {abs(c) * 100 for c in computed if c is not None and abs(c) < 50}
    missing = []
    for n in extract_numbers(claim):
        if _is_context_number(n):
            continue
        tol = 0.5 * 10 ** (-n.decimals) * n.scale + 1e-9
        if n.is_pct:
            pool = pcts | comp_pct | comp
            target = n.value
            tol = max(0.5 * 10 ** (-n.decimals), 0.051)
        else:
            pool = candidates | comp
            target = n.value * n.scale
        if not any(abs(c - target) <= tol for c in pool):
            missing.append(n.raw + (f" {n.scale:g}x" if n.scale != 1 else "") + ("%" if n.is_pct else ""))
    return missing


def validate_claims(claims: list[dict], allowed_docs: dict[int, str]) -> None:
    """Raise CitationError if any claim lacks a citation to a provided document. A note with one uncited claim fails."""
    if not claims:
        raise CitationError("note has no claims")
    for i, c in enumerate(claims):
        cit = c.get("citation") or {}
        doc_id = cit.get("document_id")
        try:
            doc_id = int(doc_id)
        except (TypeError, ValueError):
            raise CitationError(f"claim {i} has no citation document_id") from None
        if not str(c.get("text", "")).strip():
            raise CitationError(f"claim {i} is empty")
        if doc_id not in allowed_docs:
            raise CitationError(f"claim {i} cites document {doc_id}, which was not provided")
        if not str(cit.get("url", "")).startswith("http"):
            raise CitationError(f"claim {i} has no citation url")
        if not str(cit.get("quote", "")).strip():
            raise CitationError(f"claim {i} has no supporting quote")
        cit["document_id"] = doc_id
        cit["url"] = allowed_docs[doc_id]  # always the stored document's canonical URL


def deterministic_check(claim: dict, corpus: str, computed: list[float]) -> str | None:
    """None if the claim passes, else the rejection reason."""
    quote = claim["citation"]["quote"]
    if len(quote.split()) < 3:
        return "quote too short to verify"
    if normalize(quote) not in normalize(corpus):
        return "quote not found verbatim in cited document"
    if quote_is_tainted(corpus, quote):
        return "quote comes from text flagged as possible prompt injection"
    missing = numbers_supported(claim["text"], corpus, computed)
    if missing:
        return f"numbers not in cited document: {', '.join(missing[:4])}"
    if RECOMMENDATION.search(claim["text"]):
        return "recommendation language (impersonal research only)"
    return None


def quote_is_tainted(corpus: str, quote: str) -> bool:
    """Taint tracking: a quote taken from (or right next to) an instruction-like span can't support a claim, even
    though it is verbatim. Checks the quote and the line around it (agenda packets put a payload on its own line)."""
    from localledger.safety import scan

    if scan(quote):
        return True
    text = corpus.translate(_QUOTES)
    words = quote.translate(_QUOTES).split()
    if not words:
        return False
    # Match across any whitespace (a quote may wrap a line) while keeping the document's own line breaks.
    m = re.search(r"\s+".join(re.escape(w) for w in words), text, re.IGNORECASE)
    if m is None:
        return False
    first_line = text.rfind("\n", 0, m.start())
    last_line = text.find("\n", m.end())
    start = max(first_line, m.start() - 240, 0)
    end = min(last_line if last_line != -1 else len(text), m.end() + 240)
    return bool(scan(text[start:end]))


def quote_context(corpus: str, quote: str, window: int = 700) -> str:
    norm_corpus, norm_quote = normalize(corpus), normalize(quote)
    i = norm_corpus.find(norm_quote)
    if i == -1:
        return norm_corpus[: 2 * window]
    return norm_corpus[max(0, i - window) : i + len(norm_quote) + window]


def fact_check(
    claims: list[dict],
    corpora: dict[int, str],
    computed: list[float],
    llm: LLM | None,
    brand: str = "Local Ledger",
) -> tuple[list[dict], list[dict]]:
    """Return (kept, dropped). Each dropped item records the claim and why it was rejected."""
    kept, dropped = [], []
    for claim in claims:
        reason = deterministic_check(claim, corpora[claim["citation"]["document_id"]], computed)
        if reason:
            dropped.append({"claim": claim, "stage": "deterministic", "reason": reason})
        else:
            kept.append(claim)
    if kept and llm is not None:
        payload = [
            {
                "text": c["text"],
                "quote": c["citation"]["quote"],
                "document_id": c["citation"]["document_id"],
                "context": quote_context(corpora[c["citation"]["document_id"]], c["citation"]["quote"]),
            }
            for c in kept
        ]
        res = llm.json("factcheck_claims", {"brand": brand, "claims": payload})
        verdicts = {int(r.get("index", -1)): r for r in res.data.get("results", []) if isinstance(r, dict)}
        survivors = []
        for i, claim in enumerate(kept):
            v = verdicts.get(i)
            if v is not None and v.get("supported") is False:
                dropped.append({"claim": claim, "stage": "llm", "reason": str(v.get("reason", "not supported"))[:300]})
            else:
                survivors.append(claim)
        kept = survivors
    return kept, dropped


@register_mock("factcheck_claims")
def _mock_factcheck(variables: dict) -> dict:
    # The deterministic gate already ran; the mock supports every claim that reached it.
    return {
        "results": [
            {"index": i, "supported": True, "reason": "mock: deterministic checks passed"}
            for i, _ in enumerate(variables["claims"])
        ]
    }
