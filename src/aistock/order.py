"""주식 주문.

**이 모듈은 실계좌에 진짜 주문을 낸다. 되돌릴 수 없다.**

설계 원칙 세 가지. 편의보다 사고 방지를 우선했다.

1. **자동 실행 경로를 만들지 않는다.** LLM 신호나 폴러가 이 모듈을 호출하는 연결은 없다.
   호출자는 CLI뿐이고, CLI는 사람이 직접 확인을 입력해야 진행한다.
2. **주문 금액 상한을 둔다.** 수량에 0을 하나 더 붙이는 실수가 계좌를 비울 수 있다.
   `MAX_ORDER_AMOUNT`를 넘으면 거부한다.
3. **시장가를 기본으로 두지 않는다.** 지정가를 기본으로 하고 시장가는 명시적으로 골라야 한다.
   유동성이 얕은 종목에서 시장가는 체결가를 예측할 수 없다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .client import KiwoomClient
from .config import load_env
from .errors import KiwoomError

logger = logging.getLogger(__name__)

ORDR = "/api/dostk/ordr"
ACNT = "/api/dostk/acnt"

BUY = "kt10000"
SELL = "kt10001"
CANCEL = "kt10003"

# 주문 1건의 금액 상한(원). 환경변수로 조정한다. 0이면 상한 없음.
DEFAULT_MAX_ORDER_AMOUNT = 10_000_000


class OrderType(str, Enum):
    """매매구분. 값은 키움 `trde_tp` 코드."""

    LIMIT = "0"  # 보통(지정가) — 기본값
    MARKET = "3"  # 시장가
    BEST_LIMIT = "6"  # 최유리지정가
    PRIORITY_LIMIT = "7"  # 최우선지정가

    @property
    def needs_price(self) -> bool:
        return self is OrderType.LIMIT


class OrderError(KiwoomError):
    """주문 거부. 주문이 나가기 전에 막힌 경우와 서버가 거절한 경우 모두 포함한다."""


@dataclass(frozen=True)
class OrderRequest:
    side: str  # "buy" | "sell"
    stock_code: str
    quantity: int
    price: int = 0  # 지정가일 때만 의미가 있다
    order_type: OrderType = OrderType.LIMIT
    exchange: str = "KRX"

    @property
    def estimated_amount(self) -> int:
        """예상 주문금액. 시장가는 가격을 모르므로 0이다."""
        return self.quantity * self.price

    def describe(self) -> str:
        kind = "지정가" if self.order_type is OrderType.LIMIT else self.order_type.name
        base = f"{'매수' if self.side == 'buy' else '매도'} {self.stock_code} {self.quantity:,}주"
        if self.order_type is OrderType.LIMIT:
            return f"{base} @ {self.price:,}원 ({kind}) = {self.estimated_amount:,}원"
        return f"{base} ({kind})"


@dataclass(frozen=True)
class OrderResult:
    order_no: str
    exchange: str
    request: OrderRequest


def max_order_amount() -> int:
    load_env()
    raw = os.getenv("MAX_ORDER_AMOUNT", "").strip()
    if not raw:
        return DEFAULT_MAX_ORDER_AMOUNT
    try:
        return int(raw)
    except ValueError:
        logger.warning("MAX_ORDER_AMOUNT를 읽을 수 없어 기본값을 씁니다: %r", raw)
        return DEFAULT_MAX_ORDER_AMOUNT


def validate(request: OrderRequest) -> None:
    """주문이 나가기 전에 막을 수 있는 것은 여기서 막는다."""
    if request.side not in ("buy", "sell"):
        raise OrderError(f"매수/매도 구분이 잘못됐습니다: {request.side!r}")
    # 자릿수까지 본다. 5자리나 7자리를 그대로 보내면 엉뚱한 종목에 주문이 나갈 수 있다.
    if len(request.stock_code) != 6 or not request.stock_code.isdigit():
        raise OrderError(f"종목코드가 6자리 숫자가 아닙니다: {request.stock_code!r}")
    if request.quantity <= 0:
        raise OrderError(f"주문수량은 1주 이상이어야 합니다: {request.quantity}")
    if request.order_type.needs_price and request.price <= 0:
        raise OrderError(
            "지정가 주문은 주문단가가 필요합니다. --price 를 지정하거나 --market 을 쓰세요."
        )

    limit = max_order_amount()
    if limit and request.estimated_amount > limit:
        raise OrderError(
            f"주문금액 {request.estimated_amount:,}원이 상한 {limit:,}원을 넘습니다. "
            f".env의 MAX_ORDER_AMOUNT를 올리거나 수량을 줄이세요."
        )


def place(client: KiwoomClient, request: OrderRequest) -> OrderResult:
    """주문을 낸다. **실계좌에 즉시 반영된다.**

    호출 전에 반드시 `validate()`를 통과시키고, 사람의 확인을 받아야 한다.
    """
    validate(request)

    body: dict[str, Any] = {
        "dmst_stex_tp": request.exchange,
        "stk_cd": request.stock_code,
        "ord_qty": str(request.quantity),
        "trde_tp": request.order_type.value,
    }
    if request.order_type.needs_price:
        body["ord_uv"] = str(request.price)

    api_id = BUY if request.side == "buy" else SELL
    logger.warning("주문 전송: %s", request.describe())
    response = client.request(api_id, ORDR, body)

    order_no = str(response.data.get("ord_no", "")).strip()
    if not order_no:
        raise OrderError(f"주문번호가 응답에 없습니다: {response.data}")
    logger.warning("주문 접수 완료: 주문번호 %s", order_no)
    return OrderResult(
        order_no=order_no,
        exchange=str(response.data.get("dmst_stex_tp", request.exchange)),
        request=request,
    )


def cancel(
    client: KiwoomClient,
    order_no: str,
    stock_code: str,
    quantity: int = 0,
    *,
    exchange: str = "KRX",
) -> str:
    """주문 취소. quantity=0이면 잔량 전부 취소."""
    if not order_no:
        raise OrderError("원주문번호가 필요합니다.")
    body = {
        "dmst_stex_tp": exchange,
        "orig_ord_no": order_no,
        "stk_cd": stock_code,
        "cncl_qty": str(quantity),
    }
    logger.warning("주문 취소 전송: 주문번호 %s %s", order_no, stock_code)
    response = client.request(CANCEL, ORDR, body)
    return str(response.data.get("ord_no", order_no)).strip()


@dataclass(frozen=True)
class PendingOrder:
    order_no: str
    stock_code: str
    name: str
    side: str  # 주문구분 텍스트 (매수/매도)
    order_type: str
    quantity: int
    price: int
    remaining: int  # 미체결수량
    filled_price: int
    status: str
    time: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PendingOrder:
        def num(key: str) -> int:
            text = str(row.get(key, "")).strip().replace(",", "")
            if not text:
                return 0
            sign = -1 if text.startswith("-") else 1
            digits = text.lstrip("+-").lstrip("0")
            return sign * int(digits) if digits else 0

        return cls(
            order_no=str(row.get("ord_no", "")).strip(),
            stock_code=str(row.get("stk_cd", "")).strip().lstrip("A"),
            name=str(row.get("stk_nm", "")).strip(),
            side=str(row.get("io_tp_nm", "")).strip(),
            order_type=str(row.get("trde_tp", "")).strip(),
            quantity=num("ord_qty"),
            price=num("ord_pric"),
            remaining=num("oso_qty"),
            filled_price=num("cntr_pric"),
            status=str(row.get("ord_stt", "")).strip(),
            time=str(row.get("tm", "")).strip(),
        )


def pending_orders(
    client: KiwoomClient, *, stock_code: str = "", exchange_tp: str = "0"
) -> list[PendingOrder]:
    """미체결요청(ka10075). 낸 주문이 남아 있는지 확인한다."""
    body = {
        "all_stk_tp": "1" if stock_code else "0",
        "trde_tp": "0",  # 전체
        "stex_tp": exchange_tp,
    }
    if stock_code:
        body["stk_cd"] = stock_code
    rows = client.collect("ka10075", ACNT, body, rows_key="oso", max_pages=5)
    return [PendingOrder.from_row(r) for r in rows]
