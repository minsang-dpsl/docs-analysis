"""쪽 요약(none 제외 확률 추출)과 문서 단위 집계."""
from __future__ import annotations

from .questions import CONTINENT_QID, COUNTRY_QID, NAICS_QID_PREFIX, NONE_ID


def summarize_page(
    probs: dict[str, dict[str, float]],
    naics_titles: dict[str, str],
    country_names: dict[str, str],
    continent_names: dict[str, str] | None = None,
) -> dict:
    """질문별 옵션 확률에서 none 을 제외하고 NAICS/국가/대륙 목록(확률 내림차순)을 만듭니다."""
    continent_names = continent_names or {}
    naics: list[dict] = []
    countries: list[dict] = []
    continents: list[dict] = []
    for qid, opts in probs.items():
        for oid, p in opts.items():
            if oid == NONE_ID:
                continue
            if qid.startswith(NAICS_QID_PREFIX):
                naics.append({"code": oid, "name": naics_titles.get(oid, ""), "p": float(p)})
            elif qid == COUNTRY_QID:
                countries.append({"code": oid, "name": country_names.get(oid, ""), "p": float(p)})
            elif qid == CONTINENT_QID:
                continents.append({"code": oid, "name": continent_names.get(oid, ""), "p": float(p)})
    naics.sort(key=lambda x: -x["p"])
    countries.sort(key=lambda x: -x["p"])
    continents.sort(key=lambda x: -x["p"])
    return {"naics": naics, "countries": countries, "continents": continents}


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
    best_r: dict[str, dict] = {}  # 대륙: 국가와 같은 상대/절대 기준 사용
    for page in sorted(page_summaries):
        s = page_summaries[page]
        for item in s.get("naics", []):
            cur = best_n.get(item["code"])
            if cur is None or item["p"] > cur["score"]:
                best_n[item["code"]] = {
                    "code": item["code"], "name": item["name"], "score": item["p"], "evidence_page": page,
                }
        for key, best in (("countries", best_c), ("continents", best_r)):
            cs = s.get(key, [])
            if cs:
                top = max(c["p"] for c in cs)
                for item in cs:
                    if item["p"] >= top * rel and item["p"] >= min_abs:
                        cur = best.get(item["code"])
                        if cur is None or item["p"] > cur["score"]:
                            best[item["code"]] = {
                                "code": item["code"], "name": item["name"], "score": item["p"], "evidence_page": page,
                            }
    naics = sorted((v for v in best_n.values() if v["score"] >= naics_min), key=lambda x: -x["score"])
    countries = sorted(best_c.values(), key=lambda x: -x["score"])
    continents = sorted(best_r.values(), key=lambda x: -x["score"])
    return {"naics": naics, "countries": countries, "continents": continents}


def attach_region_members(doc: dict, countries: list) -> dict:
    """집계된 각 지역에 그 지역에 속한 국가를 전부 붙입니다(doc 을 제자리에서 수정하고 반환).

    countries: alpha2, name_en, region 속성을 가진 객체 목록(data.Country).
    개별 국가로도 집계된 국가는 score/evidence_page 를 채우고(앞쪽에 점수순), 나머지는 None 입니다.
    여러 지역에 걸친 국가(예: 러시아)는 모든 지역에 속하지만, 결과에는 한 번만 나옵니다.
    지역은 점수 순(doc["continents"] 순서)으로 처리하므로 점수가 가장 높은 지역 아래에 나오고,
    동점이면 국가의 region 열에서 먼저 적힌 지역이 우선입니다.
    """
    detected = {c["code"]: c for c in doc.get("countries", [])}
    conts = doc.get("continents", [])
    owner: dict[str, str] = {}  # 국가 -> 결과에서 나올 지역
    for c in countries:
        rank = {code: i for i, code in enumerate(c.regions)}
        mine = [cont for cont in conts if cont["code"] in rank]
        if mine:
            owner[c.alpha2] = max(mine, key=lambda k: (k["score"], -rank[k["code"]]))["code"]
    for cont in conts:
        members = []
        for c in countries:
            if owner.get(c.alpha2) != cont["code"]:
                continue
            d = detected.get(c.alpha2)
            members.append({
                "code": c.alpha2, "name": c.name_en,
                "score": d["score"] if d else None, "evidence_page": d["evidence_page"] if d else None,
            })
        members.sort(key=lambda m: (m["score"] is None, -(m["score"] or 0.0), m["name"]))
        cont["countries"] = members
    return doc


def result_rows(doc: dict) -> list[dict]:
    """result.csv 와 같은 평탄화 형식: kind, code, name, score, evidence_page.

    순서: naics -> country -> continent(각 지역 바로 아래에 그 지역의 continent_country 행들).
    """
    def row(kind: str, it: dict) -> dict:
        score, page = it.get("score"), it.get("evidence_page")
        return {
            "kind": kind, "code": it["code"], "name": it["name"],
            "score": "" if score is None else round(score, 4), "evidence_page": "" if page is None else page,
        }

    rows = []
    for kind, key in (("naics", "naics"), ("country", "countries")):
        rows.extend(row(kind, it) for it in doc.get(key, []))
    for cont in doc.get("continents", []):
        rows.append(row("continent", cont))
        rows.extend(row("continent_country", m) for m in cont.get("countries", []))
    return rows
