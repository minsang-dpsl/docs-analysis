"""ICP 추출에 보낼 페이지 선택.

입력은 문서 집계 결과(NAICS 코드별 score, evidence_page). evidence_page 는 "근거 페이지"가 아니라
그 코드의 확률이 가장 높게 나온 페이지입니다(의사결정 모델은 근거를 인용하지 않음).

규칙(우선순위 순):
  0. 전체 쪽수가 small_doc_pages 이하이면 전부 사용
  1. 강한 신호: score >= strong_score 인 코드가 있는 쪽 (최고 점수 내림차순)
  2. 폭넓은 커버: 쪽별 score 합 내림차순으로 누적 coverage 에 도달할 때까지
  1+2 의 합집합은 max_pages 로 자름(1단계가 먼저 채워짐)
  3. 앞쪽 always_front 쪽(제품 정의) 추가
  4. 국가 1순위의 evidence 쪽 추가 (score >= country_page_min_score 일 때)
"""
from __future__ import annotations

from collections import defaultdict


def page_stats(naics: list[dict]) -> dict[int, dict]:
    stats: dict[int, dict] = defaultdict(lambda: {"n": 0, "sum": 0.0, "max": 0.0})
    for it in naics:
        s = stats[int(it["evidence_page"])]
        s["n"] += 1
        s["sum"] += float(it["score"])
        s["max"] = max(s["max"], float(it["score"]))
    return {p: dict(v) for p, v in stats.items()}


def select_pages(doc: dict, total_pages: int, cfg: dict) -> dict:
    sel = cfg["page_selection"] if "page_selection" in cfg else cfg
    reasons: dict[int, list[str]] = defaultdict(list)
    stats = page_stats(doc.get("naics", []))

    if total_pages <= sel["small_doc_pages"]:
        pages = list(range(1, total_pages + 1))
        for p in pages:
            reasons[p].append("small_doc")
        return {"pages": pages, "reasons": dict(reasons), "dropped": [], "stats": stats}

    strong = sorted((p for p, s in stats.items() if s["max"] >= sel["strong_score"]), key=lambda p: -stats[p]["max"])

    coverage: list[int] = []
    total_sum = sum(s["sum"] for s in stats.values())
    if total_sum > 0:
        acc = 0.0
        for p in sorted(stats, key=lambda p: -stats[p]["sum"]):
            coverage.append(p)
            acc += stats[p]["sum"]
            if acc / total_sum >= sel["coverage"]:
                break

    core: list[int] = []
    for p in strong:
        if p not in core:
            core.append(p)
            reasons[p].append("strong")
    for p in coverage:
        reasons[p].append("coverage")
        if p not in core:
            core.append(p)
    dropped = core[sel["max_pages"]:]
    core = core[: sel["max_pages"]]
    for p in dropped:
        reasons[p] = [r + "(cut)" for r in reasons[p]]

    chosen = set(core)
    for p in range(1, min(sel["always_front"], total_pages) + 1):
        reasons[p].append("front")
        chosen.add(p)

    countries = doc.get("countries", [])
    if countries:
        top = max(countries, key=lambda c: c["score"])
        if top["score"] >= sel["country_page_min_score"]:
            p = int(top["evidence_page"])
            reasons[p].append("country_top")
            chosen.add(p)

    pages = sorted(p for p in chosen if 1 <= p <= total_pages)
    reasons_out = {p: reasons[p] for p in pages}
    return {"pages": pages, "reasons": reasons_out, "dropped": sorted(dropped), "stats": stats}
