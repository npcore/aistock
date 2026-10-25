from datetime import UTC, datetime, timedelta

from aistock.auth import KST, TokenRecord, _parse_expiry


def test_parse_expiry_is_kst():
    """키움 expires_dt는 KST 기준이다. UTC로 읽으면 9시간 일찍 만료된다."""
    parsed = _parse_expiry("20260806192130")
    assert parsed == datetime(2026, 8, 6, 19, 21, 30, tzinfo=KST)
    assert parsed.astimezone(UTC).hour == 10


def _record(expires_at: datetime) -> TokenRecord:
    return TokenRecord(
        access_token="t",
        token_type="bearer",
        expires_at=expires_at.isoformat(),
        mode="demo",
        issued_at=datetime.now(UTC).isoformat(),
    )


def test_token_valid_while_far_from_expiry():
    assert _record(datetime.now(UTC) + timedelta(hours=5)).is_valid()


def test_token_invalid_inside_refresh_buffer():
    """만료 10분 전 buffer 안에 들어오면 미리 재발급해야 한다."""
    assert not _record(datetime.now(UTC) + timedelta(minutes=3)).is_valid()


def test_token_invalid_after_expiry():
    assert not _record(datetime.now(UTC) - timedelta(seconds=1)).is_valid()
