import pytest

from aistock.dart.errors import DartAuthError, DartError, DartRateLimitError, raise_for_status


def test_success_does_not_raise():
    raise_for_status("000")


def test_no_data_is_not_an_error():
    """013은 '조회 결과 없음'이라 실패가 아니다. 예외로 던지면 정상 조회가 죽는다."""
    raise_for_status("013")


@pytest.mark.parametrize("status", ["010", "011", "012", "101", "901"])
def test_key_problems_raise_auth_error(status):
    with pytest.raises(DartAuthError):
        raise_for_status(status)


@pytest.mark.parametrize("status", ["020", "021"])
def test_quota_problems_raise_rate_limit_error(status):
    with pytest.raises(DartRateLimitError):
        raise_for_status(status)


def test_other_failures_raise_base_error():
    with pytest.raises(DartError) as exc:
        raise_for_status("800")
    assert "점검" in str(exc.value)


def test_unknown_status_still_raises():
    with pytest.raises(DartError):
        raise_for_status("999")


def test_server_message_wins_over_builtin_text():
    with pytest.raises(DartError) as exc:
        raise_for_status("100", "corp_code 값이 잘못되었습니다")
    assert "corp_code 값이 잘못되었습니다" in str(exc.value)


def test_no_data_is_an_error_for_binary_endpoints():
    """파일 조회의 013은 '그런 접수번호 없음'이라 돌려줄 바이트가 없다."""
    with pytest.raises(DartError):
        raise_for_status("013", empty_ok=False)


def test_empty_ok_does_not_mask_auth_errors():
    with pytest.raises(DartAuthError):
        raise_for_status("010", empty_ok=False)
