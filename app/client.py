"""DecisionClient: 두 백엔드를 같은 인터페이스로 감쌉니다.

- 재시도 대상: 429/408/409/5xx 및 네트워크 오류(지수 백오프 + 지터)
- 안전 필터 계열 400 은 blocked 로 분류(재시도 안 함)
- 에러 메시지에서 PDF base64 등 긴 문자열은 제거하고 길이를 제한
"""
from __future__ import annotations

import random
import re
import time
from typing import Callable

import requests

from . import backends
from .config import api_key
from .questions import Question

RETRY_STATUS = {408, 409, 429}
_BLOCK_RE = re.compile(r"safety|blocked|prohibited|policy|harm", re.I)
_LONG_RE = re.compile(r"[A-Za-z0-9+/=_\-]{80,}")


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False, blocked: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.blocked = blocked


def sanitize(text: str, limit: int = 300) -> str:
    """에러 본문에 섞일 수 있는 PDF 내용(긴 base64)을 제거하고 길이를 제한."""
    text = _LONG_RE.sub("[redacted]", text or "")
    return text[:limit] + ("..." if len(text) > limit else "")


def classify_http_error(status: int, body: str) -> ApiError:
    msg = f"HTTP {status}: {sanitize(body)}"
    if status == 400 and _BLOCK_RE.search(body or ""):
        return ApiError(msg, status, retryable=False, blocked=True)
    retryable = status in RETRY_STATUS or 500 <= status < 600
    return ApiError(msg, status, retryable=retryable)


class DecisionClient:
    def __init__(self, settings: dict, session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self.backend = settings.get("backend", "openrouter")
        self.session = session or requests.Session()
        self.sleep = sleep
        r = settings.get("retry", {})
        self.max_attempts = int(r.get("max_attempts", 5))
        self.base_delay = float(r.get("base_delay_s", 2.0))
        self.max_delay = float(r.get("max_delay_s", 60.0))
        self.timeout = float(settings.get("timeout_s", 180))

    # ------------------------------------------------------------ 공통
    def _post_openrouter(self, body: dict, url: str | None = None) -> dict:
        url = url or self.s["openrouter"]["base_url"]  # 기본은 chat/completions(ICP 단계)
        headers = {"Authorization": f"Bearer {api_key('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}

        def call() -> dict:
            try:
                r = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                raise ApiError(f"network error: {sanitize(str(e))}", None, retryable=True) from e
            if r.status_code >= 400:
                raise classify_http_error(r.status_code, r.text)
            try:
                return r.json()
            except ValueError as e:
                raise ApiError("응답이 JSON 이 아닙니다", r.status_code, retryable=True) from e

        return self._retry(call)

    def _retry(self, fn: Callable[[], dict]) -> dict:
        last: ApiError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except ApiError as e:
                last = e
                if not e.retryable or attempt == self.max_attempts:
                    raise
                delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
                self.sleep(delay + random.uniform(0, delay * 0.25))
        raise last  # pragma: no cover

    # ------------------------------------------------------------ Decisions
    def decide(self, page_b64: str, filename: str, questions: list[Question]):
        """한 쪽에 대해 모든 질문의 옵션 확률을 받습니다. -> (probs, usage, latency_s)"""
        if self.backend == "google":
            raise NotImplementedError(
                "backend=google 의 decisions 호출은 아직 구현되지 않았습니다. "
                "Decisions API 사양을 확인한 뒤 app/backends.py 와 이 메서드에 추가하세요."
            )
        body = backends.build_decision_request(self.s["openrouter"]["model"], page_b64, filename, questions)
        t0 = time.monotonic()
        resp = self._post_openrouter(body, self.s["openrouter"]["decisions_url"])
        latency = time.monotonic() - t0
        try:
            probs, usage = backends.parse_decision_response(resp)
        except backends.ResponseFormatError as e:
            raise ApiError(f"응답 형식 오류: {e}", None, retryable=False) from e
        if not probs and usage["refused"]:  # 모든 질문을 거절 -> 안전 필터와 같은 blocked 로 취급
            raise ApiError("모든 질문에 대해 모델이 답변을 거절했습니다", None, retryable=False, blocked=True)
        return probs, usage, latency

    # ------------------------------------------------------------ 텍스트 생성
    def generate(self, system: str, user_text: str, pdf_b64: str | None = None, model: str | None = None,
                 backend: str | None = None):
        """ICP 단계용 텍스트 생성. -> (text, usage, latency_s)"""
        backend = backend or self.s.get("icp", {}).get("backend", "openrouter")
        model = model or self.s["icp"]["model"]
        t0 = time.monotonic()
        if backend == "openrouter":
            resp = self._post_openrouter(backends.build_chat_request(model, system, user_text, pdf_b64))
            text, usage = backends.parse_chat_response(resp)
        elif backend == "google":
            text, usage = self._generate_google(model, system, user_text, pdf_b64)
        else:
            raise ValueError(f"알 수 없는 backend: {backend}")
        return text, usage, time.monotonic() - t0

    def _generate_google(self, model: str, system: str, user_text: str, pdf_b64: str | None):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("google-genai 가 필요합니다: pip install google-genai") from e
        import base64

        client = genai.Client(api_key=api_key("GOOGLE_API_KEY"))
        parts = [types.Part.from_text(text=user_text)]
        if pdf_b64:
            parts.append(types.Part.from_bytes(data=base64.b64decode(pdf_b64), mime_type="application/pdf"))
        resp = client.models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.2),
        )
        um = getattr(resp, "usage_metadata", None)
        usage = {
            "prompt_tokens": int(getattr(um, "prompt_token_count", 0) or 0),
            "completion_tokens": int(getattr(um, "candidates_token_count", 0) or 0),
        }
        return resp.text or "", usage
