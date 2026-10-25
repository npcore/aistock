"""승인된 제안을 주문으로 바꾼다. 봇 프로세스에서 돈다.

**실계좌에 주문이 나가는 유일한 자동 경로다.** 버튼을 누른 시점에 조건을 다시 확인한다
— 제안을 만든 뒤 몇 분 사이에 가격이 튀었거나 장이 닫혔을 수 있다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .. import us_order
from ..backtest.prices import parse_price
from ..client import KiwoomClient
from ..errors import KiwoomError
from ..market import domestic, overseas
from ..market_hours import KST, in_session, in_us_session, is_us_trading_day
from ..order import OrderError, OrderRequest, OrderType, place
from . import proposals as P
from .policy import TradingPolicy

logger = logging.getLogger(__name__)


# 키움이 주문을 받지 않는 시간대(애프터 마감 ~ 다음 주간거래·프리마켓)에 오는 거절.
# 2026-10-21 08:22 KST: "[2000](505217:장종료 되었습니다.)"
MARKET_CLOSED_CODE = "505217"
QUEUE_TTL = timedelta(hours=24)


def market_closed(exc: Exception) -> bool:
    text = str(exc)
    return MARKET_CLOSED_CODE in text or "장종료" in text


def reservable(p: P.Proposal | None) -> bool:
    """장종료로 실패한 미국 주문 — [🕒 예약] 버튼을 붙일 대상."""
    return bool(p and p.is_us and p.status == P.FAILED and "장종료" in p.note)


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def reject(store: P.ProposalStore, proposal_id: str) -> tuple[P.Proposal | None, str]:
    done = store.transition(
        proposal_id, P.PENDING, P.REJECTED, decided_at=_now(), note="❌ 거절했습니다."
    )
    if done is None:
        return store.get(proposal_id), "이미 처리된 제안입니다."
    return done, "❌ 거절했습니다."


def approve(
    kiwoom: KiwoomClient,
    store: P.ProposalStore,
    proposal_id: str,
    policy: TradingPolicy,
) -> tuple[P.Proposal | None, str]:
    """(갱신된 제안, 사람에게 보여줄 결과). 주문은 최대 한 번만 나간다."""
    # 이 전이가 성공한 호출만 주문까지 간다. 두 번 눌러도 두 번째는 여기서 끝난다.
    p = store.transition(proposal_id, P.PENDING, P.EXECUTING, decided_at=_now())
    if p is None:
        existing = store.get(proposal_id)
        if existing is None:
            return None, "제안을 찾을 수 없습니다."
        return existing, f"이미 처리된 제안입니다 ({existing.status})."

    def finish(status: str, message: str, **changes: object) -> tuple[P.Proposal | None, str]:
        updated = store.update(proposal_id, status=status, note=message, **changes)
        return updated, message

    if p.expired(policy.proposal_ttl_min):
        return finish(
            P.EXPIRED, f"⌛ {policy.proposal_ttl_min}분이 지나 만료됐습니다. 주문하지 않았습니다."
        )
    if p.is_us:
        return _approve_us(kiwoom, p, policy, finish)
    if not in_session():
        return finish(P.STALE, "🕒 정규장 시간이 아닙니다. 주문하지 않았습니다.")

    try:
        price = parse_price(domestic.stock_info(kiwoom, p.stock_code).get("cur_prc", "0"))
    except (KiwoomError, ValueError) as exc:
        return finish(P.FAILED, f"현재가 확인 실패 — 주문하지 않았습니다: {exc}")

    stale = _drift_message(p, price, p.limit_price, policy, f"{price:,.0f}원")
    if stale:
        return finish(P.STALE, stale)

    request = OrderRequest(
        side=p.side,
        stock_code=p.stock_code,
        quantity=p.quantity,
        price=p.limit_price,
        order_type=OrderType.LIMIT,
    )
    try:
        result = place(kiwoom, request)
    except (OrderError, KiwoomError) as exc:
        logger.error("제안 %s 주문 실패: %s", proposal_id, exc)
        return finish(P.FAILED, f"❌ 주문 실패: {exc}")

    logger.warning("제안 %s 승인 → 주문번호 %s", proposal_id, result.order_no)
    return finish(
        P.ORDERED,
        f"✅ 주문 접수 — 주문번호 {result.order_no} (체결 확인: /orders)",
        order_no=result.order_no,
    )


def _drift_message(
    p: P.Proposal, price: float, limit: float, policy: TradingPolicy, shown: str
) -> str:
    """불리하게 움직였으면 사유, 아니면 빈 문자열.

    직접 입력한 주문(manual)과 걸어두기(resting)는 보지 않는다 — 일부러 현재가와 다른 값을 거는 것이다.
    """
    if p.source in ("manual", "resting") or not limit:
        return ""
    drift = (price - limit) / limit * 100
    # 매수는 오른 쪽, 매도는 내린 쪽이 불리하다. 불리하게 벌어졌으면 판단 근거가 달라진 것이다.
    adverse = drift if p.side == "buy" else -drift
    if adverse > policy.max_drift_pct:
        return (
            f"📉 제안 후 가격이 {drift:+.1f}% 움직였습니다 (현재 {shown}). "
            f"허용 {policy.max_drift_pct}% 초과 — 주문하지 않았습니다."
        )
    return ""


def _approve_us(kiwoom: KiwoomClient, p: P.Proposal, policy: TradingPolicy, finish):
    # 장외(프리·애프터마켓, 주간거래)에도 키움은 일반 지정가 주문을 받는다 — 2026-10-20 19:15 KST
    # 주문이 '일반'으로 접수됐다. 받아줄지는 키움이 판단하게 두고, 거절하면 그 사유를 보여준다.
    regular = in_us_session() and is_us_trading_day()
    if not regular and not policy.us_extended_hours:
        return finish(P.STALE, "🕒 미국 정규장 시간이 아닙니다 (휴장 포함). 주문하지 않았습니다.")
    try:
        quote = overseas.quote(kiwoom, p.stock_code, p.exchange)
        price = parse_price(quote.get("cur_prc", "0"))
    except (KiwoomError, ValueError) as exc:
        return finish(P.FAILED, f"현재가 확인 실패 — 주문하지 않았습니다: {exc}")

    stale = _drift_message(p, price, p.price_usd, policy, f"${price:,.2f}")
    if stale:
        return finish(P.STALE, stale)

    request = us_order.UsOrderRequest(p.side, p.stock_code, p.exchange, p.quantity, p.price_usd)
    try:
        result = us_order.place(kiwoom, request)
    except KiwoomError as exc:  # UsOrderError 포함
        logger.error("제안 %s 미국 주문 실패: %s", p.id, exc)
        if market_closed(exc):
            return finish(
                P.FAILED,
                "🕒 지금은 키움이 미국 주문을 받지 않는 시간입니다(장종료). "
                "[🕒 예약]을 누르면 주문을 받기 시작할 때 이 가격 그대로 넣고 알려드립니다.",
            )
        return finish(P.FAILED, f"❌ 주문 실패: {exc}")

    logger.warning(
        "제안 %s 승인 → 미국 주문번호 %s (%s)",
        p.id,
        result.order_no,
        "정규장" if regular else "장외",
    )
    extended = "" if regular else " · 🌙 장외 주문 — 호가가 얇아 체결이 늦거나 안 될 수 있습니다"
    if p.source == "resting":
        return finish(
            P.ORDERED,
            f"📌 지정가 주문을 걸었습니다 — ${p.price_usd:,.2f}에 닿으면 체결 "
            f"(주문번호 {result.order_no} · 확인: /orders){extended}",
            order_no=result.order_no,
        )
    return finish(
        P.ORDERED,
        f"✅ 주문 접수 — 주문번호 {result.order_no} (체결 확인: /orders){extended}",
        order_no=result.order_no,
    )


def queue(store: P.ProposalStore, proposal_id: str) -> tuple[P.Proposal | None, str]:
    """장종료로 실패한 주문을 예약한다. 사람이 [🕒 예약]을 눌렀을 때만."""
    p = store.get(proposal_id)
    if not reservable(p):
        return p, "예약할 수 없는 주문입니다 (이미 처리됐거나 장종료 실패가 아님)."
    # 같은 주문을 다른 메시지에서 다시 실패·예약했으면 두 번 사게 된다.
    twin = next(
        (
            q
            for q in store.all()
            if q.id != p.id
            and q.status in (P.QUEUED, P.EXECUTING, P.ORDERED, P.PENDING)
            and (q.stock_code, q.side, q.quantity, q.price_usd)
            == (p.stock_code, p.side, p.quantity, p.price_usd)
        ),
        None,
    )
    if twin is not None:
        return (
            p,
            f"같은 주문이 이미 {twin.status} 상태입니다 ({twin.id}). 중복 예약하지 않았습니다.",
        )
    done = store.transition(
        proposal_id,
        P.FAILED,
        P.QUEUED,
        queued_at=_now(),
        note="🕒 예약됨 — 키움이 주문을 받기 시작하면 이 가격 그대로 넣고 알려드립니다 (24시간 유효 · 취소: /cancel)",
    )
    if done is None:
        return store.get(proposal_id), "이미 처리된 주문입니다."
    return done, done.note


def submit_queued(kiwoom: KiwoomClient, store: P.ProposalStore) -> list[tuple[P.Proposal, str]]:
    """예약 주문을 넣어본다. 아직 장종료면 그대로 둔다. 알릴 것만 돌려준다."""
    out = []
    now = datetime.now(KST)
    for p in store.all():
        if p.status != P.QUEUED:
            continue
        if p.queued_at and now - datetime.fromisoformat(p.queued_at) > QUEUE_TTL:
            done = store.transition(p.id, P.QUEUED, P.EXPIRED, note="⌛ 예약 24시간 만료")
            if done:
                out.append(
                    (
                        done,
                        f"⌛ 예약 만료 — {done.stock_code} {done.price_text()}. 주문하지 않았습니다.",
                    )
                )
            continue
        # 넣는 동안 /cancel과 겹치지 않게 먼저 잡는다
        claimed = store.transition(p.id, P.QUEUED, P.EXECUTING)
        if claimed is None:
            continue
        request = us_order.UsOrderRequest(p.side, p.stock_code, p.exchange, p.quantity, p.price_usd)
        try:
            result = us_order.place(kiwoom, request)
        except KiwoomError as exc:
            if market_closed(exc):
                store.transition(p.id, P.EXECUTING, P.QUEUED)  # 아직 — 다음에 다시
                continue
            done = store.update(p.id, status=P.FAILED, note=f"❌ 예약 주문 실패: {exc}")
            out.append((done, f"❌ 예약 주문 실패 — {p.stock_code} {p.price_text()}\n{exc}"))
            continue
        done = store.update(
            p.id,
            status=P.ORDERED,
            order_no=result.order_no,
            note=f"✅ 예약 주문 접수 — 주문번호 {result.order_no}",
        )
        logger.warning("예약 %s → 미국 주문번호 %s", p.id, result.order_no)
        out.append(
            (
                done,
                f"✅ 예약 주문 접수 — {p.stock_code} {p.price_text()} · 주문번호 {result.order_no} (확인: /orders)",
            )
        )
    return out


def cancel_queued(store: P.ProposalStore, proposal_id: str) -> P.Proposal | None:
    return store.transition(proposal_id, P.QUEUED, P.REJECTED, note="예약 취소")


def expire_stale(store: P.ProposalStore, policy: TradingPolicy) -> list[P.Proposal]:
    """만료된 대기 제안을 정리한다. 봇이 주기적으로 불러 버튼을 지운다."""
    out = []
    for p in store.all():
        if p.status == P.PENDING and p.expired(policy.proposal_ttl_min):
            done = store.transition(p.id, P.PENDING, P.EXPIRED, decided_at=_now(), note="시간 초과")
            if done:
                out.append(done)
    return out
