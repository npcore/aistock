"""백테스팅 검증. 합성 일봉으로 돌아 네트워크가 필요 없다."""

import json

import pytest

from aistock.backtest.benchmark import INDEX_BY_MARKET, BenchmarkCache
from aistock.backtest.costs import CostModel
from aistock.backtest.engine import (
    Entry,
    Skipped,
    SkipReason,
    Trade,
    dedupe,
    evaluate,
    load_signals,
    run,
)
from aistock.backtest.prices import Bar, PriceCache, PriceHistory, parse_price
from aistock.backtest.report import Stats, render

# 20260803(월)~20260807(금), 20260810(월)~ — 주말은 빠져 있다.
DATES = ["20260803", "20260804", "20260805", "20260806", "20260807", "20260810", "20260811"]


def _history(closes: list[int], opens: list[int] | None = None) -> PriceHistory:
    opens = opens or closes
    return PriceHistory(
        "005930",
        [
            Bar(date=d, open=o, high=max(o, c), low=min(o, c), close=c, volume=1000)
            for d, o, c in zip(DATES, opens, closes, strict=True)
        ],
    )


class StubCache(PriceCache):
    def __init__(self, histories: dict[str, PriceHistory | None]) -> None:
        self._histories = histories
        self._memory = {}

    def get(self, stock_code):  # type: ignore[override]
        return self._histories.get(stock_code.split("_")[0])


def _row(**overrides):
    base = {
        "rcept_no": "20260803000001",
        "rcept_dt": "20260803",
        "stock_code": "005930",
        "corp_name": "삼성전자",
        "action": "buy",
        "confidence": 80,
        "event_type": "자기주식취득결정",
    }
    return {**base, **overrides}


# --- 가격 파싱 ---


@pytest.mark.parametrize(
    "raw,expected",
    [("246000", 246000), ("+9875", 9875), ("-10885", 10885), (" 1,234 ", 1234), ("100.0", 100)],
)
def test_parse_price_strips_direction_sign(raw, expected):
    """부호는 전일대비 방향 표시다. 음수 가격으로 읽으면 수익률이 통째로 뒤집힌다."""
    assert parse_price(raw) == expected


def test_parse_price_rejects_empty():
    with pytest.raises(ValueError):
        parse_price("  ")


# --- 거래일 탐색 ---


def test_bar_after_skips_weekend():
    """금요일 공시의 다음 거래일은 월요일이다."""
    history = _history([100] * 7)
    assert history.bar_after("20260807").date == "20260810"


def test_bar_after_excludes_the_signal_day_itself():
    history = _history([100] * 7)
    assert history.bar_after("20260803").date == "20260804"


def test_bar_on_or_after_includes_the_day_itself():
    history = _history([100] * 7)
    assert history.bar_on_or_after("20260803").date == "20260803"


def test_bar_on_or_after_rolls_forward_from_a_holiday():
    """휴장일에 접수된 공시는 다음 거래일로 밀린다."""
    history = _history([100] * 7)
    assert history.bar_on_or_after("20260808").date == "20260810"


def test_nth_after_counts_trading_days_not_calendar_days():
    history = _history([100] * 7)
    assert history.nth_after("20260803", 5).date == "20260810"


def test_nth_after_returns_none_past_the_end():
    history = _history([100] * 7)
    assert history.nth_after("20260810", 5) is None


# --- 수익률 ---


def _trade(action="buy", entry=100, exit_=110) -> Trade:
    return Trade(
        rcept_no="r",
        stock_code="005930",
        corp_name="삼성전자",
        action=action,
        confidence=80,
        event_type="e",
        signal_date="20260803",
        entry_date="20260804",
        entry_price=entry,
        exit_date="20260810",
        exit_price=exit_,
    )


def test_buy_return_follows_the_price():
    t = _trade("buy", 100, 110)
    assert t.raw_return_pct == pytest.approx(10.0)
    assert t.signed_return_pct == pytest.approx(10.0)
    assert t.hit


def test_sell_return_is_inverted():
    """하락을 맞히면 sell은 적중이다."""
    t = _trade("sell", 100, 90)
    assert t.raw_return_pct == pytest.approx(-10.0)
    assert t.signed_return_pct == pytest.approx(10.0)
    assert t.hit


def test_sell_on_a_rising_stock_is_a_miss():
    assert not _trade("sell", 100, 110).hit


