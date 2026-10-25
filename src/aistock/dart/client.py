"""DART OpenAPI HTTP 클라이언트.

키움과 달리 GET + 쿼리스트링이고, 인증키(`crtfc_key`)를 매 요청에 붙인다.
HTTP 200이어도 body의 `status`가 "000"이 아니면 실패다.
"""

from __future__ import annotations

import logging
from typing import Any, Self

import httpx

from ..client import RateLimiter
from ..config import load_dart_key
from .errors import EMPTY_STATUSES, DartError, raise_for_status

logger = logging.getLogger(__name__)

BASE_URL = "https://opendart.fss.or.kr/api"
# 일 20,000건 제한. 초당 상한은 명시되어 있지 않아 보수적으로 잡는다.
DEFAULT_RATE_LIMIT = 5.0


class DartClient:
    """DART OpenAPI 호출기.

    >>> with DartClient.create() as dart:
    ...     result = dart.get_json("list.json", {"corp_code": "00126380"})
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        rate_limit: float = DEFAULT_RATE_LIMIT,
    ) -> None:
        if not api_key:
            raise ValueError("DART 인증키가 비어 있습니다.")
        self.api_key = api_key
        self.limiter = RateLimiter(rate_limit)
        # 일 20,000건 한도를 쓰는 쪽에서 확인할 수 있도록 실제 요청수를 센다.
        self.requests_made = 0
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout, follow_redirects=True)

    @classmethod
    def create(cls, api_key: str | None = None, **kwargs: Any) -> Self:
        return cls(api_key or load_dart_key(), **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        query["crtfc_key"] = self.api_key
        self.limiter.acquire()
        self.requests_made += 1
        response = self._client.get(path, params=query)
        response.raise_for_status()
        return response

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """JSON 엔드포인트 호출. status가 '013'(데이터 없음)이면 그대로 돌려준다."""
        response = self._request(path, params)
        try:
            data = response.json()
        except ValueError as exc:
            raise DartError("900", f"JSON이 아닌 응답: {response.text[:200]}") from exc

        status = str(data.get("status", "000"))
        raise_for_status(status, str(data.get("message", "")))
        if status in EMPTY_STATUSES:
            logger.debug("DART 조회 결과 없음: %s %s", path, params)
        return data

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """ZIP 등 바이너리 엔드포인트 호출.

        실패 시에는 바이너리 대신 XML 에러 문서가 오므로 그것을 먼저 판별한다.

        JSON 조회와 달리 여기서는 `013`도 실패다. 목록 조회의 013은 "조건에 맞는 공시 없음"
        이지만, 파일 조회의 013은 "그런 접수번호 없음"이라 돌려줄 바이트가 없다.
        """
        response = self._request(path, params)
        content = response.content
        if _looks_like_error_xml(content):
            status, message = _parse_error_xml(content)
            raise_for_status(status, message, empty_ok=False)
            raise DartError(status, message)
        return content


def _looks_like_error_xml(content: bytes) -> bool:
    """ZIP은 'PK'로 시작한다. 그 외 XML 헤더면 에러 응답이다."""
    head = content[:200].lstrip()
    return not head.startswith(b"PK") and head.startswith((b"<?xml", b"<result"))


def _parse_error_xml(content: bytes) -> tuple[str, str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(content.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return "900", "응답을 해석할 수 없습니다."
    status = root.findtext("status") or "900"
    message = root.findtext("message") or ""
    return status, message
