import pytest
import requests

from app import backends, client, questions as Q

QS = [Q.Question("naics_1125", "t", [Q.Option("11251", "Aquaculture"), Q.Option("none", "none")])]


def test_request_has_questions_and_file():
    body = backends.build_decision_request("m", "QUJD", "p1.pdf", QS)
    assert body["decisions"]["questions"][0]["id"] == "naics_1125"
    assert body["messages"][0]["content"][1]["file"]["file_data"].endswith("QUJD")


@pytest.mark.parametrize("resp", [
    {"decisions": [{"question_id": "naics_1125", "probabilities": {"11251": 0.7, "none": 0.3}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    {"choices": [{"message": {"answers": [{"id": "naics_1125", "options": [{"id": "11251", "probability": 0.7}, {"id": "none", "p": 0.3}]}]}}]},
    {"decisions": {"answers": [{"question_id": "naics_1125", "probabilities": {"11251": 0.7, "none": 0.3}}]}},
])
def test_parse_tolerates_shapes(resp):
    probs, usage = backends.parse_decision_response(resp)
    assert probs == {"naics_1125": {"11251": 0.7, "none": 0.3}}
    assert set(usage) == {"prompt_tokens", "completion_tokens"}


def test_parse_errors():
    with pytest.raises(backends.ResponseFormatError):
        backends.parse_decision_response({"choices": []})
    with pytest.raises(backends.ResponseFormatError):
        backends.parse_decision_response({"decisions": [{"probabilities": {}}]})


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
        r = self.seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


SETTINGS = {"backend": "openrouter", "openrouter": {"base_url": "http://x", "model": "m"},
            "retry": {"max_attempts": 4, "base_delay_s": 0.01, "max_delay_s": 0.02}, "timeout_s": 5,
            "icp": {"backend": "openrouter", "model": "m"}}
OK = Resp(200, {"decisions": [{"question_id": "naics_1125", "probabilities": {"11251": 0.5, "none": 0.5}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def mk(seq, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sleeps = []
    sess = FakeSession(seq)
    return client.DecisionClient(SETTINGS, session=sess, sleep=sleeps.append), sess, sleeps


def test_retries_then_succeeds(monkeypatch):
    c, sess, sleeps = mk([Resp(429, text="slow"), requests.ConnectionError("boom"), Resp(503), OK], monkeypatch)
    probs, usage, _ = c.decide("QUJD", "p.pdf", QS)
    assert sess.calls == 4 and len(sleeps) == 3 and probs["naics_1125"]["11251"] == 0.5


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
