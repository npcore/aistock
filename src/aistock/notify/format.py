"""알림 메시지 조립.

사람이 이 메시지만 보고 매수·매도를 판단하므로, 결론과 그 근거를 같이 담는다.
숫자(희석률·발행규모)와 리스크를 빼면 판단이 아니라 지시가 되어버린다.
"""

from __future__ import annotations

from typing import Any

from ..pipeline.events import DisclosureEvent
from ..signal.schema import Action, DisclosureSignal
from .telegram import escape

ACTION_MARK = {
    Action.BUY: "🟢 매수",
    Action.SELL: "🔴 매도",
    Action.WATCH: "🟡 관심",
    Action.IGNORE: "⚪ 무관",
}

IMPACT_LABEL = {
    "strong_positive": "강한 호재",
    "positive": "호재",
    "neutral": "중립",
    "negative": "악재",
    "strong_negative": "강한 악재",
}

HORIZON_LABEL = {
    "intraday": "당일",
    "days": "수일",
    "weeks": "수주",
    "unknown": "불명",
}


def signal_message(
    event: DisclosureEvent,
    signal: DisclosureSignal,
    *,
    quote: dict[str, Any] | None = None,
    note: str = "",
) -> str:
    """신호 하나를 텔레그램 메시지로."""
    d = event.disclosure
    mark = ACTION_MARK.get(signal.action, str(signal.action.value))
    lines = [f"<b>{mark}  {signal.confidence}%</b>"]

    title = f"{escape(d.corp_name)}"
    if d.stock_code:
        title += f" <code>{d.stock_code}</code>"
    if note:
        title += f" · {escape(note)}"
    lines.append(f"{title} · {escape(d.market)}")

    if quote:
        lines.append(_quote_line(quote))

    lines.append("")
    lines.append(f"<b>{escape(signal.event_type)}</b>")
    impact = IMPACT_LABEL.get(signal.impact.value, signal.impact.value)
    horizon = HORIZON_LABEL.get(signal.horizon.value, signal.horizon.value)
    lines.append(f"<i>{impact} · {horizon}</i>")

    lines.append("")
    lines.append(escape(signal.rationale))

    if signal.key_facts:
        lines.append("")
        lines.append("<b>핵심</b>")
        lines.extend(f"· {escape(f)}" for f in signal.key_facts[:6])

    if signal.risks:
        lines.append("")
        lines.append("<b>리스크</b>")
        lines.extend(f"· {escape(r)}" for r in signal.risks[:4])

    lines.append("")
    lines.append(f'<a href="{d.url}">{escape(d.report_nm)}</a>')
    return "\n".join(lines)


def _quote_line(quote: dict[str, Any]) -> str:
    """현재가 한 줄. 키움 값은 `+246000`처럼 부호가 붙어 오므로 그대로 살려 쓴다."""
    price = str(quote.get("cur_prc", "")).strip()
    rate = str(quote.get("flu_rt", "")).strip()
    volume = str(quote.get("trde_qty", "")).strip()
    if not price:
        return ""
    arrow = "▲" if price.startswith("+") else "▼" if price.startswith("-") else "-"
    bits = [f"{arrow} <b>{escape(price.lstrip('+-'))}</b>"]
    if rate:
        bits.append(f"({escape(rate)}%)")
    if volume.isdigit():
        bits.append(f"거래량 {int(volume):,}")
    return " ".join(bits)


def headline_message(event: DisclosureEvent, note: str = "") -> str:
    """원문이 오기 전, 보고서명만으로 보내는 속보.

    원문은 수 분 뒤에 올라오므로 급한 판단은 이 단계에서 해야 한다.
    """
    d = event.disclosure
    who = f"{escape(d.corp_name)}"
    if d.stock_code:
        who += f" <code>{d.stock_code}</code>"
    if note:
        who += f" · {escape(note)}"
    return (
        f"⚡ <b>공시 속보</b>\n{who}\n\n"
        f"{escape(d.report_nm)}\n\n"
        f'<a href="{d.url}">원문 보기</a>  <i>(분석은 원문 확보 후)</i>'
    )


