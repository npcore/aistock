"""텔레그램 대화형 봇.

메시지로 명령을 받아 조회·주문을 수행한다.

## 보안

**허용된 chat_id에서 온 메시지만 처리한다.** 봇 사용자명은 텔레그램 검색으로 찾을 수 있어
화이트리스트가 없으면 누구나 잔고를 조회하고 주문을 낼 수 있다. 이건 선택이 아니다.

## 주문

`/buy`, `/sell` 은 **확인 절차 없이 즉시 실계좌에 나간다**(사용자 선택). 남아 있는 방어선은
`MAX_ORDER_AMOUNT` 상한과 종목코드·수량 검증뿐이다. 휴대폰을 분실하거나 잠금이 풀린 채로
두면 계좌가 그대로 노출되므로, 폰 잠금과 텔레그램 잠금을 켜두는 것이 사실상 마지막 방어선이다.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .client import KiwoomClient
from .config import CACHE_DIR, load_env
from .errors import KiwoomError
from .market import domestic
from .notify.telegram import API_BASE, TelegramConfig, TelegramError, TelegramNotifier, escape
from .trading import ProposalStore, TradingPolicy
from .watchlist import Watchlist

logger = logging.getLogger(__name__)

# 처리한 update_id를 남겨 재시작 시 중복 처리를 막는다.
OFFSET_PATH = CACHE_DIR / "bot_offset.txt"

# 폴링 실패 시 대기. 연속 실패마다 2배씩 늘리되 상한을 둔다.
ALLOWED_UPDATES = '["message","edited_message","callback_query"]'

BASE_BACKOFF = 3.0
MAX_BACKOFF = 60.0

Handler = Callable[["BotContext", list[str]], str]

HELP = """<b>📖 설명서</b>
채팅창에 <b>/</b> 만 쳐도 명령 목록이 뜹니다.

<b>🇺🇸 TQQQ 사고팔기</b>
/us — 지금 신호·매수 신호 가격·전량 수량
/buy TQQQ 전량 — 외화 전부로 매수 (현재가)
/buy TQQQ 전량 65 — 외화 전부로 $65 지정가
/sell TQQQ 전량 — 보유 전부 매도 (현재가)
/buy TQQQ 10 68.20 — 수량·가격 직접
→ 보내면 확인 메시지가 오고, <b>[✅ 승인]</b>을 눌러야 주문됩니다

<b>💼 내 계좌</b>
/balance — 잔고·예수금 (국내·미국)
/orders — 걸려 있는(미체결) 주문
/cancel — 걸린 주문 보기·취소 (/cancel TQQQ = 그 종목 전부)

<b>🔔 봇이 먼저 보내는 것</b>
• 📌 매일 밤 11시쯤: "신호 가격에 걸어둘까요?"
• 🟢 매수 신호 · 🛑 손절 · 💰 익절 · 🔴 추세 이탈
→ 모두 [승인]을 눌러야 주문, 10분 지나면 만료
/proposals — 최근 24시간 제안과 결과
/brief — 관심·보유 종목 브리핑 (매일 국내 08:40 · 미국 개장 30분 전 자동)

<b>💬 경제 봇</b>
/ 없이 그냥 물어보세요. "TQQQ 지금 살까?"
시세·뉴스를 찾아보고 의견을 줍니다 (주문은 안 함)
답에 나온 명령은 <b>[▶]</b> 버튼으로 바로 실행, <b>[📋]</b>로 복사

<b>더 자세히</b>
/help 미국 · /help 자동 · /help 경제봇 · /help 국내"""

HELP_TOPICS = {
    "미국": """<b>🇺🇸 TQQQ 주문 자세히</b>

<b>형식</b>  /buy 종목 수량 가격
• 수량 자리에 <b>전량</b> → 봇이 계산 (매수: 키움이 계산한 최대 수량, 매도: 보유 전부)
• 전량일 때 가격을 빼면 현재가 (매수는 센트 올림, 매도는 내림)
• 숫자 수량이면 가격(달러)이 꼭 필요합니다
• 지정가만 됩니다 (시장가 X)

<b>예시</b>
/buy TQQQ 전량 — 지금 가격에 살 수 있는 만큼
/buy TQQQ 전량 65 — $65에 걸어두기
/buy TQQQ 100 69.50 — 100주를 $69.50에
/sell TQQQ 전량 75 — 보유 전부 $75에

<b>승인 후</b>
• 걸어둔 주문은 /orders 에 보입니다
• 미체결 주문이 있으면 외화가 묶여 전량 매수가 0주로 나옵니다 → /cancel 주문번호
• 체결되면 /balance 에 보유로 나옵니다

<b>장외 주문</b>
정규장(밤 10:30/11:30~새벽 5/6시) 밖에서도 승인하면 키움으로 보냅니다.
키움이 받아주면 프리·애프터마켓 주문으로 걸리고, 시간대가 안 맞으면 키움의 거절 사유가 그대로 표시됩니다.
장외는 호가가 얇아 체결이 늦거나 안 될 수 있습니다.

<b>🕒 예약</b>
키움이 아예 주문을 안 받는 시간(한국 아침 7~9시 무렵 "장종료")에 승인하면 [🕒 예약] 버튼이 뜹니다.
누르면 봇이 보관했다가 키움이 주문을 받기 시작하면 그 가격 그대로 넣고 알려줍니다.
24시간 유효 · /orders 에 🕒로 표시 · /cancel 로 취소

<b>승인을 눌러도 주문이 안 나가는 경우</b>
• 제안 후 10분이 지남
• 봇 제안인데 가격이 2% 넘게 불리하게 움직임 (직접 입력한 주문은 예외)
• 키움이 거절 (시간대·잔고 등 — 사유가 표시됨)""",
    "자동": """<b>🔔 봇의 자동 제안 (TQQQ)</b>
1분마다 확인, 뉴욕 10:00(한국 밤 11시/자정) 이후부터

<b>매수 (미보유일 때)</b>
• 📌 걸어두기 — 하루 한 번 "신호 가격 $X에 외화 전액 걸어둘까요?"
  승인하면 그 가격에 닿는 순간 자동 체결 (당일 유효)
