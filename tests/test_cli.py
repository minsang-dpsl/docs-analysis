import json
from pathlib import Path

import pytest

from app import cli, client as client_mod

SAMPLE = str(Path(__file__).parent.parent / "samples" / "pdf" / "텍스트기반" / "[Hasung Electronics] Company introduction.pdf")
STEM = Path(SAMPLE).stem  # 출력 폴더 이름은 PDF 파일명(확장자 제외)


class Fake:
    calls = []

    def __init__(self, settings):
        pass

    def decide(self, b64, filename, questions):
        Fake.calls.append(filename)
        page = int(filename.split("_")[1].split(".")[0])
        probs = {q.id: {o.id: 0.0 for o in q.options} for q in questions}
        probs["continent"]["east_asia"] = 0.6
        if page == 3:
            probs["naics_4599"]["45991"] = 0.91
            probs["country"]["JP"] = 0.2
        else:
            probs["naics_4599"]["45991"] = 0.4
            probs["country"]["KR"] = 0.5
            probs["country"]["JP"] = 0.48
        return probs, {"prompt_tokens": 100, "completion_tokens": 10}, 0.01

    def generate(self, system, user, pdf_b64=None, **kw):
        assert "DIRECT BUYER" in user and "45991" in user
        return "```json\n{\"company_summary\": \"x\", \"icp\": [], \"confidence\": \"low\"}\n```", {"prompt_tokens": 1, "completion_tokens": 1}, 0.1


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Fake.calls = []
    monkeypatch.setattr(cli, "DecisionClient", Fake)


def test_run_resume_select_icp(tmp_path, capsys):
    out = str(tmp_path)
    assert cli.main(["run", SAMPLE, "--out", out]) == 0
    base = tmp_path / "gemini-decision-1" / STEM
    res = json.loads((base / "__result.json").read_text())
    assert res["pages_done"] == [1, 2, 3] and res["total_pages"] == 3
    naics = {n["code"]: n for n in res["doc"]["naics"]}
    assert naics["45991"]["evidence_page"] == 3 and naics["45991"]["score"] == 0.91
    assert [c["code"] for c in res["doc"]["countries"]][:2] == ["KR", "JP"]
    assert (base / "__result.csv").read_bytes()[:3] == b"\xef\xbb\xbf"  # 엑셀에서 한글이 깨지지 않게 BOM
    csv_lines = (base / "__result.csv").read_text(encoding="utf-8-sig").splitlines()
    assert csv_lines[0] == "kind,code,name,score,evidence_page"
    i = next(n for n, l in enumerate(csv_lines) if l.startswith("continent,east_asia,"))
    assert csv_lines[i + 1].startswith("continent_country,KR,") and any(l.startswith("continent_country,CN,,") or l.startswith("continent_country,CN,China") for l in csv_lines[i + 1:])
    log = [json.loads(l) for l in (base / "__run_log.jsonl").read_text().splitlines()]
    assert len(log) == 3 and all(e["status"] == "ok" for e in log)

    # --resume: 같은 sha 이므로 호출 없음
    Fake.calls.clear()
    assert cli.main(["run", SAMPLE, "--out", out, "--resume"]) == 0
    assert Fake.calls == []

    # --pages 부분 실행은 기존 쪽 결과를 유지한 채 집계
    (base / "pages" / "2.json").unlink()
    assert cli.main(["run", SAMPLE, "--out", out, "--resume", "--pages", "2"]) == 0
    assert Fake.calls == ["page_2.pdf"]

    assert cli.main(["select", SAMPLE, "--out", out]) == 0
    sel = json.loads((base / "__pages_selected.json").read_text())
    assert sel["pages"] == [1, 2, 3]  # 3쪽 문서는 전체 사용

    assert cli.main(["icp", SAMPLE, "--out", out]) == 0
    icp = json.loads((base / "__icp.json").read_text())
    assert icp["selected_pages"] == [1, 2, 3] and icp["icp"]["confidence"] == "low" and icp["parse_error"] is None


