"""ICP 생성 단계 (A안/B안 비교 실험).

A안: Decision model 이 고른 쪽만 OCR 한 텍스트(__ocr.txt) + NAICS 후보(__result.csv) + 프롬프트 -> LLM (PDF 첨부 없음)
B안: PDF 를 통째로 + 같은 프롬프트 -> LLM (naics_candidates 는 "none provided")

두 안은 모델, system 프롬프트, temperature 가 같고 입력만 다릅니다.
결과: A안 __icp_a.json, B안 __icp_b.json (비교는 compare 명령).
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from . import pdf
from .ocr import read_naics_rows

MODES = ("a", "b")
ICP_FIELDS = ("industry_fit", "expand_fit", "role_fit", "disqualify", "contact_fit")
_PAGE_MARK_RE = re.compile(r"^===== Page (\d+) =====$", re.M)


class IcpInputError(Exception):
    """A안에 필요한 입력 파일이 없거나 비어 있을 때."""


# ---------------------------------------------------------------- 입력 조립
def build_naics_candidates(rows: list[dict]) -> str:
    """결과 CSV 의 naics 행 -> '코드 업종명' 줄(점수 내림차순, 점수는 넣지 않음). 점수가 같으면 CSV 순서 유지."""
    ranked = sorted(rows, key=lambda r: -float(r["score"]))
    return "\n".join(f"{r['code']} {r['name']}" for r in ranked) or "(none)"


def ocr_pages(text: str) -> list[int]:
    """__ocr.txt 의 '===== Page N =====' 표시에서 쪽 번호 목록."""
    return [int(m) for m in _PAGE_MARK_RE.findall(text)]


def build_prompt(prompts: dict, mode: str, naics_candidates: str = "", document_text: str = "") -> tuple[str, str]:
    """-> (system, user). system 은 두 안이 같습니다."""
    system = prompts["icp_system"].strip()
    if mode == "a":
        # 값에 중괄호(OCR 텍스트의 {} 등)가 있어도 안전: format 은 템플릿에만 적용
        user = prompts["icp_user"].format(naics_candidates=naics_candidates, document_text=document_text)
    elif mode == "b":
        user = prompts["icp_user_b"]
    else:
        raise ValueError(f"알 수 없는 mode: {mode}")
    return system, user.strip()


# ---------------------------------------------------------------- 응답 처리
def parse_json_text(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼냅니다(코드 펜스, 앞뒤 설명 허용)."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
    return json.loads(t[start : end + 1])


def extract_icp(parsed: dict) -> tuple[dict, list[str]]:
    """{"icp": {...}} -> (icp, 경고). 필드가 빠졌거나 리스트가 아니면 경고에 남깁니다."""
    icp = parsed.get("icp") if isinstance(parsed, dict) else None
    if not isinstance(icp, dict):
        raise ValueError("응답 JSON 에 icp 객체가 없습니다")
    warnings = [f"{f}: 리스트가 아니거나 없음" for f in ICP_FIELDS if not isinstance(icp.get(f), list)]
    return icp, warnings


def icp_cost(settings: dict, usage: dict) -> tuple[float | None, str]:
    """-> (비용 USD, 출처). 응답의 usage.cost 가 있으면 그것을, 없으면 settings.icp.pricing 으로 추정."""
    if usage.get("cost") is not None:
        return float(usage["cost"]), "응답 usage.cost"
    p = settings.get("icp", {}).get("pricing")
    if p:
        cost = usage["prompt_tokens"] / 1e6 * p["input_per_mtok"] + usage["completion_tokens"] / 1e6 * p["output_per_mtok"]
        return cost, "settings.icp.pricing 추정"
    return None, "계산 불가"


# ---------------------------------------------------------------- 실행
def run_icp(
    pdf_path: Path, mode: str, settings: dict, prompts: dict, client, out_dir: Path,
    result_csv: Path | None = None, ocr_path: Path | None = None,
) -> dict:
    """한 안(mode)을 실행하고 out_dir/__icp_{mode}.json 에 저장합니다."""
    pdf_path = Path(pdf_path)
    if mode == "a":
        csv_path = Path(result_csv) if result_csv else out_dir / "__result.csv"
        txt_path = Path(ocr_path) if ocr_path else out_dir / "__ocr.txt"
        if not csv_path.exists():
            raise IcpInputError(f"결과 CSV 가 없습니다: {csv_path} (먼저 run 을 실행하세요)")
        rows = read_naics_rows(csv_path)
        if not rows:
            raise IcpInputError(f"결과 CSV 에 NAICS 행이 없습니다: {csv_path}")
        if not txt_path.exists():
            raise IcpInputError(f"OCR 텍스트가 없습니다: {txt_path} (먼저 ocr 을 실행하세요)")
        text = txt_path.read_text(encoding="utf-8")
        if not text.strip():
            raise IcpInputError(f"OCR 텍스트가 비어 있습니다: {txt_path}")
        system, user = build_prompt(prompts, "a", build_naics_candidates(rows), text)
        pdf_b64 = None
        input_info = {"naics_candidates": len(rows), "ocr_chars": len(text), "ocr_pages": ocr_pages(text),
                      "result_csv": csv_path.name, "ocr_file": txt_path.name}
    elif mode == "b":
        system, user = build_prompt(prompts, "b")
        raw = pdf_path.read_bytes()  # 재저장 없이 원본 바이트 그대로
        pdf_b64 = base64.b64encode(raw).decode("ascii")
        input_info = {"total_pages": pdf.page_count(pdf_path), "pdf_bytes": len(raw)}
    else:
        raise ValueError(f"알 수 없는 mode: {mode}")

    text_out, usage, latency = client.generate(system, user, pdf_b64)
    icp, warnings, parse_error = None, [], None
    try:
        icp, warnings = extract_icp(parse_json_text(text_out))
    except (ValueError, json.JSONDecodeError) as e:
        parse_error = str(e)
    cost, cost_src = icp_cost(settings, usage)
    rec = {
        "pdf": pdf_path.name, "mode": mode, "model": settings["icp"]["model"],
        "input": {**input_info, "system_chars": len(system), "user_chars": len(user)},
        "usage": usage, "cost_usd": cost, "cost_source": cost_src, "latency_s": round(latency, 2),
        "icp": icp, "schema_warnings": warnings, "parse_error": parse_error,
        "raw_text": text_out if parse_error else None,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"__icp_{mode}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec
