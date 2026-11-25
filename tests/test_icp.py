import base64
import json
from pathlib import Path

import pytest

from app import config, icp

S = config.load_settings()
P = config.load_prompts()
SAMPLE = Path(__file__).parent.parent / "samples" / "pdf" / "텍스트기반" / "[Hasung Electronics] Company introduction.pdf"  # 3쪽
OCR_TEXT = "===== Page 1 =====\nHello {not a placeholder}\n\n===== Page 3 =====\nWorld\n"
CSV = ("kind,code,name,score,evidence_page\n"
       "naics,33451,Navigational Instruments Manufacturing,0.55,1\n"
       "naics,45991,Tractor Dealers,0.91,3\n"
       "country,KR,South Korea,0.5,1\n")
GOOD = json.dumps({"icp": {"industry_fit": ["a"] * 5, "expand_fit": ["b"] * 5, "role_fit": ["c"] * 3,
                           "disqualify": ["d"] * 3, "contact_fit": ["purchasing", "sourcing", "buying"]}})


class FakeClient:
    def __init__(self, text=GOOD, usage=None):
        self.text = text
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 200}
        self.calls = []

    def generate(self, system, user, pdf_b64=None, **kw):
        self.calls.append({"system": system, "user": user, "pdf_b64": pdf_b64})
        return self.text, self.usage, 0.5


@pytest.fixture
def out_dir(tmp_path):
    (tmp_path / "__result.csv").write_text(CSV, encoding="utf-8-sig")
    (tmp_path / "__ocr.txt").write_text(OCR_TEXT, encoding="utf-8")
    return tmp_path


def test_parse_json_text_variants():
    assert icp.parse_json_text('{"a": 1}') == {"a": 1}
    assert icp.parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert icp.parse_json_text('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        icp.parse_json_text("no json")


def test_extract_icp_requires_icp_object_and_warns_on_missing_fields():
    got, warnings = icp.extract_icp({"icp": {"industry_fit": ["x"]}})
    assert got == {"industry_fit": ["x"]} and any("expand_fit" in w for w in warnings) and len(warnings) == 4
    with pytest.raises(ValueError):
        icp.extract_icp({"industry_fit": ["x"]})


def test_naics_candidates_sorted_by_score_without_scores():
    rows = [{"code": "33451", "name": "Nav", "score": "0.5"}, {"code": "45991", "name": "Tractor", "score": "0.9"},
            {"code": "11111", "name": "Tie", "score": "0.5"}]
    assert icp.build_naics_candidates(rows) == "45991 Tractor\n33451 Nav\n11111 Tie"  # 동점은 입력 순서 유지
    assert icp.build_naics_candidates([]) == "(none)"


def test_ocr_pages_from_markers():
    assert icp.ocr_pages(OCR_TEXT) == [1, 3] and icp.ocr_pages("no markers") == []


def test_build_prompt_same_system_for_both_modes():
    sa, ua = icp.build_prompt(P, "a", "45991 Tractor", "===== Page 3 =====\nbrace {x}")
    sb, ub = icp.build_prompt(P, "b")
    assert sa == sb and '"industry_fit"' in sa and "ICP" in sa and "[SYSTEM]" not in sa and sa == sa.strip()
    assert ua.startswith("naics_candidates:\n45991 Tractor") and "document_text:\n===== Page 3 =====\nbrace {x}" in ua
    assert "naics_candidates:\nnone provided" in ub and "attached as a PDF" in ub
    with pytest.raises(ValueError):
        icp.build_prompt(P, "c")


def test_run_a_uses_ocr_text_and_candidates_without_pdf(out_dir):
    client = FakeClient(usage={"prompt_tokens": 1000, "completion_tokens": 200, "cost": 0.0123})
    rec = icp.run_icp(SAMPLE, "a", S, P, client, out_dir)
    call = client.calls[0]
    assert call["pdf_b64"] is None  # A안은 PDF 를 보내지 않음
    assert "45991 Tractor Dealers\n33451 Navigational" in call["user"] and "Hello {not a placeholder}" in call["user"]
    assert rec["input"]["naics_candidates"] == 2 and rec["input"]["ocr_pages"] == [1, 3] and rec["input"]["ocr_chars"] == len(OCR_TEXT)
    assert rec["cost_usd"] == 0.0123 and rec["cost_source"] == "응답 usage.cost"
    saved = json.loads((out_dir / "__icp_a.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "a" and saved["model"] == S["icp"]["model"] and saved["parse_error"] is None
    assert saved["icp"]["contact_fit"] == ["purchasing", "sourcing", "buying"] and saved["raw_text"] is None


def test_run_b_sends_whole_pdf_bytes_and_no_candidates(out_dir):
    client = FakeClient()
    rec = icp.run_icp(SAMPLE, "b", S, P, client, out_dir)
    call = client.calls[0]
    assert base64.b64decode(call["pdf_b64"]) == SAMPLE.read_bytes()  # 원본 바이트 그대로, 통째로
    assert "none provided" in call["user"]
    assert rec["input"]["total_pages"] == 3 and rec["input"]["pdf_bytes"] == SAMPLE.stat().st_size
    assert (out_dir / "__icp_b.json").exists()
    assert client.calls[0]["system"] == icp.build_prompt(P, "a", "x", "y")[0]  # 두 안의 system 은 동일


def test_cost_falls_back_to_settings_pricing(out_dir):
    rec = icp.run_icp(SAMPLE, "b", S, P, FakeClient(), out_dir)  # usage 에 cost 없음: 1000 in, 200 out
    assert rec["cost_usd"] == pytest.approx(1000 / 1e6 * 0.10 + 200 / 1e6 * 0.50) and "추정" in rec["cost_source"]
    assert icp.icp_cost({"icp": {}}, {"prompt_tokens": 1, "completion_tokens": 1}) == (None, "계산 불가")


@pytest.mark.parametrize("text, expect", [("not json at all", "JSON 객체를 찾지 못했습니다"), ('{"x": 1}', "icp 객체가 없습니다")])
def test_parse_errors_are_recorded_with_raw_text(out_dir, text, expect):
    rec = icp.run_icp(SAMPLE, "b", S, P, FakeClient(text=text), out_dir)
    assert rec["icp"] is None and expect in rec["parse_error"] and rec["raw_text"] == text


def test_run_a_input_errors(tmp_path):
    with pytest.raises(icp.IcpInputError, match="결과 CSV"):
        icp.run_icp(SAMPLE, "a", S, P, FakeClient(), tmp_path)
    (tmp_path / "__result.csv").write_text("kind,code,name,score,evidence_page\ncountry,KR,K,0.5,1\n", encoding="utf-8-sig")
    with pytest.raises(icp.IcpInputError, match="NAICS 행"):
        icp.run_icp(SAMPLE, "a", S, P, FakeClient(), tmp_path)
    (tmp_path / "__result.csv").write_text(CSV, encoding="utf-8-sig")
    with pytest.raises(icp.IcpInputError, match="OCR 텍스트가 없습니다"):
        icp.run_icp(SAMPLE, "a", S, P, FakeClient(), tmp_path)
    (tmp_path / "__ocr.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(icp.IcpInputError, match="비어"):
        icp.run_icp(SAMPLE, "a", S, P, FakeClient(), tmp_path)
