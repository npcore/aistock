"""매매 제안 검토 — 제안 메시지 아래에 3개월 차트와 경제 봇 의견을 답장으로 붙인다.

자동 규칙(decide)은 가격 차트만 본다. 실적 발표·FOMC·CPI 같은 일정, 기초지수(QQQ) 흐름,
뉴스는 모른다. 그래서 제안이 나가면 경제 봇이 그 시점의 근거를 찾아

    🟢 찬성 / 🟡 주의 / 🔴 반대  + 이유 몇 줄

을 붙인다. 승인 여부는 여전히 사람이 [승인]/[거절]로 정한다 — 이 모듈에는 주문 경로가 없다.

제안 전송(send_proposal)을 막지 않도록 별도 작업 스레드 하나가 순서대로 처리한다.
차트는 몇 초면 먼저 가고, 의견은 웹 검색을 거쳐 1~2분 뒤에 온다. 의견이 실패해도 차트는 간다.

봇 프로세스가 시작할 때 `install()`해야 동작한다. 설치하지 않으면(테스트·CLI) 아무것도 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..market_hours import KST, NY, is_us_trading_day
from ..notify.telegram import TelegramNotifier, escape
from . import proposals as P
from .indicators import rsi, sma
from .policy import TradingPolicy

logger = logging.getLogger(__name__)

CHART_DAYS = 63  # 약 3개월
REVIEW_EFFORT = "medium"  # 승인 유효시간(10분) 안에 와야 해서 대화용(high)보다 낮췄다
REVIEW_MAX_TOKENS = 8000
MAX_DAILY = 20  # 하루 의견 상한 (비용 보호). 넘으면 차트만 보낸다
REUSE_SEC = 2 * 3600  # 같은 종목·방향은 이 시간 안의 의견을 다시 쓴다(재알림마다 검색하지 않게)

SYSTEM = """당신은 개인 투자자의 매매 제안을 승인 직전에 검토하는 시장 분석가입니다. 자동 규칙이 \
가격 차트(200일선·50일선·RSI·수익률)만 보고 아래 제안을 냈습니다. 규칙이 보지 못하는 것을 확인해 \
이 제안을 지금 승인해도 되는지 판단하세요.

확인할 것:
- web_search로 이 종목과 시장의 최근 1~2일 뉴스, 이번 주와 다음 주 일정(FOMC·CPI·고용지표·\
해당 종목/주요 구성종목 실적 발표).
- 미국 레버리지 ETF(TQQQ 등)는 기초지수(QQQ)를 get_market_data로 함께 보세요.
- 필요하면 get_account로 잔고·미체결 주문을 봅니다(비중이 한쪽으로 쏠리는지).

