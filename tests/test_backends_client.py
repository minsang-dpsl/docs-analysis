import pytest
import requests

from app import backends, client, questions as Q

QS = [Q.Question("naics_1125", "t", [Q.Option("11251", "Aquaculture"), Q.Option("none", "none")])]


def test_request_has_questions_and_file():
    body = backends.build_decision_request("m", "QUJD", "p1.pdf", QS)
    assert set(body) == {"model", "state", "questions"} and body["model"] == "m"
    part = body["state"][0]
    assert part["type"] == "file" and part["file"]["file_data"] == "data:application/pdf;base64,QUJD"
    assert body["questions"] == {
        "naics_1125": {"type": "choice", "instructions": "t", "criteria": {"11251": "Aquaculture", "none": "none"}}
    }


def test_parse_choice_answers_and_usage():
    resp = {
        "answers": {"naics_1125": {"type": "choice", "choice": "11251", "confidence": 0.7,
                                   "probabilities": {"11251": 0.7, "none": 0.3}}},
        "usage": {"input_tokens": 5, "output_tokens": 2, "cost": 0.0001},
    }
    probs, usage = backends.parse_decision_response(resp)
    assert probs == {"naics_1125": {"11251": 0.7, "none": 0.3}}
    assert usage == {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.0001, "refused": []}


def test_parse_refusal_is_reported_not_fatal():
    resp = {"answers": {"a": {"type": "refusal"},
                        "b": {"type": "choice", "choice": "x", "probabilities": {"x": 1.0}}},
            "usage": {"input_tokens": 1, "output_tokens": 0}}
    probs, usage = backends.parse_decision_response(resp)
    assert probs == {"b": {"x": 1.0}} and usage["refused"] == ["a"] and usage["cost"] is None


def test_parse_errors():
    with pytest.raises(backends.ResponseFormatError):
        backends.parse_decision_response({"choices": []})
    with pytest.raises(backends.ResponseFormatError):  # 이 파이프라인은 choice 질문만 사용
        backends.parse_decision_response({"answers": {"q": {"type": "noul", "noul": 0.9}}})
    with pytest.raises(backends.ResponseFormatError):
        backends.parse_decision_response({"answers": {"q": {"type": "choice", "choice": "x"}}})


def test_sanitize_hides_base64_and_truncates():
    s = client.sanitize("err " + "A" * 500 + " tail")
    assert "AAAA" not in s and "[redacted]" in s
    assert len(client.sanitize("x" * 1000)) <= 303


def test_classify():
    assert client.classify_http_error(429, "").retryable
    assert client.classify_http_error(503, "").retryable
    assert client.classify_http_error(408, "").retryable
    assert client.classify_http_error(409, "").retryable
    b = client.classify_http_error(400, "content blocked by SAFETY filter")
    assert b.blocked and not b.retryable
    assert not client.classify_http_error(400, "bad field").retryable
    assert not client.classify_http_error(401, "").retryable


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    def post(self, *a, **k):
        self.calls += 1
        self.urls = getattr(self, "urls", []) + [a[0] if a else k.get("url")]
        r = self.seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


SETTINGS = {"backend": "openrouter", "openrouter": {"base_url": "http://x", "decisions_url": "http://d", "model": "m"},
            "retry": {"max_attempts": 4, "base_delay_s": 0.01, "max_delay_s": 0.02}, "timeout_s": 5,
            "icp": {"backend": "openrouter", "model": "m"}}
OK = Resp(200, {"answers": {"naics_1125": {"type": "choice", "choice": "11251", "probabilities": {"11251": 0.5, "none": 0.5}}},
                "usage": {"input_tokens": 1, "output_tokens": 1}})


def mk(seq, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sleeps = []
    sess = FakeSession(seq)
    return client.DecisionClient(SETTINGS, session=sess, sleep=sleeps.append), sess, sleeps


def test_retries_then_succeeds(monkeypatch):
    c, sess, sleeps = mk([Resp(429, text="slow"), requests.ConnectionError("boom"), Resp(503), OK], monkeypatch)
    probs, usage, _ = c.decide("QUJD", "p.pdf", QS)
    assert sess.calls == 4 and len(sleeps) == 3 and probs["naics_1125"]["11251"] == 0.5


def test_decide_posts_to_decisions_url(monkeypatch):
    c, sess, _ = mk([OK], monkeypatch)
    c.decide("QUJD", "p.pdf", QS)
    assert sess.urls == ["http://d"]


def test_all_refused_is_blocked(monkeypatch):
    all_refused = Resp(200, {"answers": {"naics_1125": {"type": "refusal"}}, "usage": {"input_tokens": 1, "output_tokens": 0}})
    c, sess, _ = mk([all_refused], monkeypatch)
    with pytest.raises(client.ApiError) as ei:
        c.decide("QUJD", "p.pdf", QS)
    assert ei.value.blocked and sess.calls == 1


def test_gives_up_after_max_attempts(monkeypatch):
    c, sess, _ = mk([Resp(500)] * 4, monkeypatch)
    with pytest.raises(client.ApiError):
        c.decide("QUJD", "p.pdf", QS)
    assert sess.calls == 4


def test_no_retry_for_blocked_or_client_error(monkeypatch):
    c, sess, _ = mk([Resp(400, text="blocked: SAFETY")], monkeypatch)
    with pytest.raises(client.ApiError) as ei:
        c.decide("QUJD", "p.pdf", QS)
    assert ei.value.blocked and sess.calls == 1
    c, sess, _ = mk([Resp(401, text="no")], monkeypatch)
    with pytest.raises(client.ApiError):
        c.decide("QUJD", "p.pdf", QS)
    assert sess.calls == 1


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    c = client.DecisionClient(SETTINGS, session=FakeSession([OK]), sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        c.decide("QUJD", "p.pdf", QS)


def test_google_decide_not_implemented():
    c = client.DecisionClient(dict(SETTINGS, backend="google"))
    with pytest.raises(NotImplementedError):
        c.decide("QUJD", "p.pdf", QS)


def test_chat_request_pins_pdf_engine_only_when_pdf_attached():
    with_pdf = backends.build_chat_request("m", "sys", "user", "QUJD", pdf_engine="native")
    assert with_pdf["plugins"] == [{"id": "file-parser", "pdf": {"engine": "native"}}]
    assert with_pdf["messages"][1]["content"][1]["file"]["file_data"].endswith("QUJD")
    assert "plugins" not in backends.build_chat_request("m", "sys", "user", None, pdf_engine="native")  # A안(PDF 없음)
    assert "plugins" not in backends.build_chat_request("m", "sys", "user", "QUJD")  # 엔진 미지정이면 OpenRouter 기본


def test_chat_response_keeps_cost_when_provided():
    resp = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.0004}}
    assert backends.parse_chat_response(resp) == ("hi", {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.0004})
    assert backends.parse_chat_response({"choices": [{"message": {"content": "hi"}}]})[1]["cost"] is None


def test_generate_sends_pinned_engine_from_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sent = {}

    class Sess:
        def post(self, url, json=None, **k):
            sent.update(url=url, body=json)
            return Resp(200, {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    settings = dict(SETTINGS, icp={"backend": "openrouter", "model": "m", "pdf_engine": "native"})
    c = client.DecisionClient(settings, session=Sess(), sleep=lambda s: None)
    c.generate("sys", "user", "QUJD")
    assert sent["url"] == "http://x" and sent["body"]["plugins"][0]["pdf"]["engine"] == "native"
    c.generate("sys", "user", None)
    assert "plugins" not in sent["body"]
