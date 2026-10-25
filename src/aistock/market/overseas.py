"""미국주식 시세 조회.

주의: `stex_tp`(거래소구분) 코드 체계가 국내 TR과 다르다. 미국 시세는 문자 코드를 쓴다.
    NY = NYSE, ND = NASDAQ, NA = AMEX
국내 TR의 `stex_tp`("1:KRX, 2:NXT, 3:통합")와 혼동하면 조용히 빈 결과가 나온다.
"""

from __future__ import annotations

from typing import Any, Literal

from ..client import KiwoomClient

US_MRKCOND = "/api/us/mrkcond"

Exchange = Literal["NY", "ND", "NA"]

EXCHANGES: dict[str, str] = {"NY": "NYSE", "ND": "NASDAQ", "NA": "AMEX"}


def quote(client: KiwoomClient, stk_cd: str, stex_tp: Exchange = "ND") -> dict[str, Any]:
    """미국주식 현재가 종목정보(usa20100) — 현재가, 등락률, 52주 고저, 시총 등."""
    return client.request("usa20100", US_MRKCOND, {"stex_tp": stex_tp, "stk_cd": stk_cd}).data


def order_book(client: KiwoomClient, stk_cd: str, stex_tp: Exchange = "ND") -> dict[str, Any]:
    """미국주식 현재가 10호가(usa20101)."""
    return client.request("usa20101", US_MRKCOND, {"stex_tp": stex_tp, "stk_cd": stk_cd}).data


def daily_price(
    client: KiwoomClient,
    stk_cd: str,
    base_dt: str,
    stex_tp: Exchange = "ND",
    *,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """미국주식 일별주가(usa20590) — base_dt(YYYYMMDD) 이전 내역."""
    return client.collect(
        "usa20590",
        US_MRKCOND,
        {"stex_tp": stex_tp, "stk_cd": stk_cd, "base_dt": base_dt},
        rows_key="result_list",
        max_pages=max_pages,
    )
