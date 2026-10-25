"""관심·보유 브리핑 검증. 키움·텔레그램을 호출하지 않는다."""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from aistock.market_hours import KST, NY
from aistock.trading import daily_brief as B
from aistock.trading.policy import TradingPolicy

POLICY = TradingPolicy()


def _uptrend(n=240):
    return [40.0 + i * 0.15 + (1.2 if i % 2 else -1.2) for i in range(n)]


def _pullback():
    closes = _uptrend()
    closes[-14:] = [closes[-15] - i * 0.6 for i in range(14)]
    return closes


def _cmd(p):
    return f"/buy TQQQ 전량 {p:.2f}"


def _sell(p):
    return f"/sell TQQQ 전량 {p:.2f}"


def test_held_block_shows_exit_levels_and_matching_command():
    it = B.Item("TQQQ", held_qty=108, held_rate=-4.03, avg_price=68.72, sellable=108)
    closes = _uptrend()
    lines = B._block("TQQQ", closes[-1], "-0.2", closes, it, POLICY, krw=False,
                     buy_cmd=_cmd, sell_cmd=_sell)
    text = "\n".join(lines)
    assert "보유 108주" in text and "🛑 손절 $61.85" in text and "추세이탈" in text
    shown = re.search(r"익절 \$([\d.]+)", text).group(1)
    assert f"/sell TQQQ 전량 {shown}" in text  # 표시 가격 = 버튼 가격


def test_unheld_block_shows_buy_trigger_command():
    it = B.Item("TQQQ")
    closes = _pullback()
    price = closes[-1] + 3  # 아직 신호 가격 위
    text = "\n".join(B._block("TQQQ", price, "", closes, it, POLICY, krw=False,
                              buy_cmd=_cmd, sell_cmd=_sell))
    assert "🎯 매수 신호" in text and "/buy TQQQ 전량" in text


def test_kr_block_uses_won():
    it = B.Item("005930")
    closes = [200000.0 + (3000 if i % 2 else -3000) for i in range(240)]
    text = "\n".join(B._block("삼성전자", 201000, "+0.5", closes, it, POLICY, krw=True,
                              buy_cmd=lambda p: f"/buy 005930 4 {int(p)}",
                              sell_cmd=lambda p: ""))
    assert "원" in text and "$" not in text


# --- 보내는 시각 ---


@pytest.fixture
def bot(tmp_path, monkeypatch):
    from aistock import bot as Bot
    from aistock.notify.telegram import TelegramConfig
    from aistock.trading import daily_brief

    monkeypatch.setattr(Bot, "BRIEF_STATE_PATH", tmp_path / "brief.json")
    monkeypatch.setattr(daily_brief, "kr_report", lambda k, p: "KR 브리핑")
    monkeypatch.setattr(daily_brief, "us_report", lambda k, p: "US 브리핑 /sell TQQQ 전량 82.46")
    b = Bot.TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    sent = []
    notifier = SimpleNamespace(send=lambda text, **kw: sent.append((text, kw.get("buttons"))) or 1)
    ctx = SimpleNamespace(kiwoom=None, policy=POLICY)
    return b, ctx, notifier, sent


def test_kr_brief_sent_once_at_0840(bot):
    b, ctx, n, sent = bot
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 8, 39, tzinfo=KST))  # 월요일 08:39
    assert not [t for t, _ in sent if "KR" in t]
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 8, 41, tzinfo=KST))
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 8, 45, tzinfo=KST))
    assert len([t for t, _ in sent if "KR" in t]) == 1


def test_late_start_still_sends_within_hour_but_not_after(bot):
    b, ctx, n, sent = bot
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 10, 5, tzinfo=KST))  # 1시간 넘게 지남
    assert not [t for t, _ in sent if "KR" in t]


def test_no_kr_brief_on_weekend(bot):
    b, ctx, n, sent = bot
    b._daily_brief(ctx, n, now=datetime(2026, 10, 24, 8, 45, tzinfo=KST))  # 토요일
    assert not [t for t, _ in sent if "KR" in t]


def test_us_brief_at_ny_0900_with_buttons(bot):
    b, ctx, n, sent = bot
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 9, 2, tzinfo=NY))
    [(text, buttons)] = [(t, bt) for t, bt in sent if "US" in t]
    assert buttons[0][0] == ("▶ /sell TQQQ 전량 82.46", "cmd:/sell TQQQ 전량 82.46")


def test_us_brief_skips_us_holiday(bot):
    b, ctx, n, sent = bot
    b._daily_brief(ctx, n, now=datetime(2026, 11, 26, 9, 2, tzinfo=NY))  # 추수감사절
    assert not [t for t, _ in sent if "US" in t]


def test_restart_does_not_resend(bot, tmp_path):
    from aistock import bot as Bot
    from aistock.notify.telegram import TelegramConfig

    b, ctx, n, sent = bot
    at = datetime(2026, 10, 26, 8, 41, tzinfo=KST)
    b._daily_brief(ctx, n, now=at)
    again = Bot.TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o2")
    again._daily_brief(ctx, n, now=at.replace(minute=50))
    assert len([t for t, _ in sent if "KR" in t]) == 1


def test_kr_brief_off_for_us_only_accounts(bot):
    b, ctx, n, sent = bot
    ctx.policy = TradingPolicy(kr_enabled=False)
    b._daily_brief(ctx, n, now=datetime(2026, 10, 26, 8, 41, tzinfo=KST))
    assert not [t for t, _ in sent if "KR" in t]
