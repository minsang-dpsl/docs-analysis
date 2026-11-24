"""설정 로드: config/*.yaml, .env, 데이터 파일 경로."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_env(path: Path | None = None) -> None:
    """간단한 .env 로더. 이미 설정된 환경 변수는 덮어쓰지 않습니다."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    load_env()
    return load_yaml("settings.yaml")


def load_prompts() -> dict:
    return load_yaml("prompts.yaml")


def data_path(settings: dict, key: str) -> Path:
    """settings['data'][key] 를 프로젝트 루트 기준 절대 경로로 변환."""
    p = Path(settings["data"][key])
    return p if p.is_absolute() else ROOT / p


def output_dir(settings: dict, pdf_path: Path, base: Path | None = None) -> Path:
    base = base or (ROOT / "output")
    return base / settings["output_dir_name"] / pdf_path.stem


def api_key(env_name: str) -> str:
    key = os.environ.get(env_name, "")
    if not key:
        raise RuntimeError(f"환경 변수 {env_name} 가 비어 있습니다. .env 를 확인하세요.")
    return key
