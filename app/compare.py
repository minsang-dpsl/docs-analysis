"""A안/B안 ICP 비교 (코드 기반, LLM 호출 없음).

필드별로 두 ICP 의 항목을 짝지어 일치/유사/한쪽만 있는 항목으로 나누고,
프롬프트 규칙 준수 여부(최소 개수, 중복, contact_fit 형식, 길이)를 점검합니다.

짝짓기
  - 토큰화: 소문자, 영숫자 단어, 단어 끝 복수형(s, ies) 단순 제거, 불용어(and, of, the ...) 제외
  - exact  : 토큰 순서까지 같음
  - similar: 토큰 집합의 Jaccard 유사도 >= threshold(기본 0.6)
  - 한 항목은 한 번만 짝지어짐(점수 높은 쌍부터)
한계: 표현이 크게 다른 같은 의미는 잡지 못합니다(의미 비교는 사람이 나란히 보기 파일로 확인).
"""
from __future__ import annotations

import re

FIELDS = ("industry_fit", "expand_fit", "role_fit", "disqualify", "contact_fit")
MIN_COUNTS = {"industry_fit": 5, "expand_fit": 5, "role_fit": 3, "disqualify": 3, "contact_fit": 3}
_STOP = {"and", "or", "of", "the", "for", "with", "in", "a", "an", "to", "on", "by", "at"}
_SENIORITY = {"manager", "director", "president", "head", "lead", "chief", "officer", "leader", "executive"}
MAX_WORDS = 8  # contact_fit 을 뺀 항목의 최대 단어 수(프롬프트 Writing Rules)


# ---------------------------------------------------------------- 짝짓기
def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def tokens(text: str) -> tuple[str, ...]:
    toks = [_stem(t) for t in re.findall(r"\w+", str(text).lower())]
    return tuple(t for t in toks if t not in _STOP)


def similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def match_items(a_items: list[str], b_items: list[str], threshold: float = 0.6) -> dict:
    """-> {"pairs": [{a, b, score, type}], "only_a": [...], "only_b": [...]}"""
    ta, tb = [tokens(x) for x in a_items], [tokens(x) for x in b_items]
    cands = []
    for i, x in enumerate(ta):
        for j, y in enumerate(tb):
            if x and x == y:
                cands.append((-1.0, i, j, "exact"))
                continue
            s = similarity(x, y)
            if s >= threshold:
                cands.append((-s, i, j, "similar"))
    cands.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs = []
    for neg, i, j, kind in cands:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append({"a": a_items[i], "b": b_items[j], "score": round(-neg, 2), "type": kind, "_i": i})
    pairs.sort(key=lambda p: p["_i"])
    for p in pairs:
        del p["_i"]
    return {
        "pairs": pairs,
        "only_a": [x for i, x in enumerate(a_items) if i not in used_a],
        "only_b": [x for j, x in enumerate(b_items) if j not in used_b],
    }


# ---------------------------------------------------------------- 규칙 점검
def check_icp(icp: dict, threshold: float = 0.6) -> dict:
    """프롬프트 규칙 준수 여부(코드로 판단 가능한 것만)."""
    items = {f: [str(x) for x in (icp.get(f) or [])] for f in FIELDS}
    overlap = match_items(items["industry_fit"], items["expand_fit"], threshold)["pairs"]
    contact_bad = [
        x for x in items["contact_fit"]
        if len(x.split()) > 2 or any(t in _SENIORITY for t in re.findall(r"\w+", x.lower()))
    ]
    too_long = [(f, x) for f in FIELDS if f != "contact_fit" for x in items[f] if len(x.split()) > MAX_WORDS]
    return {
        "counts": {f: len(items[f]) for f in FIELDS},
        "meets_min": {f: len(items[f]) >= MIN_COUNTS[f] for f in FIELDS},
        "industry_expand_overlap": [{"industry_fit": p["a"], "expand_fit": p["b"]} for p in overlap],
        "contact_fit_violations": contact_bad,
        "too_long": [{"field": f, "item": x} for f, x in too_long],
    }


# ---------------------------------------------------------------- 비교
def _meta(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("mode", "model", "input", "usage", "cost_usd", "cost_source", "latency_s", "schema_warnings")}


def compare(a_rec: dict, b_rec: dict, threshold: float = 0.6) -> dict:
    """__icp_a.json / __icp_b.json 내용 -> 비교 결과 dict. 둘 중 icp 가 없으면 ValueError."""
    for name, rec in (("A안", a_rec), ("B안", b_rec)):
        if not isinstance(rec.get("icp"), dict):
            raise ValueError(f"{name} 의 icp 가 없습니다(parse_error: {rec.get('parse_error')})")
    a_icp, b_icp = a_rec["icp"], b_rec["icp"]
    fields = {}
    for f in FIELDS:
        a_items = [str(x) for x in (a_icp.get(f) or [])]
        b_items = [str(x) for x in (b_icp.get(f) or [])]
        m = match_items(a_items, b_items, threshold)
        exact = sum(p["type"] == "exact" for p in m["pairs"])
        similar = len(m["pairs"]) - exact
        matched = len(m["pairs"])
        union = len(a_items) + len(b_items) - matched
        fields[f] = {
            "a": a_items, "b": b_items, **m,
            "n_a": len(a_items), "n_b": len(b_items), "exact": exact, "similar": similar,
            "overlap": round(matched / union, 3) if union else 0.0,
        }
    total_a = sum(v["n_a"] for v in fields.values())
    total_b = sum(v["n_b"] for v in fields.values())
    matched = sum(v["exact"] + v["similar"] for v in fields.values())
    return {
        "pdf": a_rec.get("pdf"), "threshold": threshold,
        "a": _meta(a_rec), "b": _meta(b_rec),
        "fields": fields,
        "checks": {"a": check_icp(a_icp, threshold), "b": check_icp(b_icp, threshold)},
        "summary": {
            "items_a": total_a, "items_b": total_b, "matched": matched,
            "overlap": round(matched / (total_a + total_b - matched), 3) if (total_a + total_b - matched) else 0.0,
        },
    }


