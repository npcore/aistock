"""미국 레버리지 ETF(TQQQ 등) 반자동 매매.

TQQQ에는 공시가 없으므로 차트 규칙으로 제안한다. 3배 레버리지 ETF는 하락장에서 원금이
빠르게 녹으므로(변동성 손실), **'추세가 살아 있을 때만 들고, 꺾이면 나온다'**가 뼈대다.

    추세 판단   현재가와 200일선 (±3% 완충 — 선 근처에서 사고팔기를 반복하지 않게)

    매수 (미보유)
      · 200일선 상향 돌파: 어제는 200일선+3% 아래, 지금은 위
      · 상승 추세 중 눌림: 200일선 위 · 50일선 > 200일선 · RSI14 ≤ 40
    매도 (보유)
      · 손절  수익률 ≤ -10%
      · 익절  수익률 ≥ +20%
      · 추세 이탈  200일선 -3% 아래

모든 제안은 텔레그램 [승인] 버튼을 거친다. 같은 종목·같은 종류는 뉴욕 날짜로 하루 한 번.
뉴욕 10:00 전(개장 30분)에는 보지 않는다 — 개장 직후의 출렁임을 신호로 쓰지 않기 위해서다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime

from ..account import UsBalance, UsHolding, fetch_us_balance
from ..backtest.prices import bars_from_rows, parse_price
from ..client import KiwoomClient
from ..errors import KiwoomError
from ..market import overseas
from ..market_hours import KST, NY, in_us_session, is_us_trading_day
from ..notify.telegram import TelegramNotifier
from ..us_order import (
    UsOrderError,
    UsOrderRequest,
    max_buy_quantity,
    resolve_exchange,
    round_price,
    validate,
)
from ..us_order import pending_orders as us_order_pending
from . import proposals as P
from .events import active_event, upcoming
from .indicators import rsi, sma
from .policy import TradingPolicy
from .proposer import STOP_LOSS, TAKE_PROFIT, send_proposal

logger = logging.getLogger(__name__)

# 예산을 '주문가능금액 전부'로 잡을 때 남겨두는 몫. 수수료(약 0.25%)를 빼고도 주문이
# 들어가야 한다 — 딱 맞춰 사면 수수료 때문에 잔액 부족으로 거부된다.
FEE_BUFFER = 0.005

TREND = "trend"
RESTING = "resting"
ADD = "add"  # 보유 중 추가 매수 (상승 추세 중 눌림)

# 이 상태로 끝난 제안은 잠시 뒤 다시 제안해도 된다 (거절·주문됨은 그날 끝)
RETRYABLE = (P.EXPIRED, P.FAILED, P.STALE)  # 신호 가격에 미리 걸어두는 지정가
MANUAL = "manual"


def split_adjusted_closes(bars: list) -> list[float]:
    """액면분할을 반영한 종가 (오래된 것부터).

    키움 미국 일봉은 **분할 수정이 안 되어 있다.** TQQQ는 2025-11-20 2:1 분할 때 종가가
    $100 → $46로 '반토막'이 난 것처럼 찍힌다. 그대로 쓰면 200일선이 200거래일 동안 왜곡되고,
    백테스트에서는 가짜 -53% 손절이 난다.

    시가가 전일 종가의 1.8배 이상이거나 0.55배 이하로 뛰면 분할로 본다. 3배 레버리지 ETF라도
    하룻밤 45% 갭은 현실적으로 없다(나스닥100이 15% 움직여야 한다).
    """
    closes = [b.close for b in bars]
    for i in range(1, len(bars)):
        prev, opened = bars[i - 1].close, bars[i].open
        if prev <= 0 or opened <= 0:
            continue
        ratio = prev / opened
        if ratio >= 1.8:
            factor = 1 / round(ratio)  # 정분할: 과거 가격을 나눈다
        elif ratio <= 0.55:
            factor = float(round(1 / ratio))  # 병합(역분할): 과거 가격을 곱한다
        else:
            continue
        logger.info("분할 감지 %s (시가 갭 x%.2f) — 과거 가격 보정", bars[i].date, 1 / ratio)
        closes[:i] = [c * factor for c in closes[:i]]
    return closes


@dataclass(frozen=True)
class UsDecision:
    side: str | None  # buy | sell | None(아무것도 안 함)
    source: str  # trend | stop_loss | take_profit
    reason: str
    summary: str  # 지표 요약 (사람이 판단 근거를 보도록)


def money(value: float, krw: bool = False) -> str:
    return f"{value:,.0f}원" if krw else f"${value:,.2f}"


def kr_policy(policy: TradingPolicy) -> TradingPolicy:
    """국내 종목에 같은 규칙을 쓰되 손절·익절은 국내 기준(-5%/+10%)으로."""
    from dataclasses import replace

    return replace(
        policy, us_stop_loss_pct=policy.stop_loss_pct, us_take_profit_pct=policy.take_profit_pct
    )


def decide(
    closes: list[float],
    price: float,
    policy: TradingPolicy,
    *,
    holding_rate: float | None,
    krw: bool = False,
    buy_block: str | None = None,
) -> UsDecision:
    """closes: 어제까지 종가(오래된 것부터), price: 현재가, holding_rate: 보유 수익률(미보유면 None).

    국내 종목은 kr_policy(policy)와 krw=True로 부른다 (규칙은 같고 표시·손절익절 기준만 다름).
    buy_block: 매수를 막을 이유(경제 일정·기초지수 하락 추세, buy_block() 참고). 매도는 막지 않는다.
    """
    d = _decide(closes, price, policy, holding_rate=holding_rate, krw=krw)
    if buy_block and d.side == "buy":
        return UsDecision(
            None, d.source, f"⏸ 매수 보류 — {buy_block} (규칙: {d.reason})", d.summary
        )
    return d


def _decide(
    closes: list[float],
    price: float,
    policy: TradingPolicy,
    *,
    holding_rate: float | None,
    krw: bool = False,
) -> UsDecision:
    n = policy.us_trend_sma
    series = [*closes, price]
    trend_now, trend_prev = sma(series, n), sma(closes, n)
    sma50, rsi14 = sma(series, 50), rsi(series, 14)

    # 손절·익절은 보유 수익률만 보면 된다. 일봉이 모자란(신규상장 등) 종목에서도 빠지면 안 된다.
    if holding_rate is not None:
        if policy.us_stop_loss_pct and holding_rate <= -policy.us_stop_loss_pct:
            return UsDecision(
                "sell",
                STOP_LOSS,
                f"손절 — 수익률 {holding_rate:+.2f}% (기준 -{policy.us_stop_loss_pct:g}%)",
                money(price, krw),
            )
        if policy.us_take_profit_pct and holding_rate >= policy.us_take_profit_pct:
            return UsDecision(
                "sell",
                TAKE_PROFIT,
                f"익절 — 수익률 {holding_rate:+.2f}% (기준 +{policy.us_take_profit_pct:g}%)",
                money(price, krw),
            )

    if trend_now is None or trend_prev is None:
        return UsDecision(
            None, TREND, f"일봉 {len(closes)}개 — {n}일선 계산 불가", money(price, krw)
        )

    gap = (price / trend_now - 1) * 100
    summary = f"{money(price, krw)} · {n}일선 {money(trend_now, krw)} ({gap:+.1f}%)"
    if sma50 is not None:
        summary += f" · 50일선 {money(sma50, krw)}"
    if rsi14 is not None:
        summary += f" · RSI {rsi14:.0f}"
    buf = policy.us_sma_buffer_pct / 100

    if holding_rate is not None:
        if price < trend_now * (1 - buf):
            return UsDecision(
                "sell",
                TREND,
                f"추세 이탈 — {n}일선보다 {gap:.1f}% 아래 (기준 -{policy.us_sma_buffer_pct:g}%)",
                summary,
            )
        if (
            policy.add_on_pullback
            and price > trend_now
            and sma50 is not None
            and sma50 > trend_now
            and rsi14 is not None
            and rsi14 <= policy.us_pullback_rsi
        ):
            return UsDecision(
                "buy", ADD, f"보유 중 추가 매수 구간 — 상승 추세 중 눌림 (RSI {rsi14:.0f})", summary
            )
        return UsDecision(None, TREND, "보유 유지 — 추세 살아 있음", summary)

    if price > trend_now * (1 + buf) and closes[-1] <= trend_prev * (1 + buf):
        return UsDecision("buy", TREND, f"{n}일선 상향 돌파 ({gap:+.1f}%)", summary)
    if (
        price > trend_now
        and sma50 is not None
        and sma50 > trend_now
        and rsi14 is not None
        and rsi14 <= policy.us_pullback_rsi
    ):
        return UsDecision("buy", TREND, f"상승 추세 중 눌림 (RSI {rsi14:.0f})", summary)
    if price < trend_now:
        return UsDecision(None, TREND, f"관망 — {n}일선 아래 (하락 추세)", summary)
    return UsDecision(None, TREND, "관망 — 진입 조건 아님", summary)


@dataclass(frozen=True)
class UnderlyingView:
    """레버리지 ETF의 기초지수 추세. 3배 ETF는 변동성 손실로 자기 차트가 지수보다 잡음이 크다."""

    symbol: str
    price: float
    sma50: float | None
    sma200: float | None

    @property
    def ok(self) -> bool:
        if self.sma200 is None:
            return True  # 판단할 수 없으면 막지 않는다
        return self.price > self.sma200 and (self.sma50 is None or self.sma50 > self.sma200)

    @property
    def text(self) -> str:
        if self.sma200 is None:
            return f"{self.symbol} ${self.price:,.2f} · 200일선 계산 불가"
        gap = (self.price / self.sma200 - 1) * 100
        out = f"{self.symbol} ${self.price:,.2f} · 200일선 {gap:+.1f}%"
        if self.sma50 is not None:
            out += " · 50일선 " + ("위" if self.sma50 > self.sma200 else "아래") + " 200일선"
        return out


def underlying_view(
    kiwoom: KiwoomClient,
    symbol: str,
    policy: TradingPolicy,
    now: datetime,
    *,
    closes_fn=None,
    exchanges: dict[str, str] | None = None,
) -> UnderlyingView:
    """closes_fn(symbol, exchange, now)를 주면 그 캐시를 쓴다 (UsTrader._closes)."""
    exchanges = {} if exchanges is None else exchanges
    exchange = exchanges.get(symbol)
    if exchange is None:
        exchange, quote = resolve_exchange(kiwoom, symbol)
        exchanges[symbol] = exchange
    else:
        quote = overseas.quote(kiwoom, symbol, exchange)
    price = parse_price(quote.get("cur_prc", "0"))
    if closes_fn is not None:
        closes = closes_fn(symbol, exchange, now)
    else:
        day = now.astimezone(NY).strftime("%Y%m%d")
        rows = overseas.daily_price(kiwoom, symbol, day, exchange, max_pages=12)
        bars = [
            b for b in sorted(bars_from_rows(rows, symbol), key=lambda b: b.date) if b.date < day
        ]
        closes = split_adjusted_closes(bars)
    series = [*closes, price]
    return UnderlyingView(symbol, price, sma(series, 50), sma(series, policy.us_trend_sma))


def buy_block(
    kiwoom: KiwoomClient, symbol: str, policy: TradingPolicy, now: datetime, **kw
) -> str | None:
    """미국 매수를 지금 막을 이유. 없으면 None. 조회가 실패하면 막지 않는다(로그만)."""
    if policy.us_event_hold and (event := active_event(now)):
        return f"{event.label} 앞 (발표 30분 뒤까지)"
    under = policy.underlying_of(symbol)
    if under:
        try:
            view = underlying_view(kiwoom, under, policy, now, **kw)
        except (KiwoomError, ValueError, UsOrderError) as exc:
            logger.warning("%s 기초지수(%s) 조회 실패 — 필터 없이 진행: %s", symbol, under, exc)
            return None
        if not view.ok:
            return f"기초지수 하락 추세 ({view.text})"
    return None


def events_line(now: datetime) -> str:
    """앞으로 7일 안의 경제 일정 한 줄. 없으면 빈 문자열."""
    soon = upcoming(now)
    if not soon:
        return ""
    return "📅 " + " · ".join(e.label for e in soon)


def buy_triggers(closes: list[float], policy: TradingPolicy) -> dict[str, float]:
    """규칙상 매수 신호가 켜지는 가격. 사람이 미리 알고 기다릴 수 있게.

    pullback  이 가격 '이하'로 내려오면 눌림 매수 (RSI ≤ 기준 · 200일선 위 · 50일선 > 200일선)
    breakout  이 가격 '이상'으로 오르면 200일선 돌파 매수 (어제 종가가 돌파선 아래였을 때만)

    RSI는 현재가에 대해 단조 증가하므로 이분 탐색으로 경계 가격을 찾는다.
    """
    n = policy.us_trend_sma
    out: dict[str, float] = {}
    prev_trend = sma(closes, n)
    if prev_trend is None or len(closes) < 50:
        return out
    buf = policy.us_sma_buffer_pct / 100

    if closes[-1] <= prev_trend * (1 + buf):
        out["breakout"] = round(prev_trend * (1 + buf), 2)

    lo, hi = closes[-1] * 0.3, closes[-1] * 1.5
    if (rsi([*closes, hi]) or 0) <= policy.us_pullback_rsi:
        p = hi
    elif (rsi([*closes, lo]) or 100) > policy.us_pullback_rsi:
        return out  # 어떤 가격에서도 RSI가 기준까지 안 내려간다
    else:
        for _ in range(50):
            mid = (lo + hi) / 2
            if (rsi([*closes, mid]) or 100) <= policy.us_pullback_rsi:
                lo = mid
            else:
                hi = mid
        p = lo
    series = [*closes, p]
    trend, s50 = sma(series, n), sma(series, 50)
    if trend is not None and s50 is not None and p > trend and s50 > trend:
        out["pullback"] = round(p - 0.005, 2)  # 센트 단위로 내림 — 이 가격이면 확실히 조건 충족
    return out


def triggers_text(triggers: dict[str, float]) -> str:
    parts = []
    if "pullback" in triggers:
        parts.append(f"${triggers['pullback']:,.2f} 이하 (눌림)")
    if "breakout" in triggers:
        parts.append(f"${triggers['breakout']:,.2f} 이상 (200일선 돌파)")
    return " 또는 ".join(parts)


class UsTrader:
    """봇 프로세스가 주기적으로 부른다. 주문은 내지 않고 제안만 한다."""

    def __init__(
        self,
        kiwoom: KiwoomClient,
        notifier: TelegramNotifier,
        *,
        store: P.ProposalStore | None = None,
        policy: TradingPolicy | None = None,
    ) -> None:
        self.kiwoom = kiwoom
        self.notifier = notifier
        self.store = store or P.ProposalStore()
        self.policy = policy or TradingPolicy.from_env()
        self._last_check = 0.0
        self._exchange: dict[str, str] = {}
        self._bars: dict[str, tuple[str, list[float]]] = {}  # 종목 → (뉴욕 날짜, 종가)

    # --- 주기 점검 ---

    def check(self, *, force: bool = False) -> int:
        """보낸 제안 수. us_check_sec(기본 60초) 간격으로만 실제 조회한다."""
        if not self.policy.us_symbols:
            return 0
        if not force and time.monotonic() - self._last_check < self.policy.us_check_sec:
            return 0
        now = datetime.now(NY)
        hh, mm = (int(x) for x in self.policy.us_check_after.split(":"))
        if not in_us_session(now) or not is_us_trading_day(now) or now.time() < dtime(hh, mm):
            return 0
        self._last_check = time.monotonic()

        balance = fetch_us_balance(self.kiwoom)
        sent = 0
        # 자동매매 대상(TQQQ 등) + 지금 보유한 미국 종목 전부 — 보유 종목은 매도·추가 매수 시점을 본다
        symbols = dict.fromkeys([*self.policy.us_symbols, *(h.symbol for h in balance.holdings)])
        for symbol in symbols:
            try:
                sent += self._check_symbol(symbol, balance, now)
            except (KiwoomError, ValueError) as exc:
                logger.error("미국 종목 점검 실패 (%s): %s", symbol, exc)
        return sent

    def _check_symbol(self, symbol: str, balance: UsBalance, now: datetime) -> int:
        exchange, name, price, closes, holding = self._snapshot(symbol, balance, now)
        rate = holding.profit_rate if holding else None
        decision = decide(closes, price, self.policy, holding_rate=rate)
        if decision.side == "buy":
            block = self._buy_block(symbol, now)
            if block:
                decision = decide(closes, price, self.policy, holding_rate=rate, buy_block=block)
        if decision.side is None:
            logger.debug("%s: %s", symbol, decision.reason)
            if holding is None and self.policy.us_resting:
                return self._propose_resting(symbol, exchange, name, price, closes, balance, now)
            return 0
        if self.store.open_for(symbol, decision.side):
            return 0
        if self._recently_proposed(symbol, decision.source, decision.side, now):
            return 0
        if decision.side == "buy" and self._buy_in_flight(symbol, now):
            return 0

        if decision.side == "buy":
            limit = round_price(price, up=True)
            quantity = self._buy_quantity(symbol, exchange, limit, balance)
            if quantity <= 0:
                logger.info(
                    "%s 매수 신호지만 살 수 있는 수량이 없음 ($%.2f, 외화 주문가능 $%.2f)",
                    symbol,
                    limit,
                    balance.usd_orderable,
                )
                return 0
        else:
            limit = round_price(price, up=False)
            quantity = holding.sellable if holding else 0
            if quantity <= 0:
                return 0

        reason = decision.reason
        if decision.side == "buy":
            trig = triggers_text(buy_triggers(closes, self.policy))
            if trig:
                reason += f" · 신호 가격 {trig}"
        if decision.side == "buy" and balance.usd_orderable < quantity * limit:
            reason += f" · ⚠️ 외화 주문가능 ${balance.usd_orderable:,.2f}"
        stamp = datetime.now(KST).isoformat(timespec="seconds")
        proposal = P.Proposal(
            id=P.new_id(),
            created_at=stamp,
            signal_at=stamp,
            side=decision.side,
            stock_code=symbol,
            name=name,
            quantity=quantity,
            limit_price=0,
            confidence=0,
            reason=reason,
            technical=decision.summary,
            source=decision.source,
            market="us",
            exchange=exchange,
            price_usd=limit,
        )
        validate(self._request(proposal))  # 상한 초과 등은 제안 전에 걸러낸다
        self.store.add(proposal)
        send_proposal(self.notifier, self.store, proposal, self.policy)
        return 1

    def _propose_resting(
        self,
        symbol: str,
        exchange: str,
        name: str,
        price: float,
        closes: list[float],
        balance: UsBalance,
        now: datetime,
    ) -> int:
        """하루 한 번: 눌림 매수 신호 가격에 지정가를 미리 걸어둘지 묻는다.

        승인하면 주문이 거래소에 걸려 있다가 가격이 닿는 순간 체결된다 — 알림을 보고
        버튼을 누르는 사이 가격이 되돌아가 놓치는 일이 없다. 지정가는 당일 유효다.
        """
        if self._recently_proposed(symbol, RESTING, "buy", now):
            return 0
        trigger = buy_triggers(closes, self.policy).get("pullback")
        if trigger is None or trigger >= price:
            return 0  # 하락 추세라 신호 가격이 없거나, 이미 신호 구간(즉시 매수 제안 몫)
        if self._buy_in_flight(symbol, now):
            return 0
        if block := self._buy_block(symbol, now):
            logger.info("%s 지정가 걸어두기 보류: %s", symbol, block)
            return 0

        limit = round_price(trigger, up=False)
        quantity = self._buy_quantity(symbol, exchange, limit, balance)
        if quantity <= 0:
            logger.info("%s 지정가 걸어두기: $%.2f에 살 수 있는 수량이 없음", symbol, limit)
            return 0

        gap = (limit / price - 1) * 100
        stamp = datetime.now(KST).isoformat(timespec="seconds")
        proposal = P.Proposal(
            id=P.new_id(),
            created_at=stamp,
            signal_at=stamp,
            side="buy",
            stock_code=symbol,
            name=name,
            quantity=quantity,
            limit_price=0,
            confidence=0,
            reason=(
                f"눌림 매수 신호 가격 ${limit:,.2f}에 미리 지정가를 걸어둡니다 "
                f"(현재 ${price:,.2f}, {gap:+.1f}%). 닿으면 자동 체결 · 오늘 장 마감까지 유효"
            ),
            technical=decide(closes, price, self.policy, holding_rate=None).summary,
            source=RESTING,
            market="us",
            exchange=exchange,
            price_usd=limit,
        )
        validate(self._request(proposal))
        self.store.add(proposal)
        send_proposal(self.notifier, self.store, proposal, self.policy)
        return 1

    def _recently_proposed(self, symbol: str, source: str, side: str, now: datetime) -> bool:
        """오늘(뉴욕 날짜) 같은 제안을 이미 보냈는가.

        거절한 제안은 그날 다시 묻지 않는다. 못 보고 만료됐거나, 승인했는데 주문이 실패·보류된
        것은 us_realert_min이 지나면 다시 알린다 — 사람이 사려고 했던 것을 하루 통째로 막지 않게.
        """
        today = now.date()
        same = [
            p
            for p in self.store.all()
            if p.stock_code == symbol
            and p.source == source
            and p.side == side
            and p.created.astimezone(NY).date() == today
        ]
        if any(p.status not in RETRYABLE for p in same):
            return True
        # 다시 알림은 하루 한 번까지. 15분마다 무한히 보내면 밤새 같은 메시지가 20번 넘게 온다
        # (2026-10-20 실제로 그랬다).
        if len(same) >= 2:
            return True
        realert_after = timedelta(minutes=self.policy.us_realert_min)
        return any(now - p.created < realert_after for p in same)

    def _buy_block(self, symbol: str, now: datetime) -> str | None:
        return buy_block(
            self.kiwoom,
            symbol,
            self.policy,
            now,
            closes_fn=self._closes,
            exchanges=self._exchange,
        )

    def _buy_quantity(self, symbol: str, exchange: str, limit: float, balance: UsBalance) -> int:
        """예산을 정해뒀으면 예산 ÷ 가격, 아니면 증권사가 계산한 전량 수량."""
        if self.policy.us_budget_usd:
            return int(self.policy.us_budget_usd // limit)
        try:
            return max_buy_quantity(self.kiwoom, symbol, exchange, limit)
        except KiwoomError as exc:
            logger.warning("주문가능수량 조회 실패 — 잔고로 어림합니다 (%s)", exc)
            return int(balance.usd_orderable * (1 - FEE_BUFFER) // limit)

    def _buy_in_flight(self, symbol: str, now: datetime) -> bool:
        """오늘 이미 이 종목 매수가 진행 중인가 — 걸어둔 지정가와 신호 매수로 두 번 사지 않게.

        봇 기록(대기·처리 중·주문됨)과 키움 미체결(직접 낸 주문 포함)을 함께 본다.
        """
        today = now.date()
        for p in self.store.all():
            if (
                p.stock_code == symbol
                and p.side == "buy"
                and p.status in (P.PENDING, P.EXECUTING, P.ORDERED, P.QUEUED)
                and p.created.astimezone(NY).date() == today
            ):
                return True
        try:
            return any(
                o.symbol == symbol and "매수" in o.side for o in us_order_pending(self.kiwoom)
            )
        except KiwoomError as exc:
            logger.warning("미국 미체결 조회 실패 — 중복 확인 없이 진행 (%s)", exc)
            return False

    # --- /us 명령용 현황 ---

    def status(self) -> str:
        """지금 규칙을 적용하면 어떻게 되는지. 장 시간과 무관하게 보여준다."""
        from ..notify.telegram import escape

        now = datetime.now(NY)
        try:
            balance = fetch_us_balance(self.kiwoom)
        except (KiwoomError, ValueError) as exc:
            return f"미국 잔고 조회 실패: {escape(str(exc))}"
        session = "정규장" if in_us_session(now) and is_us_trading_day(now) else "장외"
        lines = [f"<b>🇺🇸 미국 매매 현황</b> ({now:%m/%d %H:%M} ET · {session})"]
        if ev := events_line(now):
            lines.append(escape(ev))
        lines.append("")
        for symbol in self.policy.us_symbols:
            try:
                exchange, name, price, closes, holding = self._snapshot(symbol, balance, now)
            except (KiwoomError, ValueError) as exc:
                lines.append(f"<b>{symbol}</b> 조회 실패: {escape(str(exc))}")
                continue
            d = decide(
                closes,
                price,
                self.policy,
                holding_rate=holding.profit_rate if holding else None,
                buy_block=self._buy_block(symbol, now),
            )
            held = (
                f"보유 {holding.quantity:,}주 ({holding.profit_rate:+.2f}%)"
                if holding
                else "미보유"
            )
            verdict = {"buy": "🟢 매수 신호", "sell": "🔴 매도 신호"}.get(d.side or "", "▪️")
            lines += [
                f"<b>{symbol}</b> {escape(name)} · {held}",
                f"   {escape(d.summary)}",
                f"   {verdict} {escape(d.reason)}",
            ]
            if under := self.policy.underlying_of(symbol):
                try:
                    view = underlying_view(
                        self.kiwoom,
                        under,
                        self.policy,
                        now,
                        closes_fn=self._closes,
                        exchanges=self._exchange,
                    )
                    mark = "🧭" if view.ok else "🧭⛔"
                    lines.append(f"   {mark} 기초지수 {escape(view.text)}")
                except (KiwoomError, ValueError, UsOrderError) as exc:
                    lines.append(f"   🧭 기초지수 {under} 조회 실패: {escape(str(exc)[:80])}")
            trig = triggers_text(buy_triggers(closes, self.policy)) if not holding else ""
            if trig:
                lines.append(f"   🎯 매수 신호 가격: <b>{escape(trig)}</b>")
            lines.append(self._all_in_line(symbol, exchange, price, holding, balance))
            lines.append("")
        p = self.policy
        lines.append(
            f"<i>예산 {f'${p.us_budget_usd:,.0f}/건' if p.us_budget_usd else '외화 전액'} · 손절 -{p.us_stop_loss_pct:g}% · "
            f"익절 +{p.us_take_profit_pct:g}% · 외화 주문가능 ${balance.usd_orderable:,.2f}</i>"
        )
        return "\n".join(lines)

    def _all_in_line(
        self,
        symbol: str,
        exchange: str,
        price: float,
        holding: UsHolding | None,
        balance: UsBalance,
    ) -> str:
        """전량 매수/매도하면 몇 주인지. 사람이 계산하지 않게 한다."""
        if holding:
            sell = round_price(price, up=False)
            q = holding.sellable
            return (
                f"   💰 전량 매도: <b>{q:,}주 × ${sell:,.2f} = ${q * sell:,.2f}</b>"
                f"  → /sell {symbol} 전량"
            )
        buy = round_price(price, up=True)
        try:
            q = max_buy_quantity(self.kiwoom, symbol, exchange, buy)
        except KiwoomError:
            return "   💰 전량 매수: 주문가능수량 조회 실패"
        if q <= 0:
            return (
                f"   💰 전량 매수: 0주 — 외화 주문가능 ${balance.usd_orderable:,.2f}"
                " (미체결 주문이 묶고 있으면 /orders)"
            )
        return (
            f"   💰 전량 매수: <b>{q:,}주 × ${buy:,.2f} = ${q * buy:,.2f}</b>  → /buy {symbol} 전량"
        )

    # --- 공통 ---

    def _snapshot(
        self, symbol: str, balance: UsBalance, now: datetime
    ) -> tuple[str, str, float, list[float], UsHolding | None]:
        exchange = self._exchange.get(symbol)
        if exchange is None:
            exchange, quote = resolve_exchange(self.kiwoom, symbol)
            self._exchange[symbol] = exchange
        else:
            quote = overseas.quote(self.kiwoom, symbol, exchange)
        # 시세 TR의 부호는 전일대비 방향이다. parse_price가 떼어낸다.
        price = parse_price(quote.get("cur_prc", "0"))
        if price <= 0:
            raise ValueError(f"{symbol} 현재가를 알 수 없습니다")
        name = str(quote.get("stk_nm", "")).strip() or symbol
        holding = next((h for h in balance.holdings if h.symbol == symbol), None)
        return exchange, name, price, self._closes(symbol, exchange, now), holding

    def _closes(self, symbol: str, exchange: str, now: datetime) -> list[float]:
        """어제까지의 종가. 뉴욕 날짜가 바뀔 때만 새로 받는다 (12페이지 × 20일 ≈ 240일)."""
        day = now.strftime("%Y%m%d")
        cached = self._bars.get(symbol)
        if cached and cached[0] == day:
            return cached[1]
        rows = overseas.daily_price(self.kiwoom, symbol, day, exchange, max_pages=12)
        bars = [
            b for b in sorted(bars_from_rows(rows, symbol), key=lambda b: b.date) if b.date < day
        ]
        closes = split_adjusted_closes(bars)
        self._bars[symbol] = (day, closes)
        return closes

    @staticmethod
    def _request(p: P.Proposal) -> UsOrderRequest:
        return UsOrderRequest(p.side, p.stock_code, p.exchange, p.quantity, p.price_usd)


def manual_proposal(
    kiwoom: KiwoomClient,
    notifier: TelegramNotifier,
    store: P.ProposalStore,
    policy: TradingPolicy,
    *,
    side: str,
    symbol: str,
    quantity: int | None,
    price: float | None,
) -> P.Proposal:
    """`/buy TQQQ 10 68.20` → 바로 주문하지 않고 확인 버튼이 달린 제안을 만든다.

    quantity=None이면 전량 — 매수는 증권사가 계산한 최대 수량, 매도는 매도가능수량 전부.
    price=None이면 현재가 (매수는 센트 올림, 매도는 내림).

    형식 오류·상한 초과는 여기서 UsOrderError로 바로 알려준다 — 승인 버튼을 누른 뒤에야
    거부되면 그 사이 가격을 놓친다.
    """
    symbol = symbol.upper()
    exchange, quote = resolve_exchange(kiwoom, symbol)
    current = parse_price(quote.get("cur_prc", "0"))
    if price is None:
        price = round_price(current, up=side == "buy")
    note = ""
    if quantity is None:
        note = " · 전량"
        if side == "buy":
            quantity = max_buy_quantity(kiwoom, symbol, exchange, price)
            if quantity <= 0:
                orderable = fetch_us_balance(kiwoom).usd_orderable
                raise UsOrderError(
                    f"${price:,.2f}에 살 수 있는 수량이 없습니다 (외화 주문가능 ${orderable:,.2f}). "
                    "걸려 있는 미체결 주문이 외화를 묶고 있을 수 있습니다: /orders"
                )
        else:
            holding = next(
                (h for h in fetch_us_balance(kiwoom).holdings if h.symbol == symbol), None
            )
            if holding is None or holding.sellable <= 0:
                raise UsOrderError(f"{symbol}을 보유하고 있지 않아 팔 수량이 없습니다.")
            quantity = holding.sellable
    reason = f"직접 입력{note} · 현재가 ${current:,.2f}"
    if side == "buy" and (block := buy_block(kiwoom, symbol, policy, datetime.now(NY))):
        # 직접 낸 주문은 막지 않는다 — 사람이 알고 정하도록 경고만 붙인다
        reason += f" · ⚠️ 자동 규칙이라면 보류: {block}"
    stamp = datetime.now(KST).isoformat(timespec="seconds")
    proposal = P.Proposal(
        id=P.new_id(),
        created_at=stamp,
        signal_at=stamp,
        side=side,
        stock_code=symbol,
        name=str(quote.get("stk_nm", "")).strip() or symbol,
        quantity=quantity,
        limit_price=0,
        confidence=0,
        reason=reason,
        source=MANUAL,
        market="us",
        exchange=exchange,
        price_usd=price,
    )
    validate(UsTrader._request(proposal))
    store.add(proposal)
    send_proposal(notifier, store, proposal, policy)
    return proposal
