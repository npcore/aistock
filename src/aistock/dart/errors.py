"""DART OpenAPI 상태코드와 예외."""

from __future__ import annotations

# 공식 개발가이드 기준. HTTP는 200이어도 status로 실패를 알린다.
STATUS_MESSAGES: dict[str, str] = {
    "000": "정상",
    "010": "등록되지 않은 키입니다",
    "011": "사용할 수 없는 키입니다 (사용중지 처리)",
    "012": "접근할 수 없는 IP입니다",
    "013": "조회된 데이터가 없습니다",
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한을 초과했습니다 (일 20,000건)",
    "021": "조회 가능한 회사 개수가 초과했습니다 (최대 100건)",
    "100": "필드의 부적절한 값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검 중입니다",
    "900": "정의되지 않은 오류가 발생했습니다",
    "901": "사용자 계정의 개인정보 보유기간이 만료되었습니다",
}

# 조회 결과가 비어 있을 뿐 실패가 아닌 코드. 빈 리스트로 처리한다.
EMPTY_STATUSES = {"013"}


class DartError(Exception):
    """DART API 오류."""

    def __init__(self, status: str, message: str = "") -> None:
        detail = message or STATUS_MESSAGES.get(status, "알 수 없는 오류")
        super().__init__(f"[DART {status}] {detail}")
        self.status = status
        self.message = detail


class DartAuthError(DartError):
    """인증키 문제 (010/011/012/101)."""


class DartRateLimitError(DartError):
    """요청 한도 초과 (020/021)."""


def raise_for_status(status: str, message: str = "", *, empty_ok: bool = True) -> None:
    """status가 정상/빈결과가 아니면 알맞은 예외를 던진다.

    empty_ok=False면 `013`도 실패로 본다. 목록 조회의 013은 "조건에 맞는 공시 없음"이지만
    파일 조회의 013은 "그런 접수번호 없음"이라 돌려줄 내용이 없기 때문이다.
    """
    if status == "000":
        return
    if empty_ok and status in EMPTY_STATUSES:
        return
    if status in {"010", "011", "012", "101", "901"}:
        raise DartAuthError(status, message)
    if status in {"020", "021"}:
        raise DartRateLimitError(status, message)
    raise DartError(status, message)
