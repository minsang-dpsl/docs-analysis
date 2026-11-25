"""OCR 단계: 결과 CSV 의 NAICS 행에서 코드가 많이 몰린 쪽 상위 N개를 고르고, 그 쪽만 Google Document AI 로 텍스트를 추출합니다.

선택 규칙 (결과 CSV 의 kind == naics 행만 사용)
  1. evidence_page 별 NAICS 코드 개수가 많은 쪽 우선
  2. 개수가 같으면 그 쪽 코드들의 score 합이 큰 쪽 우선
  3. 그래도 같으면 쪽 번호가 작은 쪽 우선
  4. 상위 top_n 개. NAICS 가 나온 쪽이 그보다 적으면 있는 만큼만 사용

Document AI 온라인 처리 한도(요청당 15쪽, 40MB)를 넘으면 쪽별 요청으로 나눕니다.
인증과 프로세서 정보는 환경 변수로만 받습니다(.env 는 커밋 금지).
  GOOGLE_APPLICATION_CREDENTIALS  서비스 계정 키 경로(또는 ADC)
  DOCUMENTAI_PROJECT_ID, DOCUMENTAI_PROCESSOR_ID, DOCUMENTAI_LOCATION(기본 us)
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Callable

from . import pdf
from .client import sanitize
from .config import api_key


class OcrError(Exception):
    pass


# ---------------------------------------------------------------- 쪽 선택
def read_naics_rows(csv_path: str | Path) -> list[dict]:
    """__result.csv(BOM 유무 무관)에서 kind == naics 행만 읽습니다."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("kind") == "naics"]


def select_ocr_pages(rows: list[dict], top_n: int) -> dict:
    """-> {"pages": 선택 쪽(순위순), "stats": {쪽: {"n": 코드 수, "sum": score 합, "codes": [...]}}}"""
    stats: dict[int, dict] = {}
    for r in rows:
        if r.get("kind", "naics") != "naics" or r.get("evidence_page") in (None, ""):
            continue
        s = stats.setdefault(int(r["evidence_page"]), {"n": 0, "sum": 0.0, "codes": []})
        s["n"] += 1
        s["sum"] += float(r["score"])
        s["codes"].append(r["code"])
    for s in stats.values():
        s["sum"] = round(s["sum"], 4)
    ranked = sorted(stats, key=lambda p: (-stats[p]["n"], -stats[p]["sum"], p))
    return {"pages": ranked[: max(top_n, 0)], "stats": stats}


# ---------------------------------------------------------------- Document AI
def make_process(location_default: str = "us") -> Callable[[bytes], Any]:
    """Document AI 호출 함수 process(pdf_bytes) -> Document 를 만듭니다. SDK 는 여기서만 import 합니다."""
    project = api_key("DOCUMENTAI_PROJECT_ID")
    processor = api_key("DOCUMENTAI_PROCESSOR_ID")
    location = os.environ.get("DOCUMENTAI_LOCATION") or location_default
    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("google-cloud-documentai 가 필요합니다: pip install -r requirements.txt") from e

    client = documentai.DocumentProcessorServiceClient(
        client_options=ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    )
    name = client.processor_path(project, location, processor)

    def process(content: bytes):
        request = documentai.ProcessRequest(
            name=name, raw_document=documentai.RawDocument(content=content, mime_type="application/pdf")
        )
        try:
            return client.process_document(request=request).document
        except Exception as e:  # 메시지에 섞일 수 있는 긴 문자열은 제거
            raise OcrError(f"Document AI 호출 실패: {type(e).__name__}: {sanitize(str(e))}") from e

    return process


def pages_text(document: Any) -> dict[int, str]:
    """Document -> {제출한 PDF 안의 쪽 번호(1부터): 그 쪽 텍스트}. 쪽의 text_anchor 구간을 document.text 에서 잘라 붙입니다."""
    out: dict[int, str] = {}
    for pg in document.pages:
        segs = pg.layout.text_anchor.text_segments
        out[int(pg.page_number)] = "".join(document.text[int(s.start_index): int(s.end_index)] for s in segs)
    return out


def combine_pages_text(texts: dict[int, str]) -> str:
    """쪽 번호 오름차순으로 '===== Page N =====' 구분선을 넣어 하나의 텍스트로 합칩니다."""
    return "\n\n".join(f"===== Page {p} =====\n{texts[p].strip()}" for p in sorted(texts)) + "\n"


def extract_text(
    pdf_path: str | Path, pages: list[int], process: Callable[[bytes], Any], max_pages: int, max_bytes: int
) -> tuple[dict[int, str], int]:
    """원본 쪽 번호 -> 텍스트, 요청 횟수. 한도 안이면 선택한 쪽을 PDF 하나로 합쳐 한 번에, 넘으면 쪽별로 보냅니다."""
    asc = sorted(set(pages))
    if not asc:
        return {}, 0
    blob = pdf.extract_pages_bytes(pdf_path, asc)
    if len(asc) <= max_pages and len(blob) <= max_bytes:
        groups = [(asc, blob)]
    else:
        groups = [([p], pdf.extract_pages_bytes(pdf_path, [p])) for p in asc]
    texts: dict[int, str] = {}
    for grp, content in groups:
        if len(content) > max_bytes:
            raise OcrError(f"쪽 {grp[0]} 한 쪽이 요청 크기 한도({max_bytes:,} 바이트)를 넘습니다")
        got = pages_text(process(content))
        for i, p in enumerate(grp, 1):  # 합친 PDF 의 i 번째 쪽 = 원본 grp[i-1] 쪽
            texts[p] = got.get(i, "")
    return texts, len(groups)