def holdings_message(balance: Any, *, title: str = "💼 보유종목") -> str:
    """보유종목 시세·손익 요약.

    손익 부호를 이모지가 아니라 숫자 앞 기호로 표시한다 — 종목이 많아지면 이모지가
    줄을 밀어 정렬이 깨진다.
    """
    lines = [f"<b>{title}</b>", ""]
    for h in balance.holdings:
        sign = "+" if h.eval_profit > 0 else ""
        arrow = "🔺" if h.eval_profit > 0 else "🔻" if h.eval_profit < 0 else "▪️"
        lines.append(
            f"{arrow} <b>{escape(h.name)}</b> <code>{h.stock_code}</code>\n"
            f"   {h.current_price:,}원 · {h.quantity:,}주\n"
            f"   매입 {h.avg_price:,} → 평가 {h.eval_amount:,}\n"
            f"   <b>{sign}{h.eval_profit:,}원 ({sign}{h.profit_rate:.2f}%)</b>"
        )
    if not balance.holdings:
        lines.append("보유 종목이 없습니다.")
        return "\n".join(lines)

    sign = "+" if balance.total_profit > 0 else ""
    lines.append("")
    lines.append("─" * 18)
    lines.append(
        f"매입 {balance.total_purchase:,}\n"
        f"평가 {balance.total_eval:,}\n"
        f"<b>손익 {sign}{balance.total_profit:,}원 ({sign}{balance.total_profit_rate:.2f}%)</b>"
    )
    if balance.estimated_assets:
        lines.append(f"추정예탁자산 {balance.estimated_assets:,}원")
    return "\n".join(lines)


def account_message(balance: Any, deposit: Any = None, us: Any = None) -> str:
    """국내 보유종목 + 예수금 + 미국 보유종목·예수금. 조회 실패한 쪽은 None으로 넘기면 빠진다."""
    parts = [holdings_message(balance, title="💼 국내 보유종목")]
    if deposit is not None:
        parts.append(
            "<b>💰 예수금 (원화)</b>\n"
            f"예수금 {deposit.cash:,}원 · D+2 {deposit.d2_cash:,}원\n"
            f"<b>주문가능 {deposit.orderable:,}원</b> · 출금가능 {deposit.withdrawable:,}원"
        )
    if us is not None:
        lines = ["<b>🇺🇸 미국 보유종목</b>"]
        for h in us.holdings:
            sign = "+" if h.profit > 0 else ""
            arrow = "🔺" if h.profit > 0 else "🔻" if h.profit < 0 else "▪️"
            lines.append(
                f"{arrow} <b>{escape(h.symbol)}</b> {escape(h.name)}\n"
                f"   ${h.current_price:,.2f} · {h.quantity:,}주 (매입 ${h.avg_price:,.2f})\n"
                f"   <b>{sign}${h.profit:,.2f} ({sign}{h.profit_rate:.2f}%)</b> · {sign}{h.profit_krw:,}원"
            )
        if not us.holdings:
            lines.append("보유 종목이 없습니다.")
        lines.append(
            f"\n외화예수금 ${us.usd_cash:,.2f} · <b>주문가능 ${us.usd_orderable:,.2f}</b>"
            + (f"\n원화예수금(해외) {us.krw_cash:,}원" if us.krw_cash else "")
        )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def meritz_message(snap: Any) -> str:
    """메리츠증권 계좌 요약 (조회 전용)."""
    lines = ["<b>🏦 메리츠증권</b>"]
    for h in snap.holdings:
        sign = "+" if h.profit > 0 else ""
        arrow = "🔺" if h.profit > 0 else "🔻" if h.profit < 0 else "▪️"
        qty = f"{h.quantity:,.0f}" if h.quantity == int(h.quantity) else f"{h.quantity:,.4f}"
        if h.market == "US":
            lines.append(
                f"{arrow} 🇺🇸 <b>{escape(h.code)}</b> {qty}주 · ${h.price:,.2f} (매입 ${h.avg_price:,.2f})\n"
                f"   <b>{sign}${h.profit:,.2f} ({sign}{h.profit_rate:.2f}%)</b>"
            )
        else:
            lines.append(
                f"{arrow} <b>{escape(h.name)}</b> <code>{h.code}</code> {qty}주 · {h.price:,.0f}원\n"
                f"   <b>{sign}{h.profit:,.0f}원 ({sign}{h.profit_rate:.2f}%)</b>"
            )
    if not snap.holdings:
        lines.append("보유 종목이 없습니다.")
    d = snap.deposit
    lines.append(
        f"\n예수금 {d.cash:,}원 · 주문가능 {d.orderable:,}원 · 출금가능 {d.withdrawable:,}원"
    )
    fx = " · ".join(f"{c} {amt:,.2f}" for c, amt in snap.foreign_cash.items() if amt)
    if fx:
        lines.append(f"외화 예수금 {fx}")
    lines.append("<i>조회 전용 — 메리츠 주문은 아직 연결하지 않았습니다</i>")
    return "\n".join(lines)


