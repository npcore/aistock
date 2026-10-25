"""접근토큰 발급 · 디스크 캐싱 · 자동 갱신.

키움 REST API는 토큰을 매 요청 재발급하면 rate limit에 걸린다. 발급된 토큰은
만료 시각까지 재사용하고, 만료 buffer 안에 들어오면 그때만 재발급한다.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import CACHE_DIR, Settings
from .errors import AuthenticationError

logger = logging.getLogger(__name__)

TOKEN_PATH = "/oauth2/token"
REVOKE_PATH = "/oauth2/revoke"
KST = timezone(timedelta(hours=9))
REFRESH_BUFFER = timedelta(minutes=10)


@dataclass
class TokenRecord:
    access_token: str
    token_type: str
    expires_at: str  # ISO8601 UTC
    mode: str
    issued_at: str

    @property
    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expires_at)

    def is_valid(self, buffer: timedelta = REFRESH_BUFFER) -> bool:
        return datetime.now(UTC) + buffer < self.expiry


class TokenManager:
    """모드별 토큰 하나를 관리한다. 스레드 세이프."""

    def __init__(self, settings: Settings, *, timeout: float = 10.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._lock = threading.Lock()
        self._record: TokenRecord | None = None

    @property
    def _cache_file(self) -> Path:
        return CACHE_DIR / f"token_{self.settings.mode}.json"

    def get_token(self) -> str:
        with self._lock:
            record = self._record or self._load_cache()
            if record is not None and record.is_valid():
                self._record = record
                return record.access_token
            return self._issue().access_token

    def force_refresh(self) -> str:
        """401 등으로 토큰이 무효해졌을 때 캐시를 버리고 재발급한다."""
        with self._lock:
            self._record = None
            self._cache_file.unlink(missing_ok=True)
            return self._issue().access_token

    def authorization_header(self) -> str:
        return f"Bearer {self.get_token()}"

    def current(self) -> TokenRecord:
        """유효한 토큰 레코드를 돌려준다. 필요하면 발급한다."""
        self.get_token()
        assert self._record is not None
        return self._record

    # --- 내부 ---

    def _issue(self) -> TokenRecord:
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.settings.app_key,
            "secretkey": self.settings.app_secret,
        }
        try:
            response = httpx.post(
                f"{self.settings.base_url}{TOKEN_PATH}",
                json=payload,
                headers={"Content-Type": "application/json;charset=UTF-8"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"토큰 요청 중 네트워크 오류: {exc}") from exc

        data = _safe_json(response)
        return_code = data.get("return_code")
        if response.status_code >= 400 or return_code not in (None, 0):
            msg = data.get("return_msg") or response.text[:200]
            raise AuthenticationError(
                f"토큰 발급 실패 (HTTP {response.status_code}, return_code={return_code}): {msg}"
            )

        token = data.get("token")
        expires_dt = data.get("expires_dt")
        if not token or not expires_dt:
            raise AuthenticationError(f"토큰 응답에 token/expires_dt가 없습니다: {data}")

        record = TokenRecord(
            access_token=token,
            token_type=str(data.get("token_type", "bearer")).lower(),
            expires_at=_parse_expiry(str(expires_dt)).isoformat(),
            mode=self.settings.mode,
            issued_at=datetime.now(UTC).isoformat(),
        )
        self._record = record
        self._save_cache(record)
        logger.info("접근토큰 발급 완료 (mode=%s, 만료=%s)", record.mode, record.expiry)
        return record

    def revoke(self) -> None:
        record = self._record or self._load_cache()
        if record is None:
            return
        httpx.post(
            f"{self.settings.base_url}{REVOKE_PATH}",
            json={
                "appkey": self.settings.app_key,
                "secretkey": self.settings.app_secret,
                "token": record.access_token,
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=self.timeout,
        )
        self._record = None
        self._cache_file.unlink(missing_ok=True)

    def _load_cache(self) -> TokenRecord | None:
        path = self._cache_file
        if not path.exists():
            return None
        try:
            record = TokenRecord(**json.loads(path.read_text()))
        except (ValueError, TypeError) as exc:
            logger.warning("토큰 캐시를 읽을 수 없어 무시합니다: %s", exc)
            return None
        if record.mode != self.settings.mode:
            return None
        return record

    def _save_cache(self, record: TokenRecord) -> None:
        path = self._cache_file
        path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2))
        path.chmod(0o600)


def _parse_expiry(value: str) -> datetime:
    """키움 expires_dt는 KST 기준 'YYYYMMDDHHMMSS'."""
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST).astimezone(UTC)


def _safe_json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
