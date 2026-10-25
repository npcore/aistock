"""환경변수 기반 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["real", "demo"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = PROJECT_ROOT / "profiles"


def _profile_name() -> str:
    """AISTOCK_PROFILE — 계정 프로필 이름. 비어 있으면 메인 계정(기존 경로 그대로)."""
    name = os.getenv("AISTOCK_PROFILE", "").strip()
    if name and not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"AISTOCK_PROFILE은 영문·숫자·-·_만 쓸 수 있습니다: {name!r}")
    return name


# 계정마다 .env·캐시(토큰·제안·경제 봇 메모·봇 offset)·로그를 따로 둔다.
# 메인은 기존 경로를 그대로 쓴다 — 이미 돌고 있는 서비스·파일을 옮기지 않기 위해서다.
PROFILE = _profile_name()
HOME_DIR = PROFILES_DIR / PROFILE if PROFILE else PROJECT_ROOT
ENV_PATH = HOME_DIR / ".env"
CACHE_DIR = HOME_DIR / "data" / "cache"
LOG_DIR = HOME_DIR / "logs"


@dataclass(frozen=True)
class Settings:
    mode: Mode
    app_key: str
    app_secret: str
    base_url: str
    ws_url: str
    account_no: str

    @property
    def is_real(self) -> bool:
        return self.mode == "real"


def load_env() -> None:
    """`.env`를 프로세스 환경에 올린다.

    Anthropic SDK처럼 os.environ을 직접 읽는 라이브러리를 쓰기 전에 호출해야 한다.
    """
    _load_profile_env()


def _load_profile_env() -> None:
    """이 계정의 .env만 읽는다. **메인 .env로 빈 값을 채우지 않는다.**

    추가 계정의 .env에 앱키가 빠졌을 때 메인 것을 이어받으면, 그 계정 봇이 메인 계좌로
    주문을 낸다. 없으면 없는 대로 실패하게 둔다.
    """
    if PROFILE and not ENV_PATH.exists():
        raise ValueError(f"계정 {PROFILE!r}의 설정 파일이 없습니다: {ENV_PATH}")
    load_dotenv(ENV_PATH)


def load_dart_key() -> str:
    """DART OpenAPI 인증키(40자리)를 읽는다."""
    _load_profile_env()
    key = os.getenv("DART_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "DART_API_KEY가 없습니다. https://opendart.fss.or.kr 에서 발급 후 .env에 넣어주세요."
        )
    return key


def load_settings(mode: Mode | None = None) -> Settings:
    """`.env`를 읽어 Settings를 만든다. mode를 넘기면 환경변수 KIWOOM_MODE보다 우선한다."""
    _load_profile_env()

    resolved: Mode = mode or os.getenv("KIWOOM_MODE", "demo")  # type: ignore[assignment]
    if resolved not in ("real", "demo"):
        raise ValueError(f"KIWOOM_MODE는 'real' 또는 'demo'여야 합니다: {resolved!r}")

    if resolved == "real":
        key_var, secret_var = "KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"
        base_url = os.getenv("KIWOOM_PRD_URL", "https://api.kiwoom.com")
        ws_url = os.getenv("KIWOOM_WS_PRD_URL", "wss://api.kiwoom.com:10000")
    else:
        key_var, secret_var = "KIWOOM_APP_KEY_MOCK", "KIWOOM_APP_SECRET_MOCK"
        base_url = os.getenv("KIWOOM_MOCK_URL", "https://mockapi.kiwoom.com")
        ws_url = os.getenv("KIWOOM_WS_MOCK_URL", "wss://mockapi.kiwoom.com:10000")

    app_key = os.getenv(key_var, "").strip()
    app_secret = os.getenv(secret_var, "").strip()
    if not app_key or not app_secret:
        raise ValueError(
            f"{resolved} 모드 자격증명이 없습니다. .env에 {key_var}, {secret_var}를 채워주세요."
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    return Settings(
        mode=resolved,
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        ws_url=ws_url,
        account_no=os.getenv("KIWOOM_ACCOUNT_NO", "").strip(),
    )
