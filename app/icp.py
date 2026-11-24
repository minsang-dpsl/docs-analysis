"""ICP 추출 단계: 선택된 페이지 원문 + 분류 힌트를 LLM 에 보내 ICP JSON 을 받습니다."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from .pdf import extract_pages_bytes
from .select_pages import select_pages


def build_hints(doc: dict, icp_cfg: dict) -> tuple[str, str, str]:
    """-> (naics_hints_text, country_hints_text, country_note)"""
    naics = sorted(doc.get("naics", []), key=lambda x: -x["score"])
    strong = [n for n in naics if n["score"] >= icp_cfg["naics_hint_min"]][: icp_cfg["naics_hint_top"]]
    picked = strong or naics[: icp_cfg["naics_hint_fallback_top"]]
    naics_text = "\n".join(
        f"- {n['code']} {n['name']} (score {n['score']:.2f}, peak page {n['evidence_page']})" for n in picked
    ) or "- (none)"

    countries = sorted(doc.get("countries", []), key=lambda x: -x["score"])
    country_text = "\n".join(
        f"- {c['code']} {c['name']} (score {c['score']:.2f}, peak page {c['evidence_page']})" for c in countries
    ) or "- (none)"
    top = countries[0]["score"] if countries else 0.0
    note = "reliability: LOW, scores are low or flat; verify against the pages" if top < icp_cfg["country_weak_below"] else "reliability: OK"
    return naics_text, country_text, note


def build_prompt(prompts: dict, pages: list[int], doc: dict, icp_cfg: dict) -> tuple[str, str]:
    naics_text, country_text, note = build_hints(doc, icp_cfg)
    user = prompts["icp_user"].format(
        page_list=", ".join(str(p) for p in pages),
        naics_hints=naics_text,
        country_hints=country_text,
        country_note=note,
    )
    return prompts["icp_system"].strip(), user


def parse_json_text(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼냅니다(코드 펜스, 앞뒤 설명 허용)."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("응답에서 JSON 객체를 찾지 못했습니다")
    return json.loads(t[start : end + 1])


def run_icp(pdf_path: Path, result: dict, settings: dict, prompts: dict, client, out_dir: Path | None = None) -> dict:
    total = int(result["total_pages"])
    sel = select_pages(result["doc"], total, settings)
    pages = sel["pages"]
    system, user = build_prompt(prompts, pages, result["doc"], settings["icp"])
    pdf_b64 = base64.b64encode(extract_pages_bytes(pdf_path, pages)).decode("ascii")
    text, usage, latency = client.generate(system, user, pdf_b64)
    try:
        icp = parse_json_text(text)
        parse_error = None
    except (ValueError, json.JSONDecodeError) as e:
        icp, parse_error = None, str(e)
    out = {
        "pdf": Path(pdf_path).name,
        "selected_pages": pages,
        "selection": {"reasons": {str(k): v for k, v in sel["reasons"].items()}, "dropped": sel["dropped"]},
        "model": settings["icp"]["model"],
        "usage": usage,
        "latency_s": round(latency, 2),
        "icp": icp,
        "parse_error": parse_error,
        "raw_text": text if parse_error else None,
    }
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "__icp.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
