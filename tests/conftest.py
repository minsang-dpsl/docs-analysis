import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

_CREDENTIAL_ENV = (
    "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "DOCUMENTAI_PROJECT_ID", "DOCUMENTAI_PROCESSOR_ID", "DOCUMENTAI_LOCATION",
)


@pytest.fixture(autouse=True)
def isolate_from_real_credentials(monkeypatch):
    """테스트가 실제 .env/셸의 키로 외부 API 를 호출하지 못하게 격리합니다.

    config.load_settings() 는 .env 를 환경 변수로 읽어 들이므로, 이를 막고 자격증명 변수도 비웁니다.
    키가 필요한 테스트는 자기 안에서 monkeypatch.setenv 로 가짜 값을 넣습니다.
    """
    monkeypatch.setattr(config, "load_env", lambda path=None: None)
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