def test_resume_invalidated_when_pdf_sha_changes(tmp_path):
    out = str(tmp_path)
    cli.main(["run", SAMPLE, "--out", out, "--pages", "1"])
    pj = tmp_path / "gemini-decision-1" / STEM / "pages" / "1.json"
    d = json.loads(pj.read_text()); d["pdf_sha256"] = "0" * 64; pj.write_text(json.dumps(d))
    Fake.calls.clear()
    cli.main(["run", SAMPLE, "--out", out, "--resume", "--pages", "1"])
    assert Fake.calls == ["page_1.pdf"]


def test_dry_run_makes_no_calls(tmp_path, capsys):
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--dry-run"]) == 0
    assert Fake.calls == [] and "dry-run" in capsys.readouterr().out


def test_estimate_offline(capsys):
    assert cli.main(["estimate", SAMPLE, "--pages", "1-2"]) == 0
    assert "질문 310개" in capsys.readouterr().out


def test_blocked_page_is_recorded_and_not_retried(tmp_path, monkeypatch):
    def blocked(self, b64, filename, questions):
        raise client_mod.ApiError("blocked", 400, blocked=True)
    monkeypatch.setattr(Fake, "decide", blocked)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1"]) == 0
    base = tmp_path / "gemini-decision-1" / STEM
    assert json.loads((base / "pages" / "1.json").read_text())["status"] == "blocked"
    assert json.loads((base / "__result.json").read_text())["pages_blocked"] == [1]


def test_error_page_returns_nonzero_and_is_not_saved(tmp_path, monkeypatch):
    def boom(self, b64, filename, questions):
        raise client_mod.ApiError("HTTP 500", 500)
    monkeypatch.setattr(Fake, "decide", boom)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1"]) == 1
    base = tmp_path / "gemini-decision-1" / STEM
    assert not (base / "pages" / "1.json").exists()


def test_run_prints_estimate_and_actual_summary(tmp_path, capsys):
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[예상] 호출 3쪽(재사용 0쪽): 입력 약" in out and "출력 약 0 tok, 비용 약 $0.0" in out
    assert "[실제] 소요 " in out and "토큰 입력 300 / 출력 30 (성공 3쪽)" in out and "비용 $0.0000 (settings.pricing 추정)" in out
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--resume"]) == 0
    out = capsys.readouterr().out
    assert "[예상] 호출 0쪽(재사용 3쪽)" in out and "(성공 0쪽)" in out  # 재사용 쪽은 합계에 넣지 않음


def test_run_actual_cost_prefers_billed_usage_cost(tmp_path, capsys, monkeypatch):
    def billed(self, b64, filename, questions):
        probs = {q.id: {o.id: 0.0 for o in q.options} for q in questions}
        return probs, {"prompt_tokens": 1000, "completion_tokens": 0, "cost": 0.0123, "refused": []}, 0.01
    monkeypatch.setattr(Fake, "decide", billed)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1-2"]) == 0
    out = capsys.readouterr().out
    assert "비용 $0.0246 (응답 usage.cost 합계)" in out and "토큰 입력 2,000 / 출력 0 (성공 2쪽)" in out


def test_dry_run_prints_estimate(tmp_path, capsys):
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--dry-run", "--pages", "1-2"]) == 0
    assert "[dry-run] 예상 (2쪽): 입력 약" in capsys.readouterr().out


def test_estimate_summary_without_pricing_says_not_computable():
    from types import SimpleNamespace
    est = {"chars_per_token": 3.5, "pdf_tokens_per_page": 1500, "output_tokens_per_page": 0}
    unset = SimpleNamespace(settings={"pricing": {}, "estimate": est}, questions=[])
    assert "계산 불가(settings.pricing 단가 미설정)" in cli.estimate_summary(unset, 2)
    priced = SimpleNamespace(settings={"pricing": {"input_per_mtok": 0.10, "output_per_mtok": 0.0}, "estimate": est}, questions=[])
    # 질문 없음({} 2글자 = 약 0.57 tok) -> 쪽당 약 1,500.57 tok: 2쪽 3,001 tok x $0.10/1M = $0.0003
    assert cli.estimate_summary(priced, 2) == "입력 약 3,001 tok, 출력 약 0 tok, 비용 약 $0.0003"
