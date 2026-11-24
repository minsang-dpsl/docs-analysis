"""PDF 처리: 1쪽씩 분할(메모리), base64 변환, 쪽 범위 파싱, 파일 해시."""
from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_count(path: str | Path) -> int:
    return len(PdfReader(str(path)).pages)


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1,3-5' -> [1,3,4,5] (1부터). None/빈 문자열이면 전체. 범위를 벗어나면 ValueError."""
    if not spec:
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if lo > hi:
                raise ValueError(f"잘못된 범위: {part}")
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(part))
    bad = [p for p in pages if p < 1 or p > total]
    if bad:
        raise ValueError(f"쪽 번호가 범위(1-{total})를 벗어났습니다: {bad}")
    seen: set[int] = set()
    return [p for p in pages if not (p in seen or seen.add(p))]


def extract_pages_bytes(path: str | Path, pages: list[int]) -> bytes:
    """원본 쪽 번호 목록(1부터)을 하나의 PDF 바이트로 합칩니다(지정 순서 유지)."""
    reader = PdfReader(str(path))
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_b64(path: str | Path, page: int) -> str:
    return base64.b64encode(extract_pages_bytes(path, [page])).decode("ascii")
