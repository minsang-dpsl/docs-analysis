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


def test_continent_summarized_and_aggregated_like_country():
    p = probs({})
    p["continent"] = {"asia": 0.8, "europe": 0.15, "africa": 0.04, "none": 0.3}
    s1 = aggregate.summarize_page(p, {}, {}, {"asia": "Asia", "europe": "Europe", "africa": "Africa"})
    assert [c["code"] for c in s1["continents"]] == ["asia", "europe", "africa"] and s1["continents"][0]["name"] == "Asia"
    doc = aggregate.aggregate_document({2: s1}, TH)
    assert [c["code"] for c in doc["continents"]] == ["asia"] and doc["continents"][0]["evidence_page"] == 2
    rows = aggregate.result_rows(doc)
    assert rows[-1]["kind"] == "continent"


def test_region_members_listed_under_continent_in_csv_order():
    from app.data import Country
    countries = [Country("KR", "South Korea", "", "east_asia"), Country("JP", "Japan", "", "east_asia"),
                 Country("CN", "China", "", "east_asia"), Country("DE", "Germany", "", "western_europe")]
    p = probs({"KR": 0.8, "DE": 0.5})
    p["continent"] = {"east_asia": 0.9, "none": 0.1}
    s = aggregate.summarize_page(p, {"11251": "Aquaculture"}, {"KR": "South Korea", "DE": "Germany"}, {"east_asia": "East Asia"})
    doc = aggregate.attach_region_members(aggregate.aggregate_document({2: s}, TH), countries)
    members = doc["continents"][0]["countries"]
    assert [m["code"] for m in members] == ["KR", "CN", "JP"]  # 집계된 KR 이 앞, 나머지는 이름순, DE 는 다른 지역
    assert members[0]["score"] == 0.8 and members[1]["score"] is None
    kinds = [(r["kind"], r["code"]) for r in aggregate.result_rows(doc)]
    assert kinds.index(("country", "KR")) < kinds.index(("continent", "east_asia")) < kinds.index(("continent_country", "KR"))
    assert kinds[-3:] == [("continent_country", "KR"), ("continent_country", "CN"), ("continent_country", "JP")]
    cn = [r for r in aggregate.result_rows(doc) if r["code"] == "CN" and r["kind"] == "continent_country"][0]
    assert cn["score"] == "" and cn["evidence_page"] == ""


def test_overlapping_country_listed_once_under_highest_scoring_region():
    from app.data import Country
    countries = [Country("RU", "Russia", "", "eastern_europe;east_asia"), Country("JP", "Japan", "", "east_asia"),
                 Country("PL", "Poland", "", "eastern_europe")]

    def build(eu, asia):
        p = probs({})
        p["continent"] = {"eastern_europe": eu, "east_asia": asia, "none": 0.1}
        s = aggregate.summarize_page(p, {}, {}, {"eastern_europe": "EE", "east_asia": "EA"})
        return aggregate.attach_region_members(aggregate.aggregate_document({1: s}, TH), countries)

    def listed(doc):
        return {c["code"]: [m["code"] for m in c["countries"]] for c in doc["continents"]}

    assert listed(build(0.9, 0.5)) == {"eastern_europe": ["PL", "RU"], "east_asia": ["JP"]}  # 점수 높은 동유럽 아래
    assert listed(build(0.5, 0.9)) == {"east_asia": ["JP", "RU"], "eastern_europe": ["PL"]}
    assert listed(build(0.7, 0.7))["eastern_europe"] == ["PL", "RU"]  # 동점이면 region 열의 첫 지역
    rows = [r for r in aggregate.result_rows(build(0.9, 0.5)) if r["code"] == "RU"]
    assert len(rows) == 1  # 결과에는 한 번만
    only_asia = build(0.05, 0.9)  # 동유럽이 임계값 아래면 러시아는 집계된 동아시아 아래로
    assert listed(only_asia) == {"east_asia": ["JP", "RU"]}


def test_result_rows_flatten():
    pages = {1: summ({"11251": 0.5, "KR": 0.4})}
    rows = aggregate.result_rows(aggregate.aggregate_document(pages, TH))
    assert rows[0]["kind"] == "naics" and rows[-1]["kind"] == "country"
    assert set(rows[0]) == {"kind", "code", "name", "score", "evidence_page"}
