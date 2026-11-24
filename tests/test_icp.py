import pytest

from app import config, icp
from tests.helpers import load_doc

S = config.load_settings()
P = config.load_prompts()


def test_parse_json_text_variants():
    assert icp.parse_json_text('{"a": 1}') == {"a": 1}
    assert icp.parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert icp.parse_json_text('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        icp.parse_json_text("no json")


def test_hints_biocera_flags_flat_country_scores():
    doc = load_doc("BIOCERA")
    naics, country, note = icp.build_hints(doc, S["icp"])
    assert "11251" in naics and "peak page 5" in naics
    assert "LOW" in note  # 최고 0.28 < 0.4


def test_hints_chaesy_country_ok_and_prompt_rules():
    doc = load_doc("Chaesy")
    _, _, note = icp.build_hints(doc, S["icp"])
    assert "OK" in note
    system, user = icp.build_prompt(P, [1, 2, 11], doc, S["icp"])
    assert "DIRECT BUYER" in user and "1, 2, 11" in user and "{" in user  # JSON 스키마 중괄호 유지
    assert "ICP" in system


def test_hint_fallback_when_nothing_above_min():
    doc = {"naics": [{"code": "11111", "name": "x", "score": 0.31, "evidence_page": 2}], "countries": []}
    naics, country, note = icp.build_hints(doc, S["icp"])
    assert "11111" in naics and "(none)" in country and "LOW" in note
