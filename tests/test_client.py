import time

import pytest

from aistock.client import KiwoomResponse, RateLimiter


def test_rate_limiter_throttles_beyond_quota():
    limiter = RateLimiter(per_second=3)
    start = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    # 4번째 호출은 첫 윈도우가 비워질 때까지 기다려야 한다.
    assert time.monotonic() - start >= 0.9


def test_rate_limiter_does_not_throttle_within_quota():
    limiter = RateLimiter(per_second=5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - start < 0.1


def _response(data: dict) -> KiwoomResponse:
    return KiwoomResponse(api_id="ka10001", data=data, cont_yn=False, next_key=None)


def test_rows_returns_list_field():
    assert _response({"daly_stkpc": [{"dt": "20260805"}]}).rows("daly_stkpc") == [
        {"dt": "20260805"}
    ]


def test_rows_missing_key_is_empty():
    assert _response({"return_code": 0}).rows("daly_stkpc") == []


def test_rows_non_list_is_empty():
    """응답이 단건 dict로 올 때 리스트인 척하지 않는다."""
    assert _response({"daly_stkpc": {"dt": "20260805"}}).rows("daly_stkpc") == []


@pytest.mark.parametrize(
    "value,expected", [({"return_code": 0}, 0), ({}, 0), ({"return_code": "3"}, 3)]
)
def test_return_code_normalized_to_int(value, expected):
    assert _response(value).return_code == expected
