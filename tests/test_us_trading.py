"""TQQQ 반자동 매매 검증. 키움·텔레그램을 호출하지 않는다."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aistock.market_hours import KST, NY, is_us_trading_day
from aistock.trading import executor, us_trader
from aistock.trading import proposals as P
from aistock.trading.policy import TradingPolicy
from aistock.trading.us_trader import UsTrader, decide, manual_proposal
from aistock.us_order import UsOrderError

POLICY = TradingPolicy()


@pytest.fixture(autouse=True)
def _no_buy_gate(monkeypatch):
    """경제 일정·기초지수 필터는 test_us_gates.py에서 따로 본다. 여기서는 실행 날짜에 따라
    (FOMC 주간 등) 결과가 바뀌지 않게 끈다."""
    monkeypatch.setattr(us_trader, "buy_block", lambda *a, **k: None)


def _uptrend(n=240, start=40.0, step=0.15, swing=1.2):
    """출렁이며 오르는 추세. 마지막 값이 200일선보다 충분히 위다."""
    return [start + i * step + (swing if i % 2 else -swing) for i in range(n)]


# --- 휴장일 ---


def test_us_holidays():
    assert not is_us_trading_day(datetime(2026, 11, 26, 11, 0, tzinfo=NY))  # 추수감사절
    assert not is_us_trading_day(datetime(2026, 10, 17, 11, 0, tzinfo=NY))  # 토요일
    assert is_us_trading_day(datetime(2026, 10, 19, 11, 0, tzinfo=NY))


# --- 규칙 ---


def test_not_enough_bars_does_nothing():
    d = decide([50.0] * 100, 50.0, POLICY, holding_rate=None)
    assert d.side is None and "계산 불가" in d.reason


def test_breakout_buy():
    """어제까지 200일선 근처, 오늘 +3% 넘게 뚫음."""
    closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    d = decide(closes, 52.5, POLICY, holding_rate=None)
    assert d.side == "buy" and "상향 돌파" in d.reason


def test_no_repeat_breakout_once_above():
    closes = _uptrend()
    d = decide(closes, closes[-1], POLICY, holding_rate=None)
    assert d.side is None or "돌파" not in d.reason


def test_pullback_buy_in_uptrend():
    closes = _uptrend()
    closes[-14:] = [closes[-15] - i * 0.6 for i in range(14)]  # 2주 내리 하락 → RSI 낮음
    price = closes[-1] - 0.5
    d = decide(closes, price, POLICY, holding_rate=None)
    assert d.side == "buy" and "눌림" in d.reason


def test_downtrend_no_buy():
    closes = [90 - i * 0.15 + (1 if i % 2 else -1) for i in range(240)]
    d = decide(closes, closes[-1], POLICY, holding_rate=None)
    assert d.side is None and "하락 추세" in d.reason


def test_stop_loss_and_take_profit():
    closes = _uptrend()
    assert decide(closes, closes[-1], POLICY, holding_rate=-11).source == "stop_loss"
    assert decide(closes, closes[-1], POLICY, holding_rate=21).source == "take_profit"
    assert decide(closes, closes[-1], POLICY, holding_rate=5).side is None


def test_trend_exit_when_far_below_sma():
    closes = _uptrend()
    d = decide(closes, closes[-1] * 0.7, POLICY, holding_rate=-2)
    assert d.side == "sell" and "추세 이탈" in d.reason


def test_small_dip_below_sma_is_not_exit():
    """200일선을 살짝 밑도는 출렁임마다 팔면 수수료와 슬리피지만 쌓인다."""
    closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    d = decide(closes, 49.2, POLICY, holding_rate=-1)
    assert d.side is None


# --- 주기 점검 ---


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, *, buttons=None, preview=False):
        self.sent.append((text, buttons))
        return 900 + len(self.sent)


@pytest.fixture
def us(monkeypatch):
    """뉴욕 장중 11:00 · TQQQ $68.21 · 외화 $5,000."""
    state = SimpleNamespace(price="+68.2100", holdings=[], closes=_uptrend(), usd=5000.0)
    now = datetime.now(NY).replace(hour=11, minute=0)
    state.now = now
    monkeypatch.setattr(us_trader, "datetime", SimpleNamespace(now=lambda tz=None: now if tz is NY else datetime.now(tz)))
    monkeypatch.setattr(us_trader, "in_us_session", lambda at=None: True)
    monkeypatch.setattr(us_trader, "is_us_trading_day", lambda at=None: True)
    monkeypatch.setattr(us_trader, "resolve_exchange",
                        lambda c, s: ("ND", {"stk_nm": "TQQQ ETF", "cur_prc": state.price}))
    monkeypatch.setattr(us_trader.overseas, "quote",
                        lambda c, s, e: {"stk_nm": "TQQQ ETF", "cur_prc": state.price})
    monkeypatch.setattr(UsTrader, "_closes", lambda self, s, e, n: state.closes)
    state.open_orders = []  # 키움 미체결 (직접 낸 주문 포함)
    monkeypatch.setattr(us_trader, "us_order_pending", lambda c: state.open_orders)
    # 증권사 주문가능수량 (ust31490) — 외화 ÷ 가격에 수수료 몫을 뺀 값처럼 흉내낸다
    monkeypatch.setattr(us_trader, "max_buy_quantity", lambda c, s, e, p: int(state.usd * 0.995 // p))
    monkeypatch.setattr(us_trader, "fetch_us_balance", lambda c: SimpleNamespace(
        holdings=state.holdings, usd_orderable=state.usd))
    return state


def _trader(tmp_path, **policy):
    return UsTrader(None, FakeNotifier(), store=P.ProposalStore(tmp_path / "p.json"),
                    policy=TradingPolicy(**policy))


def _held(rate, qty=10):
    return SimpleNamespace(symbol="TQQQ", quantity=qty, sellable=qty, profit_rate=rate)


def test_stop_loss_proposal_sent_with_buttons(tmp_path, us):
    us.holdings = [_held(-12)]
    t = _trader(tmp_path)
    assert t.check(force=True) == 1
    [p] = t.store.all()
    assert p.is_us and p.side == "sell" and p.quantity == 10 and p.exchange == "ND"
    assert p.price_usd == 68.21 and p.source == "stop_loss"
    text, buttons = t.notifier.sent[0]
    assert "🇺🇸" in text and "$68.21" in text and buttons[0][0][1] == f"tp:ok:{p.id}"


def test_same_signal_once_per_ny_day(tmp_path, us):
    us.holdings = [_held(-12)]
    t = _trader(tmp_path)
    t.check(force=True)
    executor.reject(t.store, t.store.all()[0].id)
    assert t.check(force=True) == 0


def test_buy_quantity_from_budget(tmp_path, us):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    t = _trader(tmp_path, us_budget_usd=1000)
    assert t.check(force=True) == 1
    p = t.store.all()[0]
    assert p.side == "buy" and p.quantity == 19 and p.price_usd == 52.5  # 1000 // 52.5


def test_buy_warns_when_usd_short(tmp_path, us):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    us.usd = 100.0
    t = _trader(tmp_path, us_budget_usd=1000)
    t.check(force=True)
    assert "외화 주문가능" in t.store.all()[0].reason


def test_no_budget_uses_all_orderable_usd(tmp_path, us):
    """예산 0 = 외화 주문가능금액 전부. 수수료 몫(0.5%)은 남긴다."""
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    us.usd = 11784.56
    t = _trader(tmp_path, us_budget_usd=0)
    assert t.check(force=True) == 1
    p = t.store.all()[0]
    assert p.quantity == 223  # 11784.56 × 0.995 // 52.5
    assert p.amount_usd <= us.usd


def test_no_budget_and_no_usd_skips(tmp_path, us):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    us.usd = 0.0
    assert _trader(tmp_path, us_budget_usd=0).check(force=True) == 0


def test_no_usd_cap_when_zero(monkeypatch):
    from aistock.us_order import UsOrderRequest, validate

    monkeypatch.setenv("MAX_ORDER_AMOUNT_USD", "0")
    validate(UsOrderRequest("buy", "TQQQ", "ND", 200, 70.0))  # $14,000 — 상한 없음


def test_no_check_before_10am(tmp_path, us, monkeypatch):
    early = datetime.now(NY).replace(hour=9, minute=45)
    monkeypatch.setattr(us_trader, "datetime", SimpleNamespace(now=lambda tz=None: early if tz is NY else datetime.now(tz)))
    us.holdings = [_held(-12)]
    assert _trader(tmp_path).check(force=True) == 0


def test_holiday_no_check(tmp_path, us, monkeypatch):
    monkeypatch.setattr(us_trader, "is_us_trading_day", lambda at=None: False)
    us.holdings = [_held(-12)]
    assert _trader(tmp_path).check(force=True) == 0


def test_throttled(tmp_path, us):
    us.holdings = [_held(-12)]
    t = _trader(tmp_path)
    t._last_check = __import__("time").monotonic()
    assert t.check() == 0


# --- 직접 입력 (/buy TQQQ ...) ---


def test_manual_creates_proposal_not_order(tmp_path, us):
    store = P.ProposalStore(tmp_path / "p.json")
    n = FakeNotifier()
    p = manual_proposal(None, n, store, POLICY, side="buy", symbol="tqqq", quantity=10, price=65.0)
    assert store.get(p.id).status == P.PENDING
    assert p.stock_code == "TQQQ" and p.source == "manual"
    assert "주문 확인" in n.sent[0][0] and n.sent[0][1]  # 버튼이 달렸다


def test_manual_rejects_over_cap(tmp_path, us, monkeypatch):
    monkeypatch.setenv("MAX_ORDER_AMOUNT_USD", "1000")
    store = P.ProposalStore(tmp_path / "p.json")
    with pytest.raises(UsOrderError, match="상한"):
        manual_proposal(None, FakeNotifier(), store, POLICY, side="buy", symbol="TQQQ",
                        quantity=100, price=68.21)
    assert not store.all()


def test_manual_rejects_bad_tick(tmp_path, us):
    with pytest.raises(UsOrderError, match="호가단위"):
        manual_proposal(None, FakeNotifier(), P.ProposalStore(tmp_path / "p.json"), POLICY,
                        side="buy", symbol="TQQQ", quantity=1, price=68.215)


# --- 승인 → 주문 ---


@pytest.fixture
def us_market(monkeypatch):
    state = SimpleNamespace(price="+68.2100", placed=[], session=True, trading_day=True)
    monkeypatch.setattr(executor, "in_us_session", lambda: state.session)
    monkeypatch.setattr(executor, "is_us_trading_day", lambda: state.trading_day)
    monkeypatch.setattr(executor.overseas, "quote", lambda c, s, e: {"cur_prc": state.price})

    def fake_place(client, request):
        state.placed.append(request)
        return SimpleNamespace(order_no="US0001")

    monkeypatch.setattr(executor.us_order, "place", fake_place)
    return state


def _us_proposal(**kw):
    base = dict(id=P.new_id(), created_at=datetime.now(KST).isoformat(timespec="seconds"),
                side="buy", stock_code="TQQQ", name="TQQQ", quantity=10, limit_price=0,
                confidence=0, reason="", market="us", exchange="ND", price_usd=68.21,
                source="trend")
    return P.Proposal(**{**base, **kw})


def _store(tmp_path, p):
    store = P.ProposalStore(tmp_path / "p.json")
    store.add(p)
    return store


def test_us_approve_places_dollar_limit_order(tmp_path, us_market):
    p = _us_proposal()
    done, msg = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.ORDERED and done.order_no == "US0001"
    [req] = us_market.placed
    assert (req.symbol, req.exchange, req.quantity, req.price) == ("TQQQ", "ND", 10, 68.21)


def test_us_double_approve_orders_once(tmp_path, us_market):
    p = _us_proposal()
    store = _store(tmp_path, p)
    executor.approve(None, store, p.id, POLICY)
    executor.approve(None, store, p.id, POLICY)
    assert len(us_market.placed) == 1


def test_us_outside_session_not_ordered(tmp_path, us_market):
    us_market.session = False
    p = _us_proposal()
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, TradingPolicy(us_extended_hours=False))
    assert done.status == P.STALE and not us_market.placed


def test_us_holiday_not_ordered(tmp_path, us_market):
    us_market.trading_day = False
    p = _us_proposal()
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, TradingPolicy(us_extended_hours=False))
    assert done.status == P.STALE and not us_market.placed


def test_us_drift_blocks_auto_proposal(tmp_path, us_market):
    us_market.price = "+70.5000"  # +3.4%
    p = _us_proposal()
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.STALE and not us_market.placed


def test_manual_limit_below_market_is_allowed(tmp_path, us_market):
    """사람이 일부러 현재가보다 낮게 걸어둔 매수는 '가격 이탈'이 아니다."""
    p = _us_proposal(source="manual", price_usd=65.0)
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.ORDERED and us_market.placed[0].price == 65.0


def test_us_expired_not_ordered(tmp_path, us_market):
    old = (datetime.now(KST) - timedelta(minutes=30)).isoformat()
    p = _us_proposal(created_at=old)
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.EXPIRED and not us_market.placed


# --- 액면분할 ---


def test_split_adjustment():
    """키움 미국 일봉은 분할 수정이 안 돼 있다. TQQQ 2025-11-20 2:1 분할이 실제 사례."""
    from aistock.backtest.prices import Bar
    from aistock.trading.us_trader import split_adjusted_closes

    bars = [Bar("20251118", 98, 99, 97, 98.36, 0), Bar("20251119", 99, 101, 98, 100.05, 0),
            Bar("20251120", 52.95, 53, 46, 46.45, 0), Bar("20251121", 46.5, 48, 46, 47.2, 0)]
    adj = split_adjusted_closes(bars)
    assert adj == pytest.approx([49.18, 50.025, 46.45, 47.2])


def test_normal_big_move_not_treated_as_split():
    from aistock.backtest.prices import Bar
    from aistock.trading.us_trader import split_adjusted_closes

    bars = [Bar("1", 100, 100, 100, 100, 0), Bar("2", 80, 82, 75, 78, 0)]  # -20% 갭
    assert split_adjusted_closes(bars) == [100, 78]


# --- 매수 신호 가격 · 1분 점검 ---


def _pullback_setup():
    closes = _uptrend()
    closes[-14:] = [closes[-15] - i * 0.4 for i in range(14)]
    return closes


def test_trigger_price_matches_rule():
    """알려준 가격에서 실제로 매수 신호가 켜지고, 1센트 위에서는 안 켜져야 한다."""
    from aistock.trading.us_trader import buy_triggers

    closes = _pullback_setup()
    trig = buy_triggers(closes, POLICY)["pullback"]
    assert decide(closes, trig, POLICY, holding_rate=None).side == "buy"
    assert decide(closes, trig + 0.02, POLICY, holding_rate=None).side is None


def test_breakout_trigger_when_below_line():
    from aistock.trading.us_trader import buy_triggers

    closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    trig = buy_triggers(closes, POLICY)["breakout"]
    assert trig == pytest.approx(51.5, abs=0.05)
    assert decide(closes, trig + 0.05, POLICY, holding_rate=None).side == "buy"


def test_no_triggers_without_history():
    from aistock.trading.us_trader import buy_triggers

    assert buy_triggers([50.0] * 30, POLICY) == {}


def test_checks_every_minute(tmp_path, us):
    import time as _t

    us.holdings = [_held(-12)]
    t = _trader(tmp_path)
    t._last_check = _t.monotonic() - 61
    assert t.check() == 1


def test_expired_buy_realerts_after_wait(tmp_path, us, monkeypatch):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    t = _trader(tmp_path, us_realert_min=15)
    t.check(force=True)
    first = t.store.all()[0]
    just_now = us.now.astimezone(KST) - timedelta(minutes=5)
    t.store.update(first.id, status=P.EXPIRED, created_at=just_now.isoformat(timespec="seconds"))
    assert t.check(force=True) == 0  # 만료된 지 얼마 안 됨 — 아직 안 보낸다

    old = us.now.astimezone(KST) - timedelta(minutes=20)
    t.store.update(first.id, created_at=old.isoformat(timespec="seconds"))
    assert t.check(force=True) == 1


def test_rejected_buy_not_realerted(tmp_path, us):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    t = _trader(tmp_path)
    t.check(force=True)
    first = t.store.all()[0]
    old = (datetime.now(KST) - timedelta(minutes=60)).isoformat(timespec="seconds")
    t.store.update(first.id, status=P.REJECTED, created_at=old)
    assert t.check(force=True) == 0


# --- 지정가 걸어두기 ---


def test_resting_proposed_when_waiting_for_pullback(tmp_path, us):
    """신호 전(관망)이면 신호 가격에 외화 전액 지정가를 걸어둘지 묻는다."""
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    trig = buy_triggers(us.closes, POLICY)["pullback"]
    us.price = f"+{trig + 1.0:.4f}"  # 신호 가격보다 $1 위 — 아직 관망
    us.usd = 10_000.0
    t = _trader(tmp_path, us_budget_usd=0)
    assert t.check(force=True) == 1
    [p] = t.store.all()
    assert p.source == "resting" and p.price_usd == trig
    assert p.quantity == int(10_000 * 0.995 // trig)
    assert "지정가 걸어두기" in t.notifier.sent[0][0]


def test_resting_once_per_day(tmp_path, us):
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    us.price = f"+{buy_triggers(us.closes, POLICY)['pullback'] + 1.0:.4f}"
    t = _trader(tmp_path)
    t.check(force=True)
    executor.reject(t.store, t.store.all()[0].id)
    assert t.check(force=True) == 0


def test_no_resting_when_holding(tmp_path, us):
    us.closes = _pullback_setup()
    us.holdings = [_held(3)]
    t = _trader(tmp_path)
    t.check(force=True)
    assert not [p for p in t.store.all() if p.source == "resting"]


def test_holding_pullback_proposes_add_buy(tmp_path, us):
    """보유 중이어도 상승 추세 중 눌림이면 추가 매수 시점을 알린다."""
    us.closes = _pullback_setup()
    us.holdings = [_held(3)]
    t = _trader(tmp_path)
    assert t.check(force=True) == 1
    [p] = t.store.all()
    assert p.side == "buy" and p.source == "add" and "추가 매수" in p.reason


def test_add_buy_can_be_disabled(tmp_path, us):
    us.closes = _pullback_setup()
    us.holdings = [_held(3)]
    assert _trader(tmp_path, add_on_pullback=False).check(force=True) == 0


def test_no_resting_in_downtrend(tmp_path, us):
    us.closes = [90 - i * 0.15 + (1 if i % 2 else -1) for i in range(240)]
    us.price = f"+{us.closes[-1]:.4f}"
    assert _trader(tmp_path).check(force=True) == 0


def test_no_signal_buy_when_resting_order_placed(tmp_path, us):
    """걸어둔 지정가가 있으면 신호가 켜져도 또 사자고 하지 않는다 — 두 번 사면 외화가 두 배로 묶인다."""
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"  # 돌파 매수 신호
    t = _trader(tmp_path)
    t.store.add(_us_proposal(source="resting", status=P.ORDERED,
                             created_at=us.now.astimezone(KST).isoformat(timespec="seconds")))
    assert t.check(force=True) == 0


def test_no_signal_buy_when_manual_open_order(tmp_path, us):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    us.open_orders = [SimpleNamespace(symbol="TQQQ", side="매수")]
    assert _trader(tmp_path).check(force=True) == 0


def test_resting_approval_ignores_drift(tmp_path, us_market):
    """걸어두기는 일부러 현재가보다 낮게 거는 주문이다. '가격 이탈'로 막으면 안 된다."""
    us_market.price = "+72.0000"
    p = _us_proposal(source="resting", price_usd=70.83)
    done, msg = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.ORDERED and "지정가 주문을 걸었습니다" in msg
    assert us_market.placed[0].price == 70.83


def test_resting_can_be_disabled(tmp_path, us):
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    us.price = f"+{buy_triggers(us.closes, POLICY)['pullback'] + 1.0:.4f}"
    assert _trader(tmp_path, us_resting=False).check(force=True) == 0


def test_failed_resting_reproposed_after_wait(tmp_path, us):
    """승인했는데 주문이 실패하면, 그날을 통째로 막지 않고 잠시 뒤 다시 제안한다."""
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    us.price = f"+{buy_triggers(us.closes, POLICY)['pullback'] + 1.0:.4f}"
    t = _trader(tmp_path)
    t.check(force=True)
    first = t.store.all()[0]
    just = us.now.astimezone(KST) - timedelta(minutes=2)
    t.store.update(first.id, status=P.FAILED, created_at=just.isoformat(timespec="seconds"))
    assert t.check(force=True) == 0
    old = us.now.astimezone(KST) - timedelta(minutes=20)
    t.store.update(first.id, created_at=old.isoformat(timespec="seconds"))
    assert t.check(force=True) == 1


# --- 전량 ---


def test_manual_buy_all_uses_broker_quantity(tmp_path, us):
    us.usd = 11784.56
    store = P.ProposalStore(tmp_path / "p.json")
    p = manual_proposal(None, FakeNotifier(), store, POLICY, side="buy", symbol="TQQQ",
                        quantity=None, price=70.83)
    assert p.quantity == int(11784.56 * 0.995 // 70.83) and "전량" in p.reason


def test_manual_buy_all_without_price_uses_current(tmp_path, us):
    us.price = "+71.1520"
    store = P.ProposalStore(tmp_path / "p.json")
    p = manual_proposal(None, FakeNotifier(), store, POLICY, side="buy", symbol="TQQQ",
                        quantity=None, price=None)
    assert p.price_usd == 71.16  # 매수는 센트 올림


def test_manual_buy_all_with_no_cash_explains(tmp_path, us):
    """미체결 주문이 외화를 묶으면 0주다. 왜 0인지 알려줘야 한다."""
    us.usd = 26.06
    with pytest.raises(UsOrderError, match="미체결"):
        manual_proposal(None, FakeNotifier(), P.ProposalStore(tmp_path / "p.json"), POLICY,
                        side="buy", symbol="TQQQ", quantity=None, price=70.83)


def test_manual_sell_all_uses_sellable(tmp_path, us):
    us.holdings = [_held(5, qty=180)]
    us.price = "+75.0090"
    p = manual_proposal(None, FakeNotifier(), P.ProposalStore(tmp_path / "p.json"), POLICY,
                        side="sell", symbol="TQQQ", quantity=None, price=None)
    assert p.quantity == 180 and p.price_usd == 75.0  # 매도는 센트 내림


def test_manual_sell_all_without_holding(tmp_path, us):
    with pytest.raises(UsOrderError, match="보유"):
        manual_proposal(None, FakeNotifier(), P.ProposalStore(tmp_path / "p.json"), POLICY,
                        side="sell", symbol="TQQQ", quantity=None, price=None)


def test_bot_parses_all_word(tmp_path, monkeypatch):
    from aistock import bot as B

    seen = {}
    monkeypatch.setattr(us_trader, "manual_proposal", lambda *a, **kw: seen.update(kw))
    ctx = SimpleNamespace(kiwoom=None, notifier=None, store=None, policy=POLICY)
    assert B._us_order_proposal(ctx, "buy", ["TQQQ", "전량"]) == ""
    assert seen["quantity"] is None and seen["price"] is None
    B._us_order_proposal(ctx, "sell", ["tqqq", "all", "75.5"])
    assert seen["quantity"] is None and seen["price"] == 75.5
    assert "형식" in B._us_order_proposal(ctx, "buy", ["TQQQ", "10"])  # 숫자 수량은 가격 필수


def test_realert_only_once_per_day(tmp_path, us):
    """만료될 때마다 15분 뒤 다시 보내면 밤새 20번 넘게 온다 (2026-10-20 실제 사례)."""
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    us.price = f"+{buy_triggers(us.closes, POLICY)['pullback'] + 1.0:.4f}"
    t = _trader(tmp_path)
    old = us.now.astimezone(KST) - timedelta(minutes=40)
    for minutes in (40, 20):
        t.check(force=True)
        newest = t.store.all()[-1]
        stamp = us.now.astimezone(KST) - timedelta(minutes=minutes)
        t.store.update(newest.id, status=P.EXPIRED, created_at=stamp.isoformat(timespec="seconds"))
    assert len(t.store.all()) == 2
    assert t.check(force=True) == 0


def test_extended_hours_order_goes_to_broker(tmp_path, us_market):
    """장외에도 키움은 일반 지정가를 받는다 (프리마켓). 봇이 미리 막지 않는다."""
    us_market.session = False
    p = _us_proposal(source="manual", price_usd=65.0)
    done, msg = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.ORDERED and len(us_market.placed) == 1
    assert "장외 주문" in msg


def test_extended_hours_rejection_shows_broker_reason(tmp_path, us_market, monkeypatch):
    from aistock.errors import APIError

    us_market.session = False

    def reject(client, request):
        raise APIError(20, "[2000](주문 가능 시간이 아닙니다)", "ust20000")

    monkeypatch.setattr(executor.us_order, "place", reject)
    p = _us_proposal(source="manual", price_usd=65.0)
    done, msg = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert done.status == P.FAILED and "주문 가능 시간이 아닙니다" in msg


# --- 장종료 → 예약 ---


def _closed():
    from aistock.errors import APIError

    return APIError(20, "[2000](505217:장종료 되었습니다.)", "ust20000")


def test_market_closed_failure_is_reservable(tmp_path, us_market, monkeypatch):
    """2026-10-21 08:22 KST 실제 거절. 실패로 끝내지 말고 예약할 수 있게 한다."""
    monkeypatch.setattr(executor.us_order, "place", lambda c, r: (_ for _ in ()).throw(_closed()))
    p = _us_proposal(source="manual", price_usd=70.5, quantity=55)
    store = _store(tmp_path, p)
    done, msg = executor.approve(None, store, p.id, POLICY)
    assert done.status == P.FAILED and "예약" in msg
    assert executor.reservable(done)


def test_other_failures_not_reservable(tmp_path, us_market, monkeypatch):
    from aistock.errors import APIError

    err = APIError(20, "[2000](573500:해외레버리지ETP교육 이수 후 주문 가능합니다.)", "ust20000")
    monkeypatch.setattr(executor.us_order, "place", lambda c, r: (_ for _ in ()).throw(err))
    p = _us_proposal(source="manual")
    done, _ = executor.approve(None, _store(tmp_path, p), p.id, POLICY)
    assert not executor.reservable(done)
    assert executor.queue(P.ProposalStore(tmp_path / "p.json"), p.id)[0].status == P.FAILED


def _queued_store(tmp_path, **kw):
    p = _us_proposal(status=P.FAILED, note="🕒 ... (장종료) ...", source="manual", **kw)
    store = _store(tmp_path, p)
    queued, _ = executor.queue(store, p.id)
    assert queued.status == P.QUEUED
    return store, p


def test_queued_waits_while_closed_then_submits(tmp_path, monkeypatch):
    store, p = _queued_store(tmp_path, price_usd=70.5, quantity=55)
    calls = []

    def place(c, r):
        calls.append(r)
        if len(calls) == 1:
            raise _closed()
        return SimpleNamespace(order_no="0001234")

    monkeypatch.setattr(executor.us_order, "place", place)
    assert executor.submit_queued(None, store) == []  # 아직 장종료 — 조용히 대기
    assert store.get(p.id).status == P.QUEUED
    [(done, msg)] = executor.submit_queued(None, store)
    assert done.status == P.ORDERED and done.order_no == "0001234" and "예약 주문 접수" in msg
    assert calls[-1].price == 70.5 and calls[-1].quantity == 55


def test_queued_other_error_fails_and_reports(tmp_path, monkeypatch):
    from aistock.errors import APIError

    store, p = _queued_store(tmp_path)
    monkeypatch.setattr(
        executor.us_order, "place",
        lambda c, r: (_ for _ in ()).throw(APIError(20, "잔고 부족", "ust20000")),
    )
    [(done, msg)] = executor.submit_queued(None, store)
    assert done.status == P.FAILED and "잔고 부족" in msg


def test_queued_expires_after_24h(tmp_path, monkeypatch):
    store, p = _queued_store(tmp_path)
    old = (datetime.now(KST) - timedelta(hours=25)).isoformat(timespec="seconds")
    store.update(p.id, queued_at=old)
    monkeypatch.setattr(executor.us_order, "place", lambda c, r: pytest.fail("만료된 예약을 주문했다"))
    [(done, msg)] = executor.submit_queued(None, store)
    assert done.status == P.EXPIRED and "만료" in msg


def test_cancelled_reservation_not_submitted(tmp_path, monkeypatch):
    store, p = _queued_store(tmp_path)
    executor.cancel_queued(store, p.id)
    monkeypatch.setattr(executor.us_order, "place", lambda c, r: pytest.fail("취소한 예약을 주문했다"))
    assert executor.submit_queued(None, store) == []


def test_reservation_blocks_duplicate_buy_proposal(tmp_path, us):
    """예약해 둔 매수가 있으면 신호가 떠도 또 사자고 하지 않는다."""
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"
    t = _trader(tmp_path)
    t.store.add(_us_proposal(status=P.QUEUED,
                             created_at=us.now.astimezone(KST).isoformat(timespec="seconds")))
    assert t.check(force=True) == 0


def test_duplicate_reservation_refused(tmp_path):
    """08:22에 실패한 메시지와 08:40에 다시 보낸 메시지가 같은 주문 — 둘 다 예약하면 두 번 산다."""
    store = P.ProposalStore(tmp_path / "p.json")
    first = _us_proposal(status=P.FAILED, note="(장종료)", source="manual", quantity=55, price_usd=70.5)
    second = _us_proposal(status=P.FAILED, note="(장종료)", source="manual", quantity=55, price_usd=70.5)
    store.add(first)
    store.add(second)
    assert executor.queue(store, first.id)[0].status == P.QUEUED
    p, msg = executor.queue(store, second.id)
    assert p.status == P.FAILED and "중복" in msg


def test_cancel_marks_proposal_cancelled(tmp_path):
    """취소한 주문이 '주문됨'으로 남으면 봇이 매수 진행 중으로 보고 자동 제안을 멈춘다."""
    store = P.ProposalStore(tmp_path / "p.json")
    p = _us_proposal(status=P.ORDERED, order_no="000012678")
    store.add(p)
    assert store.mark_cancelled("000012678").status == P.CANCELLED
    assert store.mark_cancelled("000012678") is None


def test_held_non_target_symbol_is_watched(tmp_path, us):
    """자동매매 대상(TQQQ)이 아니어도 보유 중인 미국 종목은 손절 등을 실시간으로 본다."""
    us.holdings = [SimpleNamespace(symbol="ETHU", quantity=10, sellable=10, profit_rate=-15)]
    t = _trader(tmp_path, us_symbols=("TQQQ",))
    t.check(force=True)
    ethu = [(p.stock_code, p.source) for p in t.store.all() if p.stock_code == "ETHU"]
    assert ethu == [("ETHU", "stop_loss")]


def test_stop_loss_works_without_history():
    """신규상장처럼 일봉이 모자라도 손절·익절은 수익률만으로 판단한다."""
    assert decide([50.0] * 30, 45.0, POLICY, holding_rate=-12).source == "stop_loss"
    assert decide([50.0] * 30, 60.0, POLICY, holding_rate=25).source == "take_profit"
    assert decide([50.0] * 30, 50.0, POLICY, holding_rate=1).side is None


# --- 매수 보류 (경제 일정·기초지수) ---


def test_blocked_buy_sends_nothing(tmp_path, us, monkeypatch):
    us.closes = [50.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
    us.price = "+52.5000"  # 200일선 돌파 매수 신호
    monkeypatch.setattr(us_trader, "buy_block", lambda *a, **k: "FOMC 10/28 14:00 ET 앞")
    t = _trader(tmp_path)
    assert t.check(force=True) == 0 and t.store.all() == []


def test_blocked_resting_sends_nothing(tmp_path, us, monkeypatch):
    from aistock.trading.us_trader import buy_triggers

    us.closes = _pullback_setup()
    us.price = f"+{buy_triggers(us.closes, POLICY)['pullback'] + 1.0:.4f}"
    monkeypatch.setattr(us_trader, "buy_block", lambda *a, **k: "기초지수 하락 추세")
    assert _trader(tmp_path).check(force=True) == 0


def test_block_does_not_stop_stop_loss(tmp_path, us, monkeypatch):
    us.holdings = [_held(-12)]
    monkeypatch.setattr(us_trader, "buy_block", lambda *a, **k: "FOMC")
    t = _trader(tmp_path)
    assert t.check(force=True) == 1 and t.store.all()[0].side == "sell"
