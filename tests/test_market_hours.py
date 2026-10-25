"""장 운영시간 판정 검증."""

from datetime import datetime, timedelta, timezone

import pytest

from aistock import market_hours as mh
from aistock.notify.format import ticker_message

KST = timezone(timedelta(hours=9))


def at(y=2026, m=8, d=6, hh=10, mm=0) -> datetime:
    """20260806은 목요일(거래일)."""
    return datetime(y, m, d, hh, mm, tzinfo=KST)


# --- 요일·시각 ---


@pytest.mark.parametrize("day", [3, 4, 5, 6, 7])  # 2026-08-03(월) ~ 08-07(금)
def test_weekdays_are_sessions(day):
    assert mh.is_weekday(at(d=day))


@pytest.mark.parametrize("day", [8, 9])  # 토, 일
def test_weekend_is_not_a_session(day):
    assert not mh.is_weekday(at(d=day))
    assert not mh.in_session(at(d=day))


@pytest.mark.parametrize(
    "hh,mm,expected", [(8, 59, False), (9, 0, True), (15, 30, True), (15, 31, False)]
)
def test_session_window_boundaries(hh, mm, expected):
    """09:00 시작, 15:30 마감. 경계가 어긋나면 알림이 장 밖에서 울린다."""
    assert mh.in_session(at(hh=hh, mm=mm)) is expected


# --- 다음 틱 ---


def test_next_tick_snaps_to_the_hour():
    """60분 간격이면 10:00, 11:00처럼 정각에 와야 한다."""
    assert mh.next_tick(60, at(hh=10, mm=17)) == at(hh=11, mm=0)


def test_next_tick_half_hour():
    assert mh.next_tick(30, at(hh=10, mm=17)) == at(hh=10, mm=30)
    assert mh.next_tick(30, at(hh=10, mm=45)) == at(hh=11, mm=0)


def test_next_tick_is_strictly_future_on_exact_boundary():
    """정각에 실행돼도 같은 시각을 다시 주면 즉시 재전송 루프가 된다."""
    assert mh.next_tick(60, at(hh=10, mm=0)) == at(hh=11, mm=0)


def test_next_tick_rejects_zero_interval():
    with pytest.raises(ValueError):
        mh.next_tick(0)


def test_session_bounds():
    open_at, close_at = mh.session_bounds(at(hh=12))
    assert (open_at.hour, open_at.minute) == (9, 0)
    assert (close_at.hour, close_at.minute) == (15, 30)


# --- 거래일 판정 ---


class StubClient:
    def __init__(self, latest: str | None):
        self.latest = latest


def _patch_chart(monkeypatch, latest, raises=False):
    from aistock.market import domestic

    def fake(client, code, base_dt, **kwargs):
        if raises:
            from aistock.errors import KiwoomError

            raise KiwoomError("조회 실패")
        return [{"dt": latest}] if latest else []

    monkeypatch.setattr(domestic, "daily_chart", fake)


def test_trading_day_when_today_bar_exists(monkeypatch):
    _patch_chart(monkeypatch, "20260806")
    assert mh.is_trading_day(StubClient("20260806"), at())


def test_holiday_when_today_bar_missing(monkeypatch):
    """평일인데 오늘 봉이 없으면 공휴일이다 — 달력을 따로 두지 않아도 된다."""
    _patch_chart(monkeypatch, "20260805")
    assert not mh.is_trading_day(StubClient("20260805"), at())


def test_weekend_short_circuits_without_api(monkeypatch):
    _patch_chart(monkeypatch, None, raises=True)  # 호출되면 예외
    assert not mh.is_trading_day(StubClient(None), at(d=9))


def test_probe_failure_assumes_open(monkeypatch, caplog):
    """조회 실패로 알림이 통째로 멈추는 쪽이 더 나쁘다."""
    _patch_chart(monkeypatch, None, raises=True)
    with caplog.at_level("WARNING"):
        assert mh.is_trading_day(StubClient(None), at())
    assert "장중으로 간주" in caplog.text


