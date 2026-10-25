"""반자동 매매 검증. 키움·텔레그램을 호출하지 않는다.

실계좌 주문이 걸린 경로라 '주문이 나가면 안 되는 경우'를 특히 많이 고정한다.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aistock.backtest.prices import Bar, PriceHistory
from aistock.bot import TelegramBot
from aistock.dart.disclosure import Disclosure
from aistock.market_hours import KST
from aistock.notify.telegram import TelegramConfig
from aistock.pipeline.events import DisclosureEvent, Stage
from aistock.signal.schema import Action, DisclosureSignal, Horizon, Impact
from aistock.trading import executor, proposer
from aistock.trading import proposals as P
from aistock.trading.indicators import rsi, sma
from aistock.trading.policy import TradingPolicy
from aistock.trading.sizing import quantity_for, round_to_tick, tick_size
from aistock.trading.technical import evaluate

# --- 지표 ---


def test_sma():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 3) is None


def test_rsi_extremes():
    assert rsi(list(range(1, 30))) == 100.0  # 오르기만 함
    assert rsi(list(range(30, 1, -1))) == pytest.approx(0.0)
    assert rsi([5.0] * 20) == 50.0  # 변동 없음
    assert rsi([1, 2, 3]) is None


# --- 기술적 필터 ---


def _zigzag_up(n=80, start=200_000, step=400, swing=3_000):
    """오르되 출렁이는 추세. 직선으로 오르면 RSI가 100이라 '과열'로 막힌다."""
    return [start + i * step + (swing if i % 2 else -swing) for i in range(n)]


def test_uptrend_passes():
    closes = _zigzag_up()
    view = evaluate(closes, closes[-1], 1.0)
    assert view.buy_ok, view.blocks


def test_downtrend_blocks():
    closes = [200 - i for i in range(80)]
    view = evaluate(closes, 119, -0.5)
    assert any("하락 추세" in b for b in view.blocks)


def test_chasing_blocks():
    closes = [100.0 + (i % 5) for i in range(80)]
    view = evaluate(closes, 118, 16.0)
    assert any("급등" in b for b in view.blocks)


def test_too_few_bars_blocks():
    """신규상장주는 추세를 볼 수 없다. 모르면 사지 않는다."""
    assert not evaluate([100.0] * 10, 100, 0).buy_ok


# --- 호가단위 ---


@pytest.mark.parametrize(
    "price,tick", [(1_999, 1), (2_000, 5), (19_990, 10), (49_950, 50), (199_900, 100),
                   (200_000, 500), (500_000, 1_000)]
)
def test_tick_size(price, tick):
    assert tick_size(price) == tick


def test_round_to_tick():
    assert round_to_tick(230_050, up=True) == 230_500
    assert round_to_tick(230_050, up=False) == 230_000
    assert round_to_tick(230_000, up=True) == 230_000
    assert round_to_tick(1_999.5, up=True) == 2_000
    assert round_to_tick(4_998, up=True) == 5_000


def test_quantity_for():
    assert quantity_for(1_000_000, 230_000) == 4
    assert quantity_for(100_000, 230_000) == 0


# --- 저장소 ---


def _proposal(**kw) -> P.Proposal:
    base = dict(
        id=P.new_id(),
        created_at=datetime.now(KST).isoformat(timespec="seconds"),
        side="buy",
        stock_code="005930",
        name="삼성전자",
        quantity=4,
        limit_price=230_000,
        confidence=80,
        reason="자기주식취득결정",
    )
    return P.Proposal(**{**base, **kw})


def test_store_round_trip(tmp_path):
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    assert store.get(p.id) == p
    assert store.open_for("005930", "buy") == p
    assert store.open_for("005930", "sell") is None


def test_transition_only_once(tmp_path):
    """승인 버튼을 두 번 눌러도 executing 전이는 한 번만 성공한다 — 중복 주문 방지의 핵심."""
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    assert store.transition(p.id, P.PENDING, P.EXECUTING) is not None
    assert store.transition(p.id, P.PENDING, P.EXECUTING) is None


def test_old_closed_proposals_pruned(tmp_path):
    store = P.ProposalStore(tmp_path / "p.json")
    old = (datetime.now(KST) - timedelta(days=60)).isoformat()
    store.add(_proposal(id="old", created_at=old, status=P.ORDERED))
    store.add(_proposal(id="new"))
    assert [p.id for p in store.all()] == ["new"]


# --- 승인 → 주문 ---


@pytest.fixture
def market(monkeypatch):
    """장중 · 현재가 230,000 · place()는 호출 기록만 남긴다."""
    state = SimpleNamespace(price="+230000", placed=[], session=True)
    monkeypatch.setattr(executor, "in_session", lambda: state.session)
    monkeypatch.setattr(
        executor.domestic, "stock_info", lambda client, code: {"cur_prc": state.price}
    )

    def fake_place(client, request):
        state.placed.append(request)
        return SimpleNamespace(order_no="0001234")

    monkeypatch.setattr(executor, "place", fake_place)
    return state


def test_approve_places_limit_order(tmp_path, market):
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    done, msg = executor.approve(None, store, p.id, TradingPolicy())
    assert done.status == P.ORDERED and done.order_no == "0001234"
    assert len(market.placed) == 1
    assert market.placed[0].price == 230_000 and market.placed[0].quantity == 4


def test_double_approve_orders_once(tmp_path, market):
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    executor.approve(None, store, p.id, TradingPolicy())
    _, msg = executor.approve(None, store, p.id, TradingPolicy())
    assert len(market.placed) == 1
    assert "이미 처리" in msg


def test_expired_proposal_not_ordered(tmp_path, market):
    store = P.ProposalStore(tmp_path / "p.json")
    old = (datetime.now(KST) - timedelta(minutes=30)).isoformat()
    p = _proposal(created_at=old)
    store.add(p)
    done, _ = executor.approve(None, store, p.id, TradingPolicy(proposal_ttl_min=10))
    assert done.status == P.EXPIRED and not market.placed


def test_price_drift_blocks_buy(tmp_path, market):
    """제안 후 3% 올랐으면 판단 근거가 달라진 것이다."""
    market.price = "+236900"
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    done, _ = executor.approve(None, store, p.id, TradingPolicy(max_drift_pct=2.0))
    assert done.status == P.STALE and not market.placed


def test_price_drop_does_not_block_buy(tmp_path, market):
    market.price = "-220000"  # 부호는 전일대비 방향. 가격은 220,000
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    done, _ = executor.approve(None, store, p.id, TradingPolicy())
    assert done.status == P.ORDERED


def test_outside_session_not_ordered(tmp_path, market):
    market.session = False
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    done, _ = executor.approve(None, store, p.id, TradingPolicy())
    assert done.status == P.STALE and not market.placed


def test_reject_then_approve_does_nothing(tmp_path, market):
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal()
    store.add(p)
    executor.reject(store, p.id)
    done, _ = executor.approve(None, store, p.id, TradingPolicy())
    assert done.status == P.REJECTED and not market.placed


def test_expire_stale(tmp_path):
    store = P.ProposalStore(tmp_path / "p.json")
    old = (datetime.now(KST) - timedelta(minutes=30)).isoformat()
    store.add(_proposal(id="a", created_at=old))
    store.add(_proposal(id="b"))
    expired = executor.expire_stale(store, TradingPolicy(proposal_ttl_min=10))
    assert [p.id for p in expired] == ["a"]
    assert store.get("b").status == P.PENDING


# --- 신호 → 제안 ---


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, *, buttons=None, preview=False):
        self.sent.append((text, buttons))
        return 777


class FakePrices:
    def __init__(self, closes):
        self.history = PriceHistory(
            "005930", [Bar(f"2026{i:04d}", c, c, c, c, 1) for i, c in enumerate(closes)]
        )

    def get(self, code):
        return self.history


def _event(code="005930"):
    d = Disclosure(f"20261017{code}", "00126380", "삼성전자", code, "Y",
                   "주요사항보고서(자기주식취득결정)", "삼성전자", "20261017", "")
    return DisclosureEvent(Stage.DOCUMENT, d, datetime.now(KST), text="원문")


def _signal(action=Action.BUY, confidence=80):
    return DisclosureSignal(
        action=action, confidence=confidence, impact=Impact.POSITIVE, horizon=Horizon.DAYS,
        event_type="자기주식취득", rationale="대규모 자사주 매입.",
    )


@pytest.fixture
def live(monkeypatch):
    state = SimpleNamespace(holdings=[], quote={"stk_nm": "삼성전자", "cur_prc": "+230000",
                                                 "flu_rt": "+1.20"})
    monkeypatch.setattr(proposer, "in_session", lambda at=None: True)
    monkeypatch.setattr(proposer, "is_trading_day", lambda c, at=None: True)
    monkeypatch.setattr(proposer.domestic, "stock_info", lambda c, code: state.quote)
    monkeypatch.setattr(
        proposer, "fetch_balance", lambda c: SimpleNamespace(holdings=state.holdings)
    )
    # 일봉은 테스트용 가짜 가격(FakePrices)에서 — 실제로는 하루 한 번 키움에서 새로 받는다
    monkeypatch.setattr(
        proposer.TradeProposer, "_daily_closes",
        lambda self, code, now: [b.close for b in self.prices.get(code).bars],
    )
    return state


def _proposer(tmp_path, closes=None, **policy):
    closes = closes or _zigzag_up()
    return proposer.TradeProposer(
        None, FakeNotifier(), store=P.ProposalStore(tmp_path / "p.json"),
        policy=TradingPolicy(**policy), prices=FakePrices(closes),
    )


def test_buy_signal_sends_proposal_with_buttons(tmp_path, live):
    tp = _proposer(tmp_path)
    tp(_event(), _signal())
    [(text, buttons)] = tp.notifier.sent
    assert "매수 제안" in text
    [p] = tp.store.all()
    assert p.status == P.PENDING and p.message_id == 777
    assert p.quantity == 4 and p.limit_price == 230_000
    assert buttons[0][0][1] == f"tp:ok:{p.id}"


def test_low_confidence_ignored(tmp_path, live):
    tp = _proposer(tmp_path)
    tp(_event(), _signal(confidence=60))
    assert not tp.notifier.sent and not tp.store.all()


def test_technical_block_records_without_buttons(tmp_path, live):
    tp = _proposer(tmp_path, closes=[300_000 - i * 1_000 for i in range(80)])
    tp(_event(), _signal())
    assert not tp.notifier.sent
    [p] = tp.store.all()
    assert p.status == P.BLOCKED and p.blocks


def test_already_holding_blocks_buy(tmp_path, live):
    live.holdings = [SimpleNamespace(stock_code="005930", quantity=3, sellable=3)]
    tp = _proposer(tmp_path)
    tp(_event(), _signal())
    assert tp.store.all()[0].status == P.BLOCKED


def test_daily_buy_limit(tmp_path, live):
    tp = _proposer(tmp_path, max_daily_buys=1)
    tp(_event("005930"), _signal())
    tp(_event("000660"), _signal())
    statuses = [p.status for p in tp.store.all()]
    assert statuses == [P.PENDING, P.BLOCKED]


def test_expired_buys_do_not_use_up_daily_limit(tmp_path, live):
    """아침에 못 보고 만료된 제안 3건이 하루 종일 다른 제안을 막으면 안 된다."""
    tp = _proposer(tmp_path, max_daily_buys=1)
    tp(_event("005930"), _signal())
    first = tp.store.all()[0]
    tp.store.update(first.id, status=P.EXPIRED)
    tp(_event("000660"), _signal())
    assert tp.store.all()[1].status == P.PENDING


def test_us_proposals_do_not_count_toward_kr_limit(tmp_path, live):
    tp = _proposer(tmp_path, max_daily_buys=1)
    tp.store.add(_proposal(id="us1", stock_code="TQQQ", market="us", exchange="ND", price_usd=70.0))
    tp(_event("005930"), _signal())
    assert tp.store.get("us1").status == P.PENDING
    assert [p.status for p in tp.store.all() if not p.is_us] == [P.PENDING]


def test_duplicate_disclosure_not_reproposed(tmp_path, live):
    """[기재정정]·[첨부추가]로 같은 사건이 여러 번 온다."""
    tp = _proposer(tmp_path)
    tp(_event(), _signal())
    tp(_event(), _signal())
    assert len(tp.store.all()) == 1


def test_sell_only_when_holding(tmp_path, live):
    tp = _proposer(tmp_path)
    tp(_event(), _signal(Action.SELL))
    assert not tp.store.all()

    live.holdings = [SimpleNamespace(stock_code="005930", quantity=5, sellable=5)]
    tp(_event(), _signal(Action.SELL))
    [p] = tp.store.all()
    assert p.side == "sell" and p.quantity == 5 and p.status == P.PENDING


def test_sell_not_blocked_by_downtrend(tmp_path, live):
    """악재에 차트까지 나쁘면 더더욱 팔아야 한다."""
    live.holdings = [SimpleNamespace(stock_code="005930", quantity=5, sellable=5)]
    tp = _proposer(tmp_path, closes=[300_000 - i * 1_000 for i in range(80)])
    tp(_event(), _signal(Action.SELL))
    assert tp.store.all()[0].status == P.PENDING


def test_after_hours_signal_is_deferred(tmp_path, live, monkeypatch):
    """장 마감 후 신호는 버튼 없이 보관만 한다 — 그 가격으로 내일 사면 안 된다."""
    monkeypatch.setattr(proposer, "in_session", lambda at=None: False)
    tp = _proposer(tmp_path)
    tp(_event(), _signal())
    [p] = tp.store.all()
    assert p.status == P.DEFERRED and p.ref_price == 230_000
    [(text, buttons)] = tp.notifier.sent
    assert buttons is None and "장외" in text


def _at(monkeypatch, hh, mm, days_ago=0):
    t = (datetime.now(KST) - timedelta(days=days_ago)).replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )
    monkeypatch.setattr(proposer, "_now", lambda: t)
    return t


def _deferred(tp, monkeypatch, *, ref="+230000"):
    """어제 18:00에 나온 매수 신호를 보관해 둔다."""
    monkeypatch.setattr(proposer, "in_session", lambda at=None: False)
    _at(monkeypatch, 18, 0, days_ago=1)
    tp(_event(), _signal())
    monkeypatch.setattr(proposer, "in_session", lambda at=None: True)


def test_deferred_released_after_open(tmp_path, live, monkeypatch):
    tp = _proposer(tmp_path)
    _deferred(tp, monkeypatch)
    _at(monkeypatch, 9, 1)
    assert tp.release_deferred() == 0  # 09:05 전에는 동시호가 여파를 피한다
    _at(monkeypatch, 9, 6)
    assert tp.release_deferred() == 1
    [p] = tp.store.all()
    assert p.status == P.PENDING and p.quantity == 4
    assert p.created_at[:10] != p.signal_at[:10]  # 승인 유효시간은 오늘부터
    assert "재평가" in tp.notifier.sent[-1][0]


def test_deferred_gap_up_blocked(tmp_path, live, monkeypatch):
    tp = _proposer(tmp_path)
    _deferred(tp, monkeypatch)
    live.quote = {**live.quote, "cur_prc": "+245000"}  # +6.5% 갭
    _at(monkeypatch, 9, 10)
    tp.release_deferred()
    [p] = tp.store.all()
    assert p.status == P.BLOCKED and any("갭상승" in b for b in p.blocks)


def test_signal_after_close_today_waits_for_tomorrow(tmp_path, live, monkeypatch):
    tp = _proposer(tmp_path)
    monkeypatch.setattr(proposer, "in_session", lambda at=None: False)
    _at(monkeypatch, 16, 0)
    tp(_event(), _signal())
    monkeypatch.setattr(proposer, "in_session", lambda at=None: True)
    _at(monkeypatch, 15, 0)  # 같은 날 (테스트용으로 시계를 되감음)
    assert tp.release_deferred() == 0
    assert tp.store.all()[0].status == P.DEFERRED


def test_old_deferred_expires(tmp_path, live, monkeypatch):
    tp = _proposer(tmp_path, deferred_max_days=3)
    monkeypatch.setattr(proposer, "in_session", lambda at=None: False)
    _at(monkeypatch, 18, 0, days_ago=5)
    tp(_event(), _signal())
    monkeypatch.setattr(proposer, "in_session", lambda at=None: True)
    _at(monkeypatch, 10, 0)
    tp.release_deferred()
    assert tp.store.all()[0].status == P.EXPIRED


def test_holiday_does_not_release(tmp_path, live, monkeypatch):
    tp = _proposer(tmp_path)
    _deferred(tp, monkeypatch)
    monkeypatch.setattr(proposer, "is_trading_day", lambda c, at=None: False)
    _at(monkeypatch, 9, 30)
    assert tp.release_deferred() == 0


# --- 손절·익절 ---


def _holding(rate, code="005930"):
    return SimpleNamespace(stock_code=code, name="삼성전자", quantity=5, sellable=5,
                           avg_price=240_000, profit_rate=rate)


def test_stop_loss_proposes_sell(tmp_path, live):
    live.holdings = [_holding(-6.2)]
    tp = _proposer(tmp_path, stop_loss_pct=5)
    assert tp.check_exits(force=True) == 1
    [p] = tp.store.all()
    assert p.source == "stop_loss" and p.side == "sell" and p.quantity == 5
    assert "손절 제안" in tp.notifier.sent[0][0]


def test_take_profit_proposes_sell(tmp_path, live):
    live.holdings = [_holding(12.0)]
    tp = _proposer(tmp_path, take_profit_pct=10)
    assert tp.check_exits(force=True) == 1
    assert tp.store.all()[0].source == "take_profit"


def test_within_band_no_exit(tmp_path, live):
    live.holdings = [_holding(-2.0), _holding(4.0, "000660")]
    assert _proposer(tmp_path).check_exits(force=True) == 0


def test_rejected_stop_loss_not_asked_again_today(tmp_path, live):
    """거절한 손절을 5분마다 다시 물으면 결국 알림을 무시하게 된다."""
    live.holdings = [_holding(-6.0)]
    tp = _proposer(tmp_path)
    tp.check_exits(force=True)
    executor.reject(tp.store, tp.store.all()[0].id)
    assert tp.check_exits(force=True) == 0


def test_exit_check_throttled(tmp_path, live):
    live.holdings = [_holding(-6.0)]
    tp = _proposer(tmp_path)
    tp._last_exit_check = __import__("time").monotonic()
    assert tp.check_exits() == 0


def test_exits_disabled(tmp_path, live):
    live.holdings = [_holding(-50.0)]
    tp = _proposer(tmp_path, stop_loss_pct=0, take_profit_pct=0)
    assert tp.check_exits(force=True) == 0


# --- 봇 버튼 ---


class CallbackNotifier:
    def __init__(self):
        self.answers, self.edits = [], []

    def answer_callback(self, cid, text=""):
        self.answers.append(text)

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text))


def test_callback_from_stranger_ignored(tmp_path, market):
    bot = TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal(message_id=5)
    store.add(p)
    ctx = SimpleNamespace(kiwoom=None, store=store, policy=TradingPolicy())
    n = CallbackNotifier()
    query = {"id": "q", "from": {"id": 999}, "message": {"chat": {"id": 999}},
             "data": f"tp:ok:{p.id}"}
    bot._dispatch(ctx, n, {"callback_query": query})
    assert not market.placed and store.get(p.id).status == P.PENDING


def test_callback_approve_edits_message(tmp_path, market):
    bot = TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    store = P.ProposalStore(tmp_path / "p.json")
    p = _proposal(message_id=5)
    store.add(p)
    ctx = SimpleNamespace(kiwoom=None, store=store, policy=TradingPolicy())
    n = CallbackNotifier()
    query = {"id": "q", "from": {"id": 111}, "message": {"chat": {"id": 111}},
             "data": f"tp:ok:{p.id}"}
    bot._dispatch(ctx, n, {"callback_query": query})
    assert len(market.placed) == 1
    assert "주문 접수" in n.answers[0]
    assert n.edits and n.edits[0][0] == 5
