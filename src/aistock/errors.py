"""키움 API 예외 계층."""

from __future__ import annotations


class KiwoomError(Exception):
    """모든 키움 관련 오류의 최상위."""


class AuthenticationError(KiwoomError):
    """토큰 발급/갱신 실패."""


class HTTPError(KiwoomError):
    """HTTP 레벨 오류."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"[HTTP {status_code}] {message}")
        self.status_code = status_code
        self.message = message


class APIError(KiwoomError):
    """정상 HTTP 응답이지만 return_code != 0 인 경우."""

    def __init__(self, return_code: int, return_msg: str, api_id: str = "") -> None:
        prefix = f"[{api_id}] " if api_id else ""
        super().__init__(f"{prefix}return_code={return_code}: {return_msg}")
        self.return_code = return_code
        self.return_msg = return_msg
        self.api_id = api_id