# --- 메시지 ---


def test_ticker_message_shows_price_and_rate():
    msg = ticker_message(
        [
            {
                "code": "005930",
                "name": "삼성전자",
                "quote": {"cur_prc": "+231000", "flu_rt": "+0.22"},
            }
        ],
        at="10:00",
    )
    assert "10:00" in msg and "삼성전자" in msg and "231000" in msg and "0.22" in msg
    assert "🔺" in msg


def test_ticker_message_marks_decline():
    msg = ticker_message(
        [
            {
                "code": "000660",
                "name": "SK하이닉스",
                "quote": {"cur_prc": "-1422000", "flu_rt": "-4.88"},
            }
        ],
        at="10:00",
    )
    assert "🔻" in msg


def test_ticker_message_survives_quote_failure():
    """한 종목 조회가 실패해도 나머지는 나가야 한다."""
    msg = ticker_message(
        [
            {"code": "005930", "name": "삼성전자", "quote": {}},
            {"code": "000660", "name": "SK하이닉스", "quote": {"cur_prc": "+1000", "flu_rt": "+1"}},
        ],
        at="10:00",
    )
    assert "시세 조회 실패" in msg
    assert "SK하이닉스" in msg


def test_ticker_message_marks_close():
    assert "장마감" in ticker_message([], at="15:30", closing=True)


def test_ticker_message_groups_by_section():
    """국내와 미국을 섞어 보내면 어느 쪽 시세인지 구분이 안 된다."""
    msg = ticker_message(
        [
            {
                "code": "005930",
                "name": "삼성전자",
                "quote": {"cur_prc": "+1", "flu_rt": "+1"},
                "section": "🇰🇷 국내",
            },
            {
                "code": "TQQQ",
                "name": "TQQQ",
                "quote": {"cur_prc": "+2", "flu_rt": "+2"},
                "section": "🇺🇸 미국",
            },
        ],
        at="23:30",
    )
    assert "🇰🇷 국내" in msg and "🇺🇸 미국" in msg
    assert msg.index("국내") < msg.index("미국")


# --- 미국장 시간 (서머타임) ---


def us_at(y, m, d, hh, mm) -> datetime:
    """뉴욕 현지 시각으로 만든 뒤 KST로 넘긴다."""
    from zoneinfo import ZoneInfo

    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).astimezone(KST)


@pytest.mark.parametrize(
    "hh,mm,expected",
    [(9, 29, False), (9, 30, True), (12, 0, True), (16, 0, True), (16, 1, False)],
)
def test_us_session_window(hh, mm, expected):
    """미국 정규장은 09:30~16:00 ET."""
    assert mh.in_us_session(us_at(2026, 8, 11, hh, mm)) is expected


def test_us_session_in_summer_is_2230_kst():
    """서머타임(EDT): ET 09:30 = KST 22:30. 시각이 어긋나면 알림이 1시간 밀린다."""
    opening = us_at(2026, 8, 11, 9, 30)
    assert opening.astimezone(KST).strftime("%H:%M") == "22:30"
    assert mh.in_us_session(opening)


def test_us_session_in_winter_is_2330_kst():
    """표준시(EST): ET 09:30 = KST 23:30."""
    opening = us_at(2026, 12, 15, 9, 30)
    assert opening.astimezone(KST).strftime("%H:%M") == "23:30"
    assert mh.in_us_session(opening)


def test_us_weekend_is_closed():
    assert not mh.in_us_session(us_at(2026, 8, 15, 12, 0))  # 토
    assert not mh.in_us_session(us_at(2026, 8, 16, 12, 0))  # 일


def test_korean_and_us_sessions_do_not_overlap():
    """한국 장중(10시)에 미국장이 열려 있다고 판정되면 종가를 실시간으로 오인한다."""
    kr_noon = at(hh=10)
    assert mh.in_session(kr_noon)
    assert not mh.in_us_session(kr_noon)


def test_us_session_label_shows_local_time():
    assert "ET" in mh.us_session_label(us_at(2026, 8, 11, 9, 30))
