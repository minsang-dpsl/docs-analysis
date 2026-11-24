"""백엔드별 요청 빌더와 응답 파서.

경고: Decisions API 의 실제 요청/응답 JSON 형식을 확인하지 못한 채 'OpenRouter 일반 형식'으로
추정해서 작성했습니다. 실제 사양이 다르면 이 파일의 ASSUMED 표시 부분만 고치면 됩니다.
나머지 코드(client, cli, aggregate)는 이 파일의 두 함수 계약에만 의존합니다.

  build_decision_request(model, page_b64, filename, questions) -> dict (요청 JSON)
  parse_decision_response(resp_json) -> (probs, usage)
      probs : {question_id: {option_id: probability}}
      usage : {"prompt_tokens": int, "completion_tokens": int}
"""
from __future__ import annotations

from .questions import Question, questions_payload


class ResponseFormatError(Exception):
    pass


# ---------------------------------------------------------------- OpenRouter
def build_decision_request(model: str, page_b64: str, filename: str, questions: list[Question]) -> dict:
    # ASSUMED: 파일은 OpenAI 호환 content part(type=file), 질문은 최상위 "decisions" 필드.
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Answer the decision questions about the attached document page."},
                    {
                        "type": "file",
                        "file": {
                            "filename": filename,
                            "file_data": f"data:application/pdf;base64,{page_b64}",
                        },
                    },
                ],
            }
        ],
        "decisions": {"questions": questions_payload(questions), "return_probabilities": True},  # ASSUMED
    }


def _find_decisions(resp: dict):
    # ASSUMED 위치: 최상위 또는 choices[0].message 아래의 decisions / answers
    for holder in (resp, ((resp.get("choices") or [{}])[0].get("message") or {})):
        for key in ("decisions", "answers"):
            if key in holder and holder[key] is not None:
                return holder[key]
    return None


def parse_decision_response(resp: dict) -> tuple[dict[str, dict[str, float]], dict]:
    raw = _find_decisions(resp)
    if raw is None:
        raise ResponseFormatError("응답에서 decisions/answers 를 찾지 못했습니다")
    if isinstance(raw, dict):  # {"answers":[...]} 또는 {qid: {...}} 형태 허용
        if "answers" in raw:
            raw = raw["answers"]
        elif "decisions" in raw:
            raw = raw["decisions"]
        else:
            raw = [{"question_id": k, "probabilities": v} for k, v in raw.items()]
    probs: dict[str, dict[str, float]] = {}
    for item in raw:
        qid = item.get("question_id") or item.get("id")
        if not qid:
            raise ResponseFormatError("question_id 가 없는 항목이 있습니다")
        if isinstance(item.get("probabilities"), dict):
            opts = {str(k): float(v) for k, v in item["probabilities"].items()}
        elif isinstance(item.get("options"), list):
            opts = {}
            for o in item["options"]:
                p = o.get("probability", o.get("p"))
                if p is None:
                    raise ResponseFormatError(f"{qid}: 옵션 확률이 없습니다")
                opts[str(o["id"])] = float(p)
        else:
            raise ResponseFormatError(f"{qid}: probabilities/options 가 없습니다")
        probs[str(qid)] = opts
    u = resp.get("usage") or {}
    usage = {
        "prompt_tokens": int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0),
        "completion_tokens": int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0),
    }
    return probs, usage


# ---------------------------------------------------------------- 텍스트 생성(ICP 단계)
def build_chat_request(model: str, system: str, user_text: str, pdf_b64: str | None, filename: str = "pages.pdf") -> dict:
    content: list[dict] = [{"type": "text", "text": user_text}]
    if pdf_b64:
        content.append(
            {"type": "file", "file": {"filename": filename, "file_data": f"data:application/pdf;base64,{pdf_b64}"}}
        )
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "temperature": 0.2,
    }


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
    }
    return text or "", usage
