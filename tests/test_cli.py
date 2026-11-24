import json
from pathlib import Path

import pytest

from app import cli, client as client_mod

SAMPLE = str(Path(__file__).parent.parent / "samples" / "Hasung_Electronics_Company_introduction.pdf")


class Fake:
    calls = []

    def __init__(self, settings):
        pass

    def decide(self, b64, filename, questions):
        Fake.calls.append(filename)
        page = int(filename.split("_")[1].split(".")[0])
        probs = {q.id: {o.id: 0.0 for o in q.options} for q in questions}
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
    base = tmp_path / "gemini-decision-1" / "Hasung_Electronics_Company_introduction"
    res = json.loads((base / "__result.json").read_text())
    assert res["pages_done"] == [1, 2, 3] and res["total_pages"] == 3
    naics = {n["code"]: n for n in res["doc"]["naics"]}
    assert naics["45991"]["evidence_page"] == 3 and naics["45991"]["score"] == 0.91
    assert [c["code"] for c in res["doc"]["countries"]][:2] == ["KR", "JP"]
    assert (base / "__result.csv").read_text().splitlines()[0] == "kind,code,name,score,evidence_page"
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
    pj = tmp_path / "gemini-decision-1" / "Hasung_Electronics_Company_introduction" / "pages" / "1.json"
    d = json.loads(pj.read_text()); d["pdf_sha256"] = "0" * 64; pj.write_text(json.dumps(d))
    Fake.calls.clear()
    cli.main(["run", SAMPLE, "--out", out, "--resume", "--pages", "1"])
    assert Fake.calls == ["page_1.pdf"]


def test_dry_run_makes_no_calls(tmp_path, capsys):
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--dry-run"]) == 0
    assert Fake.calls == [] and "dry-run" in capsys.readouterr().out


def test_estimate_offline(capsys):
    assert cli.main(["estimate", SAMPLE, "--pages", "1-2"]) == 0
    assert "질문 309개" in capsys.readouterr().out


def test_blocked_page_is_recorded_and_not_retried(tmp_path, monkeypatch):
    def blocked(self, b64, filename, questions):
        raise client_mod.ApiError("blocked", 400, blocked=True)
    monkeypatch.setattr(Fake, "decide", blocked)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1"]) == 0
    base = tmp_path / "gemini-decision-1" / "Hasung_Electronics_Company_introduction"
    assert json.loads((base / "pages" / "1.json").read_text())["status"] == "blocked"
    assert json.loads((base / "__result.json").read_text())["pages_blocked"] == [1]


def test_error_page_returns_nonzero_and_is_not_saved(tmp_path, monkeypatch):
    def boom(self, b64, filename, questions):
        raise client_mod.ApiError("HTTP 500", 500)
    monkeypatch.setattr(Fake, "decide", boom)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1"]) == 1
    base = tmp_path / "gemini-decision-1" / "Hasung_Electronics_Company_introduction"
    assert not (base / "pages" / "1.json").exists()
