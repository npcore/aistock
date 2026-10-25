"""제안 검토 — 3개월 차트 + 경제 봇 의견을 제안 메시지에 답장으로 붙인다."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from aistock.trading import proposals as P
from aistock.trading import review
from aistock.trading.policy import TradingPolicy


@dataclass
class FakeNotifier:
    photos: list[dict] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)

    def send_photo(self, image: bytes, caption: str = "", **kw) -> None:
        self.photos.append({"image": image, **kw})

    def send(self, text: str, **kw) -> int:
        self.texts.append({"text": text, **kw})
        return 1


def _proposal(store: P.ProposalStore, status: str = P.PENDING) -> P.Proposal:
    p = P.Proposal(
        id=P.new_id(),
        created_at="2026-10-23T10:30:00+09:00",
        side="buy",
        stock_code="TQQQ",
        name="ProShares UltraPro QQQ",
        quantity=10,
        limit_price=0,
        confidence=0,
        reason="상승 추세 중 RSI 38 눌림",
        source="trend",
        market="us",
        exchange="NASD",
        price_usd=50.5,
        status=status,
    )
    store.add(p)
    return p


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = P.ProposalStore(tmp_path / "proposals.json")
    notifier = FakeNotifier()
    calls = []
    monkeypatch.setattr(review, "chart_png", lambda k, p, pol: b"PNG")

    def fake_opinion(kiwoom, p, policy, *, client=None):
        calls.append(p.id)
        return "🟡 주의 — 이번 주 FOMC\n• 10/28 FOMC"

    monkeypatch.setattr(review, "opinion", fake_opinion)
    r = review.Reviewer(object(), notifier, store, TradingPolicy())
    return r, store, notifier, calls


def test_handle_sends_chart_and_opinion_as_replies(setup):
    r, store, notifier, calls = setup
    p = _proposal(store)
    r.handle(p, 77)
    assert notifier.photos == [{"image": b"PNG", "reply_to": 77}]
    (msg,) = notifier.texts
    assert msg["reply_to"] == 77
    assert "경제 봇 의견" in msg["text"] and "🟡 주의" in msg["text"]
    assert "이미 처리됨" not in msg["text"]
    assert calls == [p.id]


def test_reuses_recent_opinion_for_same_symbol(setup):
    r, store, notifier, calls = setup
    r.handle(_proposal(store), 1)
    r.handle(_proposal(store), 2)
    assert len(calls) == 1  # 재알림마다 검색하지 않는다
    assert "검토 재사용" in notifier.texts[1]["text"]
    assert len(notifier.photos) == 2  # 차트는 매번 (가격이 바뀌므로)


def test_marks_already_decided_proposal(setup):
    r, store, notifier, _ = setup
    p = _proposal(store, status=P.ORDERED)
    r.handle(p, 5)
    assert "이미 처리됨 (ordered)" in notifier.texts[0]["text"]


def test_chart_failure_still_sends_opinion(setup, monkeypatch):
    r, store, notifier, _ = setup

    def boom(*a):
        raise ValueError("일봉 없음")

    monkeypatch.setattr(review, "chart_png", boom)
    r.handle(_proposal(store), 3)
    assert notifier.photos == [] and len(notifier.texts) == 1


def test_daily_cap_sends_chart_only(setup, monkeypatch):
    r, store, notifier, calls = setup
    monkeypatch.setattr(review, "MAX_DAILY", 0)
    r.handle(_proposal(store), 4)
    assert len(notifier.photos) == 1 and notifier.texts == [] and calls == []


def test_without_llm_sends_chart_only(setup):
    r, store, notifier, calls = setup
    r.use_llm = False
    r.handle(_proposal(store), 6)
    assert len(notifier.photos) == 1 and notifier.texts == [] and calls == []


def test_enqueue_is_noop_until_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "_reviewer", None)
    review.enqueue(_proposal(P.ProposalStore(tmp_path / "p.json")), 9)  # 예외 없이 무시


def test_install_respects_policy_flag(monkeypatch):
    monkeypatch.setenv("TRADE_REVIEW", "0")
    assert TradingPolicy.from_env().review_proposals is False
    assert review.install(object(), FakeNotifier(), None, TradingPolicy.from_env()) is None


def test_render_signal_produces_png():
    from aistock.chart import render_signal

    n = 80
    closes = [50 + i * 0.1 for i in range(n)]
    dates = [f"2026{7 + i // 30:02d}{1 + i % 28:02d}" for i in range(n)]
    png = render_signal(
        dates,
        closes,
        sma50=[None] * 10 + closes[10:],
        sma200=[None] * n,
        rsi=[45.0] * n,
        order_price=55.0,
        order_label="매수 제안가",
        title="TQQQ · 최근 3개월",
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_brief_includes_proposal_and_rules():
    store_p = P.Proposal(
        id="x",
        created_at="2026-10-23T10:30:00+09:00",
        side="sell",
        stock_code="005930",
        name="삼성전자",
        quantity=3,
        limit_price=70000,
        confidence=0,
        reason="손절 -5%",
        source="stop_loss",
    )
    text = review._brief(store_p, TradingPolicy())
    assert "삼성전자 (005930) 매도" in text and "손절 -5%" in text
