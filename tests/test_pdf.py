from pathlib import Path

import pytest
from pypdf import PdfReader
import io

from app import pdf

SAMPLE = Path(__file__).parent.parent / "samples" / "pdf" / "텍스트기반" / "[Hasung Electronics] Company introduction.pdf"


def test_parse_pages():
    assert pdf.parse_pages(None, 3) == [1, 2, 3]
    assert pdf.parse_pages("1,3-4", 5) == [1, 3, 4]
    assert pdf.parse_pages("2,2,1", 5) == [2, 1]
    with pytest.raises(ValueError):
        pdf.parse_pages("6", 5)
    with pytest.raises(ValueError):
        pdf.parse_pages("4-2", 5)


def test_split_and_merge():
    assert pdf.page_count(SAMPLE) == 3
    one = pdf.extract_pages_bytes(SAMPLE, [2])
    assert len(PdfReader(io.BytesIO(one)).pages) == 1
    two = pdf.extract_pages_bytes(SAMPLE, [3, 1])
    assert len(PdfReader(io.BytesIO(two)).pages) == 2
    assert len(pdf.sha256_file(SAMPLE)) == 64 and pdf.page_b64(SAMPLE, 1)
