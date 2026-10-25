"""미국주식 주문.

**이 모듈은 실계좌에 진짜 주문을 낸다. 되돌릴 수 없다.** 원칙은 국내 `order.py`와 같다.

국내와 다른 점:

- **지정가만 지원한다.** 미국은 프리·애프터마켓 호가가 얇고, TQQQ 같은 3배 레버리지 ETF는
  개장 직후 몇 초 사이에도 1~2%씩 움직인다. 시장가는 체결가를 예측할 수 없다.
- **금액 상한이 달러다** (`MAX_ORDER_AMOUNT_USD`, 기본 $7,000). 원화 상한을 환율로 바꾸면
  환율 조회가 실패할 때 상한 검사가 같이 무너진다.
- **가격이 소수다.** $1 이상은 센트(0.01) 단위로만 낼 수 있다.
- **거래소 코드(ND/NY/NA)가 필요하다.** 틀리면 거부되므로 `resolve_exchange()`로 시세를 조회해
  맞는 거래소를 찾는다.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from .account import parse_amount, parse_usd
from .client import KiwoomClient
from .config import load_env
from .errors import APIError, KiwoomError
from .market import overseas

logger = logging.getLogger(__name__)

ORDR = "/api/us/ordr"
ACNT = "/api/us/acnt"

BUY = "ust20000"
SELL = "ust20001"
CANCEL = "ust20003"
PENDING = "ust21050"
ORDERABLE = "ust31490"

LIMIT = "00"
EXCHANGES = ("ND", "NY", "NA")  # 조회 순서. 대부분의 ETF·기술주가 나스닥이다

DEFAULT_MAX_ORDER_USD = 7_000

# 미체결이 없으면 빈 목록 대신 오류(return_code=20)로 온다.
NO_PENDING_CODE = 20

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class UsOrderError(KiwoomError):
    """주문이 나가기 전에 막혔거나 서버가 거절했다."""


def is_us_symbol(code: str) -> bool:
    """`TQQQ`처럼 영문으로 시작하면 미국 종목. 국내는 6자리 숫자다."""
    return bool(_SYMBOL.match(code.strip().upper()))


def max_order_usd() -> float:
    load_env()
    raw = os.getenv("MAX_ORDER_AMOUNT_USD", "").strip()
    if not raw:
        return DEFAULT_MAX_ORDER_USD
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        logger.warning("MAX_ORDER_AMOUNT_USD를 읽을 수 없어 기본값을 씁니다: %r", raw)
        return DEFAULT_MAX_ORDER_USD


def tick(price: float) -> Decimal:
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def price_text(price: float) -> str:
    """주문 단가 문자열. 키움은 $1 이상은 소수 2자리, $1 미만은 4자리**만** 받는다.

    `70.8300`처럼 자릿수를 더 붙여도 거부된다(1517 입력 값 형식 오류) — 실제로 승인한 주문이
    이것 때문에 나가지 않았다.
    """
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def round_price(price: float, *, up: bool) -> float:
    """호가단위로 맞춘다. 매수는 올림, 매도는 내림."""
    t = tick(price)
    return float(Decimal(str(price)).quantize(t, rounding=ROUND_UP if up else ROUND_DOWN))


@dataclass(frozen=True)
class UsOrderRequest:
    side: str  # buy | sell
    symbol: str
    exchange: str  # ND | NY | NA
    quantity: int
    price: float  # USD 지정가

    @property
    def estimated_usd(self) -> float:
        return round(self.quantity * self.price, 2)

    def describe(self) -> str:
        side = "매수" if self.side == "buy" else "매도"
        market = overseas.EXCHANGES.get(self.exchange, self.exchange)
        return (
            f"{side} {self.symbol}({market}) {self.quantity:,}주 @ ${self.price:,.2f} (지정가) "
            f"= ${self.estimated_usd:,.2f}"
        )


@dataclass(frozen=True)
class UsOrderResult:
    order_no: str
    request: UsOrderRequest


def validate(request: UsOrderRequest) -> None:
    if request.side not in ("buy", "sell"):
        raise UsOrderError(f"매수/매도 구분이 잘못됐습니다: {request.side!r}")
    if not is_us_symbol(request.symbol) or request.symbol != request.symbol.upper():
        raise UsOrderError(f"미국 종목코드 형식이 아닙니다: {request.symbol!r}")
    if request.exchange not in EXCHANGES:
        raise UsOrderError(f"거래소 코드는 ND/NY/NA 중 하나여야 합니다: {request.exchange!r}")
    if request.quantity <= 0:
        raise UsOrderError(f"주문수량은 1주 이상이어야 합니다: {request.quantity}")
    if request.price <= 0:
        raise UsOrderError("지정가가 필요합니다. 미국주식은 시장가 주문을 지원하지 않습니다.")
    if Decimal(str(request.price)) % tick(request.price) != 0:
        raise UsOrderError(
            f"가격 ${request.price}가 호가단위({tick(request.price)})에 맞지 않습니다."
        )
    limit = max_order_usd()
    if limit and request.estimated_usd > limit:
        raise UsOrderError(
            f"주문금액 ${request.estimated_usd:,.2f}이 상한 ${limit:,.0f}을 넘습니다. "
            f".env의 MAX_ORDER_AMOUNT_USD를 올리거나 수량을 줄이세요."
        )


def resolve_exchange(client: KiwoomClient, symbol: str) -> tuple[str, dict[str, Any]]:
    """종목이 상장된 거래소를 찾는다 → (거래소 코드, 시세). 없으면 UsOrderError."""
    symbol = symbol.strip().upper()
    for exchange in EXCHANGES:
        try:
            data = overseas.quote(client, symbol, exchange)
        except APIError:
            continue  # 다른 거래소면 '종목 정보가 없습니다'로 거절된다
        if str(data.get("stk_nm", "")).strip():
            return exchange, data
    raise UsOrderError(f"미국 종목 {symbol}을 찾을 수 없습니다 (NASDAQ/NYSE/AMEX 모두 조회 실패).")


def place(client: KiwoomClient, request: UsOrderRequest) -> UsOrderResult:
    """주문을 낸다. **실계좌에 즉시 반영된다.**"""
    validate(request)
    body = {
        "stex_tp": request.exchange,
        "stk_cd": request.symbol,
        "ord_qty": str(request.quantity),
        "ord_uv": price_text(request.price),
        "trde_tp": LIMIT,
    }
    logger.warning("미국 주문 전송: %s", request.describe())
    response = client.request(BUY if request.side == "buy" else SELL, ORDR, body)
    order_no = str(response.data.get("ord_no", "")).strip()
    if not order_no:
        raise UsOrderError(f"주문번호가 응답에 없습니다: {response.data}")
    logger.warning("미국 주문 접수 완료: 주문번호 %s", order_no)
    return UsOrderResult(order_no=order_no, request=request)


def max_buy_quantity(client: KiwoomClient, symbol: str, exchange: str, price: float) -> int:
    """이 가격에 현금으로(미수 없이) 살 수 있는 최대 수량 — 증권사가 계산한 값 (ust31490).

    직접 '외화 ÷ 가격'으로 계산하면 수수료·이미 걸린 미체결 주문이 묶은 금액을 놓친다.
    """
    body = {"stex_tp": exchange, "stk_cd": symbol, "uv": price_text(price)}
    data = client.request(ORDERABLE, ORDR, body).data
    return parse_amount(data.get("min_ord_alowq"))


def cancel(client: KiwoomClient, order_no: str, symbol: str, exchange: str) -> str:
    """미체결 잔량 전부 취소."""
    if not order_no:
        raise UsOrderError("원주문번호가 필요합니다.")
    body = {"orig_ord_no": order_no, "stex_tp": exchange, "stk_cd": symbol}
    logger.warning("미국 주문 취소 전송: 주문번호 %s %s", order_no, symbol)
    response = client.request(CANCEL, ORDR, body)
    return str(response.data.get("ord_no", order_no)).strip()


@dataclass(frozen=True)
class UsPendingOrder:
    order_no: str
    symbol: str
    name: str
    exchange: str
    side: str  # 매수/매도
    quantity: int
    price: float
    filled: int
    remaining: int
    status: str
    time: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UsPendingOrder:
        name = str(row.get("stex_nm", "")).strip().upper()
        exchange = (
            "ND"
            if "NASDAQ" in name or "나스닥" in name
            else ("NY" if "NYSE" in name or "뉴욕" in name else "NA" if "AMEX" in name else "")
        )
        return cls(
            order_no=str(row.get("ord_no", "")).strip(),
            symbol=str(row.get("stk_cd", "")).strip(),
            name=str(row.get("frgn_stk_nm", "")).strip(),
            exchange=exchange,
            side=str(row.get("slby_tp_nm", "")).strip(),
            quantity=parse_amount(row.get("ord_qty")),
            price=parse_usd(row.get("ord_uv")),
            filled=parse_amount(row.get("cntr_qty")),
            remaining=parse_amount(row.get("ord_remnq")),
            status=str(row.get("ord_stat", "")).strip(),
            time=str(row.get("ord_time", "")).strip(),
        )


def pending_orders(client: KiwoomClient) -> list[UsPendingOrder]:
    """미국주식 원장 미체결(ust21050). 오늘 주문 기준."""
    try:
        data = client.request(PENDING, ACNT, {}).data
    except APIError as exc:
        # 코드만 보지 않는다 — 같은 코드로 다른 실패가 오면 '미체결 없음'으로 오인한다.
        if exc.return_code == NO_PENDING_CODE and "없습니다" in exc.return_msg:
            return []
        raise
    rows = data.get("result_list") or []
    orders = [UsPendingOrder.from_row(r) for r in rows if isinstance(r, dict)]
    return [o for o in orders if o.remaining > 0]
