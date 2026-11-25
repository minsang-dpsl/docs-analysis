"""CLI: run / estimate / select / icp"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import aggregate, compare as compare_mod, config, data, ocr as ocr_mod, pdf, questions as Q
from .client import ApiError, DecisionClient
from .icp import MODES, IcpInputError, run_icp
from .select_pages import select_pages


# ---------------------------------------------------------------- 공통
def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def cost_usd(settings: dict, prompt_tokens: int, completion_tokens: int) -> float:
    p = settings.get("pricing", {})
    return prompt_tokens / 1e6 * p.get("input_per_mtok", 0.0) + completion_tokens / 1e6 * p.get("output_per_mtok", 0.0)


class Context:
    def __init__(self, args):
        self.settings = config.load_settings()
        self.prompts = config.load_prompts()
        s = self.settings
        self.naics = data.load_naics(config.data_path(s, "naics_xlsx"))
        data.load_naics_ko(config.data_path(s, "naics_ko_csv"), self.naics)
        self.countries = data.load_countries(config.data_path(s, "countries_csv"))
        problems = data.validate_counts(
            self.naics, self.countries, s["data"]["expected"], getattr(args, "allow_count_mismatch", False)
        )
        problems += data.validate_regions(
            self.countries, [r["id"] for r in self.prompts.get("continent_options", [])],
            getattr(args, "allow_count_mismatch", False),
        )
        for pr in problems:
            print(f"[경고] 개수 불일치 무시: {pr}", file=sys.stderr)
        idx_path = config.data_path(s, "naics_index_xlsx")
        self.index = data.load_index(idx_path) if s["index"].get("enabled", True) and idx_path.exists() else {}
        self.questions = Q.build_questions(self.naics, self.countries, self.prompts, self.index, s["index"])
        Q.validate_questions(self.questions, s["limits"])
        self.qsha = Q.questions_sha(self.questions)
        self.country_names = {c.alpha2: c.name_en for c in self.countries}
        self.continent_names = {r["id"]: r["label"] for r in self.prompts.get("continent_options", [])}


def out_dir_for(ctx: Context, pdf_path: Path, args) -> Path:
    base = Path(args.out) if getattr(args, "out", None) else None
    return config.output_dir(ctx.settings, pdf_path, base)


# ---------------------------------------------------------------- estimate
def estimate_tokens(ctx: Context, n_pages: int) -> tuple[float, float, float, float, float]:
    """-> (질문 토큰, 쪽당 입력, 쪽당 출력, 합계 입력, 합계 출력). 글자 수/chars_per_token 기반 거친 추정."""
    e = ctx.settings["estimate"]
    q_tokens = Q.questions_chars(ctx.questions) / e["chars_per_token"]
    in_per_page = q_tokens + e["pdf_tokens_per_page"]
    out_per_page = e["output_tokens_per_page"]
    return q_tokens, in_per_page, out_per_page, in_per_page * n_pages, out_per_page * n_pages


def estimate_summary(ctx: Context, n_pages: int) -> str:
    *_, total_in, total_out = estimate_tokens(ctx, n_pages)
    p = ctx.settings.get("pricing", {})
    if p.get("input_per_mtok") or p.get("output_per_mtok"):
        cost = f"약 ${cost_usd(ctx.settings, int(total_in), int(total_out)):.4f}"
    else:
        cost = "계산 불가(settings.pricing 단가 미설정)"
    return f"입력 약 {total_in:,.0f} tok, 출력 약 {total_out:,.0f} tok, 비용 {cost}"


def cmd_estimate(args) -> int:
    ctx = Context(args)
    s = ctx.settings
    pdf_path = Path(args.pdf)
    total = pdf.page_count(pdf_path)
    pages = pdf.parse_pages(args.pages, total)
    e = s["estimate"]
    n = len(pages)
    q_tokens, in_per_page, out_per_page, total_in, total_out = estimate_tokens(ctx, n)
    print(f"PDF: {pdf_path.name} ({total}쪽 중 {n}쪽)")
    print(f"질문 {len(ctx.questions)}개, 최대 옵션 {max(len(q.options) for q in ctx.questions)}개, "
          f"index 설명 {'사용' if ctx.index else '미사용'}")
    print(f"쪽당 입력 약 {in_per_page:,.0f} tok (질문 {q_tokens:,.0f} + PDF {e['pdf_tokens_per_page']:,}), "
          f"출력 약 {out_per_page:,} tok")
    print(f"합계 입력 약 {total_in:,.0f} tok, 출력 약 {total_out:,.0f} tok")
    print(f"예상 비용 약 ${cost_usd(s, int(total_in), int(total_out)):.4f} (settings.pricing 단가 기준, 0이면 미설정)")
    return 0


# ---------------------------------------------------------------- run
def cmd_run(args) -> int:
    ctx = Context(args)
    s = ctx.settings
    pdf_path = Path(args.pdf)
    total = pdf.page_count(pdf_path)
    pages = pdf.parse_pages(args.pages, total)
    pdf_sha = pdf.sha256_file(pdf_path)
    out = out_dir_for(ctx, pdf_path, args)
    pages_dir = out / "pages"
    log_path = out / "__run_log.jsonl"
    lock = threading.Lock()

    if args.dry_run:
        from .backends import build_decision_request
        body = build_decision_request(s["openrouter"]["model"], "<base64>", f"page_{pages[0]}.pdf", ctx.questions)
        size = len(json.dumps(body, ensure_ascii=False))
        print(f"[dry-run] {pdf_path.name}: {total}쪽 중 {len(pages)}쪽 대상, 질문 {len(ctx.questions)}개")
        print(f"[dry-run] 요청 본문 약 {size:,} 바이트(PDF 제외), questions_sha={ctx.qsha[:12]}")
        print(f"[dry-run] 예상 ({len(pages)}쪽): {estimate_summary(ctx, len(pages))}")
        print(f"[dry-run] 출력 위치: {out}")
        return 0

    client = DecisionClient(s)
    out.mkdir(parents=True, exist_ok=True)

    def log(entry: dict) -> None:
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with lock, open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def reusable(p: Path) -> bool:
        if not (args.resume and p.exists()):
            return False
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return d.get("pdf_sha256") == pdf_sha and d.get("questions_sha") == ctx.qsha and d.get("status") in ("ok", "blocked")

    totals = {"in": 0, "out": 0, "cost": 0.0, "ok": 0, "billed": False}  # 이번 실행에서 새로 호출한 쪽의 합계

    def work(page: int) -> tuple[int, str]:
        pj = pages_dir / f"{page}.json"
        if reusable(pj):
            log({"page": page, "status": "reused"})
            return page, "reused"
        try:
            b64 = pdf.page_b64(pdf_path, page)
            probs, usage, latency = client.decide(b64, f"page_{page}.pdf", ctx.questions)
            billed = usage.get("cost")  # 응답의 실제 청구액(크레딧). 없으면 settings.pricing 으로 추정
            rec = {
                "page": page, "status": "ok", "pdf_sha256": pdf_sha, "questions_sha": ctx.qsha,
                "probs": probs, "usage": usage,
                "cost_usd": billed if billed is not None else cost_usd(s, usage["prompt_tokens"], usage["completion_tokens"]),
                "latency_s": round(latency, 2),
            }
        except ApiError as e:
            if e.blocked:
                rec = {"page": page, "status": "blocked", "pdf_sha256": pdf_sha, "questions_sha": ctx.qsha,
                       "probs": {}, "error": str(e)}
            else:
                log({"page": page, "status": "error", "error": str(e)})
                return page, "error"
        write_json_atomic(pj, rec)
        log({k: rec.get(k) for k in ("page", "status", "latency_s", "cost_usd", "usage", "error")})
        if rec["status"] == "ok":
            with lock:
                totals["in"] += rec["usage"]["prompt_tokens"]
                totals["out"] += rec["usage"]["completion_tokens"]
                totals["cost"] += rec["cost_usd"]
                totals["ok"] += 1
                totals["billed"] = totals["billed"] or rec["usage"].get("cost") is not None
        return page, rec["status"]

    n_call = sum(1 for p in pages if not reusable(pages_dir / f"{p}.json"))
    print(f"[예상] 호출 {n_call}쪽(재사용 {len(pages) - n_call}쪽): {estimate_summary(ctx, n_call)}")
    workers = args.concurrency or int(s.get("concurrency", 2))
    t0 = time.monotonic()
    statuses: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, p) for p in pages]
        for f in as_completed(futs):
            p, st = f.result()
            statuses[p] = st
            print(f"  쪽 {p}: {st}")

    result = build_result(ctx, pdf_path, pdf_sha, total, pages_dir)
    write_json_atomic(out / "__result.json", result)
    write_result_csv(out / "__result.csv", result["doc"])
    src = "응답 usage.cost 합계" if totals["billed"] else "settings.pricing 추정"
    print(f"[실제] 소요 {time.monotonic() - t0:.1f}s | 토큰 입력 {totals['in']:,} / 출력 {totals['out']:,} "
          f"(성공 {totals['ok']}쪽) | 비용 ${totals['cost']:.4f} ({src})")
    print(f"완료, 결과: {out}/__result.json")
    failed = [p for p, st in statuses.items() if st == "error"]
    if failed:
        print(f"[경고] 실패한 쪽: {sorted(failed)}. --resume 으로 다시 실행하세요.", file=sys.stderr)
        return 1
    return 0


def build_result(ctx: Context, pdf_path: Path, pdf_sha: str, total: int, pages_dir: Path) -> dict:
    summaries: dict[int, dict] = {}
    blocked: list[int] = []
    for pj in sorted(pages_dir.glob("*.json")):
        try:
            d = json.loads(pj.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("pdf_sha256") != pdf_sha or d.get("questions_sha") != ctx.qsha:
            continue
        if d.get("status") == "blocked":
            blocked.append(int(d["page"]))
        elif d.get("status") == "ok":
            summaries[int(d["page"])] = aggregate.summarize_page(
                d["probs"], ctx.naics.items5, ctx.country_names, ctx.continent_names
            )
    doc = aggregate.aggregate_document(summaries, ctx.settings["thresholds"])
    aggregate.attach_region_members(doc, ctx.countries)
    return {
        "pdf": pdf_path.name, "pdf_sha256": pdf_sha, "questions_sha": ctx.qsha, "total_pages": total,
        "pages_done": sorted(summaries), "pages_blocked": sorted(blocked),
        "thresholds": ctx.settings["thresholds"], "doc": doc,
    }


def write_result_csv(path: Path, doc: dict) -> None:
    rows = aggregate.result_rows(doc)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # BOM: 엑셀에서 한글이 깨지지 않게
        w = csv.DictWriter(f, fieldnames=["kind", "code", "name", "score", "evidence_page"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- select / icp
def _load_result(args, ctx_settings: dict, pdf_path: Path) -> tuple[dict, Path]:
    out = config.output_dir(ctx_settings, pdf_path, Path(args.out) if getattr(args, "out", None) else None)
    rp = Path(args.result) if getattr(args, "result", None) else out / "__result.json"
    if not rp.exists():
        raise SystemExit(f"결과 파일이 없습니다: {rp} (먼저 run 을 실행하세요)")
    return json.loads(rp.read_text(encoding="utf-8")), out


def cmd_select(args) -> int:
    settings = config.load_settings()
    pdf_path = Path(args.pdf)
    result, out = _load_result(args, settings, pdf_path)
    sel = select_pages(result["doc"], int(result["total_pages"]), settings)
    print(f"선택된 쪽: {sel['pages']} (전체 {result['total_pages']}쪽)")
    for p, rs in sel["reasons"].items():
        print(f"  {p}쪽: {', '.join(rs)}")
    if sel["dropped"]:
        print(f"  상한으로 제외: {sel['dropped']}")
    write_json_atomic(out / "__pages_selected.json", {**sel, "reasons": {str(k): v for k, v in sel["reasons"].items()},
                                                       "stats": {str(k): v for k, v in sel["stats"].items()}})
    return 0


def cmd_icp(args) -> int:
    """A안(OCR 텍스트 + NAICS 후보) 또는 B안(PDF 통째로)으로 ICP 생성. --mode both 는 둘 다."""
    settings = config.load_settings()
    prompts = config.load_prompts()
    pdf_path = Path(args.pdf)
    out = config.output_dir(settings, pdf_path, Path(args.out) if getattr(args, "out", None) else None)
    client = DecisionClient(settings)
    rc = 0
    for mode in (list(MODES) if args.mode == "both" else [args.mode]):
        label = "A안" if mode == "a" else "B안"
        rec = run_icp(pdf_path, mode, settings, prompts, client, out,
                      result_csv=Path(args.result) if getattr(args, "result", None) else None,
                      ocr_path=Path(args.ocr) if getattr(args, "ocr", None) else None)
        i = rec["input"]
        if mode == "a":
            pages = ", ".join(str(p) for p in i["ocr_pages"])
            print(f"[입력] {label}: NAICS 후보 {i['naics_candidates']}개, OCR 텍스트 {i['ocr_chars']:,}자 (쪽 {pages})")
        else:
            print(f"[입력] {label}: PDF 통째로 ({i['total_pages']}쪽, {i['pdf_bytes']:,} 바이트)")
        u, cost = rec["usage"], rec["cost_usd"]
        cost_txt = "계산 불가" if cost is None else f"${cost:.4f} ({rec['cost_source']})"
        print(f"[실제] {label} 소요 {rec['latency_s']}s | 토큰 입력 {u['prompt_tokens']:,} / 출력 {u['completion_tokens']:,} | 비용 {cost_txt}")
        if rec["parse_error"]:
            print(f"[경고] {label} JSON 파싱 실패: {rec['parse_error']} (__icp_{mode}.json 의 raw_text 확인)", file=sys.stderr)
            rc = 1
            continue
        for w in rec["schema_warnings"]:
            print(f"[경고] {label} {w}", file=sys.stderr)
        print(f"  항목 수: " + ", ".join(f"{f} {len(rec['icp'].get(f) or [])}" for f in compare_mod.FIELDS))
        print(f"결과: {out}/__icp_{mode}.json")
    return rc


def cmd_compare(args) -> int:
    """__icp_a.json 과 __icp_b.json 을 비교해 __compare.json, __compare.md 를 만듭니다."""
    settings = config.load_settings()
    pdf_path = Path(args.pdf)
    out = config.output_dir(settings, pdf_path, Path(args.out) if getattr(args, "out", None) else None)
    recs = {}
    for m in MODES:
        p = out / f"__icp_{m}.json"
        if not p.exists():
            raise SystemExit(f"{p.name} 이(가) 없습니다: {p} (먼저 python -m app icp <pdf> --mode {m} 를 실행하세요)")
        recs[m] = json.loads(p.read_text(encoding="utf-8"))
    try:
        cmp = compare_mod.compare(recs["a"], recs["b"], args.threshold)
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    write_json_atomic(out / "__compare.json", cmp)
    (out / "__compare.md").write_text(compare_mod.render_markdown(cmp), encoding="utf-8")
    print(f"{'필드':<13}{'A':>4}{'B':>4}{'일치':>5}{'유사':>5}{'A만':>5}{'B만':>5}{'겹침률':>7}")
    for f, v in cmp["fields"].items():
        print(f"{f:<13}{v['n_a']:>4}{v['n_b']:>4}{v['exact']:>5}{v['similar']:>5}{len(v['only_a']):>5}{len(v['only_b']):>5}{v['overlap']:>7.0%}")
    s = cmp["summary"]
    print(f"전체: A {s['items_a']}개, B {s['items_b']}개, 짝지어진 항목 {s['matched']}개, 겹침률 {s['overlap']:.0%}")
    print(f"결과: {out}/__compare.md (나란히 보기), {out}/__compare.json")
    return 0


# ---------------------------------------------------------------- ocr
def cmd_ocr(args) -> int:
    """결과 CSV 의 NAICS 코드가 많이 몰린 쪽 상위 N개만 Document AI 로 텍스트 추출."""
    settings = config.load_settings()
    cfg = settings["ocr"]
    pdf_path = Path(args.pdf)
    out = config.output_dir(settings, pdf_path, Path(args.out) if getattr(args, "out", None) else None)
    csv_path = Path(args.result) if getattr(args, "result", None) else out / "__result.csv"
    if not csv_path.exists():
        raise SystemExit(f"결과 CSV 가 없습니다: {csv_path} (먼저 run 을 실행하세요)")
    total = pdf.page_count(pdf_path)
    top_n = args.top or int(cfg["top_pages"])
    sel = ocr_mod.select_ocr_pages(ocr_mod.read_naics_rows(csv_path), top_n)
    pages, stats = sel["pages"], sel["stats"]
    if not pages:
        print("[오류] 결과 CSV 에 NAICS 행이 없어 선택할 쪽이 없습니다.", file=sys.stderr)
        return 1
    if max(pages) > total:
        raise SystemExit(f"evidence_page {max(pages)} 가 PDF 쪽수({total})를 넘습니다. 다른 PDF 의 결과 CSV 가 아닌지 확인하세요.")

    print(f"[선택] NAICS 코드가 많이 몰린 쪽 상위 {top_n} (NAICS가 나온 {len(stats)}쪽 중 {len(pages)}쪽 사용)")
    for rank, p in enumerate(pages, 1):
        print(f"  {rank}위 쪽 {p}: 코드 {stats[p]['n']}개, score 합 {stats[p]['sum']:.4f}")
    price = float(cfg["price_per_1000_pages"])
    print(f"[예상] Document AI {len(pages)}쪽: 비용 약 ${len(pages) / 1000 * price:.4f} (단가 ${price}/1,000쪽, 미확정)")
    if args.dry_run:
        print("[dry-run] Document AI 를 호출하지 않았습니다.")
        return 0

    process = ocr_mod.make_process()
    t0 = time.monotonic()
    texts, n_requests = ocr_mod.extract_text(
        pdf_path, pages, process, int(cfg["max_pages_per_request"]), int(cfg["max_bytes_per_request"])
    )
    elapsed = time.monotonic() - t0
    chars = {p: len(texts[p]) for p in pages}
    rec = {
        "pdf": pdf_path.name, "pdf_sha256": pdf.sha256_file(pdf_path), "source_csv": csv_path.name, "top_n": top_n,
        "selected": [{"rank": i, "page": p, "naics_count": stats[p]["n"], "score_sum": stats[p]["sum"],
                      "naics_codes": stats[p]["codes"]} for i, p in enumerate(pages, 1)],
        "ocr": {"requests": n_requests, "pages_sent": len(pages), "chars": sum(chars.values()),
                "latency_s": round(elapsed, 2), "cost_usd_est": round(len(pages) / 1000 * price, 6)},  # 단가 미확정 추정치
        "pages": {str(p): texts[p] for p in sorted(pages)},
    }
    write_json_atomic(out / "__ocr.json", rec)
    (out / "__ocr.txt").write_text(ocr_mod.combine_pages_text(texts), encoding="utf-8")  # 쪽 구분선이 있는 txt 하나
    print(f"[실제] 소요 {elapsed:.1f}s | 요청 {n_requests}회 | {len(pages)}쪽, 글자 {sum(chars.values()):,}자 "
          f"(쪽별 {', '.join(f'{p}쪽 {chars[p]:,}' for p in sorted(pages))})")
    empty = [p for p in pages if not texts[p].strip()]
    if empty:
        print(f"[경고] 텍스트가 비어 있는 쪽: {empty}", file=sys.stderr)
    print(f"완료, 결과: {out}/__ocr.txt (메타데이터: {out}/__ocr.json)")
    return 0


# ---------------------------------------------------------------- pipeline
def _run_log_cost(log_path: Path, offset: int) -> float:
    """offset 이후에 추가된 __run_log.jsonl 의 ok 호출 비용 합계(재사용 쪽은 0)."""
    if not log_path.exists():
        return 0.0
    with open(log_path, "rb") as f:
        f.seek(offset)
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    total = 0.0
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("status") == "ok" and d.get("cost_usd") is not None:
            total += float(d["cost_usd"])
    return total


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_pipeline(args) -> int:
    """run -> ocr -> icp 를 한 번에 실행합니다. 앞 단계가 실패하면 즉시 중단(뒤 단계 비용 방지)."""
    settings = config.load_settings()
    pdf_path = Path(args.pdf)
    out = config.output_dir(settings, pdf_path, Path(args.out) if getattr(args, "out", None) else None)
    base = dict(vars(args))
    base.setdefault("pages", None)
    modes = list(MODES) if args.mode == "both" else [args.mode]
    steps: list[tuple[str, float, float | None, str]] = []  # (이름, 소요 s, 비용 USD, 비용 표기)

    def fail(name: str, rc: int) -> int:
        print(f"[중단] {name} 단계가 실패했습니다(코드 {rc}). 뒤 단계는 실행하지 않았습니다.", file=sys.stderr)
        return rc if rc else 1

    if "a" in modes:
        run_args = argparse.Namespace(**{**base, "pages": getattr(args, "pages", None), "concurrency": None})
        ocr_args = argparse.Namespace(**{**base, "result": None, "top": args.top})
        if args.dry_run:
            print("=== [1/3] run (dry-run) ===")
            rc = cmd_run(run_args)
            if rc:
                return fail("run", rc)
            print("=== [2/3] ocr (dry-run) ===")
            if (out / "__result.csv").exists():
                rc = cmd_ocr(ocr_args)
                if rc:
                    return fail("ocr", rc)
            else:
                top = args.top or int(settings["ocr"]["top_pages"])
                price = float(settings["ocr"]["price_per_1000_pages"])
                print(f"[dry-run] __result.csv 가 아직 없어 선택은 못 합니다. 최대 {top}쪽, "
                      f"비용 약 ${top / 1000 * price:.4f} 이하(단가 ${price}/1,000쪽, 미확정)")
            print("=== [3/3] icp ===")
            print("[dry-run] LLM 을 호출하지 않았습니다. 모델/입력은 icp 실행 시 출력됩니다.")
            return 0

        log_path = out / "__run_log.jsonl"
        offset = log_path.stat().st_size if log_path.exists() else 0
        print("=== [1/3] run: 쪽별 NAICS·국가 판별 ===")
        t0 = time.monotonic()
        rc = cmd_run(run_args)
        run_t = time.monotonic() - t0
        steps.append(("run", run_t, _run_log_cost(log_path, offset), "새 호출분 usage.cost 합계"))
        if rc:
            print("[안내] 실패한 쪽은 --resume 을 붙여 다시 실행하면 성공한 쪽은 재사용합니다.", file=sys.stderr)
            return fail("run", rc)
        blocked = _read_json(out / "__result.json").get("pages_blocked") or []
        if blocked:
            print(f"[경고] 안전 필터로 차단된 쪽: {blocked} (결과에서 제외됨, 계속 진행)", file=sys.stderr)

        print("=== [2/3] ocr: 상위 쪽 텍스트 추출 ===")
        t0 = time.monotonic()
        rc = cmd_ocr(ocr_args)
        ocr_t = time.monotonic() - t0
        ocr_cost = (_read_json(out / "__ocr.json").get("ocr") or {}).get("cost_usd_est")
        steps.append(("ocr", ocr_t, ocr_cost, "추정(단가 미확정)"))
        if rc:
            return fail("ocr", rc)

    for mode in modes:
        label = "A안" if mode == "a" else "B안"
        print(f"=== [{'3/3' if mode == 'a' else 'B'}] icp: {label} ===")
        icp_args = argparse.Namespace(**{**base, "mode": mode, "result": None, "ocr": None})
        t0 = time.monotonic()
        rc = cmd_icp(icp_args)
        el = time.monotonic() - t0
        rec = _read_json(out / f"__icp_{mode}.json")
        steps.append((f"icp {label}", el, rec.get("cost_usd"), rec.get("cost_source") or "계산 불가"))
        if rc:
            return fail(f"icp {label}", rc)

    print("=== 요약 ===")
    print(f"{'단계':<10}{'소요(s)':>9}{'비용(USD)':>12}  비고")
    total_t, total_c, unknown = 0.0, 0.0, False
    for name, t, c, note in steps:
        total_t += t
        if c is None:
            unknown = True
        else:
            total_c += c
        print(f"{name:<10}{t:>9.1f}{('계산불가' if c is None else f'${c:.4f}'):>12}  {note}")
    a_steps = [s for s in steps if s[0] in ("run", "ocr") or s[0] == "icp A안"]
    if "a" in modes and len(modes) > 1:
        ta = sum(s[1] for s in a_steps)
        ca = sum(s[2] or 0.0 for s in a_steps)
        print(f"{'A안 합계':<10}{ta:>9.1f}{f'${ca:.4f}':>12}  run + ocr + icp A안")
    print(f"{'전체 합계':<10}{total_t:>9.1f}{f'${total_c:.4f}':>12}  {'일부 비용 계산 불가로 과소 합산' if unknown else ''}")
    print(f"결과 폴더: {out}")
    return 0


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app", description="영업자료 PDF NAICS/국가 추출 및 ICP 생성")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, pages=False):
        p.add_argument("pdf")
        p.add_argument("--out", help="출력 기준 디렉터리(기본 output/)")
        p.add_argument("--allow-count-mismatch", action="store_true", help="NAICS/국가 개수 불일치를 경고로 낮춤")
        if pages:
            p.add_argument("--pages", help="예: 1,3-5 (기본 전체)")

    r = sub.add_parser("run", help="쪽별 의사결정 호출 후 문서 집계")
    common(r, pages=True)
    r.add_argument("--resume", action="store_true", help="pdf/질문 sha 가 같은 기존 쪽 결과를 재사용")
    r.add_argument("--dry-run", action="store_true", help="API 호출 없이 요청 규모만 출력")
    r.add_argument("--concurrency", type=int)
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("estimate", help="API 호출 없이 토큰과 비용 추정")
    common(e, pages=True)
    e.set_defaults(fn=cmd_estimate)

    s = sub.add_parser("select", help="집계 결과로 ICP 입력 페이지 선택")
    common(s)
    s.add_argument("--result", help="__result.json 경로(기본: 출력 폴더)")
    s.set_defaults(fn=cmd_select)

    i = sub.add_parser("icp", help="ICP 생성: a=OCR 텍스트+NAICS 후보(run, ocr 먼저), b=PDF 통째로, both=둘 다")
    common(i)
    i.add_argument("--mode", required=True, choices=[*MODES, "both"])
    i.add_argument("--result", help="A안: __result.csv 경로(기본: 출력 폴더)")
    i.add_argument("--ocr", help="A안: __ocr.txt 경로(기본: 출력 폴더)")
    i.set_defaults(fn=cmd_icp)

    c = sub.add_parser("compare", help="A안(__icp_a.json)과 B안(__icp_b.json) ICP 비교")
    c.add_argument("pdf")
    c.add_argument("--out", help="출력 기준 디렉터리(기본 output/)")
    c.add_argument("--threshold", type=float, default=0.6, help="유사 항목 판정 기준(토큰 Jaccard, 기본 0.6)")
    c.set_defaults(fn=cmd_compare)

    o = sub.add_parser("ocr", help="결과 CSV 의 NAICS 가 많이 몰린 쪽 상위 N개만 Document AI 로 텍스트 추출")
    common(o)
    o.add_argument("--result", help="__result.csv 경로(기본: 출력 폴더)")
    o.add_argument("--top", type=int, help="사용할 쪽 수(기본 settings.ocr.top_pages)")
    o.add_argument("--dry-run", action="store_true", help="Document AI 호출 없이 선택된 쪽과 예상 비용만 출력")
    o.set_defaults(fn=cmd_ocr)

    pl = sub.add_parser("pipeline", help="run -> ocr -> icp 를 한 번에(기본 A안, --mode both 면 B안도 실행)")
    common(pl, pages=True)
    pl.add_argument("--mode", choices=[*MODES, "both"], default="a", help="a(기본)=run+ocr+icp A안, b=PDF 통째로만, both=A안 후 B안")
    pl.add_argument("--resume", action="store_true", help="run 단계에서 기존 쪽 결과 재사용")
    pl.add_argument("--dry-run", action="store_true", help="API 호출 없이 run/ocr 의 예상만 출력")
    pl.add_argument("--top", type=int, help="OCR 에 사용할 쪽 수(기본 settings.ocr.top_pages)")
    pl.set_defaults(fn=cmd_pipeline)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (data.CountMismatch, Q.QuestionError, ocr_mod.OcrError, IcpInputError, RuntimeError) as e:  # RuntimeError: 환경 변수/키 누락
        print(f"[오류] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
