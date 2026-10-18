import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import fitz  # PyMuPDF
from fastapi import HTTPException, status

from app.core.config import settings

MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB hard ceiling


@dataclass
class ExtractedEssay:
    canonical_text: str
    page_count: int
    word_count: int
    saved_path: str


def normalize_canonical_text(raw_text: str) -> str:
    """
    Produces a deterministic, NFKC-normalized canonical string so character
    offsets (quote_start, quote_end) remain identical across PostgreSQL,
    Python AI workers, and the frontend reader.
    """
    text = unicodedata.normalize("NFKC", raw_text)
    text = re.sub(r"[\x00\u200b\u200c\u200d\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    joined = "\n".join(lines)
    canonical = re.sub(r"\n{3,}", "\n\n", joined).strip()
    return canonical


def count_words(canonical_text: str) -> int:
    if not canonical_text:
        return 0
    return len(re.findall(r"\b\w+(?:['’-]\w+)*\b", canonical_text))


def locate_verbatim_quote(canonical_text: str, quote_text: str) -> Tuple[str, int | None, int | None]:
    if not quote_text or not quote_text.strip():
        return ("none", None, None)

    norm_quote = normalize_canonical_text(quote_text)
    if not norm_quote:
        return ("none", None, None)

    idx = canonical_text.find(norm_quote)
    if idx != -1:
        return ("verified", idx, idx + len(norm_quote))

    tokens = [re.escape(tok) for tok in norm_quote.split()]
    if not tokens:
        return ("none", None, None)

    pattern = r"\s+".join(tokens)
    match = re.search(pattern, canonical_text, flags=re.IGNORECASE)
    if match:
        return ("verified", match.start(), match.end())

    return ("unverified", None, None)


def process_and_store_pdf(
    pdf_bytes: bytes,
    participant_number: int,
    enforce_min_words: bool = True,
) -> ExtractedEssay:
    if not pdf_bytes or len(pdf_bytes) < 8 or not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only standard PDF documents are accepted.",
        )

    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF file exceeds the 10 MB maximum size limit.",
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Corrupted or unreadable PDF document.",
        )

    if doc.is_encrypted:
        doc.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password-protected PDFs are not allowed.",
        )

    page_count = doc.page_count
    if page_count < 1 or page_count > settings.ESSAY_MAX_PAGES:
        doc.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PDF page count ({page_count}) must be between 1 and {settings.ESSAY_MAX_PAGES} pages.",
        )

    pages_text = []
    for page in doc:
        pages_text.append(page.get_text("text"))
    doc.close()

    canonical_text = normalize_canonical_text("\n\n".join(pages_text))
    word_count = count_words(canonical_text)

    if enforce_min_words and word_count < settings.ESSAY_MIN_WORDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Extracted text contains only {word_count} words (minimum {settings.ESSAY_MIN_WORDS}). "
                "Scanned image-only PDFs are not permitted; upload a text-selectable PDF."
            ),
        )

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"p{participant_number}_{uuid.uuid4().hex[:12]}.pdf"
    file_path = upload_dir / filename
    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    return ExtractedEssay(
        canonical_text=canonical_text,
        page_count=page_count,
        word_count=word_count,
        saved_path=str(file_path),
    )
