import pytest

from app import config, data, questions as Q


@pytest.fixture(scope="module")
def built():
    s = config.load_settings()
    p = config.load_prompts()
    n = data.load_naics(config.data_path(s, "naics_xlsx"))
    c = data.load_countries(config.data_path(s, "countries_csv"))
    ix = data.load_index(config.data_path(s, "naics_index_xlsx"))
    return s, p, n, c, ix, Q.build_questions(n, c, p, ix, s["index"])


def test_shape(built):
    s, _, n, c, _, qs = built
    assert len(qs) == 308 + 2  # NAICS 4자리 308 + 국가 + 대륙(마지막)
    Q.validate_questions(qs, s["limits"])
    country, continent = qs[-2], qs[-1]
    assert country.id == "country" and len(country.options) == 196
    assert continent.id == "continent" and [o.id for o in continent.options][-3:] == ["south_america", "oceania", "none"]
    for q in qs:
        assert q.options[-1].id == "none"


def test_payload_matches_decisions_api_shape(built):
    *_, qs = built
    payload = Q.questions_payload(qs)
    assert list(payload)[-2:] == ["country", "continent"] and len(payload) == len(qs)
    for qid, q in payload.items():
        assert set(q) == {"type", "instructions", "criteria"} and q["type"] == "choice"
        assert list(q["criteria"])[-1] == "none" and all(isinstance(v, str) and v for v in q["criteria"].values())
    assert payload["country"]["criteria"]["KR"].startswith("대한민국") and "(South Korea" in payload["country"]["criteria"]["KR"]
    assert payload["country"]["criteria"]["none"] == "어느 국가도 언급되지 않았거나 판단할 수 없다."


def test_none_enforced(built):
    s, *_, qs = built
    bad = Q.Question("naics_x", "t", [Q.Option("11111", "a")])
    with pytest.raises(Q.QuestionError):
        Q.validate_questions([bad], s["limits"])


def test_id_regex_and_limits(built):
    s, *_ = built
    q = Q.Question("bad id!", "t", [Q.Option("none", "n")])
    with pytest.raises(Q.QuestionError):
        Q.validate_questions([q], s["limits"])
    lim = dict(s["limits"], max_questions=1)
    two = [Q.Question("a", "t", [Q.Option("none", "n")]), Q.Question("b", "t", [Q.Option("none", "n")])]
    with pytest.raises(Q.QuestionError):
        Q.validate_questions(two, lim)


def test_index_description_caps_and_priority(built):
    s, _, _, _, ix, _ = built
    d = Q.build_index_description(ix["33331"], 6, 160, [])
    assert d.startswith("e.g. ") and len(d) <= 160 + 5
    d2 = Q.build_index_description(ix["33331"], 6, 160, ["water purification"])
    assert "Water purification equipment" in d2
    assert Q.build_index_description([], 6, 160) is None
    assert Q.build_index_description(ix["33331"], 0, 160) is None


def test_sha_changes_with_prompt(built):
    s, p, n, c, ix, qs = built
    base = Q.questions_sha(qs)
    p2 = dict(p, naics_question=p["naics_question"] + " x")
    qs2 = Q.build_questions(n, c, p2, ix, s["index"])
    assert Q.questions_sha(qs2) != base
    assert Q.questions_sha(qs) == base


def test_index_disabled_removes_descriptions(built):
    s, p, n, c, ix, _ = built
    qs = Q.build_questions(n, c, p, ix, dict(s["index"], enabled=False))
    assert all(o.description is None for q in qs if q.id.startswith("naics_") for o in q.options if o.id != "none")


def test_every_country_belongs_to_a_defined_region(built):
    _, p, _, c, *_ = built
    ids = [r["id"] for r in p["continent_options"]]
    assert data.validate_regions(c, ids) == []
    assert {r for x in c for r in x.regions} == set(ids)  # 빈 지역 없음
    ru = next(x for x in c if x.alpha2 == "RU")
    assert ru.regions == ["eastern_europe", "east_asia"]  # 겹치는 국가는 여러 지역에 소속, 첫 번째가 우선
    kr = next(x for x in c if x.alpha2 == "KR")
    assert kr.region == "east_asia"
    broken = [data.Country("ZZ", "Zed", "", "atlantis")]
    with pytest.raises(data.CountMismatch):
        data.validate_regions(broken, ids)
    assert data.validate_regions(broken, ids, allow_mismatch=True)
    assert data.validate_regions(broken, []) == []  # 대륙 질문이 없으면 검사 생략


def test_countries_csv_has_bom_for_excel_and_loader_accepts_both(tmp_path):
    from app import config
    path = config.data_path(config.load_settings(), "countries_csv")
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"
    plain = tmp_path / "c.csv"
    plain.write_text("alpha2,name_en,name_ko,region\nKR,South Korea,대한민국,east_asia\n", encoding="utf-8")
    assert data.load_countries(plain)[0].name_ko == "대한민국"
    assert data.load_countries(path)[0].alpha2 == "AF"  # BOM 이 첫 열 이름을 망가뜨리지 않음
