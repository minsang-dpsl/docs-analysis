import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from app import cli, ocr

SAMPLE = str(Path(__file__).parent.parent / "samples" / "pdf" / "텍스트기반" / "[Hasung Electronics] Company introduction.pdf")
STEM = Path(SAMPLE).stem  # 3쪽짜리 샘플


def row(code, score, page, kind="naics"):
    return {"kind": kind, "code": code, "name": code, "score": str(score), "evidence_page": str(page)}


# ---------------------------------------------------------------- 쪽 선택
def test_select_counts_codes_then_score_sum_then_page():
    rows = [
        row("a1", 0.9, 4), row("a2", 0.3, 4), row("a3", 0.3, 4),      # 쪽 4: 3개
        row("b1", 0.5, 2), row("b2", 0.5, 2),                         # 쪽 2: 2개, 합 1.0
        row("c1", 0.6, 7), row("c2", 0.6, 7),                         # 쪽 7: 2개, 합 1.2 (쪽 2보다 앞)
        row("d1", 0.5, 1), row("d2", 0.5, 1),                         # 쪽 1: 2개, 합 1.0 (쪽 2와 동점 -> 쪽 번호 작은 1이 앞)
        row("e1", 0.99, 9),                                           # 쪽 9: 1개
        row("country", 0.99, 3, kind="country"),                      # naics 가 아니면 무시
    ]
    sel = ocr.select_ocr_pages(rows, 4)
    assert sel["pages"] == [4, 7, 1, 2]
    assert sel["stats"][4] == {"n": 3, "sum": 1.5, "codes": ["a1", "a2", "a3"]}
    assert 3 not in sel["stats"]


def test_select_uses_only_pages_that_exist_when_fewer_than_top():
    sel = ocr.select_ocr_pages([row("x", 0.8, 5), row("y", 0.4, 2)], 5)
    assert sel["pages"] == [5, 2] or sel["pages"] == [2, 5]  # 개수 동률이면 score 합이 큰 5쪽이 먼저
    assert sel["pages"] == [5, 2]
    assert ocr.select_ocr_pages([], 5)["pages"] == []