def test_flat_is_not_a_hit():
    assert not _trade("buy", 100, 100).hit


# --- 진입 시점 ---


def test_next_open_entry_avoids_look_ahead():
    """공시일 종가로 들어가면 장마감 후 공시를 미리 안 셈이 된다. 기본은 다음날 시가."""
    history = _history(
        closes=[100, 120, 130, 140, 150, 160, 170], opens=[100, 110, 125, 135, 145, 155, 165]
    )
    trade = evaluate(_row(), StubCache({"005930": history}), horizon=2, entry=Entry.NEXT_OPEN)
    assert isinstance(trade, Trade)
    assert trade.entry_date == "20260804"
    assert trade.entry_price == 110  # 20260804 시가
    assert trade.exit_date == "20260806"  # 진입일로부터 2거래일
    assert trade.exit_price == 140


def test_same_close_entry_uses_the_signal_day():
    history = _history(
        closes=[100, 120, 130, 140, 150, 160, 170], opens=[100, 110, 125, 135, 145, 155, 165]
    )
    trade = evaluate(_row(), StubCache({"005930": history}), horizon=2, entry=Entry.SAME_CLOSE)
    assert isinstance(trade, Trade)
    assert trade.entry_date == "20260803"
    assert trade.entry_price == 100  # 20260803 종가
    assert trade.exit_date == "20260805"


def test_same_close_is_the_optimistic_upper_bound():
    """당일 반응이 큰 종목일수록 두 진입 방식의 격차가 벌어진다."""
    history = _history(
        closes=[100, 150, 150, 150, 150, 150, 150], opens=[100, 150, 150, 150, 150, 150, 150]
    )
    cache = StubCache({"005930": history})
    optimistic = evaluate(_row(), cache, horizon=2, entry=Entry.SAME_CLOSE)
    honest = evaluate(_row(), cache, horizon=2, entry=Entry.NEXT_OPEN)
    assert optimistic.signed_return_pct > honest.signed_return_pct


# --- 제외 처리 ---


@pytest.mark.parametrize("action", ["watch", "ignore"])
def test_non_directional_signals_are_skipped(action):
    result = evaluate(_row(action=action), StubCache({}), horizon=2)
    assert isinstance(result, Skipped)
    assert result.reason is SkipReason.NOT_DIRECTIONAL


def test_low_confidence_is_skipped():
    result = evaluate(_row(confidence=50), StubCache({}), horizon=2, min_confidence=70)
    assert result.reason is SkipReason.BELOW_CONFIDENCE


def test_missing_stock_code_is_skipped():
    result = evaluate(_row(stock_code=""), StubCache({}), horizon=2)
    assert result.reason is SkipReason.NO_STOCK_CODE


def test_missing_price_data_is_skipped():
    """상장폐지·거래정지 종목은 일봉이 없다."""
    result = evaluate(_row(), StubCache({"005930": None}), horizon=2)
    assert result.reason is SkipReason.NO_PRICE_DATA


def test_recent_signal_without_enough_future_is_skipped():
    """오늘 나온 신호는 아직 5일 뒤가 없다. 실패가 아니라 평가 불가다."""
    result = evaluate(
        _row(rcept_dt="20260810"), StubCache({"005930": _history([100] * 7)}), horizon=5
    )
    assert result.reason is SkipReason.NO_EXIT_BAR


def test_exchange_suffix_is_normalized():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    result = evaluate(_row(stock_code="005930_AL"), StubCache({"005930": history}), horizon=2)
    assert isinstance(result, Trade)


# --- 집계 ---


def test_run_splits_trades_and_skips():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    # 병합 로직과 섞이지 않도록 (종목, 날짜)를 서로 다르게 둔다.
    rows = [
        _row(),
        _row(rcept_dt="20260804", action="watch"),
        _row(rcept_dt="20260805", stock_code=""),
    ]
    trades, skipped, merged = run(rows, StubCache({"005930": history}), horizon=2)
    assert len(trades) == 1 and len(skipped) == 2 and merged == 0


def test_stats_of_empty_is_zero():
    stats = Stats.of([])
    assert stats.count == 0 and stats.hit_rate == 0.0