답변 형식 (텔레그램, 휴대폰):
- 첫 줄은 반드시 셋 중 하나로 시작: "🟢 찬성", "🟡 주의", "🔴 반대" + 한 문장 요지.
- 이어서 • 글머리표 3~5줄로 근거. 일정은 날짜를 적고, 최신 정보는 출처를 짧게(예: 10/24 로이터).
- 주의·반대라면 무엇을 보고 다시 판단할지(가격·날짜·조건) 한 줄.
- 전체 600자 이내. 표·제목·코드블록 금지, 강조는 **굵게**만. 명령어(/buy 등)는 쓰지 마세요.
- 확신이 낮으면 그렇다고 말하세요. 면책 문구는 넣지 마세요.
"""


def _tools() -> list[dict[str, Any]]:
    """경제 봇 도구 중 조회용만 (save_note 제외)."""
    from ..advisor import TOOLS

    return [t for t in TOOLS if t.get("name") != "save_note"]


# --- 차트 ---


def chart_png(kiwoom: Any, p: P.Proposal, policy: TradingPolicy) -> bytes:
    from ..chart import render_signal

    dates, closes = _history(kiwoom, p)
    if len(closes) < 2:
        raise ValueError("일봉이 부족해 차트를 그릴 수 없습니다")
    start = max(0, len(closes) - CHART_DAYS)
    window = range(start, len(closes))
    side = "매수" if p.side == "buy" else "매도"
    krw = not p.is_us
    return render_signal(
        dates[start:],
        closes[start:],
        sma50=[sma(closes[: i + 1], 50) for i in window],
        sma200=[sma(closes[: i + 1], policy.us_trend_sma) for i in window],
        rsi=[rsi(closes[: i + 1], 14) for i in window],
        order_price=p.price_usd if p.is_us else float(p.limit_price),
        order_label=f"{side} 제안가",
        title=f"{p.name} ({p.stock_code}) · 최근 3개월",
        rsi_line=policy.us_pullback_rsi,
        krw=krw,
    )


def _history(kiwoom: Any, p: P.Proposal) -> tuple[list[str], list[float]]:
    """(날짜, 종가) 오래된 것부터. 마지막은 오늘 날짜의 현재가."""
    from ..backtest.prices import bars_from_rows, parse_price

    if p.is_us:
        from ..market import overseas
        from ..us_order import resolve_exchange
        from .us_trader import split_adjusted_closes

        today = datetime.now(NY).strftime("%Y%m%d")
        if p.exchange:
            exchange, quote = p.exchange, overseas.quote(kiwoom, p.stock_code, p.exchange)
        else:
            exchange, quote = resolve_exchange(kiwoom, p.stock_code)
        price = parse_price(quote.get("cur_prc", "0"))
        rows = overseas.daily_price(kiwoom, p.stock_code, today, exchange, max_pages=14)
        bars = [
            b
            for b in sorted(bars_from_rows(rows, p.stock_code), key=lambda b: b.date)
            if b.date < today
        ]
        closes = split_adjusted_closes(bars)
    else:
        from ..backtest import PriceCache
        from ..market import domestic

        today = datetime.now(KST).strftime("%Y%m%d")
        price = parse_price(domestic.stock_info(kiwoom, p.stock_code).get("cur_prc", "0"))
        history = PriceCache(kiwoom).get(p.stock_code)
        bars = [b for b in (history.bars if history else []) if b.date < today]
        closes = [float(b.close) for b in bars]
    dates = [b.date for b in bars]
    # 쉬는 날에는 현재가 = 마지막 종가라 같은 점이 하나 더 찍힌다
    trading = is_us_trading_day(datetime.now(NY)) if p.is_us else datetime.now(KST).weekday() < 5
    if price > 0 and trading:
        dates, closes = [*dates, today], [*closes, float(price)]
    return dates, closes


# --- 의견 ---


def opinion(kiwoom: Any, p: P.Proposal, policy: TradingPolicy, *, client: Any = None) -> str:
    import anthropic

    from ..advisor import MODEL, account_data, market_data, run_tool_loop

    def call_tool(name: str, args: dict[str, Any]) -> str:
        if name == "get_market_data":
            return json.dumps(market_data(kiwoom, str(args.get("symbol", ""))), ensure_ascii=False)
        if name == "get_account":
            return json.dumps(account_data(kiwoom), ensure_ascii=False)
        raise ValueError(f"모르는 도구: {name}")

    return run_tool_loop(
        client or anthropic.Anthropic(),
        model=MODEL,
        system=SYSTEM,
        tools=_tools(),
        messages=[{"role": "user", "content": _brief(p, policy)}],
        call_tool=call_tool,
        effort=REVIEW_EFFORT,
        max_tokens=REVIEW_MAX_TOKENS,
    )


def _brief(p: P.Proposal, policy: TradingPolicy) -> str:
    now = datetime.now(KST)
    side = "매수" if p.side == "buy" else "매도"
    lines = [
        f"[지금] 한국 {now:%Y-%m-%d %a %H:%M} · 뉴욕 {now.astimezone(NY):%m-%d %a %H:%M}",
        f"[제안] {p.name} ({p.stock_code}) {side} {p.price_text()} · 출처 {p.source or '공시'}",
        f"[규칙 근거] {p.reason}",
    ]
    if p.technical:
        lines.append(f"[차트] {p.technical}")
    if p.is_us:
        lines.append(
            f"[봇 규칙] 손절 -{policy.us_stop_loss_pct:g}% · 익절 +{policy.us_take_profit_pct:g}% · "
            f"{policy.us_trend_sma}일선 ±{policy.us_sma_buffer_pct:g}% 추세 · "
            f"RSI {policy.us_pullback_rsi:g} 이하 눌림"
        )
    lines.append("이 제안을 지금 승인해도 될지 검토해 주세요.")
    return "\n".join(lines)


def opinion_message(p: P.Proposal, text: str, *, status: str = "", age_min: int = 0) -> str:
    from ..advisor import to_telegram_html

    side = "매수" if p.side == "buy" else "매도"
    head = f"🧠 <b>경제 봇 의견</b> · {escape(p.name)} {side}"
    if age_min:
        head += f" <i>({age_min}분 전 검토 재사용)</i>"
    lines = [head, "", to_telegram_html(text)]
    if status and status != P.PENDING:
        lines += ["", f"<i>이 제안은 이미 처리됨 ({status})</i>"]
    return "\n".join(lines)


# --- 작업 스레드 ---


@dataclass
class Reviewer:
    kiwoom: Any
    notifier: TelegramNotifier
    store: P.ProposalStore
    policy: TradingPolicy
    client: Any = None
    use_llm: bool = True
    jobs: queue.Queue = field(default_factory=queue.Queue)
    _recent: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    _day: tuple[str, int] = ("", 0)
    _thread: threading.Thread | None = None

    def submit(self, p: P.Proposal, message_id: int) -> None:
        self.jobs.put((p, message_id))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="review", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            p, message_id = self.jobs.get()
            try:
                self.handle(p, message_id)
            except Exception:  # 검토 실패가 봇을 멈추면 안 된다
                logger.exception("제안 검토 실패: %s", p.id)

    def handle(self, p: P.Proposal, message_id: int) -> None:
        try:
            self.notifier.send_photo(chart_png(self.kiwoom, p, self.policy), reply_to=message_id)
        except Exception as exc:  # noqa: BLE001 — 시세 조회·렌더링·전송 어디서 실패해도 의견은 보낸다
            logger.warning("제안 차트 실패 %s: %s", p.stock_code, exc)

        if not self.use_llm:
            return
        key = (p.stock_code, p.side)
        now = time.time()
        cached = self._recent.get(key)
        age_min = 0
        if cached and now - cached[0] < REUSE_SEC:
            text, age_min = cached[1], max(1, int((now - cached[0]) / 60))
        else:
            today = datetime.now(KST).strftime("%Y%m%d")
            day, count = self._day if self._day[0] == today else (today, 0)
            if count >= MAX_DAILY:
                logger.warning("제안 의견 하루 상한(%d) — 차트만 보냄", MAX_DAILY)
                return
            self._day = (day, count + 1)
            text = opinion(self.kiwoom, p, self.policy, client=self.client)
            self._recent[key] = (now, text)
        current = self.store.get(p.id)
        self.notifier.send(
            opinion_message(p, text, status=current.status if current else "", age_min=age_min),
            reply_to=message_id,
        )


_reviewer: Reviewer | None = None


def install(
    kiwoom: Any, notifier: TelegramNotifier, store: P.ProposalStore, policy: TradingPolicy
) -> Reviewer | None:
    """봇 프로세스에서 한 번. 정책에서 끄면(TRADE_REVIEW=0) 설치하지 않는다."""
    global _reviewer
    if not policy.review_proposals:
        _reviewer = None
        return None
    from ..advisor import Advisor

    use_llm = Advisor.has_credentials()
    if not use_llm:
        logger.warning("ANTHROPIC_API_KEY가 없어 제안에는 차트만 붙입니다")
    _reviewer = Reviewer(kiwoom, notifier, store, policy, use_llm=use_llm)
    return _reviewer


def enqueue(p: P.Proposal, message_id: int) -> None:
    if _reviewer is not None and message_id:
        _reviewer.submit(p, message_id)
