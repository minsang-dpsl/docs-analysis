from app import config
from app.select_pages import page_stats, select_pages
from tests.helpers import load_doc

S = config.load_settings()


def sel(name, total):
    return select_pages(load_doc(name), total, S)


def test_small_doc_uses_all_pages():
    r = sel("Hasung", 3)
    assert r["pages"] == [1, 2, 3] and r["dropped"] == []


def test_chaesy_picks_partner_page_front_and_country():
    r = sel("Chaesy", 12)
    assert 11 in r["pages"] and {1, 2} <= set(r["pages"])
    assert "strong" in r["reasons"][11]


def test_biocera_covers_application_and_buyer_pages():
    r = sel("BIOCERA", 12)
    assert {5, 9, 1, 2} <= set(r["pages"])
    assert len(r["pages"]) <= 6


def test_cheongcho_keeps_single_high_score_pages():
    """합계 순위로는 밀리는 13쪽(단일 고점수 0.91)과 11쪽이 강한 신호로 포함되어야 함."""
    doc = load_doc("Cheongcho")
    stats = page_stats(doc["naics"])
    assert stats[13]["n"] == 1 and stats[13]["max"] > 0.9
    r = select_pages(doc, 14, S)
    assert {13, 11, 6, 12} <= set(r["pages"])
    assert 3 in r["pages"]  # 명시적 ICP 목록이 있는 쪽(커버 단계)


def test_core_cap_applies_and_reports_dropped():
    cfg = {"page_selection": dict(S["page_selection"], max_pages=2)}
    r = select_pages(load_doc("Cheongcho"), 14, cfg)
    assert r["dropped"]
    assert all(d not in r["pages"] for d in r["dropped"] if d not in (1, 2, 9))


def test_empty_doc_falls_back_to_front_pages():
    r = select_pages({"naics": [], "countries": []}, 10, S)
    assert r["pages"] == [1, 2]
