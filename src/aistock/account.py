"""계좌 잔고 조회.

**조회 전용이다. 이 모듈에 주문을 내는 경로는 없다.** 국내 잔고·예수금과 미국 잔고·예수금을 읽는다.

실계좌를 읽으므로 응답 파싱이 틀리면 보유 현황을 잘못 보게 된다. 키움 계좌 TR은 시세 TR과
표기 규칙이 달라 두 지점을 특히 조심해야 한다.

    stk_cd      "A005930" — 시장 접두사 A가 붙는다. 시세 TR의 "005930"과 다르다.
    숫자 필드    "-00000000196888" — 부호가 앞, 뒤는 0으로 채워진 고정폭.
                 int()에 그냥 넣으면 되지만 0패딩을 8진수로 오해하는 파서를 쓰면 깨진다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .client import KiwoomClient

logger = logging.getLogger(__name__)

ACNT = "/api/dostk/acnt"


def parse_amount(value: Any) -> int:
    """`-00000000196888` / `000000000124500` 형태를 정수로."""
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    sign = -1 if text[0] == "-" else 1
    digits = text.lstrip("+-").lstrip("0")
    return sign * int(digits) if digits else 0


def parse_rate(value: Any) -> float:
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_code(stk_cd: str) -> str:
    """`A005930` → `005930`. 시세·공시 쪽 코드 체계와 맞춘다."""
    code = str(stk_cd).strip()
    if code[:1].isalpha():
        code = code[1:]
    return code.split("_", 1)[0]


@dataclass(frozen=True)
class Holding:
    stock_code: str
    name: str
    quantity: int
    avg_price: int  # 매입단가
    current_price: int
    purchase_amount: int
    eval_amount: int
    eval_profit: int  # 평가손익 (수수료·세금 반영된 키움 계산값)
    profit_rate: float  # %
    weight: float  # 계좌 내 비중 %
    sellable: int  # 매도가능수량

    @property
    def is_profit(self) -> bool:
        return self.eval_profit > 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Holding:
        return cls(
            stock_code=normalize_code(row.get("stk_cd", "")),
            name=str(row.get("stk_nm", "")).strip(),
            quantity=parse_amount(row.get("rmnd_qty")),
            avg_price=parse_amount(row.get("pur_pric")),
            current_price=parse_amount(row.get("cur_prc")),
            purchase_amount=parse_amount(row.get("pur_amt")),
            eval_amount=parse_amount(row.get("evlt_amt")),
            eval_profit=parse_amount(row.get("evltv_prft")),
            profit_rate=parse_rate(row.get("prft_rt")),
            weight=parse_rate(row.get("poss_rt")),
            sellable=parse_amount(row.get("trde_able_qty")),
        )


@dataclass(frozen=True)
class Balance:
    total_purchase: int
    total_eval: int
    total_profit: int
    total_profit_rate: float
    estimated_assets: int  # 추정예탁자산
    holdings: list[Holding]

    @property
    def codes(self) -> list[str]:
        return [h.stock_code for h in self.holdings if h.stock_code]

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Balance:
        rows = data.get("acnt_evlt_remn_indv_tot") or []
        holdings = [Holding.from_row(r) for r in rows if isinstance(r, dict)]
        # 수량 0인 잔여 행이 섞여 오는 경우가 있어 걸러낸다.
        holdings = [h for h in holdings if h.quantity > 0]
        return cls(
            total_purchase=parse_amount(data.get("tot_pur_amt")),
            total_eval=parse_amount(data.get("tot_evlt_amt")),
            total_profit=parse_amount(data.get("tot_evlt_pl")),
            total_profit_rate=parse_rate(data.get("tot_prft_rt")),
            estimated_assets=parse_amount(data.get("prsm_dpst_aset_amt")),
            holdings=holdings,
        )


def fetch_balance(client: KiwoomClient, *, exchange: str = "KRX", max_pages: int = 5) -> Balance:
    """계좌평가잔고내역요청(kt00018) — 보유종목과 평가손익.

    조회만 한다. exchange는 KRX 또는 NXT.
    """
    body = {"qry_tp": "2", "dmst_stex_tp": exchange}  # 2=개별(종목별)
    first: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for response in client.paginate("kt00018", ACNT, body, max_pages=max_pages):
        if first is None:
            first = response.data
        rows.extend(response.rows("acnt_evlt_remn_indv_tot"))

    if first is None:
        raise ValueError("계좌 잔고 응답이 비어 있습니다.")
    merged = dict(first)
    merged["acnt_evlt_remn_indv_tot"] = rows
    return Balance.from_data(merged)


# --- 예수금 ---


@dataclass(frozen=True)
class Deposit:
    """국내 원화 예수금(kt00001, 추정조회)."""

    cash: int  # 예수금
    d2_cash: int  # D+2 추정예수금 — 매도대금 정산까지 반영한 값
    orderable: int  # 주문가능금액
    withdrawable: int  # 출금가능금액

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Deposit:
        return cls(
            cash=parse_amount(data.get("entr")),
            d2_cash=parse_amount(data.get("d2_entra")),
            orderable=parse_amount(data.get("ord_alow_amt")),
            withdrawable=parse_amount(data.get("pymn_alow_amt")),
        )


def fetch_deposit(client: KiwoomClient) -> Deposit:
    """예수금상세현황요청(kt00001). 조회만 한다."""
    return Deposit.from_data(client.request("kt00001", ACNT, {"qry_tp": "3"}).data)


# --- 미국주식 ---

US_ACNT = "/api/us/acnt"

# 잔고 응답의 거래소명 → 주문 TR의 거래소 코드. 매도할 때 필요하다.
_US_EXCHANGE_BY_NAME = {
    "NASDAQ": "ND",
    "나스닥": "ND",
    "NYSE": "NY",
    "뉴욕": "NY",
    "AMEX": "NA",
    "아멕스": "NA",
}


def parse_usd(value: Any) -> float:
    """`-12.3400` / `0068.2100` 형태. 국내 금액과 달리 소수가 있고 부호는 진짜 음수다(손익)."""
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class UsHolding:
    symbol: str
    name: str
    exchange: str  # ND/NY/NA, 모르면 ""
    quantity: int
    sellable: int
    avg_price: float  # USD
    current_price: float  # USD
    eval_amount: float  # USD
    profit: float  # USD
    profit_rate: float  # %
    eval_krw: int
    profit_krw: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UsHolding:
        exch_name = str(row.get("stex_nm", "")).strip().upper()
        exchange = next((v for k, v in _US_EXCHANGE_BY_NAME.items() if k.upper() in exch_name), "")
        return cls(
            symbol=str(row.get("stk_cd", "")).strip(),
            name=str(row.get("frgn_stk_nm", "")).strip(),
            exchange=exchange,
            quantity=parse_amount(row.get("poss_qty")),
            sellable=parse_amount(row.get("sell_alowq")),
            avg_price=parse_usd(row.get("frgn_stk_book_uv")),
            current_price=parse_usd(row.get("now_pric")),
            eval_amount=parse_usd(row.get("evlt_amt")),
            profit=parse_usd(row.get("pl_amt")),
            profit_rate=parse_rate(row.get("pl_rt")),
            eval_krw=parse_amount(row.get("evlt_amt_krw")),
            profit_krw=parse_amount(row.get("pl_amt_krw")),
        )


@dataclass(frozen=True)
class UsBalance:
    holdings: list[UsHolding]
    total_eval: float  # USD
    total_purchase: float
    total_profit: float
    total_profit_rate: float
    total_eval_krw: int
    total_profit_krw: int
    krw_cash: int  # 해외주식 계좌의 원화예수금
    usd_cash: float  # 외화(USD) 예수금
    usd_orderable: float  # 외화 주문가능금액


def fetch_us_balance(client: KiwoomClient) -> UsBalance:
    """미국주식 원장잔고(ust21070) + 해외주식 예수금(ust21110). 조회만 한다."""
    data = client.request("ust21070", US_ACNT, {}).data
    rows = data.get("result_list") or []
    holdings = [UsHolding.from_row(r) for r in rows if isinstance(r, dict)]
    holdings = [h for h in holdings if h.quantity > 0]

    cash = client.request("ust21110", US_ACNT, {}).data
    usd = next(
        (r for r in cash.get("result_list") or [] if str(r.get("crnc_code", "")).strip() == "USD"),
        {},
    )
    return UsBalance(
        holdings=holdings,
        total_eval=parse_usd(data.get("tot_evlt_amt")),
        total_purchase=parse_usd(data.get("tot_prch_amt")),
        total_profit=parse_usd(data.get("tot_pl_amt")),
        total_profit_rate=parse_rate(data.get("tot_pl_rt")),
        total_eval_krw=parse_amount(data.get("tot_evlt_amt_krw")),
        total_profit_krw=parse_amount(data.get("tot_pl_amt_krw")),
        krw_cash=parse_amount(cash.get("krw_entra")),
        usd_cash=parse_usd(usd.get("fc_entra")),
        usd_orderable=parse_usd(usd.get("fc_ord_alowa")),
    )
