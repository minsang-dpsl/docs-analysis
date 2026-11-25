"""백엔드별 요청 빌더와 응답 파서.

Decisions 호출은 OpenRouter 공개 문서(POST /api/alpha/decisions, alpha 단계라 형식이 바뀔 수 있음)를
따릅니다. 실호출로는 아직 검증하지 못했습니다. 사양이 바뀌면 이 파일만 고치면 됩니다.
나머지 코드(client, cli, aggregate)는 이 파일의 두 함수 계약에만 의존합니다.

  build_decision_request(model, page_b64, filename, questions) -> dict (요청 JSON)
  parse_decision_response(resp_json) -> (probs, usage)
      probs : {question_id: {option_id: probability}}
      usage : {"prompt_tokens": int, "completion_tokens": int, "cost": float | None, "refused": [question_id...]}
              cost 는 OpenRouter 가 청구한 크레딧, refused 는 모델이 답변을 거절한 질문 ID
"""
from __future__ import annotations

from .questions import Question, questions_payload


class ResponseFormatError(Exception):
    pass


# ---------------------------------------------------------------- Decisions (OpenRouter alpha)
def build_decision_request(model: str, page_b64: str, filename: str, questions: list[Question]) -> dict:
    """state 에 1쪽 PDF 를 file 파트(base64 데이터 URL)로 넣고, questions 는 {질문ID: 질문 객체}."""
    return {
        "model": model,
        "state": [
            {
                "type": "file",
                "file": {"file_data": f"data:application/pdf;base64,{page_b64}", "filename": filename},
            }
        ],
        "questions": questions_payload(questions),
    }


def parse_decision_response(resp: dict) -> tuple[dict[str, dict[str, float]], dict]:
    answers = resp.get("answers") if isinstance(resp, dict) else None
    if not isinstance(answers, dict):
        raise ResponseFormatError("응답에서 answers 객체를 찾지 못했습니다")
    probs: dict[str, dict[str, float]] = {}
    refused: list[str] = []
    for qid, ans in answers.items():
        kind = (ans or {}).get("type")
        if kind == "refusal":
            refused.append(str(qid))
            continue
        if kind != "choice":
            raise ResponseFormatError(f"{qid}: 예상하지 못한 답변 타입 {kind!r}")
        p = ans.get("probabilities")
        if not isinstance(p, dict) or not p:
            raise ResponseFormatError(f"{qid}: probabilities 가 없습니다")
        probs[str(qid)] = {str(k): float(v) for k, v in p.items()}
    u = resp.get("usage") or {}
    usage = {
        "prompt_tokens": int(u.get("input_tokens", 0) or 0),
        "completion_tokens": int(u.get("output_tokens", 0) or 0),
        "cost": float(u["cost"]) if u.get("cost") is not None else None,
        "refused": refused,
    }
    return probs, usage


# ---------------------------------------------------------------- 텍스트 생성(ICP 단계)
def build_chat_request(
    model: str, system: str, user_text: str, pdf_b64: str | None, filename: str = "pages.pdf",
    pdf_engine: str | None = None,
) -> dict:
    """pdf_engine 을 주면 OpenRouter 의 PDF 처리 엔진을 고정합니다(file-parser 플러그인).

    "native" 는 모델이 PDF 를 직접 읽습니다(입력 토큰으로 과금). 지정하지 않으면 모델이 네이티브를 지원하지
    않을 때 OpenRouter 가 조용히 mistral-ocr($2/1,000쪽)로 바꾸므로, B안처럼 "모델이 PDF 를 읽는다"를
    보장해야 할 때는 "native" 로 고정하세요(지원하지 않는 모델이면 400 으로 실패).
    """
    content: list[dict] = [{"type": "text", "text": user_text}]
    if pdf_b64:
        content.append(
            {"type": "file", "file": {"filename": filename, "file_data": f"data:application/pdf;base64,{pdf_b64}"}}
        )
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "temperature": 0.2,
    }
    if pdf_b64 and pdf_engine:
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": pdf_engine}}]
    return body


def parse_chat_response(resp: dict) -> tuple[str, dict]:
    try:
        text = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ResponseFormatError("choices[0].message.content 가 없습니다") from e
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    u = resp.get("usage") or {}
    usage = {
        "prompt_tokens": int(u.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(u.get("completion_tokens", 0) or 0),
        "cost": float(u["cost"]) if u.get("cost") is not None else None,  # OpenRouter 가 청구액을 주는 경우
    }
    return text or "", usage