• 🟢 눌림 — 200일선 위 상승 추세에서 RSI 40 이하
• 🟢 돌파 — 200일선을 3% 넘게 뚫고 올라감
• /us 의 🎯 "매수 신호 가격"이 이 기준입니다

<b>⏸ 매수 보류 (매도는 그대로)</b>
• FOMC·CPI·고용보고서 발표 전 거래일 ~ 발표 30분 뒤
• TQQQ는 기초지수 QQQ가 하락 추세(200일선 아래)면 보류
• 직접 /buy 하면 막지 않고 ⚠️ 경고만 붙임

<b>제안이 오면</b>
• 답장으로 3개월 차트 + 경제 봇 의견(🟢찬성·🟡주의·🔴반대)

<b>매도 (보유 중일 때)</b>
• 🛑 손절 −10% · 💰 익절 +20%
• 🔴 추세 이탈 — 200일선보다 3% 넘게 아래

<b>알림 규칙</b>
• 모두 [✅ 승인]을 눌러야 주문, 10분 지나면 만료
• 못 보고 만료되면 15분 뒤 한 번 더 (하루 한 번까지)
• 거절하면 그날은 다시 안 물어봄
• 이미 걸린 매수 주문이 있으면 추가 매수 제안 안 함
• 미국 휴장일엔 쉼""",
    "경제봇": """<b>💬 경제 봇</b>
/ 없이 아무 말이나 보내면 경제 봇이 답합니다 (30초~1분).

<b>이런 걸 물어보세요</b>
• "TQQQ 지금 들어가도 될까?"
• "81불에 팔았는데 언제 다시 사?"
• "이번 주 일정 중에 조심할 거 있어?"
• "70달러 깨지면 어떻게 해?"

<b>할 수 있는 것</b>
• 시세·이동평균·RSI·고점 대비 낙폭 조회
• 내 잔고 확인 · 최신 뉴스·실적·금리 일정 검색
• 매매 기록·원칙을 말하면 기억

<b>못 하는 것</b>
• 주문 — 사려면 /buy … 후 [승인]

<b>관리</b>
/notes — 기억하는 메모 · /forget — 메모 지우기
/reset — 대화 맥락 초기화 (메모는 유지)
하루 40회까지 (비용 때문)""",
    "국내": """<b>🇰🇷 국내 주식</b>

<b>조회</b>
/quote 005930 — 현재가
/watchlist — 관심종목 시세 · /trend [종목] — 추이 차트
/add 005930 · /remove 005930 — 관심종목

<b>주문</b> ⚠️ 국내는 <b>확인 없이 즉시</b> 주문됩니다
/buy 005930 10 230000 — 지정가 매수
/buy 005930 10 market — 시장가 매수
/sell 005930 10 235000 — 지정가 매도
/cancel 주문번호 — 취소

