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
    assert len(qs) == 308 + 1
    Q.validate_questions(qs, s["limits"])
    country = qs[-1]
    assert country.id == "country" and len(country.options) == 196
    for q in qs:
        assert q.options[-1].id == "none"


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
