"""환율 조회·이력 검증."""

from datetime import datetime, timedelta, timezone

import pytest

from aistock import fx
from aistock.fx import MIN_RECORD_GAP, FxRate, JpyRate, append_history, load_history, summary

KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def no_live_jpy(monkeypatch):
    """summary()는 엔화를 실제로 조회하고 실제 이력 파일에 기록한다.

    막지 않으면 테스트가 네트워크·디스크 상태에 따라 결과가 바뀌고(실제 엔화 이력의
    '최근 29일'이 섞여 실패했다), 돌릴 때마다 운영 이력 파일에 값이 쌓인다.
    엔화를 보는 테스트는 이 값을 직접 바꾼다.
    """
    state = {"rate": None, "history": []}
    monkeypatch.setattr(fx, "fetch_jpy", lambda **kw: state["rate"])
    monkeypatch.setattr(fx, "load_jpy_history", lambda *a, **kw: state["history"])
    return state


def _jpy(per100=930.0, minutes_ago=0, src="er-api") -> JpyRate:
    when = datetime.now(KST) - timedelta(minutes=minutes_ago)
    return JpyRate(at=when.isoformat(timespec="seconds"), per100=per100, src=src)


def _rate(mid=1415.0, spread=13.7, minutes_ago=0) -> FxRate:
    when = datetime.now(KST) - timedelta(minutes=minutes_ago)
    return FxRate(
        at=when.isoformat(timespec="seconds"),
        buy=mid + spread,
        sell=mid - spread,
        label="고시환율",
    )


# --- 값 계산 ---


def test_mid_is_between_buy_and_sell():
    """한쪽만 보면 스프레드만큼 치우친 값을 본다."""
    r = _rate(mid=1415.0, spread=13.7)
    assert r.mid == pytest.approx(1415.0)
    assert r.sell < r.mid < r.buy


def test_spread_is_the_gap():
    assert _rate(mid=1415.0, spread=13.7).spread == pytest.approx(27.4)


def test_mid_falls_back_when_one_side_is_zero():
    """TR은 방향에 따라 반대쪽을 0으로 준다 — 그걸 평균 내면 절반이 된다."""
    only_buy = FxRate(at=_rate().at, buy=1428.9, sell=0.0)
    assert only_buy.mid == pytest.approx(1428.9)
    only_sell = FxRate(at=_rate().at, buy=0.0, sell=1401.7)
    assert only_sell.mid == pytest.approx(1401.7)


# --- 이력 ---


def test_history_round_trip(tmp_path):
    path = tmp_path / "fx.jsonl"
    assert append_history(_rate(1410.0), path)
    assert append_history(_rate(1420.0, minutes_ago=-60), path)
    history = load_history(path)
    assert len(history) == 2
    assert history[0].mid == pytest.approx(1410.0)


def test_history_is_sorted_by_time(tmp_path):
    path = tmp_path / "fx.jsonl"
    append_history(_rate(1420.0, minutes_ago=0), path)
    append_history(_rate(1410.0, minutes_ago=120), path)
    assert [round(r.mid) for r in load_history(path)] == [1410, 1420]


def test_duplicate_within_gap_is_skipped(tmp_path):
    """고시환율은 자주 바뀌지 않는다. 같은 값을 반복 기록하면 그래프가 계단이 된다."""
    path = tmp_path / "fx.jsonl"
    assert append_history(_rate(1415.0), path)
    assert not append_history(_rate(1415.0), path)
    assert len(load_history(path)) == 1


def test_changed_value_is_recorded_even_within_gap(tmp_path):
    """값이 움직였으면 간격과 무관하게 남긴다 — 변동을 놓치면 안 된다."""
    path = tmp_path / "fx.jsonl"
    append_history(_rate(1415.0), path)
    assert append_history(_rate(1418.0), path)


def test_same_value_after_the_gap_is_recorded(tmp_path):
    path = tmp_path / "fx.jsonl"
    append_history(_rate(1415.0, minutes_ago=int(MIN_RECORD_GAP.total_seconds() // 60) + 5), path)
    assert append_history(_rate(1415.0), path)


def test_missing_history_file_is_empty(tmp_path):
    assert load_history(tmp_path / "none.jsonl") == []


def test_corrupt_line_is_skipped(tmp_path):
    path = tmp_path / "fx.jsonl"
    path.write_text('{"at":"2026-08-11T10:00:00+09:00","buy":1428.9,"sell":1401.7}\n{깨짐\n')
    assert len(load_history(path)) == 1


# --- 요약 ---


def test_summary_shows_both_sides():
    text = summary(_rate(1415.0))
    assert "1,415" in text
    assert "매수" in text and "매도" in text


def test_summary_shows_change_when_history_exists():
    history = [_rate(1400.0, minutes_ago=180), _rate(1415.0)]
    text = summary(history[-1], history)
    assert "+15" in text


def test_summary_without_history_omits_change():
    assert "최근" not in summary(_rate(1415.0), [])


def test_summary_includes_jpy_with_change(no_live_jpy):
    no_live_jpy["rate"] = _jpy(935.5)
    no_live_jpy["history"] = [_jpy(930.0, minutes_ago=180), _jpy(935.5)]
    text = summary(_rate(1415.0))
    assert "원/100엔" in text and "935.50" in text and "+5.50" in text


def test_summary_survives_missing_jpy():
    """엔화를 못 가져와도 달러 줄은 그대로 나가야 한다."""
    text = summary(_rate(1415.0))
    assert "원/달러" in text and "엔" not in text


def test_summary_single_source_note_when_same(no_live_jpy):
    no_live_jpy["rate"] = _jpy(src="er-api")
    text = summary(FxRate(at=_rate().at, buy=1415.0, sell=1415.0, src="er-api"))
    assert text.count("er-api") == 1


# --- 차트 ---


def test_chart_needs_at_least_two_points():
    from aistock.chart import render_fx

    with pytest.raises(ValueError, match="2건 이상"):
        render_fx([_rate(1415.0)])


def test_chart_renders_png():
    from aistock.chart import render_fx

    history = [_rate(1410.0, minutes_ago=120), _rate(1415.0, minutes_ago=60), _rate(1418.0)]
    png = render_fx(history)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