def test_stats_computes_hit_rate_and_mean():
    trades = [_trade("buy", 100, 110), _trade("buy", 100, 90), _trade("buy", 100, 120)]
    stats = Stats.of(trades)
    assert stats.count == 3
    assert stats.hit_rate == pytest.approx(200 / 3)  # 3건 중 2건 적중
    # mean_return은 비용 차감 후다. 비용 전 평균은 (+10 -10 +20)/3 = 6.67%.
    assert stats.mean_gross == pytest.approx(20 / 3)
    assert stats.mean_return < stats.mean_gross
    assert stats.mean_return == pytest.approx(6.42, abs=0.01)


def test_render_handles_no_trades():
    out = render(
        [], [Skipped("r", "005930", SkipReason.NO_PRICE_DATA)], horizon=5, entry="next_open"
    )
    assert "평가할 신호가 없습니다" in out
    assert "일봉 조회 불가" in out


def test_render_warns_about_short_selling():
    out = render([_trade("sell", 100, 90)], [], horizon=5, entry="next_open")
    assert "공매도" in out
    assert "슬리피지" in out


# --- 파일 로딩 ---


def test_load_signals_reads_jsonl(tmp_path):
    path = tmp_path / "signals.jsonl"
    path.write_text(
        json.dumps(_row(), ensure_ascii=False) + "\n\n" + json.dumps(_row(), ensure_ascii=False),
        encoding="utf-8",
    )
    assert len(load_signals(path)) == 2


