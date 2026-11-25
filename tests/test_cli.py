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
        assert "naics_candidates:" in user
        if "none provided" in user:  # B안: PDF 통째로, NAICS 후보 없음
            assert pdf_b64 is not None
            mode = "b"
        else:  # A안: OCR 텍스트 + NAICS 후보, PDF 첨부 없음
            assert pdf_b64 is None and "45991" in user and "===== Page 1 =====" in user
            mode = "a"
        shared = ["Frozen seafood import distributor", "Korean food import wholesaler", "Seafood retail chain",
                  "Online seafood retailer", "Premium supermarket"]
        icp = {"industry_fit": shared, "expand_fit": ["Restaurant wholesaler" if mode == "a" else "Catering operator"] * 5,
               "role_fit": ["importer"] * 3, "disqualify": ["own-brand manufacturer"] * 3,
               "contact_fit": ["purchasing", "sourcing", "buying"]}
        return "```json\n" + json.dumps({"icp": icp}) + "\n```", {"prompt_tokens": 1, "completion_tokens": 1}, 0.1


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Fake.calls = []
    monkeypatch.setattr(cli, "DecisionClient", Fake)


def test_run_resume_select_icp(tmp_path, capsys):
    out = str(tmp_path)
    assert cli.main(["run", SAMPLE, "--out", out]) == 0
    base = tmp_path / STEM
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

    # icp A안은 run 결과(csv)와 ocr 결과(__ocr.txt)가 필요: ocr 단계는 가짜 텍스트 파일로 대신함
    assert cli.main(["icp", SAMPLE, "--out", out, "--mode", "a"]) == 2  # __ocr.txt 없음 -> 오류 메시지
    (base / "__ocr.txt").write_text("===== Page 1 =====\n본문\n\n===== Page 3 =====\n끝\n", encoding="utf-8")
    assert cli.main(["icp", SAMPLE, "--out", out, "--mode", "both"]) == 0
    a = json.loads((base / "__icp_a.json").read_text())
    b = json.loads((base / "__icp_b.json").read_text())
    assert a["mode"] == "a" and a["input"]["ocr_pages"] == [1, 3] and a["parse_error"] is None
    assert b["mode"] == "b" and b["input"]["total_pages"] == 3 and b["icp"]["contact_fit"] == ["purchasing", "sourcing", "buying"]
    printed = capsys.readouterr().out
    assert "[입력] A안: NAICS 후보" in printed and "[입력] B안: PDF 통째로 (3쪽" in printed and "[실제] A안 소요" in printed and "[실제] B안 소요" in printed

    assert cli.main(["compare", SAMPLE, "--out", out]) == 0
    cmp = json.loads((base / "__compare.json").read_text())
    assert cmp["fields"]["industry_fit"]["exact"] == 5 and cmp["fields"]["expand_fit"]["exact"] == 0
    md = (base / "__compare.md").read_text(encoding="utf-8")
    assert "| Frozen seafood import distributor | Frozen seafood import distributor | 일치 |" in md
    assert "겹침률" in capsys.readouterr().out


def test_resume_invalidated_when_pdf_sha_changes(tmp_path):
    out = str(tmp_path)
    cli.main(["run", SAMPLE, "--out", out, "--pages", "1"])
    pj = tmp_path / STEM / "pages" / "1.json"
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
    base = tmp_path / STEM
    assert json.loads((base / "pages" / "1.json").read_text())["status"] == "blocked"
    assert json.loads((base / "__result.json").read_text())["pages_blocked"] == [1]


def test_error_page_returns_nonzero_and_is_not_saved(tmp_path, monkeypatch):
    def boom(self, b64, filename, questions):
        raise client_mod.ApiError("HTTP 500", 500)
    monkeypatch.setattr(Fake, "decide", boom)
    assert cli.main(["run", SAMPLE, "--out", str(tmp_path), "--pages", "1"]) == 1
    base = tmp_path / STEM
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


def test_compare_requires_both_icp_files(tmp_path):
    with pytest.raises(SystemExit, match="__icp_a.json"):
        cli.main(["compare", SAMPLE, "--out", str(tmp_path)])
    base = tmp_path / STEM
    base.mkdir(parents=True)
    (base / "__icp_a.json").write_text(json.dumps({"icp": None, "parse_error": "x"}), encoding="utf-8")
    (base / "__icp_b.json").write_text(json.dumps({"icp": {}, "parse_error": None}), encoding="utf-8")
    assert cli.main(["compare", SAMPLE, "--out", str(tmp_path)]) == 1  # A안 icp 없음


def test_icp_requires_mode(tmp_path):
    with pytest.raises(SystemExit):  # argparse: --mode 필수
        cli.main(["icp", SAMPLE, "--out", str(tmp_path)])


# ---------------------------------------------------------------- pipeline (단계 함수는 가짜로 바꿔 호출 순서만 검증)
def _patch_steps(monkeypatch, calls, fail_at=None):
    def mk(name):
        def fn(args):
            calls.append((name, getattr(args, "mode", None), getattr(args, "dry_run", None)))
            return 1 if name == fail_at else 0
        return fn
    for name in ("run", "ocr", "icp"):
        monkeypatch.setattr(cli, f"cmd_{name}", mk(name))


def test_pipeline_default_runs_a_flow_in_order(tmp_path, monkeypatch, capsys):
    calls = []
    _patch_steps(monkeypatch, calls)
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path)]) == 0
    assert [c[0] for c in calls] == ["run", "ocr", "icp"] and calls[2][1] == "a"
    out = capsys.readouterr().out
    assert "=== 요약 ===" in out and "전체 합계" in out


def test_pipeline_stops_after_failed_step(tmp_path, monkeypatch, capsys):
    calls = []
    _patch_steps(monkeypatch, calls, fail_at="run")
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path)]) == 1
    assert [c[0] for c in calls] == ["run"]
    assert "[중단] run" in capsys.readouterr().err
    calls.clear()
    _patch_steps(monkeypatch, calls, fail_at="ocr")
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path)]) == 1
    assert [c[0] for c in calls] == ["run", "ocr"]


def test_pipeline_mode_both_runs_b_last_and_mode_b_skips_run_ocr(tmp_path, monkeypatch):
    calls = []
    _patch_steps(monkeypatch, calls)
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path), "--mode", "both"]) == 0
    assert [c[0] for c in calls] == ["run", "ocr", "icp", "icp"]
    assert [c[1] for c in calls if c[0] == "icp"] == ["a", "b"]
    calls.clear()
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path), "--mode", "b"]) == 0
    assert [(c[0], c[1]) for c in calls] == [("icp", "b")]


def test_pipeline_dry_run_never_calls_icp(tmp_path, monkeypatch, capsys):
    calls = []
    _patch_steps(monkeypatch, calls)
    assert cli.main(["pipeline", SAMPLE, "--out", str(tmp_path), "--dry-run"]) == 0
    assert [c[0] for c in calls] == ["run"]  # __result.csv 가 없으면 ocr 선택도 건너뜀
    assert all(c[2] for c in calls) and "LLM 을 호출하지 않았습니다" in capsys.readouterr().out


def test_run_log_cost_sums_only_new_ok_calls(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text('{"status":"ok","cost_usd":0.5}\n', encoding="utf-8")
    off = p.stat().st_size
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"status":"ok","cost_usd":0.25}\n{"status":"reused"}\n{"status":"error"}\n')
    assert cli._run_log_cost(p, off) == 0.25
    assert cli._run_log_cost(tmp_path / "none.jsonl", 0) == 0.0
