"""주문 제안 — 세 군데서 나온다.

    1. 장중 공시 신호      `SignalHandler(callbacks=[proposer])` — 바로 제안
    2. 장 마감 후 공시 신호  같은 콜백. `deferred`로 저장했다가 다음 장 09:05 이후
                           `release_deferred()`가 새 가격·차트로 다시 평가해 제안
    3. 손절·익절          `check_exits()`가 보유 종목 수익률을 주기적으로 보고 매도 제안

2·3은 상시 떠 있는 봇 프로세스가 주기적으로 부른다.

**여기서는 주문을 내지 않는다.** 주문은 사람이 승인 버튼을 눌렀을 때 봇 프로세스의
`executor.approve()`가 낸다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from ..account import Holding, fetch_balance
from ..backtest.prices import PriceCache, parse_price
from ..client import KiwoomClient
from ..errors import KiwoomError
from ..market import domestic
from ..market_hours import KST, OPEN, in_session, is_trading_day
from ..notify.telegram import TelegramError, TelegramNotifier, escape
from ..pipeline.events import DisclosureEvent
from ..signal.schema import Action, DisclosureSignal
from . import proposals as P
from . import review
from .policy import TradingPolicy
from .sizing import quantity_for, round_to_tick
from .technical import TechnicalView, evaluate

logger = logging.getLogger(__name__)

CALLBACK_APPROVE = "tp:ok:"
CALLBACK_REJECT = "tp:no:"
CALLBACK_RESERVE = "tp:rsv:"

# 하루 매수 한도에 세는 상태: 승인 대기 중이거나 실제로 주문까지 간 것
COUNTED_BUYS = (P.PENDING, P.EXECUTING, P.ORDERED)

STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"


@dataclass
class Assessment:
    """한 종목·한 방향에 대한 지금 시점의 판단."""

    name: str
    price: float
    view: TechnicalView
    holding: Holding | None
    limit_price: int = 0
    quantity: int = 0
    blocks: list[str] = field(default_factory=list)
    skip: str = ""  # 기록할 가치도 없는 경우(예: 보유하지 않은 종목의 매도)


class TradeProposer:
    def __init__(
        self,
        kiwoom: KiwoomClient,
        notifier: TelegramNotifier,
        *,
        store: P.ProposalStore | None = None,
        policy: TradingPolicy | None = None,
        prices: PriceCache | None = None,
    ) -> None:
        self.kiwoom = kiwoom
        self.notifier = notifier
        self.store = store or P.ProposalStore()
        self.policy = policy or TradingPolicy.from_env()
        self.prices = prices or PriceCache(kiwoom)
        self.stats = {"proposed": 0, "blocked": 0, "deferred": 0, "skipped": 0, "failed": 0}
        self._last_exit_check = 0.0
        self._trading_day: tuple[str, bool] | None = None
        self._closes_cache: dict[str, tuple[str, list[float]]] = {}

    # --- 1·2. 공시 신호 ---

    def __call__(self, event: DisclosureEvent, signal: DisclosureSignal) -> None:
        # 폴러가 핸들러 예외를 격리하지만, 여기서 죽으면 같은 신호의 다른 콜백도 영향을 받는다.
        try:
            self._on_signal(event, signal)
        except (KiwoomError, TelegramError, ValueError) as exc:
            self.stats["failed"] += 1
            logger.error("주문 제안 실패 (%s): %s", event.rcept_no, exc)

    def _skip(self, why: str, *args: object) -> None:
        self.stats["skipped"] += 1
        logger.info("제안 생략: " + why, *args)

    def _on_signal(self, event: DisclosureEvent, signal: DisclosureSignal) -> None:
        code = event.disclosure.stock_code.strip()
        if signal.action not in (Action.BUY, Action.SELL) or not code:
            return
        if signal.confidence < self.policy.min_confidence:
            return self._skip(
                "%s 확신도 %d < %d", code, signal.confidence, self.policy.min_confidence
            )

        side = signal.action.value
        if any(p.rcept_no == event.rcept_no for p in self.store.all()):
            return self._skip("%s 같은 공시로 이미 제안함 (%s)", code, event.rcept_no)
        if self.store.open_for(code, side):
            return self._skip("%s 이미 대기 중인 %s 제안 있음", code, side)

        now = _now()
        base = P.Proposal(
            id=P.new_id(),
            created_at=now.isoformat(timespec="seconds"),
            signal_at=now.isoformat(timespec="seconds"),
            side=side,
            stock_code=code,
            name=event.disclosure.corp_name,
            quantity=0,
            limit_price=0,
            confidence=signal.confidence,
            reason=f"{signal.event_type} — {signal.rationale}",
            rcept_no=event.rcept_no,
        )

        if not in_session(now):
            # 장 마감 후 공시를 그 시점 가격으로 제안하면 다음 날 시가와 동떨어진다.
            # 신호만 들고 있다가 다음 장에서 새 가격으로 다시 판단한다.
            try:
                ref = int(parse_price(domestic.stock_info(self.kiwoom, code).get("cur_prc", "0")))
            except (KiwoomError, ValueError):
                ref = 0
            deferred = replace(
                base, status=P.DEFERRED, ref_price=ref, note="장외 신호 — 다음 장에서 재평가"
            )
            self.store.add(deferred)
            self.stats["deferred"] += 1
            logger.info("장외 신호 보관: %s %s", deferred.name, side)
            self.notifier.send(deferred_message(deferred, self.policy))
            return

        self._publish(base, self._assess(code, side))

    def release_deferred(self) -> int:
        """장 마감 후 신호를 다음 장에서 다시 평가해 제안한다. 보낸 건수를 돌려준다."""
        waiting = [p for p in self.store.all() if p.status == P.DEFERRED]
        if not waiting:
            return 0
        now = _now()
        hh, mm = (int(x) for x in self.policy.release_after.split(":"))
        if not in_session(now) or now.time() < now.time().replace(hour=hh, minute=mm, second=0):
            return 0
        if not self._is_trading_day(now):
            return 0

        today_open = now.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
        sent = 0
        for p in waiting:
            signal_at = datetime.fromisoformat(p.signal_at or p.created_at)
            if signal_at >= today_open:
                continue  # 오늘 장 마감 후 신호는 내일 몫이다
            if now - signal_at > timedelta(days=self.policy.deferred_max_days):
                self.store.transition(p.id, P.DEFERRED, P.EXPIRED, note="장외 신호가 너무 오래됨")
                continue
            try:
                a = self._assess(p.stock_code, p.side)
            except (KiwoomError, ValueError) as exc:
                logger.error("장외 신호 재평가 실패 (%s): %s", p.stock_code, exc)
                continue
            if p.side == "buy" and p.ref_price and a.price:
                gap = (a.price - p.ref_price) / p.ref_price * 100
                if gap > self.policy.max_gap_pct:
                    a.blocks.append(f"신호 이후 {gap:+.1f}% 갭상승 — 이미 가격에 반영")
            if self._publish(p, a, from_status=P.DEFERRED):
                sent += 1
        return sent

    # --- 3. 손절·익절 ---

    def check_exits(self, *, force: bool = False) -> int:
        """보유 종목 실시간 점검 — 매도(손절·익절·추세 이탈)와 추가 매수(상승 추세 중 눌림).

        미국 TQQQ와 같은 규칙(us_trader.decide)을 쓰되 손절·익절은 국내 기준(-5%/+10%).
        exit_check_min(기본 1분) 간격으로만 실제 조회한다.
        """
        from .us_trader import decide, kr_policy

        policy = self.policy
        if not force and time.monotonic() - self._last_exit_check < policy.exit_check_min * 60:
            return 0
        now = _now()
        if not in_session(now) or not self._is_trading_day(now):
            return 0
        self._last_exit_check = time.monotonic()

        rules = kr_policy(policy)
        today = self.store.today(now)
        sent = 0
        for h in fetch_balance(self.kiwoom).holdings:
            try:
                info = domestic.stock_info(self.kiwoom, h.stock_code)
                price = parse_price(info.get("cur_prc", "0"))
                closes = self._daily_closes(h.stock_code, now)
            except (KiwoomError, ValueError) as exc:
                logger.error("보유 종목 점검 실패 (%s): %s", h.stock_code, exc)
                continue
            d = decide(closes, price, rules, holding_rate=h.profit_rate, krw=True)
            if d.side is None or (d.side == "sell" and h.sellable <= 0):
                continue
            # 거절한 제안을 1분마다 다시 묻지 않는다. 종목·종류별로 하루 한 번.
            if self.store.open_for(h.stock_code, d.side) or any(
                p.stock_code == h.stock_code and p.source == d.source and p.side == d.side
                for p in today
            ):
                continue
            base = P.Proposal(
                id=P.new_id(),
                created_at=now.isoformat(timespec="seconds"),
                signal_at=now.isoformat(timespec="seconds"),
                side=d.side,
                stock_code=h.stock_code,
                name=h.name,
                quantity=0,
                limit_price=0,
                confidence=0,
                reason=f"{d.reason} · 매입가 {h.avg_price:,}원 · 수익률 {h.profit_rate:+.2f}%",
                source=d.source,
            )
            try:
                if d.side == "sell":
                    assessment = self._assess(h.stock_code, "sell", holding=h)
                else:
                    assessment = self._assess_add(h.stock_code, h, price, info)
            except (KiwoomError, ValueError) as exc:
                logger.error("보유 종목 제안 평가 실패 (%s): %s", h.stock_code, exc)
                continue
            if self._publish(base, assessment):
                sent += 1
        return sent

    def _assess_add(self, code: str, holding: Holding, price: float, info: dict) -> Assessment:
        """보유 종목 추가 매수. 신규 매수와 달리 '이미 보유 중'으로 막지 않는다."""
        from ..account import fetch_deposit

        limit = round_to_tick(price, up=True)
        quantity = quantity_for(self.policy.budget_per_trade, limit)
        view = evaluate(self._daily_closes(code, _now()), price, _float(info.get("flu_rt")))
        a = Assessment(
            name=str(info.get("stk_nm", "")).strip() or holding.name,
            price=price,
            view=view,
            holding=holding,
            limit_price=limit,
            quantity=quantity,
        )
        if quantity <= 0:
            a.blocks.append(f"1주 가격 {limit:,}원이 1건 예산을 넘음")
        else:
            orderable = fetch_deposit(self.kiwoom).orderable
            if orderable < quantity * limit:
                a.blocks.append(f"원화 주문가능 {orderable:,}원 — {quantity * limit:,}원 부족")
        return a

    def _daily_closes(self, code: str, now: datetime) -> list[float]:
        """어제까지의 일봉 종가. 하루 한 번만 새로 받는다 — 봇은 며칠씩 떠 있어서
        PriceCache의 메모리 캐시를 그대로 쓰면 날짜가 바뀌어도 옛 봉으로 계산한다."""
        from ..backtest.prices import bars_from_rows

        day = now.strftime("%Y%m%d")
        cached = self._closes_cache.get(code)
        if cached and cached[0] == day:
            return cached[1]
        rows = domestic.daily_chart(self.kiwoom, code, day, max_pages=1, warn_on_truncate=False)
        bars = sorted(bars_from_rows(rows, code), key=lambda b: b.date)
        closes = [b.close for b in bars if b.date < day]
        self._closes_cache[code] = (day, closes)
        return closes

    # --- 공통 ---

    def _assess(self, code: str, side: str, *, holding: Holding | None = None) -> Assessment:
        info = domestic.stock_info(self.kiwoom, code)
        name = str(info.get("stk_nm", "")).strip()
        price = parse_price(info.get("cur_prc", "0"))
        change = _float(info.get("flu_rt"))
        if price <= 0:
            raise ValueError(f"{code} 현재가를 알 수 없습니다")

        history = self.prices.get(code)
        today = _now().strftime("%Y%m%d")
        closes = [b.close for b in history.bars if b.date < today] if history else []
        view = evaluate(closes, price, change)
        if holding is None:
            holding = next(
                (h for h in fetch_balance(self.kiwoom).holdings if h.stock_code == code), None
            )
        a = Assessment(name=name, price=price, view=view, holding=holding)

        if side == "buy":
            a.blocks.extend(view.blocks)
            if holding is not None:
                a.blocks.append(f"이미 {holding.quantity:,}주 보유 중")
            # 아무도 안 누르고 만료·거절된 제안까지 세면, 아침에 놓친 3건이 하루를 막는다.
            # 미국 제안은 별도 규칙이라 세지 않는다.
            bought = sum(
                1
                for p in self.store.today()
                if p.side == "buy" and not p.is_us and p.status in COUNTED_BUYS
            )
            if bought >= self.policy.max_daily_buys:
                a.blocks.append(f"오늘 매수 제안 한도 {self.policy.max_daily_buys}건 소진")
            a.limit_price = round_to_tick(price, up=True)
            a.quantity = quantity_for(self.policy.budget_per_trade, a.limit_price)
            if a.quantity <= 0:
                a.blocks.append(f"1주 가격 {a.limit_price:,}원이 1건 예산을 넘음")
        else:
            # 매도는 차트로 막지 않는다 — 악재에 차트까지 나쁘면 더더욱 팔아야 한다.
            if holding is None or holding.sellable <= 0:
                a.skip = "보유하지 않음"
            a.limit_price = round_to_tick(price, up=False)
            a.quantity = holding.sellable if holding else 0
        return a

    def _publish(self, base: P.Proposal, a: Assessment, *, from_status: str = "") -> bool:
        """판단을 제안으로 저장하고, 막힌 게 없으면 버튼과 함께 보낸다. 보냈으면 True."""
        if a.skip:
            if from_status:
                self.store.transition(base.id, from_status, P.EXPIRED, note=a.skip)
            self._skip("%s %s — %s", base.stock_code, base.side, a.skip)
            return False

        status = P.BLOCKED if a.blocks else P.PENDING
        fields = {
            "created_at": _now().isoformat(timespec="seconds"),  # 승인 유효시간은 보낸 때부터
            "name": a.name or base.name,
            "quantity": a.quantity,
            "limit_price": a.limit_price,
            "technical": a.view.summary(),
            "blocks": a.blocks,
            "note": "",
        }
        if from_status:
            proposal = self.store.transition(base.id, from_status, status, **fields)
            if proposal is None:
                return False  # 다른 곳에서 먼저 처리했다
        else:
            proposal = replace(base, status=status, **fields)
            self.store.add(proposal)

        if a.blocks:
            self.stats["blocked"] += 1
            logger.info("제안 보류: %s %s — %s", proposal.name, proposal.side, "; ".join(a.blocks))
            return False

        send_proposal(self.notifier, self.store, proposal, self.policy)
        self.stats["proposed"] += 1
        return True

    def _is_trading_day(self, now: datetime) -> bool:
        """공휴일 평일에도 in_session()은 True다. 하루 한 번만 실제로 확인한다."""
        day = now.strftime("%Y%m%d")
        if self._trading_day is None or self._trading_day[0] != day:
            self._trading_day = (day, is_trading_day(self.kiwoom, now))
        return self._trading_day[1]


def send_proposal(
    notifier: TelegramNotifier, store: P.ProposalStore, p: P.Proposal, policy: TradingPolicy
) -> int:
    """[승인][거절] 버튼과 함께 보내고 message_id를 저장한다. 국내·미국 공용."""
    message_id = notifier.send(
        proposal_message(p, policy),
        buttons=[[("✅ 승인", CALLBACK_APPROVE + p.id), ("❌ 거절", CALLBACK_REJECT + p.id)]],
    )
    store.update(p.id, message_id=message_id)
    logger.warning("주문 제안 전송: %s %s %s (%s)", p.name, p.side, p.price_text(), p.source)
    review.enqueue(p, message_id)  # 차트·경제 봇 의견을 답장으로 (봇이 설치했을 때만, 비동기)
    return message_id


def proposal_message(p: P.Proposal, policy: TradingPolicy) -> str:
    if p.source == STOP_LOSS:
        mark = "🛑 <b>손절 제안</b>"
    elif p.source == TAKE_PROFIT:
        mark = "💰 <b>익절 제안</b>"
    elif p.source == "add":
        mark = "➕ <b>추가 매수 제안</b>"
    elif p.source == "resting":
        mark = "📌 <b>지정가 걸어두기</b>"
    elif p.source == "manual":
        mark = "✋ <b>매수 주문 확인</b>" if p.side == "buy" else "✋ <b>매도 주문 확인</b>"
    elif p.side == "buy":
        mark = "🟢 <b>매수 제안</b>"
    else:
        mark = "🔴 <b>매도 제안</b>"
    if p.is_us:
        mark = "🇺🇸 " + mark
    lines = [
        f"{mark}  {escape(p.name)} <code>{p.stock_code}</code>",
        f"<b>{p.price_text()}</b> (지정가)",
        "",
    ]
    if p.source == "manual":
        lines.append(f"✍️ {escape(p.reason)}")
    elif p.source == "disclosure":
        lines.append(f"📰 확신 {p.confidence}% · {escape(p.reason)}")
    else:
        lines.append(f"📊 {escape(p.reason)}")
    if p.signal_at and p.signal_at[:10] != p.created_at[:10]:
        lines.append(f"🕒 장외 신호 ({p.signal_at[5:16].replace('T', ' ')}) — 오늘 가격으로 재평가")
    if p.technical:
        lines.append(f"📈 {escape(p.technical)}")
    if p.rcept_no:
        lines.append(
            f'<a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo={p.rcept_no}">공시 원문</a>'
        )
    lines += ["", f"⏱ {policy.proposal_ttl_min}분 안에 승인하지 않으면 만료"]
    return "\n".join(lines)


def deferred_message(p: P.Proposal, policy: TradingPolicy) -> str:
    side = "매수" if p.side == "buy" else "매도"
    ref = f" · 현재 {p.ref_price:,}원" if p.ref_price else ""
    return (
        f"🕒 <b>장외 {side} 신호</b>  {escape(p.name)} <code>{p.stock_code}</code>{ref}\n"
        f"📰 확신 {p.confidence}% · {escape(p.reason)}\n\n"
        f"<i>다음 장 {policy.release_after} 이후 그때 가격·차트로 다시 평가해 제안합니다.</i>"
    )


def _now() -> datetime:
    return datetime.now(KST)


def _float(value: object) -> float:
    try:
        return float(str(value).strip().replace(",", "").replace("%", "") or 0)
    except ValueError:
        return 0.0