def test_load_signals_skips_corrupt_lines(tmp_path, caplog):
    path = tmp_path / "signals.jsonl"
    path.write_text(json.dumps(_row()) + "\n{ broken\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        rows = load_signals(path)
    assert len(rows) == 1
    assert "파싱 실패" in caplog.text


def test_load_signals_missing_file_is_empty(tmp_path):
    assert load_signals(tmp_path / "nope.jsonl") == []


# --- 중복 병합 ---


def test_dedupe_merges_same_stock_same_day():
    """한 회사가 하루에 공시를 여러 건 내면 진입·청산이 같아 표본만 부풀린다."""
    rows = [
        _row(rcept_no="20260803000001", confidence=60),
        _row(rcept_no="20260803000002", confidence=90),
    ]
    merged, dropped = dedupe(rows)
    assert dropped == 1
    assert len(merged) == 1
    assert merged[0]["confidence"] == 90  # confidence가 높은 쪽을 남긴다


def test_dedupe_keeps_different_days():
    rows = [_row(rcept_dt="20260803"), _row(rcept_dt="20260804")]
    merged, dropped = dedupe(rows)
    assert dropped == 0 and len(merged) == 2


def test_dedupe_keeps_different_stocks():
    rows = [_row(stock_code="005930"), _row(stock_code="035720")]
    merged, dropped = dedupe(rows)
    assert dropped == 0 and len(merged) == 2


def test_dedupe_tie_breaks_deterministically():
    """confidence가 같으면 접수번호가 늦은 쪽 — 실행할 때마다 결과가 달라지면 안 된다."""
    a = _row(rcept_no="20260803000001", confidence=75)
    b = _row(rcept_no="20260803000009", confidence=75)
    assert dedupe([a, b])[0][0]["rcept_no"] == "20260803000009"
    assert dedupe([b, a])[0][0]["rcept_no"] == "20260803000009"


def test_run_reports_merged_count():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    rows = [_row(rcept_no="20260803000001"), _row(rcept_no="20260803000002")]
    trades, _, merged = run(rows, StubCache({"005930": history}), horizon=2)
    assert merged == 1 and len(trades) == 1


def test_run_can_keep_duplicates():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    rows = [_row(rcept_no="20260803000001"), _row(rcept_no="20260803000002")]
    trades, _, merged = run(rows, StubCache({"005930": history}), horizon=2, deduplicate=False)
    assert merged == 0 and len(trades) == 2


# --- 거래비용 ---


def test_stt_is_charged_on_sale_only():
    """국내 주식 증권거래세는 매도할 때만 붙는다."""
    cost = CostModel(commission_rate=0.0, slippage_rate=0.0)
    # 가격이 그대로여도 매도 시 세금만큼 손실이 난다.
    assert cost.net_return_pct("buy", 100, 100, "Y") == pytest.approx(-0.20, abs=0.001)


def test_commission_applies_to_both_legs():
    cost = CostModel(commission_rate=0.001, slippage_rate=0.0, stt_rates={})
    # 매수 0.1% + 매도 0.1% ≈ 0.2% 손실
    assert cost.net_return_pct("buy", 100, 100, "Y") == pytest.approx(-0.1998, abs=0.001)


def test_slippage_hurts_both_directions():
    """매수는 더 비싸게, 매도는 더 싸게 체결된다고 본다."""
    free = CostModel(commission_rate=0.0, slippage_rate=0.0, stt_rates={})
    slipped = CostModel(commission_rate=0.0, slippage_rate=0.002, stt_rates={})
    assert slipped.net_return_pct("buy", 100, 110, "Y") < free.net_return_pct("buy", 100, 110, "Y")
    assert slipped.net_return_pct("sell", 100, 90, "Y") < free.net_return_pct("sell", 100, 90, "Y")


def test_free_model_matches_gross():
    cost = CostModel.free()
    assert cost.net_return_pct("buy", 100, 110, "Y") == pytest.approx(10.0)
    assert not cost.enabled


@pytest.mark.parametrize("corp_cls", ["Y", "K", "N", "E"])
def test_all_markets_have_a_tax_rate(corp_cls):
    assert CostModel().tax_rate(corp_cls) > 0


def test_unknown_market_falls_back_to_default_rate():
    """모르는 시장 구분이 와도 세금을 0으로 두면 비용을 과소 계상한다."""
    assert CostModel().tax_rate("ZZZ") == pytest.approx(0.0020)


def test_round_trip_is_about_23bp():
    """키움 온라인 기준 왕복 비용. 5일 전략의 기대수익이 이 아래면 실질 손실이다."""
    assert CostModel().round_trip_pct("Y") == pytest.approx(0.23, abs=0.001)


def test_cost_drag_is_reported():
    t = _trade("buy", 100, 110)
    assert t.signed_return_pct == pytest.approx(10.0)
    assert t.net_return_pct < t.signed_return_pct
    assert t.cost_drag_pct == pytest.approx(t.signed_return_pct - t.net_return_pct)


def test_hit_is_judged_after_costs():
    """총수익이 비용보다 작으면 적중이 아니다."""
    tiny_gain = Trade(
        rcept_no="r",
        stock_code="005930",
        corp_name="c",
        action="buy",
        confidence=80,
        event_type="e",
        signal_date="20260803",
        entry_date="20260804",
        entry_price=10000,
        exit_date="20260810",
        exit_price=10010,  # +0.10% < 왕복 0.23%
    )
    assert tiny_gain.signed_return_pct > 0
    assert not tiny_gain.hit


def test_sell_costs_reduce_the_short_return():
    t = _trade("sell", 100, 90)
    assert t.signed_return_pct == pytest.approx(10.0)
    assert t.net_return_pct < 10.0


def test_run_passes_cost_model_through():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    trades, _, _ = run([_row()], StubCache({"005930": history}), horizon=2, cost=CostModel.free())
    assert trades[0].net_return_pct == pytest.approx(trades[0].signed_return_pct)


def test_corp_cls_flows_into_the_trade():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    trades, _, _ = run([_row(corp_cls="K")], StubCache({"005930": history}), horizon=2)
    assert trades[0].corp_cls == "K"


def test_render_shows_cost_line():
    out = render([_trade("buy", 100, 110)], [], horizon=5, entry="next_open", cost=CostModel())
    assert "거래세" in out and "왕복" in out
    assert "갉아먹음" in out


def test_render_marks_gross_mode():
    out = render([_trade("buy", 100, 110)], [], horizon=5, entry="next_open", cost=CostModel.free())
    assert "비용 미반영" in out


# --- 지수 대비 초과수익 ---


class StubBenchmark(BenchmarkCache):
    def __init__(self, history: PriceHistory | None) -> None:
        self._history = history
        self._memory = {}
        self.client = None
        self.base_dt = None

    def for_market(self, corp_cls):  # type: ignore[override]
        return self._history


def _bench(closes, opens=None) -> StubBenchmark:
    return StubBenchmark(_history(closes, opens))


def test_excess_is_net_minus_index():
    """지수가 오른 기간이면 그만큼을 넘어야 실력이다."""
    history = _history(closes=[100, 110, 120, 130, 140, 150, 160], opens=[100] * 7)
    trades, _, _ = run(
        [_row()],
        StubCache({"005930": history}),
        horizon=2,
        cost=CostModel.free(),
        benchmark=_bench([1000, 1000, 1000, 1010, 1010, 1010, 1010], [1000] * 7),
    )
    t = trades[0]
    # 종목: 20260804 시가 100 → 20260806 종가 130 = +30%
    assert t.net_return_pct == pytest.approx(30.0)
    # 지수: 20260804 시가 1000 → 20260806 종가 1010 = +1%
    assert t.benchmark_pct == pytest.approx(1.0)
    assert t.excess_return_pct == pytest.approx(29.0)
    assert t.beat_market


def test_losing_trade_can_still_beat_a_falling_market():
    """평균 수익률만 보면 실패로 보이는 신호가 지수 대비로는 선방일 수 있다."""
    history = _history(closes=[100, 100, 95, 95, 95, 95, 95], opens=[100] * 7)
    trades, _, _ = run(
        [_row()],
        StubCache({"005930": history}),
        horizon=2,
        cost=CostModel.free(),
        benchmark=_bench([1000, 1000, 800, 800, 800, 800, 800], [1000] * 7),
    )
    t = trades[0]
    assert t.net_return_pct < 0  # 종목도 빠졌다
    assert t.excess_return_pct > 0  # 그래도 지수보다 덜 빠졌다
    assert t.beat_market
    assert not t.hit  # '적중'과 '지수 초과'는 다른 질문이다


def test_short_benchmark_is_inverted():
    """공매도는 지수가 내리면 그만큼 더 내려야 초과다."""
    history = _history(closes=[100, 100, 90, 90, 90, 90, 90], opens=[100] * 7)
    trades, _, _ = run(
        [_row(action="sell")],
        StubCache({"005930": history}),
        horizon=2,
        cost=CostModel.free(),
        benchmark=_bench([1000, 1000, 950, 950, 950, 950, 950], [1000] * 7),
    )
    t = trades[0]
    assert t.benchmark_pct == pytest.approx(-5.0)
    assert t.benchmark_signed_pct == pytest.approx(5.0)  # 공매도 입장에서는 +5%
    assert t.net_return_pct == pytest.approx(10.0)
    assert t.excess_return_pct == pytest.approx(5.0)


def test_no_benchmark_leaves_excess_none():
    history = _history([100, 110, 120, 130, 140, 150, 160])
    trades, _, _ = run([_row()], StubCache({"005930": history}), horizon=2)
    assert trades[0].benchmark_pct is None
    assert trades[0].excess_return_pct is None
    assert trades[0].beat_market is None


def test_missing_index_data_is_not_faked():
    """지수 일봉이 없으면 근사하지 않고 비교를 포기한다."""
    history = _history([100, 110, 120, 130, 140, 150, 160])
    trades, _, _ = run(
        [_row()], StubCache({"005930": history}), horizon=2, benchmark=StubBenchmark(None)
    )
    assert trades[0].excess_return_pct is None


def test_benchmark_matches_the_entry_convention():
    """종목이 시가에 들어갔으면 지수도 시가 기준이어야 공정하다."""
    history = _history(closes=[100] * 7, opens=[100] * 7)
    bench = _bench(closes=[1000, 1100, 1100, 1100, 1100, 1100, 1100], opens=[1000] * 7)
    cache = StubCache({"005930": history})
    on_open = evaluate(_row(), cache, horizon=2, entry=Entry.NEXT_OPEN, benchmark=bench)
    on_close = evaluate(_row(), cache, horizon=2, entry=Entry.SAME_CLOSE, benchmark=bench)
    # 시가 진입은 20260804 시가(1000) 기준, 종가 진입은 20260803 종가(1000) 기준
    assert on_open.benchmark_pct == pytest.approx(10.0)
    assert on_close.benchmark_pct == pytest.approx(10.0)


def test_index_by_market_maps_kosdaq():
    assert INDEX_BY_MARKET["Y"] != INDEX_BY_MARKET["K"]
    assert BenchmarkCache(None).index_code("K") == INDEX_BY_MARKET["K"]


def test_unknown_market_falls_back_to_kospi():
    assert BenchmarkCache(None).index_code("ZZ") == INDEX_BY_MARKET["Y"]


def test_stats_aggregates_excess():
    history = _history(closes=[100, 100, 120, 120, 120, 120, 120], opens=[100] * 7)
    trades, _, _ = run(
        [_row()],
        StubCache({"005930": history}),
        horizon=2,
        cost=CostModel.free(),
        benchmark=_bench([1000, 1000, 1050, 1050, 1050, 1050, 1050], [1000] * 7),
    )
    stats = Stats.of(trades)
    assert stats.mean_benchmark == pytest.approx(5.0)
    assert stats.mean_excess == pytest.approx(15.0)
    assert stats.beat_rate == pytest.approx(100.0)


def test_render_shows_excess_columns():
    history = _history(closes=[100, 100, 120, 120, 120, 120, 120], opens=[100] * 7)
    trades, skipped, _ = run(
        [_row()],
        StubCache({"005930": history}),
        horizon=2,
        benchmark=_bench([1000, 1000, 1050, 1050, 1050, 1050, 1050], [1000] * 7),
    )
    out = render(trades, skipped, horizon=2, entry="next_open")
    assert "벤치마크" in out and "초과" in out and "승률" in out


def test_dedupe_prefers_directional_over_higher_confidence():
    """confidence만 보면 watch 80%가 buy 75%를 밀어내 매매 가능한 신호가 사라진다."""
    watch = _row(rcept_no="20260803000001", action="watch", confidence=80)
    buy = _row(rcept_no="20260803000002", action="buy", confidence=75)
    assert dedupe([watch, buy])[0][0]["action"] == "buy"
    assert dedupe([buy, watch])[0][0]["action"] == "buy"


def test_dedupe_prefers_directional_over_ignore():
    """실제 표본에서 watch 38/40%와 ignore 62%가 같은 날 나온 사례가 있었다."""
    rows = [
        _row(rcept_no="20260803000001", action="watch", confidence=38),
        _row(rcept_no="20260803000002", action="ignore", confidence=62),
        _row(rcept_no="20260803000003", action="sell", confidence=55),
    ]
    kept, dropped = dedupe(rows)
    assert dropped == 2
    assert kept[0]["action"] == "sell"


def test_dedupe_falls_back_to_confidence_among_directionals():
    rows = [
        _row(rcept_no="20260803000001", action="buy", confidence=60),
        _row(rcept_no="20260803000002", action="sell", confidence=85),
    ]
    assert dedupe(rows)[0][0]["confidence"] == 85


def test_dedupe_falls_back_to_confidence_among_non_directionals():
    rows = [
        _row(rcept_no="20260803000001", action="watch", confidence=40),
        _row(rcept_no="20260803000002", action="watch", confidence=70),
    ]
    assert dedupe(rows)[0][0]["confidence"] == 70


# --- 캐시 만료 ---


def _write_cache(tmp_path, code, last_date, monkeypatch):
    import json

    from aistock.backtest import prices as pm

    monkeypatch.setattr(pm, "PRICE_CACHE_DIR", tmp_path)
    (tmp_path / f"{code}.json").write_text(
        json.dumps(
            [{"date": last_date, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1}]
        )
    )


def test_fresh_cache_is_used(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from aistock.backtest.prices import KST, PriceCache

    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
    _write_cache(tmp_path, "005930", yesterday, monkeypatch)
    assert PriceCache(None).get("005930") is not None


def test_stale_cache_is_discarded(tmp_path, monkeypatch):
    """만료가 없으면 매일 도는 차트가 몇 주 전 데이터를 계속 그린다."""
    from datetime import datetime, timedelta

    from aistock.backtest.prices import KST, PriceCache

    old = (datetime.now(KST) - timedelta(days=10)).strftime("%Y%m%d")
    _write_cache(tmp_path, "005930", old, monkeypatch)
    # 클라이언트가 없으니 갱신도 못 해 None이 된다 — 낡은 값을 쓰는 것보다 낫다
    assert PriceCache(None).get("005930") is None


def test_weekend_gap_does_not_expire_cache(tmp_path, monkeypatch):
    """주말·공휴일엔 새 봉이 없다. 그때마다 갱신하면 불필요한 조회가 반복된다."""
    from datetime import datetime, timedelta

    from aistock.backtest.prices import KST, PriceCache

    three_days = (datetime.now(KST) - timedelta(days=3)).strftime("%Y%m%d")
    _write_cache(tmp_path, "005930", three_days, monkeypatch)
    assert PriceCache(None).get("005930") is not None


def test_backtest_with_base_dt_keeps_old_cache(tmp_path, monkeypatch):
    """과거 구간 백테스팅은 낡은 캐시가 정상이다 — 만료 판정에서 빼야 한다."""
    from aistock.backtest.prices import PriceCache

    _write_cache(tmp_path, "005930", "20240301", monkeypatch)
    assert PriceCache(None, base_dt="20240401").get("005930") is not None
