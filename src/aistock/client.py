"""키움 REST API 공통 클라이언트.

- `api-id` 헤더로 TR을 구분하고 body는 JSON
- `cont-yn` / `next-key` 헤더 왕복으로 연속조회
- 초당 요청수를 제한하고, 401·토큰만료 return_code는 1회 재발급 후 재시도
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from .auth import TokenManager
from .config import Mode, Settings, load_settings
from .errors import APIError, HTTPError

logger = logging.getLogger(__name__)

# 토큰 만료 계열 return_code — 재발급 후 1회 재시도한다.
AUTH_RETRY_CODES = {3, 8, 40, 100}
DEFAULT_RATE_LIMIT = 4.0  # 초당 요청수. 키움 권장치가 명시되지 않아 보수적으로 잡음.


@dataclass
class KiwoomResponse:
    """단일 페이지 응답."""

    api_id: str
    data: dict[str, Any]
    cont_yn: bool
    next_key: str | None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def return_code(self) -> int:
        return int(self.data.get("return_code", 0) or 0)

    @property
    def return_msg(self) -> str:
        return str(self.data.get("return_msg", ""))

    def rows(self, key: str) -> list[dict[str, Any]]:
        """응답 안의 리스트 필드를 꺼낸다. 없으면 빈 리스트."""
        value = self.data.get(key)
        return value if isinstance(value, list) else []


class RateLimiter:
    """슬라이딩 윈도우 방식 초당 요청수 제한."""

    def __init__(self, per_second: float = DEFAULT_RATE_LIMIT) -> None:
        self.per_second = per_second
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_second:
                    self._calls.append(now)
                    return
                time.sleep(1.0 - (now - self._calls[0]) + 0.001)


class KiwoomClient:
    """키움 REST API 호출기.

    >>> client = KiwoomClient.create(mode="real")
    >>> res = client.request("ka10001", "/api/dostk/stkinfo", {"stk_cd": "005930"})
    >>> res.data["stk_nm"]
    '삼성전자'
    """

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = 15.0,
        rate_limit: float = DEFAULT_RATE_LIMIT,
    ) -> None:
        self.settings = settings
        self.auth = TokenManager(settings)
        self.limiter = RateLimiter(rate_limit)
        self._client = httpx.Client(base_url=settings.base_url, timeout=timeout)

    @classmethod
    def create(cls, mode: Mode | None = None, **kwargs: Any) -> Self:
        return cls(load_settings(mode), **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def request(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        cont_yn: bool = False,
        next_key: str | None = None,
        _retry: bool = True,
    ) -> KiwoomResponse:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": self.auth.authorization_header(),
        }
        if cont_yn:
            headers["cont-yn"] = "Y"
        if next_key:
            headers["next-key"] = next_key

        self.limiter.acquire()
        response = self._client.post(path, headers=headers, json=body or {})

        if response.status_code == 401 and _retry:
            logger.info("401 수신 — 토큰 재발급 후 재시도합니다.")
            self.auth.force_refresh()
            return self.request(
                api_id, path, body, cont_yn=cont_yn, next_key=next_key, _retry=False
            )

        try:
            data = response.json()
        except ValueError:
            raise HTTPError(
                response.status_code, f"JSON이 아닌 응답: {response.text[:200]}"
            ) from None
        if not isinstance(data, dict):
            raise HTTPError(response.status_code, f"예상치 못한 응답 형식: {type(data).__name__}")

        return_code = int(data.get("return_code", 0) or 0)
        if return_code in AUTH_RETRY_CODES and _retry:
            logger.info("인증 계열 return_code=%s — 토큰 재발급 후 재시도합니다.", return_code)
            self.auth.force_refresh()
            return self.request(
                api_id, path, body, cont_yn=cont_yn, next_key=next_key, _retry=False
            )

        if response.status_code >= 400:
            raise HTTPError(response.status_code, data.get("return_msg", "API 요청 실패"))
        if return_code != 0:
            raise APIError(return_code, data.get("return_msg", ""), api_id)

        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        return KiwoomResponse(
            api_id=api_id,
            data=data,
            cont_yn=resp_headers.get("cont-yn", "N").upper() == "Y",
            next_key=resp_headers.get("next-key") or None,
            headers=resp_headers,
        )

    def paginate(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        max_pages: int = 10,
        warn_on_truncate: bool = True,
    ) -> Iterator[KiwoomResponse]:
        """연속조회(cont-yn/next-key)를 따라가며 페이지를 순차 반환한다.

        `warn_on_truncate=False`는 "이만큼만 필요하다"가 의도인 호출용이다.
        기본값이 True인 이유는, 다 가져온 줄 알았는데 잘린 경우가 훨씬 위험하기 때문이다.
        """
        cont_yn = False
        next_key: str | None = None
        for _ in range(max_pages):
            response = self.request(api_id, path, body, cont_yn=cont_yn, next_key=next_key)
            yield response
            if not response.cont_yn or not response.next_key:
                return
            cont_yn, next_key = True, response.next_key
        if warn_on_truncate:
            logger.warning(
                "max_pages=%d 도달 — 남은 페이지를 가져오지 않았습니다 (%s).", max_pages, api_id
            )

    def collect(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        rows_key: str,
        max_pages: int = 10,
        warn_on_truncate: bool = True,
    ) -> list[dict[str, Any]]:
        """연속조회를 끝까지 돌며 rows_key 리스트를 모두 이어붙인다."""
        out: list[dict[str, Any]] = []
        for response in self.paginate(
            api_id, path, body, max_pages=max_pages, warn_on_truncate=warn_on_truncate
        ):
            out.extend(response.rows(rows_key))
        return out
