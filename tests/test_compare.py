import pytest

from app import compare as C


def rec(mode, **fields):
    base = {"industry_fit": [], "expand_fit": [], "role_fit": [], "disqualify": [], "contact_fit": []}
    base.update(fields)
    return {"pdf": "x.pdf", "mode": mode, "model": "m", "input": {"naics_candidates": 2, "ocr_chars": 10, "ocr_pages": [1]},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.001, "cost_source": "응답 usage.cost",
            "latency_s": 1.0, "schema_warnings": [], "parse_error": None, "icp": base}


def test_tokens_normalize_case_plural_and_stopwords():
    assert C.tokens("Frozen Seafood Importers") == C.tokens("frozen seafood importer")
    assert C.tokens("Retailers of the Cosmetics") == ("retailer", "cosmetic")
    assert C.tokens("Companies") == ("company",)


def test_match_items_exact_similar_and_only():
    a = ["Frozen seafood import distributor", "Seafood specialty retail chain", "Pet grooming chain"]
    b = ["frozen seafood import distributors", "Specialty seafood retail chains operator", "Online retailer"]
    m = C.match_items(a, b, 0.6)
    types = {p["a"]: p["type"] for p in m["pairs"]}
    assert types["Frozen seafood import distributor"] == "exact"          # 대소문자/복수형 무시
    assert types["Seafood specialty retail chain"] == "similar"           # 토큰 집합 유사
    assert m["only_a"] == ["Pet grooming chain"] and m["only_b"] == ["Online retailer"]


def test_match_is_one_to_one_best_first():
    m = C.match_items(["retail chain"], ["retail chain", "retail chain operator"], 0.5)
    assert [(p["a"], p["b"], p["type"]) for p in m["pairs"]] == [("retail chain", "retail chain", "exact")]
    assert m["only_b"] == ["retail chain operator"]


def test_compare_counts_overlap_and_checks():
    a = rec("a", industry_fit=["Frozen seafood importer", "Asian grocery chain"], expand_fit=["Seafood importer"],
            contact_fit=["purchasing", "procurement manager", "category management"])
    b = rec("b", industry_fit=["frozen seafood importers", "Online seafood retailer"],
            contact_fit=["purchasing", "sourcing"])
    cmp = C.compare(a, b)
    f = cmp["fields"]["industry_fit"]
    assert (f["n_a"], f["n_b"], f["exact"], f["similar"]) == (2, 2, 1, 0) and f["overlap"] == pytest.approx(1 / 3, abs=1e-3)
    assert cmp["fields"]["contact_fit"]["exact"] == 1
    ca, cb = cmp["checks"]["a"], cmp["checks"]["b"]
    assert ca["meets_min"]["industry_fit"] is False and ca["counts"]["industry_fit"] == 2
    assert ca["contact_fit_violations"] == ["procurement manager"]  # 직급어 포함만 위반(category management 는 2단어라 정상)
    assert cb["contact_fit_violations"] == []
    assert [o["industry_fit"] for o in ca["industry_expand_overlap"]] == ["Frozen seafood importer"]  # 안 내부 중복
    s = cmp["summary"]
    assert s["items_a"] == 6 and s["items_b"] == 4 and s["matched"] == 2  # A: 2+1+3, B: 2+2, 일치: 필드별 1개씩


def test_contact_violation_two_words_ok_three_words_not():
    c = C.check_icp({"contact_fit": ["category management", "brand portfolio manager", "head of buying", "lead"]})
    assert c["contact_fit_violations"] == ["brand portfolio manager", "head of buying", "lead"]


def test_too_long_items_flagged_outside_contact_fit():
    long = "cosmetics and personal-care brand commissioning custom containers packaging beauty accessories"
    c = C.check_icp({"industry_fit": [long, "short item"]})
    assert c["too_long"] == [{"field": "industry_fit", "item": long}]


def test_compare_rejects_missing_icp():
    bad = rec("b")
    bad["icp"] = None
    bad["parse_error"] = "JSON 객체를 찾지 못했습니다"
    with pytest.raises(ValueError, match="B안"):
        C.compare(rec("a"), bad)


def test_markdown_has_side_by_side_rows_and_escapes_pipes():
    a = rec("a", industry_fit=["Frozen seafood importer", "A|B retailer"])
    b = rec("b", industry_fit=["Frozen seafood importers", "Online store"])
    md = C.render_markdown(C.compare(a, b))
    assert "# A안 vs B안 ICP 비교: x.pdf" in md and "## industry_fit" in md
    assert "| Frozen seafood importer | Frozen seafood importers | 일치 |" in md
    assert "| A\\|B retailer | | A만 |" in md and "| | Online store | B만 |" in md
    assert "NAICS 후보 2개 + OCR 텍스트 10자(쪽 1)" in md and "## 규칙 점검" in md
