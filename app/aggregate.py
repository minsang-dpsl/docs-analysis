"""쪽 요약(none 제외 확률 추출)과 문서 단위 집계."""
from __future__ import annotations

from .questions import COUNTRY_QID, NAICS_QID_PREFIX, NONE_ID


def summarize_page(
    probs: dict[str, dict[str, float]],
    naics_titles: dict[str, str],
    country_names: dict[str, str],
) -> dict:
    """질문별 옵션 확률에서 none 을 제외하고 NAICS/국가 목록(확률 내림차순)을 만듭니다."""
    naics: list[dict] = []
    countries: list[dict] = []
    for qid, opts in probs.items():
        for oid, p in opts.items():
            if oid == NONE_ID:
                continue
            if qid.startswith(NAICS_QID_PREFIX):
                naics.append({"code": oid, "name": naics_titles.get(oid, ""), "p": float(p)})
            elif qid == COUNTRY_QID:
                countries.append({"code": oid, "name": country_names.get(oid, ""), "p": float(p)})
    naics.sort(key=lambda x: -x["p"])
    countries.sort(key=lambda x: -x["p"])
    return {"naics": naics, "countries": countries}


def aggregate_document(page_summaries: dict[int, dict], thresholds: dict) -> dict:
    """문서 집계.

    NAICS: 코드별 쪽 최댓값이 naics_min 이상이면 후보 (evidence_page = 최댓값이 나온 쪽).
    국가: 각 쪽에서 1순위 대비 country_relative 이상이고 절대 country_min_abs 이상인 것만
          통과시킨 뒤, 쪽 간 최댓값을 점수로 사용.
    """
    naics_min = thresholds["naics_min"]
    rel = thresholds["country_relative"]
    min_abs = thresholds["country_min_abs"]

    best_n: dict[str, dict] = {}
    best_c: dict[str, dict] = {}
    for page in sorted(page_summaries):
        s = page_summaries[page]
        for item in s.get("naics", []):
            cur = best_n.get(item["code"])
            if cur is None or item["p"] > cur["score"]:
                best_n[item["code"]] = {
                    "code": item["code"], "name": item["name"], "score": item["p"], "evidence_page": page,
                }
        cs = s.get("countries", [])
        if cs:
            top = max(c["p"] for c in cs)
            for item in cs:
                if item["p"] >= top * rel and item["p"] >= min_abs:
                    cur = best_c.get(item["code"])
                    if cur is None or item["p"] > cur["score"]:
                        best_c[item["code"]] = {
                            "code": item["code"], "name": item["name"], "score": item["p"], "evidence_page": page,
                        }
    naics = sorted((v for v in best_n.values() if v["score"] >= naics_min), key=lambda x: -x["score"])
    countries = sorted(best_c.values(), key=lambda x: -x["score"])
    return {"naics": naics, "countries": countries}


def result_rows(doc: dict) -> list[dict]:
    """result.csv 와 같은 평탄화 형식: kind, code, name, score, evidence_page."""
    rows = []
    for kind, key in (("naics", "naics"), ("country", "countries")):
        for it in doc.get(key, []):
            rows.append({
                "kind": kind, "code": it["code"], "name": it["name"],
                "score": round(it["score"], 4), "evidence_page": it["evidence_page"],
            })
    return rows