<b>공시 자동 제안</b>
DART 공시 → Claude 분석 → 차트 필터 → [승인] 버튼 제안
• 하루 3건까지, 1건 100만원
• 장 마감 후 공시는 다음 날 09:05에 다시 판단
• 손절 −5% · 익절 +10% 제안""",
}

# 텔레그램 채팅창에 / 를 치면 뜨는 메뉴. 외우지 않아도 되게 한다.
MENU = [
    ("help", "설명서 (/help 미국 · 자동 · 경제봇 · 국내)"),
    ("us", "TQQQ 신호 · 매수 신호 가격 · 전량 수량"),
    ("buy", "매수 — 예: /buy TQQQ 전량"),
    ("sell", "매도 — 예: /sell TQQQ 전량"),
    ("balance", "잔고 · 예수금"),
    ("orders", "걸려 있는 주문"),
    ("cancel", "걸린 주문 보기·취소 — /cancel TQQQ"),
    ("brief", "관심·보유 종목 브리핑 (/brief 미국 · 국내)"),
    ("proposals", "최근 24시간 자동 제안"),
    ("notes", "경제 봇 메모"),
    ("reset", "경제 봇 대화 초기화"),
]


@dataclass
class BotContext:
    kiwoom: KiwoomClient
    notifier: TelegramNotifier
    watchlist: Watchlist
    store: ProposalStore = field(default_factory=ProposalStore)
    policy: TradingPolicy = field(default_factory=TradingPolicy.from_env)


def _fmt_quote(code: str, data: dict[str, Any]) -> str:
    price = str(data.get("cur_prc", "")).strip()
    arrow = "🔺" if price.startswith("+") else "🔻" if price.startswith("-") else "▪️"
    name = escape(data.get("stk_nm", code))
    lines = [
        f"{arrow} <b>{name}</b> <code>{code}</code>",
        f"   <b>{escape(price.lstrip('+-'))}</b> ({escape(str(data.get('flu_rt', '')))}%)",
    ]
    detail = []
    for key, label in (("open_pric", "시"), ("high_pric", "고"), ("low_pric", "저")):
        v = str(data.get(key, "")).strip().lstrip("+-")
        if v.isdigit():
            detail.append(f"{label} {int(v):,}")
    if detail:
        lines.append("   " + " / ".join(detail))
    return "\n".join(lines)


# --- 명령 구현 ---


def cmd_help(ctx: BotContext, args: list[str]) -> str:
    if not args:
        return HELP
    topic = args[0].strip()
    for key, text in HELP_TOPICS.items():
        if topic.startswith(key) or key.startswith(topic):
            return text
    return f"그런 항목이 없습니다. /help {' · /help '.join(HELP_TOPICS)}"


def cmd_quote(ctx: BotContext, args: list[str]) -> str:
    if not args:
        return "종목코드를 입력하세요. 예: /quote 005930"
    try:
        return _fmt_quote(args[0], domestic.stock_info(ctx.kiwoom, args[0]))
    except KiwoomError as exc:
        return f"조회 실패: {escape(str(exc))}"


def cmd_watchlist(ctx: BotContext, args: list[str]) -> str:
    wl = Watchlist.load()
    if not wl:
        return "관심종목이 비어 있습니다. /add 005930"
    out = []
    for e in wl.entries:
        try:
            out.append(_fmt_quote(e.code, domestic.stock_info(ctx.kiwoom, e.code)))
        except KiwoomError:
            out.append(f"▪️ <code>{e.code}</code> {escape(e.note)} (조회 실패)")
    return "<b>📋 관심종목</b>\n\n" + "\n\n".join(out)


def cmd_add(ctx: BotContext, args: list[str]) -> str:
    if not args:
        return "종목코드를 입력하세요. 예: /add 005930"
    wl = Watchlist.load()
    added = []
    for code in args:
        note = ""
        try:
            note = domestic.stock_info(ctx.kiwoom, code).get("stk_nm", "")
        except KiwoomError:
            pass
        if wl.add(code, note):
            added.append(f"{code} {note}".strip())
    wl.save()
    return f"추가 {len(added)}건: {escape(', '.join(added))}" if added else "이미 있는 종목입니다."


def cmd_remove(ctx: BotContext, args: list[str]) -> str:
    if not args:
        return "종목코드를 입력하세요."
    wl = Watchlist.load()
    removed = [c for c in args if wl.remove(c)]
    wl.save()
    return f"삭제 {len(removed)}건: {escape(', '.join(removed))}" if removed else "목록에 없습니다."


def cmd_balance(ctx: BotContext, args: list[str]) -> str:
    from .account import fetch_balance, fetch_deposit, fetch_us_balance
    from .notify.format import account_message

    try:
        balance = fetch_balance(ctx.kiwoom)
    except (KiwoomError, ValueError) as exc:
        return f"잔고 조회 실패: {escape(str(exc))}"
    # 예수금·미국 잔고는 실패해도 국내 잔고는 보여준다.
    extras = []
    for fetch in (fetch_deposit, fetch_us_balance):
        try:
            extras.append(fetch(ctx.kiwoom))
        except (KiwoomError, ValueError) as exc:
            logger.warning("%s 실패: %s", fetch.__name__, exc)
            extras.append(None)
    message = account_message(balance, *extras)
    from .brokers.meritz import MeritzConfig, MeritzError, fetch_snapshot

    if MeritzConfig.available():
        from .notify.format import meritz_message

        try:
            message += "\n\n" + meritz_message(fetch_snapshot())
        except MeritzError as exc:
            message += f"\n\n<b>🏦 메리츠증권</b>\n조회 실패: {escape(str(exc)[:200])}"
    return message


def cmd_orders(ctx: BotContext, args: list[str]) -> str:
    from . import us_order
    from .order import pending_orders

    try:
        rows = pending_orders(ctx.kiwoom)
        us_rows = us_order.pending_orders(ctx.kiwoom)
    except KiwoomError as exc:
        return f"조회 실패: {escape(str(exc))}"
    queued = _queued(ctx)
    if not rows and not us_rows and not queued:
        return "미체결 주문이 없습니다."
    out = ["<b>미체결 주문</b>", ""]
    for p in queued:
        out.append(
            f"🕒 <b>예약</b> {escape(p.stock_code)} {escape(p.price_text())}\n"
            f"   키움이 주문을 받기 시작하면 자동 제출 · 취소 /cancel {p.id}"
        )
    for r in us_rows:
        out.append(
            f"🇺🇸 <code>{r.order_no}</code> {escape(r.symbol)} {escape(r.side)}\n"
            f"   {r.quantity:,}주 중 {r.remaining:,}주 미체결 @ ${r.price:,.2f} · {escape(r.status)}"
        )
    for r in rows:
        out.append(
            f"<code>{r.order_no}</code> {escape(r.name)} {escape(r.side)}\n"
            f"   {r.quantity:,}주 중 {r.remaining:,}주 미체결 @ {r.price:,}원 · {escape(r.status)}"
        )
    return "\n".join(out)


def cmd_trend(ctx: BotContext, args: list[str]) -> str:
    from .backtest import PriceCache
    from .chart import compute_stats, render_trends, stats_text

    wl = Watchlist.load()
    codes = args or wl.codes
    if not codes:
        return "종목코드를 입력하거나 관심종목을 추가하세요."
    prices = PriceCache(ctx.kiwoom)
    items, stats = [], []
    for code in codes[:5]:
        history = prices.get(code)
        if history is None:
            continue
        name = wl.note_for(code) or code
        items.append((history, code, name))
        st = compute_stats(history, code, name)
        if st:
            stats.append(st)
    if not items:
        return "일봉을 가져오지 못했습니다."
    png = render_trends(items, days=126, title="최근 126거래일 추이")
    ctx.notifier.send_photo(png, stats_text(stats))
    return ""  # 사진으로 이미 보냈다


ALL_WORDS = {"전량", "전부", "all", "max"}


def _us_order_proposal(ctx: BotContext, side: str, args: list[str]) -> str:
    """미국 종목은 바로 주문하지 않는다. [승인] 버튼이 달린 확인 메시지를 보낸다."""
    from .trading.us_trader import manual_proposal
    from .us_order import UsOrderError

    usage = (
        f"형식: /{side} TQQQ 수량 가격  예: /{side} TQQQ 10 68.20\n"
        f"전량: /{side} TQQQ 전량 [가격] — 가격을 빼면 현재가"
    )
    if len(args) < 2:
        return usage
    symbol, qty_raw = args[0], args[1].lower()
    price_raw = args[2].lower().lstrip("$") if len(args) >= 3 else ""
    if price_raw in ("market", "시장가"):
        return "미국주식은 지정가만 됩니다. 예: /buy TQQQ 10 68.20"
    all_in = qty_raw in ALL_WORDS
    if not all_in and not price_raw:
        return usage
    try:
        quantity = None if all_in else int(qty_raw)
        price = float(price_raw) if price_raw else None
    except ValueError:
        return f"수량·가격이 숫자가 아닙니다: {escape(qty_raw)} {escape(price_raw)}"
    try:
        manual_proposal(
            ctx.kiwoom,
            ctx.notifier,
            ctx.store,
            ctx.policy,
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
        )
    except UsOrderError as exc:
        return f"❌ 주문 불가\n{escape(str(exc))}"
    except KiwoomError as exc:
        return f"❌ 조회 실패\n{escape(str(exc))}"
    return ""  # 확인 메시지를 이미 보냈다


def _order(ctx: BotContext, side: str, args: list[str]) -> str:
    from .order import OrderError, OrderRequest, OrderType, place
    from .us_order import is_us_symbol

    if args and is_us_symbol(args[0]):
        return _us_order_proposal(ctx, side, args)
    if not ctx.policy.kr_enabled:
        return "이 계정은 국내 주식 주문을 쓰지 않습니다 (TRADE_KR_ENABLED=0). 미국: /buy TQQQ 전량"
    if len(args) < 3:
        return f"형식: /{side} 종목코드 수량 가격|market"
    code, qty_raw, price_raw = args[0], args[1], args[2].lower()
    try:
        quantity = int(qty_raw)
    except ValueError:
        return f"수량이 숫자가 아닙니다: {escape(qty_raw)}"

    if price_raw in ("market", "시장가"):
        order_type, price = OrderType.MARKET, 0
    else:
        try:
            price = int(price_raw)
        except ValueError:
            return f"가격이 숫자가 아닙니다: {escape(price_raw)}"
        order_type = OrderType.LIMIT

    request = OrderRequest(
        side=side, stock_code=code, quantity=quantity, price=price, order_type=order_type
    )
    try:
        result = place(ctx.kiwoom, request)
    except OrderError as exc:
        return f"❌ 주문 거부\n{escape(str(exc))}"
    except KiwoomError as exc:
        return f"❌ 주문 실패\n{escape(str(exc))}"

    mark = "🟢 매수" if side == "buy" else "🔴 매도"
    return (
        f"{mark} <b>주문 접수</b>\n\n"
        f"{escape(request.describe())}\n"
        f"주문번호 <code>{result.order_no}</code>\n\n"
        f"<i>체결 확인: /orders</i>"
    )


def cmd_buy(ctx: BotContext, args: list[str]) -> str:
    return _order(ctx, "buy", args)


def cmd_sell(ctx: BotContext, args: list[str]) -> str:
    return _order(ctx, "sell", args)


def cmd_cancel(ctx: BotContext, args: list[str]) -> str:
    """미체결 주문 취소.

    /cancel             걸려 있는 주문 목록 (번호마다 바로 누를 수 있는 취소 명령)
    /cancel 000012678   그 주문 취소
    /cancel TQQQ        그 종목 미체결 전부 취소 (국내는 6자리 코드)
    /cancel 전부         모든 미체결 취소
    """
    from . import us_order
    from .order import OrderError, cancel, pending_orders

    try:
        kr = pending_orders(ctx.kiwoom)
        us = us_order.pending_orders(ctx.kiwoom)
    except KiwoomError as exc:
        return f"미체결 조회 실패: {escape(str(exc))}"
    queued = _queued(ctx)

    if not args:
        if not kr and not us and not queued:
            return "걸려 있는 주문이 없습니다."
        out = ["<b>걸려 있는 주문</b> — 명령을 눌러 취소", ""]
        for p in queued:
            out.append(
                f"🕒 예약 {escape(p.stock_code)} {escape(p.price_text())}\n   /cancel {p.id}"
            )
        for o in us:
            out.append(
                f"🇺🇸 {escape(o.symbol)} {escape(o.side)} {o.remaining:,}주 @ ${o.price:,.2f}"
                f"\n   /cancel {o.order_no}"
            )
        for o in kr:
            out.append(
                f"🇰🇷 {escape(o.name)} {escape(o.side)} {o.remaining:,}주 @ {o.price:,}원"
                f"\n   /cancel {o.order_no}"
            )
        out.append("\n종목 전부: /cancel TQQQ · 모두: /cancel 전부")
        return "\n".join(out)

    key = args[0].strip()
    everything = key in ("전부", "전체", "all")
    us_targets = [
        o for o in us if everything or o.order_no == key or o.symbol.upper() == key.upper()
    ]
    kr_targets = [o for o in kr if everything or o.order_no == key or o.stock_code == key]
    q_targets = [
        p for p in queued if everything or p.id == key or p.stock_code.upper() == key.upper()
    ]
    if not us_targets and not kr_targets and not q_targets:
        return f"취소할 미체결 주문이 없습니다: {escape(key)}\n목록: /cancel"

    done, failed = [], []
    for p in q_targets:
        from .trading.executor import cancel_queued

        if cancel_queued(ctx.store, p.id):
            done.append(f"🕒 예약 {p.stock_code} {p.price_text()} ({p.id})")
    exchanges: dict[str, str] = {}
    for o in us_targets:
        try:
            # 미체결 응답의 거래소명이 '미국'으로만 오는 경우가 있어, 시세로 거래소를 찾는다.
            exchange = o.exchange or exchanges.get(o.symbol)
            if not exchange:
                exchange = us_order.resolve_exchange(ctx.kiwoom, o.symbol)[0]
                exchanges[o.symbol] = exchange
            us_order.cancel(ctx.kiwoom, o.order_no, o.symbol, exchange)
            _forget_order(ctx, o.order_no)
            done.append(f"🇺🇸 {o.symbol} {o.remaining:,}주 @ ${o.price:,.2f} ({o.order_no})")
        except KiwoomError as exc:
            failed.append(f"{o.order_no}: {exc}")
    for o in kr_targets:
        try:
            cancel(ctx.kiwoom, o.order_no, o.stock_code)
            _forget_order(ctx, o.order_no)
            done.append(f"🇰🇷 {o.name} {o.remaining:,}주 @ {o.price:,}원 ({o.order_no})")
        except (OrderError, KiwoomError) as exc:
            failed.append(f"{o.order_no}: {exc}")

    lines = []
    if done:
        lines.append("<b>취소 접수</b>\n" + "\n".join(escape(d) for d in done))
    if failed:
        lines.append("<b>취소 실패</b>\n" + "\n".join(escape(f) for f in failed))
    lines.append("<i>확인: /orders · 외화 주문가능: /balance</i>")
    return "\n\n".join(lines)


def _forget_order(ctx: BotContext, order_no: str) -> None:
    """취소한 주문의 제안 기록을 '취소됨'으로. 안 하면 봇이 아직 매수 중으로 보고 제안을 멈춘다."""
    store = getattr(ctx, "store", None)
    if store is not None:
        store.mark_cancelled(order_no)


def _queued(ctx: BotContext) -> list:
    """봇이 보관 중인 예약 주문 (키움에는 아직 안 들어감)."""
    from .trading import proposals as P

    store = getattr(ctx, "store", None)
    return [p for p in store.all() if p.status == P.QUEUED] if store else []


STATUS_LABEL = {
    "pending": "⏳ 대기",
    "queued": "🕒 예약",
    "cancelled": "🚫 취소됨",
    "deferred": "🕒 장외 보관",
    "executing": "⚙️ 처리 중",
    "ordered": "✅ 주문",
    "failed": "❌ 실패",
    "stale": "📉 보류",
    "rejected": "🚫 거절",
    "expired": "⌛ 만료",
    "blocked": "🛑 필터",
}


def cmd_proposals(ctx: BotContext, args: list[str]) -> str:
    from datetime import datetime, timedelta

    from .market_hours import KST

    # '오늘'이 아니라 최근 24시간 — 미국장은 한국 자정을 넘어 이어진다.
    since = datetime.now(KST) - timedelta(hours=24)
    rows = [p for p in ctx.store.all() if p.created >= since]
    if not rows:
        return "최근 24시간 주문 제안이 없습니다."
    out = ["<b>최근 24시간 주문 제안</b>", ""]
    for p in rows:
        side = {
            "stop_loss": "손절",
            "take_profit": "익절",
            "resting": "걸어두기",
            "add": "추가매수",
        }.get(p.source, "매수" if p.side == "buy" else "매도")
        flag = "🇺🇸 " if p.is_us else ""
        out.append(
            f"{STATUS_LABEL.get(p.status, p.status)} {p.created_at[11:16]} "
            f"{flag}{escape(p.name)} {side} {escape(p.price_text())}"
        )
        why = "; ".join(p.blocks) or p.note
        if why:
            out.append(f"   <i>{escape(why[:120])}</i>")
    return "\n".join(out)


def cmd_us(ctx: BotContext, args: list[str]) -> str:
    from .trading.us_trader import UsTrader

    return UsTrader(ctx.kiwoom, ctx.notifier, store=ctx.store, policy=ctx.policy).status()


def cmd_brief(ctx: BotContext, args: list[str]) -> str:
    """관심·보유 브리핑을 지금 받기. /brief 미국 · /brief 국내 · /brief (둘 다)"""
    from .trading.daily_brief import kr_report, us_report

    which = (args[0] if args else "").strip()
    parts = []
    if which in ("", "국내", "한국", "kr") and ctx.policy.kr_enabled:
        parts.append(kr_report(ctx.kiwoom, ctx.policy))
    if which in ("", "미국", "us"):
        parts.append(us_report(ctx.kiwoom, ctx.policy))
    return "\n\n".join(p for p in parts if p) or "관심종목·보유 종목이 없습니다."


COMMANDS: dict[str, Handler] = {
    "help": cmd_help,
    "start": cmd_help,
    "quote": cmd_quote,
    "watchlist": cmd_watchlist,
    "add": cmd_add,
    "remove": cmd_remove,
    "balance": cmd_balance,
    "orders": cmd_orders,
    "trend": cmd_trend,
    "buy": cmd_buy,
    "sell": cmd_sell,
    "cancel": cmd_cancel,
    "proposals": cmd_proposals,
    "brief": cmd_brief,
    "us": cmd_us,
}


class TelegramBot:
    """long-polling 방식. 웹훅을 열지 않아 공인 IP나 인증서가 필요 없다."""

    def __init__(
        self,
        config: TelegramConfig,
        kiwoom: KiwoomClient,
        *,
        allowed_chat_ids: set[str] | None = None,
        poll_timeout: int = 30,
        offset_path: Path | None = None,
    ) -> None:
        self.config = config
        self.kiwoom = kiwoom
        # 기본 허용 대상은 .env의 chat_id 하나뿐이다.
        self.allowed = allowed_chat_ids or _allowed_chat_ids(config)
        self.poll_timeout = poll_timeout
        self._client = httpx.Client(timeout=poll_timeout + 15)
        self.offset_path = offset_path or OFFSET_PATH
        self._offset = self._load_offset()
        self._failures = 0
        self._running = False
        self.stats = {"handled": 0, "rejected": 0, "errors": 0}
        self._proposer = None  # 장외 신호 재평가·손절·익절. 첫 tick에 만든다
        self._advisor = None  # 경제 봇. 첫 질문 때 만든다
        self._last_queue_try = 0.0
        self._advisor_busy = threading.Lock()  # 한 번에 한 질문만 (비용·순서 보장)
        self._advisor_day = ("", 0)  # (날짜, 오늘 질문 수)

    def close(self) -> None:
        self._client.close()

    def stop(self) -> None:
        self._running = False

    def run(self, *, max_cycles: int | None = None) -> None:
        notifier = TelegramNotifier(self.config)
        ctx = BotContext(self.kiwoom, notifier, Watchlist.load())
        self._running = True
        cycle = 0
        logger.info("봇 대기 시작 (허용 chat_id: %s)", ", ".join(sorted(self.allowed)))
        self._register_menu()
        try:
            while self._running:
                cycle += 1
                for update in self._poll():
                    self._dispatch(ctx, notifier, update)
                self._tick(ctx, notifier)
                if max_cycles is not None and cycle >= max_cycles:
                    return
        finally:
            self._running = False
            notifier.close()

    # --- 내부 ---

    def _poll(self) -> list[dict[str, Any]]:
        try:
            r = self._client.get(
                f"{API_BASE}/bot{self.config.token}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": self.poll_timeout,
                    # 명시하지 않으면 이전에 설정된 값을 따른다. 승인 버튼(callback_query)이
                    # 빠져 있으면 버튼을 눌러도 봇이 영영 모른다.
                    "allowed_updates": ALLOWED_UPDATES,
                },
            )
            payload = r.json()
        except httpx.TimeoutException:
            # long polling에서 타임아웃은 정상이다. 쉬지 않고 바로 다시 건다 —
            # 여기서 3초를 쉬면 그만큼 명령 응답이 늦어진다.
            logger.debug("폴링 타임아웃, 바로 재연결합니다.")
            return []
        except (httpx.HTTPError, ValueError) as exc:
            self._backoff(f"업데이트 수신 실패: {exc}")
            return []
        if not payload.get("ok"):
            # 429는 텔레그램이 얼마나 쉬어야 하는지 알려준다. 그걸 무시하고 고정 간격으로
            # 다시 때리면 제한이 길어진다.
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            self._backoff(
                f"getUpdates 실패: {payload.get('description')}",
                wait=float(retry_after) if retry_after else None,
            )
            return []

        self._failures = 0
        updates = payload.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
            # 처리하기 **전에** 저장한다. 주문 처리 도중 죽으면 재시작 시 같은 /buy가
            # 다시 실행되어 중복 체결이 난다. 놓치는 쪽이 두 번 사는 쪽보다 낫다.
            self._save_offset()
        return updates

    def _backoff(self, reason: str, *, wait: float | None = None) -> None:
        """연속 실패마다 대기를 늘린다.

        텔레그램이 502를 뱉는 동안 같은 간격으로 계속 때리면 429로 번지고, 그러면 제한이
        더 길어진다. 상한을 두는 이유는 장애가 풀렸을 때 너무 늦게 복귀하지 않기 위해서다.
        """
        self._failures += 1
        if wait is None:
            wait = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (self._failures - 1)))
        logger.error("%s — %.0f초 후 재시도 (연속 %d회)", reason, wait, self._failures)
        time.sleep(wait)

    def _load_offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        try:
            self.offset_path.parent.mkdir(parents=True, exist_ok=True)
            self.offset_path.write_text(str(self._offset))
        except OSError as exc:
            logger.warning("offset 저장 실패 — 재시작 시 중복 처리될 수 있습니다: %s", exc)

    # --- 경제 봇 ---

    def _advisor_command(
        self, ctx: BotContext, notifier: TelegramNotifier, name: str, rest: list[str]
    ) -> None:
        if name == "ask":
            if rest:
                self._ask(ctx, notifier, rest[0])
            else:
                self._reply(notifier, "질문을 함께 보내주세요. 예: /ask 지금 TQQQ 사도 될까?")
            return
        advisor = self._get_advisor(ctx)
        if advisor is None:
            self._reply(notifier, "ANTHROPIC_API_KEY가 없어 경제 봇을 쓸 수 없습니다.")
            return
        if name == "reset":
            advisor.reset()
            self._reply(notifier, "대화 맥락을 지웠습니다. 메모는 남아 있습니다 (/notes).")
        elif name == "notes":
            notes = advisor.notes
            self._reply(
                notifier,
                "<b>📝 경제 봇 메모</b>\n" + "\n".join(f"• {escape(n)}" for n in notes)
                if notes
                else "저장된 메모가 없습니다. 대화 중에 매매 기록을 말하면 기억합니다.",
            )
        elif name == "forget":
            advisor.forget_notes()
            self._reply(notifier, "메모를 모두 지웠습니다.")

    def _get_advisor(self, ctx: BotContext):
        from .advisor import Advisor

        if self._advisor is None:
            if not Advisor.has_credentials():
                return None
            self._advisor = Advisor(ctx.kiwoom)
        return self._advisor

    def _ask(self, ctx: BotContext, notifier: TelegramNotifier, question: str) -> None:
        """질문을 백그라운드에서 처리한다.

        웹 검색까지 하면 30초~1분이 걸린다. 폴링 루프에서 기다리면 그동안 승인 버튼이 먹통이
        되고 승인 유효시간이 흘러간다 — 그래서 스레드로 뺀다.
        """
        from .advisor import extract_commands, to_telegram_html

        advisor = self._get_advisor(ctx)
        if advisor is None:
            self._reply(notifier, "ANTHROPIC_API_KEY가 없어 경제 봇을 쓸 수 없습니다.")
            return
        today = time.strftime("%Y-%m-%d")
        day, count = self._advisor_day
        count = count if day == today else 0
        limit = _advisor_daily_limit()
        if limit and count >= limit:
            self._reply(
                notifier, f"오늘 질문 한도({limit}회)를 다 썼습니다. ADVISOR_DAILY_LIMIT로 조정."
            )
            return
        if not self._advisor_busy.acquire(blocking=False):
            self._reply(notifier, "앞 질문을 아직 분석 중입니다. 답이 오면 이어서 물어봐 주세요.")
            return
        self._advisor_day = (today, count + 1)

        try:
            placeholder = notifier.send("🤔 시세·뉴스를 살펴보는 중… (보통 30초~1분)")
        except TelegramError:
            placeholder = 0

        def work() -> None:
            # 폴링 루프와 HTTP 클라이언트를 나눠 쓴다.
            out = TelegramNotifier(self.config)
            try:
                answer = advisor.ask(question)
                html = to_telegram_html(answer)
                buttons = command_buttons(extract_commands(answer))
                try:
                    if placeholder and len(html) < 3800:
                        out.edit(placeholder, html, buttons=buttons)
                    else:
                        out.send(html, buttons=buttons)
                except TelegramError:
                    out.send(html, buttons=buttons)
            except Exception:
                logger.exception("경제 봇 처리 실패")
                try:
                    out.send("경제 봇 처리 중 오류가 났습니다. 잠시 후 다시 물어봐 주세요.")
                except TelegramError:
                    pass
            finally:
                out.close()
                self._advisor_busy.release()

        threading.Thread(target=work, name="advisor", daemon=True).start()

    def _register_menu(self) -> None:
        """채팅창 / 메뉴를 등록한다. 실패해도 봇은 돈다 (메뉴는 편의 기능)."""
        try:
            r = self._client.post(
                f"{API_BASE}/bot{self.config.token}/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in MENU]},
            )
            if not r.json().get("ok"):
                logger.warning("명령 메뉴 등록 실패: %s", r.json().get("description"))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("명령 메뉴 등록 실패: %s", exc)

    def _tick(self, ctx: BotContext, notifier: TelegramNotifier) -> None:
        """폴링 한 번마다 도는 주기 작업. 어느 하나가 실패해도 봇은 계속 돈다."""
        self._expire(ctx, notifier)
        if ctx.kiwoom is None:
            return
        if self._proposer is None:
            from .trading import TradeProposer
            from .trading.us_trader import UsTrader

            self._proposer = TradeProposer(ctx.kiwoom, notifier, store=ctx.store, policy=ctx.policy)
            self._us_trader = UsTrader(ctx.kiwoom, notifier, store=ctx.store, policy=ctx.policy)
            from .trading import review

            review.install(ctx.kiwoom, notifier, ctx.store, ctx.policy)
        jobs = [
            ("관심종목 브리핑", lambda: self._daily_brief(ctx, notifier)),
            ("예약 주문 제출", lambda: self._submit_queued(ctx, notifier)),
            ("미국 종목 점검", self._us_trader.check),
        ]
        if ctx.policy.kr_enabled:
            jobs = [
                ("장외 신호 재평가", self._proposer.release_deferred),
                ("손절·익절 점검", self._proposer.check_exits),
                *jobs,
            ]
        for name, job in jobs:
            try:
                job()
            except Exception:
                logger.exception("%s 실패", name)

    def _daily_brief(self, ctx: BotContext, notifier: TelegramNotifier, *, now=None) -> None:
        """하루 한 번 관심·보유 브리핑. 보낸 날짜를 파일에 남겨 재시작해도 두 번 보내지 않는다."""
        from datetime import datetime

        from .market_hours import KST, NY, is_us_trading_day
        from .trading.daily_brief import kr_report, us_report

        state = _load_brief_state()
        p = ctx.policy
        base = now or datetime.now(KST)
        now_k, now_n = base.astimezone(KST), base.astimezone(NY)
        jobs = []
        if p.kr_enabled and p.brief_kr_at and now_k.weekday() < 5:
            jobs.append(("kr", now_k, p.brief_kr_at, kr_report))
        if p.brief_us_at and is_us_trading_day(now_n):
            jobs.append(("us", now_n, p.brief_us_at, us_report))
        for key, now, at, build in jobs:
            hh, mm = (int(x) for x in at.split(":"))
            start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # 정해진 시각부터 1시간 안에만 — 봇이 늦게 떠도 보내되, 장중 한참 뒤엔 보내지 않는다
            if not (start <= now < start.replace(hour=min(hh + 1, 23))):
                continue
            day = now.strftime("%Y%m%d")
            if state.get(key) == day:
                continue
            state[key] = day
            _save_brief_state(state)  # 실패해도 1분마다 다시 보내지 않게 먼저 적는다
            text = build(ctx.kiwoom, p)
            if text:
                self._send_with_command_buttons(notifier, text)

    def _send_with_command_buttons(self, notifier: TelegramNotifier, text: str) -> None:
        from .advisor import extract_commands

        buttons = command_buttons(extract_commands(re.sub(r"<[^>]+>", "", text)))
        self._reply(notifier, text, buttons=buttons or None)

    def _submit_queued(self, ctx: BotContext, notifier: TelegramNotifier) -> None:
        """예약 주문을 2분마다 넣어본다. 키움이 받아주면 알린다."""
        from .trading.executor import submit_queued

        if time.monotonic() - self._last_queue_try < QUEUE_RETRY_SEC:
            return
        self._last_queue_try = time.monotonic()
        for p, message in submit_queued(ctx.kiwoom, ctx.store):
            self._reply(notifier, escape(message))
            self._close_proposal(ctx, notifier, p, p.note if p else message)

    def _expire(self, ctx: BotContext, notifier: TelegramNotifier) -> None:
        """만료된 제안의 버튼을 지운다. 눌러도 주문은 안 나가지만, 남아 있으면 헷갈린다."""
        from .trading.executor import expire_stale

        try:
            expired = expire_stale(ctx.store, ctx.policy)
        except (OSError, ValueError) as exc:
            logger.error("제안 만료 처리 실패: %s", exc)
            return
        for p in expired:
            self._close_proposal(ctx, notifier, p, "⌛ 시간 초과로 만료 — 주문하지 않았습니다.")

    def _close_proposal(self, ctx, notifier: TelegramNotifier, p, result: str) -> None:
        from .trading.executor import reservable
        from .trading.proposer import CALLBACK_RESERVE, proposal_message

        if not p or not p.message_id:
            return
        buttons = (
            [[("🕒 예약 — 장 열리면 자동 주문", CALLBACK_RESERVE + p.id)]]
            if reservable(p)
            else None
        )
        try:
            notifier.edit(
                p.message_id,
                proposal_message(p, ctx.policy) + f"\n\n<b>{escape(result)}</b>",
                buttons=buttons,
            )
        except TelegramError as exc:
            logger.warning("제안 메시지 수정 실패: %s", exc)

    def _on_callback(
        self, ctx: BotContext, notifier: TelegramNotifier, query: dict[str, Any]
    ) -> None:
        from .trading import executor
        from .trading.proposer import CALLBACK_APPROVE, CALLBACK_REJECT, CALLBACK_RESERVE

        # 버튼 누름도 같은 화이트리스트를 거친다. 누른 사람과 메시지가 있는 채팅 둘 다 본다.
        user_id = str((query.get("from") or {}).get("id", ""))
        chat_id = str(((query.get("message") or {}).get("chat") or {}).get("id", ""))
        if user_id not in self.allowed and chat_id not in self.allowed:
            self.stats["rejected"] += 1
            logger.warning("허용되지 않은 사용자의 버튼 무시: %s", user_id)
            return

        data = str(query.get("data", ""))
        try:
            if data.startswith(CALLBACK_APPROVE):
                p, result = executor.approve(
                    ctx.kiwoom, ctx.store, data[len(CALLBACK_APPROVE) :], ctx.policy
                )
            elif data.startswith(CALLBACK_REJECT):
                p, result = executor.reject(ctx.store, data[len(CALLBACK_REJECT) :])
            elif data.startswith(CALLBACK_RESERVE):
                p, result = executor.queue(ctx.store, data[len(CALLBACK_RESERVE) :])
            elif data.startswith(CALLBACK_COMMAND):
                p, result = (
                    None,
                    self._run_command_button(ctx, notifier, data[len(CALLBACK_COMMAND) :]),
                )
            else:
                p, result = None, "알 수 없는 버튼입니다."
        except Exception as exc:
            self.stats["errors"] += 1
            logger.exception("버튼 처리 실패: %s", data)
            p, result = None, f"오류: {str(exc)[:150]}"
        self.stats["handled"] += 1

        try:
            notifier.answer_callback(str(query.get("id", "")), result)
        except TelegramError as exc:
            logger.warning("버튼 응답 실패: %s", exc)
        # 두 번째 누름이면 결과 대신 처음 결정을 다시 보여준다 — 덮어쓰면 기록이 사라진다.
        self._close_proposal(ctx, notifier, p, (p.note if p else "") or result)

    def _run_command_button(self, ctx: BotContext, notifier: TelegramNotifier, cmd: str) -> str:
        """경제 봇 답의 [▶] 버튼 — 그 명령을 직접 친 것처럼 실행한다.

        버튼 데이터는 믿지 않고 다시 검사한다. 미국 매수·매도는 확인 메시지([승인])를 만들 뿐이라
        버튼 하나로 주문이 나가지 않는다. 국내 매수·매도는 확인 없이 즉시 체결되는 경로라 받지 않는다.
        """
        if not runnable(cmd):
            return "이 명령은 버튼으로 실행할 수 없습니다. 복사해서 보내주세요."
        name, *args = cmd[1:].split()
        reply = COMMANDS[name](ctx, args)
        if reply:
            self._reply(notifier, reply)
        return (
            "확인 메시지를 보냈습니다 — [승인]을 눌러야 주문됩니다"
            if name != "cancel"
            else "실행했습니다"
        )

    def _dispatch(
        self, ctx: BotContext, notifier: TelegramNotifier, update: dict[str, Any]
    ) -> None:
        if "callback_query" in update:
            self._on_callback(ctx, notifier, update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text:
            return

        if chat_id not in self.allowed:
            # 응답하지 않는다. 응답하면 봇의 존재와 반응 패턴을 알려주는 셈이다.
            self.stats["rejected"] += 1
            logger.warning("허용되지 않은 chat_id의 메시지 무시: %s", chat_id)
            return

        if not text.startswith("/"):
            self._ask(ctx, notifier, text)
            return

        parts = text.split()
        name = parts[0][1:].split("@")[0].lower()  # /quote@botname 형태 대응
        if name in ADVISOR_COMMANDS:
            self._advisor_command(ctx, notifier, name, text.split(maxsplit=1)[1:])
            return
        handler = COMMANDS.get(name)
        if handler is None:
            self._reply(notifier, f"모르는 명령입니다: {escape(parts[0])}\n/help 를 보세요.")
            return

        try:
            reply = handler(ctx, parts[1:])
        except Exception as exc:
            self.stats["errors"] += 1
            logger.exception("명령 처리 실패: %s", text)
            reply = f"오류가 발생했습니다: {escape(str(exc)[:200])}"
        self.stats["handled"] += 1
        if reply:
            buttons = None
            if name in BUTTON_REPLIES:
                from .advisor import extract_commands

                # /us 의 "→ /buy TQQQ 전량", /cancel 목록의 "/cancel 주문번호"를 바로 누르게
                buttons = command_buttons(extract_commands(re.sub(r"<[^>]+>", "", reply)))
            self._reply(notifier, reply, buttons=buttons)

    def _reply(self, notifier: TelegramNotifier, text: str, *, buttons: list | None = None) -> None:
        try:
            notifier.send(text, buttons=buttons) if buttons else notifier.send(text)
        except TelegramError as exc:
            logger.error("응답 전송 실패: %s", exc)


ADVISOR_COMMANDS = {"ask", "reset", "notes", "forget"}

QUEUE_RETRY_SEC = 120  # 예약 주문을 넣어보는 간격
BRIEF_STATE_PATH = CACHE_DIR / "daily_brief.json"


def _load_brief_state() -> dict:
    import json

    try:
        return json.loads(BRIEF_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_brief_state(state: dict) -> None:
    import json

    try:
        BRIEF_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        logger.warning("브리핑 상태 저장 실패: %s", exc)


CALLBACK_COMMAND = "cmd:"
# 답 속 명령에 실행 버튼을 붙일 명령 (도움말처럼 예시가 많은 답에는 붙이지 않는다)
BUTTON_REPLIES = {"us", "cancel", "brief"}
CALLBACK_DATA_LIMIT = 64  # 텔레그램 callback_data 상한(바이트)


def runnable(cmd: str) -> bool:
    """버튼으로 바로 실행해도 되는 명령인가: 미국 매수·매도(확인 메시지로 이어짐)와 취소."""
    from .us_order import is_us_symbol

    parts = cmd.split()
    if len(parts) < 2 or not parts[0].startswith("/"):
        return False
    name = parts[0][1:]
    if name == "cancel":
        return True
    return name in ("buy", "sell") and is_us_symbol(parts[1])


def command_buttons(commands: list[str]) -> list[list]:
    """명령마다 한 줄: [▶ 실행] [📋 복사]. 실행할 수 없는 명령은 복사만."""
    rows = []
    for cmd in commands:
        row: list = []
        data = CALLBACK_COMMAND + cmd
        if runnable(cmd) and len(data.encode()) <= CALLBACK_DATA_LIMIT:
            row.append((f"▶ {cmd}", data))
        row.append({"text": "📋 복사" if row else f"📋 {cmd}", "copy_text": {"text": cmd}})
        rows.append(row)
    return rows


def _advisor_daily_limit() -> int:
    """경제 봇 하루 질문 상한. 질문 하나에 웹 검색까지 하면 수십~수백 원이 든다."""
    load_env()
    try:
        return int(os.getenv("ADVISOR_DAILY_LIMIT", "40"))
    except ValueError:
        return 40


def _allowed_chat_ids(config: TelegramConfig) -> set[str]:
    load_env()
    extra = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids = {config.chat_id} | {c.strip() for c in extra.split(",") if c.strip()}
    return ids