# ---------------------------------------------------------------- 보기 좋은 출력
def _esc(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _input_text(meta: dict) -> str:
    i = meta.get("input") or {}
    if meta.get("mode") == "a":
        pages = ", ".join(str(p) for p in i.get("ocr_pages", []))
        return f"NAICS 후보 {i.get('naics_candidates')}개 + OCR 텍스트 {i.get('ocr_chars', 0):,}자(쪽 {pages})"
    return f"PDF 통째로 ({i.get('total_pages')}쪽, {i.get('pdf_bytes', 0):,} 바이트)"


def _cost_text(meta: dict) -> str:
    c = meta.get("cost_usd")
    return "계산 불가" if c is None else f"${c:.4f} ({meta.get('cost_source')})"


def render_markdown(cmp: dict) -> str:
    a, b = cmp["a"], cmp["b"]
    ua, ub = a.get("usage") or {}, b.get("usage") or {}
    L = [f"# A안 vs B안 ICP 비교: {cmp['pdf']}", "",
         "- A안: Decision model 이 고른 쪽의 OCR 텍스트 + NAICS 후보 -> LLM",
         "- B안: PDF 통째로 -> LLM (같은 모델, 같은 system 프롬프트)",
         f"- 짝짓기: 토큰 Jaccard >= {cmp['threshold']} 이면 유사, 토큰 순서까지 같으면 일치", "",
         "## 실행 정보", "", "| | A안 | B안 |", "|---|---|---|",
         f"| 입력 | {_esc(_input_text(a))} | {_esc(_input_text(b))} |",
         f"| 모델 | {a.get('model')} | {b.get('model')} |",
         f"| 토큰 입력/출력 | {ua.get('prompt_tokens', 0):,} / {ua.get('completion_tokens', 0):,} | "
         f"{ub.get('prompt_tokens', 0):,} / {ub.get('completion_tokens', 0):,} |",
         f"| 비용 | {_cost_text(a)} | {_cost_text(b)} |",
         f"| 소요 | {a.get('latency_s')}s | {b.get('latency_s')}s |", "",
         "## 요약", "", "| 필드 | A 개수 | B 개수 | 일치 | 유사 | A만 | B만 | 겹침률 |", "|---|---|---|---|---|---|---|---|"]
    for f, v in cmp["fields"].items():
        L.append(f"| {f} | {v['n_a']} | {v['n_b']} | {v['exact']} | {v['similar']} | {len(v['only_a'])} | "
                 f"{len(v['only_b'])} | {v['overlap']:.0%} |")
    s = cmp["summary"]
    L += ["", f"전체: A {s['items_a']}개, B {s['items_b']}개, 짝지어진 항목 {s['matched']}개, 겹침률 {s['overlap']:.0%}", ""]
    for f, v in cmp["fields"].items():
        L += [f"## {f}", "", "| A안 | B안 | 구분 |", "|---|---|---|"]
        for p in v["pairs"]:
            kind = "일치" if p["type"] == "exact" else f"유사 ({p['score']})"
            L.append(f"| {_esc(p['a'])} | {_esc(p['b'])} | {kind} |")
        L += [f"| {_esc(x)} | | A만 |" for x in v["only_a"]]
        L += [f"| | {_esc(x)} | B만 |" for x in v["only_b"]]
        L.append("")
    ca, cb = cmp["checks"]["a"], cmp["checks"]["b"]
    L += ["## 규칙 점검 (코드로 판단 가능한 것만)", "", "| 항목 | A안 | B안 |", "|---|---|---|"]
    for f in FIELDS:
        ok = lambda c: "충족" if c["meets_min"][f] else "미달"
        L.append(f"| {f} 최소 {MIN_COUNTS[f]}개 | {ca['counts'][f]}개 {ok(ca)} | {cb['counts'][f]}개 {ok(cb)} |")
    L.append(f"| industry_fit/expand_fit 중복 | {len(ca['industry_expand_overlap'])}건 | {len(cb['industry_expand_overlap'])}건 |")
    L.append(f"| contact_fit 형식 위반(직급어, 2단어 초과) | {len(ca['contact_fit_violations'])}건 | {len(cb['contact_fit_violations'])}건 |")
    L.append(f"| 항목 길이 {MAX_WORDS}단어 초과 | {len(ca['too_long'])}건 | {len(cb['too_long'])}건 |")
    fmt = {
        "industry_expand_overlap": lambda x: f"{x['industry_fit']} / {x['expand_fit']}",
        "contact_fit_violations": lambda x: x,
        "too_long": lambda x: f"{x['field']}: {x['item']}",
    }
    for label, c in (("A안", ca), ("B안", cb)):
        for key, title in (("industry_expand_overlap", "industry_fit/expand_fit 중복"),
                           ("contact_fit_violations", "contact_fit 위반"), ("too_long", "길이 초과")):
            if c[key]:
                L += ["", f"- {label} {title}: " + "; ".join(_esc(fmt[key](x)) for x in c[key])]
    L.append("")
    return "\n".join(L)