def watchlist_digest(rows: list[dict[str, Any]]) -> str:
    """관심종목 현황 요약. rows는 {code, name, quote, signals} 형태."""
    lines = ["<b>📋 관심종목 현황</b>", ""]
    for row in rows:
        name = escape(row.get("name") or row["code"])
        line = f"<code>{row['code']}</code> {name}"
        quote_line = _quote_line(row.get("quote") or {})
        if quote_line:
            line += f"\n   {quote_line}"
        recent = row.get("signals") or []
        if recent:
            for s in recent[:2]:
                mark = ACTION_MARK.get(Action(s["action"]), s["action"])
                line += f"\n   {mark} {s['confidence']}% · {escape(s['event_type'][:30])}"
        lines.append(line)
    return "\n".join(lines)


def ticker_message(rows: list[dict[str, Any]], *, at: str, closing: bool = False) -> str:
    """시간별 시세 알림.

    한 종목당 두 줄로 고정한다 — 종목이 늘어나도 훑어보는 위치가 변하지 않는다.
    rows에 `section`이 있으면 그 값으로 묶어 보여준다(국내/미국).
    """
    head = "🔔 <b>장마감</b>" if closing else f"🕐 <b>{escape(at)}</b>"
    lines = [head, ""]
    section = None
    for row in rows:
        if row.get("section") and row["section"] != section:
            section = row["section"]
            if len(lines) > 2:
                lines.append("")
            lines.append(f"<b>{escape(section)}</b>")
        quote = row.get("quote") or {}
        name = escape(row.get("name") or row["code"])
        price = str(quote.get("cur_prc", "")).strip()
        rate = str(quote.get("flu_rt", "")).strip()
        if not price:
            lines.append(f"<b>{name}</b> <code>{row['code']}</code>\n   (시세 조회 실패)")
            continue
        arrow = "🔺" if price.startswith("+") else "🔻" if price.startswith("-") else "▪️"
        detail = []
        for key, label in (("open_pric", "시"), ("high_pric", "고"), ("low_pric", "저")):
            v = str(quote.get(key, "")).strip().lstrip("+-")
            if v:
                detail.append(f"{label} {int(v):,}" if v.isdigit() else f"{label} {v}")
        lines.append(
            f"{arrow} <b>{name}</b> <code>{row['code']}</code>\n"
            f"   <b>{escape(price.lstrip('+-'))}</b> ({escape(rate)}%)"
            + (f"  ·  {' / '.join(detail)}" if detail else "")
        )
    return "\n".join(lines)
