"""매일 관심종목 브리핑 — 키움 앱 관심종목 + 보유 종목.

    국내  평일 08:40 (장 전)       삼성전자·SK하이닉스 등
    미국  뉴욕 09:00 (개장 30분 전)  TQQQ·AGQ·ETHU 등

종목마다: 현재가 · 추세(200일·50일선) · RSI · 지금 규칙의 판단 · 기다릴 가격.
보유 종목이면 손절·익절·추세이탈 가격, 아니면 매수 신호 가격. 실시간 제안(us_trader·
proposer.check_exits)과 **같은 규칙(decide)**을 쓰므로 브리핑과 실시간 알림이 서로 다르게
말하지 않는다.

조회만 한다. 명령(/buy …)은 줄에 적어 두고, 봇이 [▶] 버튼을 붙인다(누르면 [승인] 단계로).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..account import fetch_balance, fetch_us_balance
from ..app_watchlist import fetch_all
from ..backtest.prices import bars_from_rows, parse_price
from ..client import KiwoomClient
from ..errors import KiwoomError
from ..market import domestic, overseas
from ..market_hours import KST, NY
from ..notify.telegram import escape
from ..us_order import resolve_exchange, round_price
from .events import active_event
from .indicators import sma
from .policy import TradingPolicy
from .sizing import round_to_tick
from .us_trader import (
    UnderlyingView,
    buy_triggers,
    decide,
    events_line,
    kr_policy,
    money,
    split_adjusted_closes,
    underlying_view,
)

logger = logging.getLogger(__name__)


@dataclass
class Item:
    code: str
    exchange: str = ""  # 미국만
    held_qty: float = 0
    held_rate: float | None = None  # 보유 수익률 (미보유 None)
    avg_price: float = 0.0
    sellable: float = 0


def kr_report(kiwoom: KiwoomClient, policy: TradingPolicy) -> str:
    items: dict[str, Item] = {}
    try:
        for s in fetch_all(kiwoom):
            if not s.is_us:
                items.setdefault(s.code, Item(s.code))
    except KiwoomError as exc:
        logger.warning("앱 관심종목 조회 실패: %s", exc)
    for h in fetch_balance(kiwoom).holdings:
        items[h.stock_code] = Item(
            h.stock_code,
            held_qty=h.quantity,
            held_rate=h.profit_rate,
            avg_price=h.avg_price,
            sellable=h.sellable,
        )
    if not items:
        return ""
    now = datetime.now(KST)
    lines = [f"<b>🇰🇷 국내 관심·보유 브리핑</b> ({now:%m/%d %a})", ""]
    rules = kr_policy(policy)
    for it in items.values():
        try:
            lines += _kr_block(kiwoom, it, rules, now)
        except (KiwoomError, ValueError) as exc:
            lines.append(f"<b>{it.code}</b> 조회 실패: {escape(str(exc)[:100])}")
        lines.append("")
    lines.append(_footer(rules, krw=True))
    return "\n".join(lines)


def us_report(kiwoom: KiwoomClient, policy: TradingPolicy) -> str:
    items: dict[str, Item] = {}
    try:
        for s in fetch_all(kiwoom):
            if s.is_us:
                items.setdefault(s.code, Item(s.code, exchange=s.exchange))
    except KiwoomError as exc:
        logger.warning("앱 관심종목 조회 실패: %s", exc)
    for sym in policy.us_symbols:
        items.setdefault(sym, Item(sym))
    balance = fetch_us_balance(kiwoom)
    for h in balance.holdings:
        it = items.setdefault(h.symbol, Item(h.symbol, exchange=h.exchange))
        it.held_qty, it.held_rate, it.avg_price, it.sellable = (
            h.quantity,
            h.profit_rate,
            h.avg_price,
            h.sellable,
        )
    now = datetime.now(NY)
    lines = [
        f"<b>🇺🇸 미국 관심·보유 브리핑</b> ({now:%m/%d %a} · 뉴욕 {now:%H:%M})",
        f"외화 주문가능 ${balance.usd_orderable:,.2f}",
    ]
    if ev := events_line(now):
        lines.append(escape(ev))
    lines.append("")
    exchanges: dict[str, str] = {}
    unders: dict[str, UnderlyingView | None] = {}  # 같은 기초지수를 종목마다 다시 받지 않게
    for it in items.values():
        try:
            lines += _us_block(kiwoom, it, policy, now, exchanges, unders)
        except (KiwoomError, ValueError) as exc:
            lines.append(f"<b>{it.code}</b> 조회 실패: {escape(str(exc)[:100])}")
        lines.append("")
    lines.append(_footer(policy, krw=False))
    return "\n".join(lines)


# --- 종목 한 개 ---


def _us_block(
    kiwoom: KiwoomClient,
    it: Item,
    policy: TradingPolicy,
    now: datetime,
    exchanges: dict[str, str],
    unders: dict[str, UnderlyingView | None],
) -> list[str]:
    exchange = it.exchange
    if exchange:
        quote = overseas.quote(kiwoom, it.code, exchange)
    else:
        exchange, quote = resolve_exchange(kiwoom, it.code)
    price = parse_price(quote.get("cur_prc", "0"))
    change = str(quote.get("flu_rt", "")).strip()
    day = now.strftime("%Y%m%d")
    rows = overseas.daily_price(kiwoom, it.code, day, exchange, max_pages=12)
    bars = [b for b in sorted(bars_from_rows(rows, it.code), key=lambda b: b.date) if b.date < day]
    closes = split_adjusted_closes(bars)

    block, extra = None, []
    if policy.us_event_hold and (event := active_event(now)):
        block = f"{event.label} 앞 (발표 30분 뒤까지)"
    if under := policy.underlying_of(it.code):
        if under not in unders:
            try:
                unders[under] = underlying_view(kiwoom, under, policy, now, exchanges=exchanges)
            except (KiwoomError, ValueError) as exc:
                logger.warning("기초지수 %s 조회 실패: %s", under, exc)
                unders[under] = None
        view = unders[under]
        if view is not None:
            extra.append(f"   {'🧭' if view.ok else '🧭⛔'} 기초지수 {escape(view.text)}")
            if not view.ok and block is None:
                block = f"기초지수 하락 추세 ({view.text})"
    return _block(
        f"🇺🇸 <b>{escape(it.code)}</b> {escape(str(quote.get('stk_nm', '')).strip())[:20]}",
        price,
        change,
        closes,
        it,
        policy,
        krw=False,
        buy_cmd=lambda p: f"/buy {it.code} 전량 {round_price(p, up=False):.2f}",
        sell_cmd=lambda p: f"/sell {it.code} 전량 {round_price(p, up=False):.2f}",
        buy_block=block,
        extra=extra,
    )


def _kr_block(kiwoom: KiwoomClient, it: Item, policy: TradingPolicy, now: datetime) -> list[str]:
    info = domestic.stock_info(kiwoom, it.code)
    price = parse_price(info.get("cur_prc", "0"))
    change = str(info.get("flu_rt", "")).strip()
    day = now.strftime("%Y%m%d")
    rows = domestic.daily_chart(kiwoom, it.code, day, max_pages=1, warn_on_truncate=False)
    closes = [
        b.close for b in sorted(bars_from_rows(rows, it.code), key=lambda b: b.date) if b.date < day
    ]
    qty = max(1, policy.budget_per_trade // max(1, int(price)))
    return _block(
        f"🇰🇷 <b>{escape(str(info.get('stk_nm', it.code)).strip())}</b> <code>{it.code}</code>",
        price,
        change,
        closes,
        it,
        policy,
        krw=True,
        # 국내 /buy·/sell은 확인 없이 즉시 체결되는 경로라 봇이 실행 버튼을 붙이지 않는다(복사만)
        buy_cmd=lambda p: f"/buy {it.code} {qty} {round_to_tick(p, up=False)}",
        sell_cmd=lambda p: f"/sell {it.code} {int(it.sellable)} {round_to_tick(p, up=False)}",
    )


def _block(
    title,
    price,
    change,
    closes,
    it: Item,
    policy,
    *,
    krw,
    buy_cmd,
    sell_cmd,
    buy_block: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    held = it.held_rate is not None
    d = decide(closes, price, policy, holding_rate=it.held_rate, krw=krw, buy_block=buy_block)
    pct = f" ({change}%)" if change else ""
    head = f"{title} {money(price, krw)}{pct}"
    if held:
        qty = f"{it.held_qty:,.0f}" if it.held_qty == int(it.held_qty) else f"{it.held_qty:,.4f}"
        head += f" · 보유 {qty}주 ({it.held_rate:+.2f}%)"
    mark = {"buy": "🟢", "sell": "🔴"}.get(d.side or "", "▪️")
    out = [head, f"   {escape(d.summary)}", f"   {mark} {escape(d.reason)}", *(extra or [])]

    if held and it.avg_price:
        levels = []
        if policy.us_stop_loss_pct:
            levels.append(
                f"🛑 손절 {money(it.avg_price * (1 - policy.us_stop_loss_pct / 100), krw)}"
            )
        take = (
            it.avg_price * (1 + policy.us_take_profit_pct / 100) if policy.us_take_profit_pct else 0
        )
        if take:
            # 표시 가격과 버튼 명령의 가격이 같아야 한다 (호가단위로 내림한 값 하나만 쓴다)
            take = round_to_tick(take, up=False) if krw else round_price(take, up=False)
            levels.append(f"💰 익절 {money(take, krw)}")
        trend = sma([*closes, price], policy.us_trend_sma)
        if trend:
            levels.append(f"🔴 추세이탈 {money(trend * (1 - policy.us_sma_buffer_pct / 100), krw)}")
        if levels:
            out.append("   " + " · ".join(levels))
        if take and it.sellable:
            out.append(f"   익절가에 걸어두기 → {sell_cmd(take)}")
    else:
        trig = buy_triggers(closes, policy)
        if buy_block:
            out.append(f"   ⏸ 자동 매수 보류 중 — {escape(buy_block)}")
        if "pullback" in trig and trig["pullback"] < price:
            out.append(
                f"   🎯 매수 신호 {money(trig['pullback'], krw)} 이하 → {buy_cmd(trig['pullback'])}"
            )
        if "breakout" in trig and trig["breakout"] > price:
            out.append(f"   🎯 200일선 돌파 {money(trig['breakout'], krw)} 이상")
    return out


def _footer(policy: TradingPolicy, *, krw: bool) -> str:
    return (
        f"<i>규칙: 200일선 추세 · RSI {policy.us_pullback_rsi:.0f} 이하 눌림 · "
        f"손절 -{policy.us_stop_loss_pct:g}% · 익절 +{policy.us_take_profit_pct:g}%"
        f"{' · 국내 명령은 복사해서 보내세요(즉시 주문)' if krw else ''}</i>"
    )
