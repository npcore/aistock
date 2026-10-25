"""매수 보류 필터 — 경제 일정(FOMC·CPI·고용)과 기초지수(QQQ) 추세."""

from datetime import datetime

from aistock.trading import events as E
from aistock.trading import us_trader
from aistock.trading.policy import TradingPolicy
from aistock.trading.us_trader import UnderlyingView, buy_block, decide

NY = E.NY
POLICY = TradingPolicy()


def _uptrend(n=240):
    return [40 + i * 0.15 + (1.2 if i % 2 else -1.2) for i in range(n)]


# --- 경제 일정 ---


def test_fomc_holds_day_before_and_until_30min_after():
    assert E.active_event(datetime(2026, 10, 26, 15, 0, tzinfo=NY)) is None  # 월
    e = E.active_event(datetime(2026, 10, 27, 10, 0, tzinfo=NY))  # 화 (회의 첫날)
    assert e and e.name.startswith("FOMC")
    assert E.active_event(datetime(2026, 10, 28, 14, 29, tzinfo=NY))
    assert E.active_event(datetime(2026, 10, 28, 14, 31, tzinfo=NY)) is None


def test_premarket_releases_hold_previous_trading_day():
    # 고용보고서 11/6(금) 08:30 → 목요일 하루 보류, 금요일 10:00에는 이미 결과가 나와 있다
    assert E.active_event(datetime(2026, 11, 5, 11, 0, tzinfo=NY)).name == "고용보고서"
    assert E.active_event(datetime(2026, 11, 6, 10, 0, tzinfo=NY)) is None
    # CPI 11/10(화) → 월요일
    assert E.active_event(datetime(2026, 11, 9, 11, 0, tzinfo=NY)).name == "CPI 발표"


def test_hold_window_skips_weekend_and_holiday():
    # 월요일 발표면 직전 거래일은 금요일
    monday = E.Event("X", datetime(2026, 11, 30, 8, 30, tzinfo=NY))
    start, _ = E.hold_window(monday)
    assert start.date().isoformat() == "2026-11-27"
    # 추수감사절(11/26 목) 다음 날 발표면 직전 거래일은 수요일
    fri = E.Event("X", datetime(2026, 11, 27, 8, 30, tzinfo=NY))
    assert E.hold_window(fri)[0].date().isoformat() == "2026-11-25"


def test_upcoming_lists_week_ahead():
    soon = E.upcoming(datetime(2026, 11, 2, 9, 0, tzinfo=NY))
    assert [e.name for e in soon] == ["고용보고서"]


def test_events_sorted_and_in_ny_time():
    assert E.EVENTS == sorted(E.EVENTS, key=lambda e: e.at)
    assert all(e.at.tzinfo is NY for e in E.EVENTS)


# --- decide에 보류 사유 ---


def test_buy_block_turns_buy_into_hold():
    closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]  # 횡보 후 돌파
    d = decide(closes, 52.5, POLICY, holding_rate=None)
    assert d.side == "buy"
    held = decide(closes, 52.5, POLICY, holding_rate=None, buy_block="FOMC")
    assert held.side is None and "매수 보류 — FOMC" in held.reason


def test_buy_block_never_stops_sells():
    d = decide(_uptrend(), 30.0, POLICY, holding_rate=-15, buy_block="FOMC")
    assert d.side == "sell"


# --- 기초지수 ---


def test_underlying_view_ok_only_in_uptrend():
    assert UnderlyingView("QQQ", 110, 105, 100).ok
    assert not UnderlyingView("QQQ", 98, 105, 100).ok  # 200일선 아래
    assert not UnderlyingView("QQQ", 101, 99, 100).ok  # 50일선이 200일선 아래 (데드크로스)
    assert UnderlyingView("QQQ", 101, None, None).ok  # 판단 불가면 막지 않는다
    assert "200일선 -2.0%" in UnderlyingView("QQQ", 98, 105, 100).text


def test_buy_block_checks_event_then_underlying(monkeypatch):
    calm = datetime(2026, 11, 12, 11, 0, tzinfo=NY)
    monkeypatch.setattr(
        us_trader, "underlying_view", lambda k, s, p, now, **kw: UnderlyingView(s, 90, 95, 100)
    )
    assert "기초지수 하락 추세 (QQQ" in buy_block(None, "TQQQ", POLICY, calm)
    assert buy_block(None, "AGQ", POLICY, calm) is None  # 기초지수 매핑 없음
    fomc = datetime(2026, 10, 27, 11, 0, tzinfo=NY)
    assert "FOMC" in buy_block(None, "AGQ", POLICY, fomc)
    off = TradingPolicy(us_event_hold=False, us_underlying=())
    assert buy_block(None, "TQQQ", off, fomc) is None


def test_buy_block_fails_open_on_lookup_error(monkeypatch):
    from aistock.errors import KiwoomError

    def boom(*a, **k):
        raise KiwoomError("timeout")

    monkeypatch.setattr(us_trader, "underlying_view", boom)
    assert buy_block(None, "TQQQ", POLICY, datetime(2026, 11, 12, 11, 0, tzinfo=NY)) is None


def test_underlying_env(monkeypatch):
    monkeypatch.setenv("TRADE_US_UNDERLYING", "tqqq:qqq, soxl:soxx")
    p = TradingPolicy.from_env()
    assert p.underlying_of("TQQQ") == "QQQ" and p.underlying_of("UPRO") == ""
    monkeypatch.setenv("TRADE_US_UNDERLYING", "0")
    assert TradingPolicy.from_env().us_underlying == ()
    monkeypatch.setenv("TRADE_US_EVENT_HOLD", "0")
    assert TradingPolicy.from_env().us_event_hold is False