def test_read_naics_rows_handles_bom_and_filters_kind(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("kind,code,name,score,evidence_page\nnaics,11251,Aqua,0.5,2\ncountry,KR,Korea,0.4,1\n"
                 "continent,east_asia,동아시아,0.6,1\n", encoding="utf-8-sig")
    rows = ocr.read_naics_rows(p)
    assert [r["code"] for r in rows] == ["11251"]
    p.write_text("kind,code,name,score,evidence_page\nnaics,11251,Aqua,0.5,2\n", encoding="utf-8")  # BOM 없어도 동작
    assert ocr.read_naics_rows(p)[0]["evidence_page"] == "2"


# ---------------------------------------------------------------- 텍스트 추출 (Document AI 는 가짜)
def fake_document(page_texts):
    """Document AI 응답 모양: 전체 text 와 쪽별 text_anchor 구간."""
    text, pages, pos = "", [], 0
    for i, t in enumerate(page_texts, 1):
        seg = SimpleNamespace(start_index=pos, end_index=pos + len(t))
        text += t
        pos += len(t)
        pages.append(SimpleNamespace(page_number=i, layout=SimpleNamespace(text_anchor=SimpleNamespace(text_segments=[seg]))))
    return SimpleNamespace(text=text, pages=pages)


class FakeProcess:
    """제출된 PDF 의 쪽 수만큼 'p{i}' 텍스트를 돌려주고 호출 내용을 기록합니다."""

    def __init__(self):
        self.calls = []

    def __call__(self, content: bytes):
        n = len(PdfReader(io.BytesIO(content)).pages)
        self.calls.append(n)
        return fake_document([f"텍스트{i}" for i in range(1, n + 1)])


def test_pages_text_slices_per_page_and_tolerates_empty_page():
    doc = fake_document(["가나다", "", "ABC"])
    assert ocr.pages_text(doc) == {1: "가나다", 2: "", 3: "ABC"}


def test_combine_pages_text_has_separators_in_page_order():
    assert ocr.combine_pages_text({3: "C\n", 1: " A ", 2: ""}) == "===== Page 1 =====\nA\n\n===== Page 2 =====\n\n\n===== Page 3 =====\nC\n"


def test_extract_sends_selected_pages_in_one_request_and_maps_to_original_pages():
    proc = FakeProcess()
    texts, n_req = ocr.extract_text(SAMPLE, [3, 1], proc, max_pages=15, max_bytes=35_000_000)
    assert n_req == 1 and proc.calls == [2]          # 순위 순서와 무관하게 쪽 번호 오름차순으로 합쳐 한 번에
    assert texts == {1: "텍스트1", 3: "텍스트2"}      # 합친 PDF 의 2번째 쪽 = 원본 3쪽


def test_extract_splits_into_per_page_requests_over_limit():
    proc = FakeProcess()
    texts, n_req = ocr.extract_text(SAMPLE, [1, 2, 3], proc, max_pages=2, max_bytes=35_000_000)
    assert n_req == 3 and proc.calls == [1, 1, 1] and set(texts) == {1, 2, 3}
    with pytest.raises(ocr.OcrError):                  # 한 쪽도 크기 한도를 넘으면 보내지 않고 오류
        ocr.extract_text(SAMPLE, [1], FakeProcess(), max_pages=15, max_bytes=10)
    assert ocr.extract_text(SAMPLE, [], proc, 15, 35_000_000) == ({}, 0)


# ---------------------------------------------------------------- CLI
@pytest.fixture
def result_csv(tmp_path):
    base = tmp_path / "gemini-decision-1" / STEM
    base.mkdir(parents=True)
    (base / "__result.csv").write_text(
        "kind,code,name,score,evidence_page\n"
        "naics,45991,A,0.91,3\nnaics,45992,B,0.60,3\nnaics,33451,C,0.50,1\n"
        "country,KR,South Korea,0.52,1\n", encoding="utf-8-sig")
    return base


def test_cli_dry_run_selects_but_never_calls_document_ai(tmp_path, result_csv, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "make_process", lambda *a, **k: pytest.fail("dry-run 은 Document AI 를 만들면 안 됩니다"))
    assert cli.main(["ocr", SAMPLE, "--out", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1위 쪽 3: 코드 2개, score 합 1.5100" in out and "2위 쪽 1: 코드 1개" in out
    assert "[예상] Document AI 2쪽: 비용 약 $0.0030" in out and "[dry-run]" in out
    assert not (result_csv / "__ocr.json").exists()


def test_cli_ocr_writes_json_and_page_text_files(tmp_path, result_csv, monkeypatch, capsys):
    proc = FakeProcess()
    monkeypatch.setattr(ocr, "make_process", lambda *a, **k: proc)
    assert cli.main(["ocr", SAMPLE, "--out", str(tmp_path), "--top", "5"]) == 0
    assert proc.calls == [2]
    rec = json.loads((result_csv / "__ocr.json").read_text(encoding="utf-8"))
    assert [s["page"] for s in rec["selected"]] == [3, 1] and rec["selected"][0]["naics_codes"] == ["45991", "45992"]
    assert rec["pages"] == {"1": "텍스트1", "3": "텍스트2"} and rec["ocr"]["requests"] == 1 and rec["ocr"]["pages_sent"] == 2
    assert (result_csv / "__ocr.txt").read_text(encoding="utf-8") == "===== Page 1 =====\n텍스트1\n\n===== Page 3 =====\n텍스트2\n"
    assert not (result_csv / "ocr").exists()  # 쪽별 txt 폴더는 만들지 않음
    out = capsys.readouterr().out
    assert "[실제] 소요 " in out and "요청 1회 | 2쪽, 글자 8자" in out


def test_cli_ocr_errors(tmp_path, result_csv, monkeypatch, capsys):
    with pytest.raises(SystemExit):                    # 결과 CSV 없음
        cli.main(["ocr", SAMPLE, "--out", str(tmp_path / "nowhere")])
    monkeypatch.delenv("DOCUMENTAI_PROJECT_ID", raising=False)
    monkeypatch.delenv("DOCUMENTAI_PROCESSOR_ID", raising=False)
    assert cli.main(["ocr", SAMPLE, "--out", str(tmp_path)]) == 2   # 환경 변수 누락은 트레이스백 대신 오류 메시지
    assert "DOCUMENTAI_PROJECT_ID" in capsys.readouterr().err
    (result_csv / "__result.csv").write_text("kind,code,name,score,evidence_page\ncountry,KR,Korea,0.5,1\n", encoding="utf-8-sig")
    assert cli.main(["ocr", SAMPLE, "--out", str(tmp_path), "--dry-run"]) == 1  # NAICS 행 없음
    (result_csv / "__result.csv").write_text("kind,code,name,score,evidence_page\nnaics,1,A,0.5,9\n", encoding="utf-8-sig")
    with pytest.raises(SystemExit):                    # 3쪽 PDF 에 9쪽이 나옴 -> 다른 PDF 의 CSV
        cli.main(["ocr", SAMPLE, "--out", str(tmp_path), "--dry-run"])


def test_tests_never_see_real_credentials():
    """conftest 격리 확인: load_settings() 가 .env 를 환경 변수로 올리지 않는다(실호출 방지)."""
    import os
    from app import config
    config.load_settings()
    assert not any(os.environ.get(n) for n in ("DOCUMENTAI_PROJECT_ID", "DOCUMENTAI_PROCESSOR_ID", "OPENROUTER_API_KEY"))
