"""PDF text extraction (pdfplumber)."""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def pdf_to_text(content: bytes, max_pages: int = 200) -> str:
    import pdfplumber

    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages[:max_pages]:
                pages.append(page.extract_text() or "")
    except Exception as exc:  # malformed PDFs are common in municipal postings
        log.warning("pdf extraction failed: %s", exc)
    return "\n\n".join(pages).strip()
