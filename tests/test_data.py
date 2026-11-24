import pytest

from app import config, data


@pytest.fixture(scope="module")
def ctx():
    s = config.load_settings()
    n = data.load_naics(config.data_path(s, "naics_xlsx"))
    c = data.load_countries(config.data_path(s, "countries_csv"))
    ix = data.load_index(config.data_path(s, "naics_index_xlsx"))
    return s, n, c, ix


def test_counts(ctx):
    s, n, c, _ = ctx
    assert len(n.groups4) == 308 and len(n.items5) == 689 and len(c) == 195
    assert data.validate_counts(n, c, s["data"]["expected"]) == []


def test_count_mismatch_raises(ctx):
    s, n, c, _ = ctx
    with pytest.raises(data.CountMismatch):
        data.validate_counts(n, c[:-1], s["data"]["expected"])
    assert data.validate_counts(n, c[:-1], s["data"]["expected"], allow_mismatch=True)


def test_range_codes_excluded(ctx):
    _, n, _, _ = ctx
    assert all(code.isdigit() for code in n.items5)
    assert n.items5["33331"] == "Commercial and Service Industry Machinery Manufacturing"
    assert "33331" in n.children["3333"]


def test_index_maps_water_purification_to_33331(ctx):
    _, _, _, ix = ctx
    assert "Water purification equipment manufacturing" in ix["33331"]
    assert all(code.isdigit() and len(code) == 5 for code in ix)  # ****** 행 제외


def test_countries_unique_and_titles(ctx):
    _, _, c, _ = ctx
    assert len({x.alpha2 for x in c}) == 195
    assert {x.alpha2: x.name_en for x in c}["ER"] == "Eritrea"
