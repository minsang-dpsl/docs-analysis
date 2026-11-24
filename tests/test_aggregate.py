from app import aggregate

TH = {"naics_min": 0.3, "country_relative": 0.25, "country_min_abs": 0.05}


def probs(page_vals):
    return {
        "naics_1125": {"11251": page_vals.get("11251", 0.0), "none": 0.1},
        "naics_3256": {"32562": page_vals.get("32562", 0.0), "none": 0.1},
        "country": {"KR": page_vals.get("KR", 0.0), "DE": page_vals.get("DE", 0.0), "US": page_vals.get("US", 0.0), "none": 0.5},
    }


def summ(vals):
    return aggregate.summarize_page(probs(vals), {"11251": "Aquaculture", "32562": "Toilet"}, {"KR": "South Korea", "DE": "Germany", "US": "United States"})


def test_summarize_excludes_none_and_sorts():
    s = summ({"11251": 0.4, "32562": 0.7, "KR": 0.3})
    assert [x["code"] for x in s["naics"]] == ["32562", "11251"]
    assert all(x["code"] != "none" for x in s["naics"] + s["countries"])


def test_naics_max_over_pages_and_threshold():
    pages = {1: summ({"11251": 0.2}), 2: summ({"11251": 0.6, "32562": 0.29})}
    doc = aggregate.aggregate_document(pages, TH)
    assert [(x["code"], x["evidence_page"], x["score"]) for x in doc["naics"]] == [("11251", 2, 0.6)]


def test_country_relative_and_abs_filters():
    # 쪽 내 1순위 KR 0.8 -> DE 0.15(<0.2 상대 컷) 제외, US 0.04 제외
    pages = {3: summ({"KR": 0.8, "DE": 0.15, "US": 0.04})}
    doc = aggregate.aggregate_document(pages, TH)
    assert [c["code"] for c in doc["countries"]] == ["KR"]


def test_country_takes_max_over_pages():
    pages = {1: summ({"KR": 0.3, "DE": 0.3}), 4: summ({"KR": 0.9})}
    doc = aggregate.aggregate_document(pages, TH)
    kr = [c for c in doc["countries"] if c["code"] == "KR"][0]
    assert kr["score"] == 0.9 and kr["evidence_page"] == 4


def test_result_rows_flatten():
    pages = {1: summ({"11251": 0.5, "KR": 0.4})}
    rows = aggregate.result_rows(aggregate.aggregate_document(pages, TH))
    assert rows[0]["kind"] == "naics" and rows[-1]["kind"] == "country"
    assert set(rows[0]) == {"kind", "code", "name", "score", "evidence_page"}
