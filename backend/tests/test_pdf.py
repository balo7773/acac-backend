import fitz
import pytest
from fastapi import HTTPException
from app.services.pdf_service import (
    count_words,
    locate_verbatim_quote,
    normalize_canonical_text,
    process_and_store_pdf,
)


def _make_pdf(num_pages: int, words_per_page: int) -> bytes:
    doc = fitz.open()
    # 12 words per sentence
    sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
    repeats = (words_per_page // 12) + 2
    for _ in range(num_pages):
        page = doc.new_page()
        text = sentence * repeats
        rect = fitz.Rect(50, 50, 550, 750)
        page.insert_textbox(rect, text, fontsize=8)
    b = doc.tobytes()
    doc.close()
    return b


def test_normalize_canonical_text_and_ligatures():
    raw = "The  ﬁnancial   ﬂow\r\n\r\n\r\nin Nigeria\u200b is growing."
    norm = normalize_canonical_text(raw)
    assert norm == "The financial flow\n\nin Nigeria is growing."
    assert count_words(norm) == 7


def test_locate_verbatim_quote_exact_and_wrapped():
    essay = "Monetary policy in Nigeria requires structural fiscal coordination.\n\nInflation targeting alone cannot resolve supply shocks."
    status_1, start_1, end_1 = locate_verbatim_quote(essay, "structural fiscal coordination.")
    assert status_1 == "verified"
    assert essay[start_1:end_1] == "structural fiscal coordination."

    status_2, start_2, end_2 = locate_verbatim_quote(essay, "hallucinated quote not in text")
    assert status_2 == "unverified"
    assert start_2 is None and end_2 is None


def test_process_and_store_pdf_valid_and_invalid(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    # 1. Reject non-PDF bytes
    with pytest.raises(HTTPException) as exc:
        process_and_store_pdf(b"not a pdf file", participant_number=1001)
    assert exc.value.status_code == 400

    # 2. Valid PDF with >= 1000 words and <= 5 pages (600 words * 2 pages = 1200 words)
    pdf_bytes = _make_pdf(num_pages=2, words_per_page=600)
    extracted = process_and_store_pdf(pdf_bytes, participant_number=1001)
    assert extracted.page_count == 2
    assert extracted.word_count >= 1000
    assert "Nigeria economic policy" in extracted.canonical_text


def test_fix_2_1_page_and_word_limits(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    # NOTE: page-count maximum removed 2026-10-24. Word count is the content gate.
    # 6-page PDF with >=1000 words should now be accepted.
    pdf_6_pages = _make_pdf(num_pages=6, words_per_page=200)  # 1200 words
    res_6p = process_and_store_pdf(pdf_6_pages, participant_number=1002)
    assert res_6p.page_count == 6
    assert res_6p.word_count >= 1000

    # 800-word PDF rejected (< 1000)
    doc = fitz.open()
    sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
    for _ in range(2):
        p = doc.new_page()
        p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 30, fontsize=9)  # 30 * 12 = 360 words/page -> 720 total
    pdf_under_1000 = doc.tobytes()
    doc.close()

    with pytest.raises(HTTPException) as exc2:
        process_and_store_pdf(pdf_under_1000, participant_number=1003)
    assert exc2.value.status_code == 400
    assert "minimum 1000" in exc2.value.detail

    # 5-page, >=1000-word PDF passes (250 words * 5 pages = 1250 words)
    pdf_5_pages_1000 = _make_pdf(num_pages=5, words_per_page=250)
    res = process_and_store_pdf(pdf_5_pages_1000, participant_number=1004)
    assert res.page_count == 5
    assert res.word_count >= 1000
