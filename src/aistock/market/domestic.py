"""국내주식 · ETF 시세 조회.

파라미터는 모두 `reference/kiwoom_api_spec.json`에서 확인한 실제 스펙 기준이다.
"""

from __future__ import annotations

from typing import Any

from ..client import KiwoomClient

STKINFO = "/api/dostk/stkinfo"
MRKCOND = "/api/dostk/mrkcond"
CHART = "/api/dostk/chart"
ETF = "/api/dostk/etf"


def stock_info(client: KiwoomClient, stk_cd: str) -> dict[str, Any]:
    """주식기본정보요청(ka10001) — 현재가, PER/PBR/ROE, 시총, 52주 고저 등.

    stk_cd는 거래소별 코드를 받는다: KRX `005930`, NXT `005930_NX`, SOR `005930_AL`.
    """
    return client.request("ka10001", STKINFO, {"stk_cd": stk_cd}).data


def order_book(client: KiwoomClient, stk_cd: str) -> dict[str, Any]:
    """주식호가요청(ka10004) — 매도/매수 10호가와 잔량."""
    return client.request("ka10004", MRKCOND, {"stk_cd": stk_cd}).data


def daily_price(
    client: KiwoomClient,
    stk_cd: str,
    qry_dt: str,
    indc_tp: str = "0",
    *,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """일별주가요청(ka10086) — qry_dt(YYYYMMDD) 이전의 일별 시세.

    indc_tp: 0=수량, 1=금액(백만원)
    """
    return client.collect(
        "ka10086",
        MRKCOND,
        {"stk_cd": stk_cd, "qry_dt": qry_dt, "indc_tp": indc_tp},
        rows_key="daly_stkpc",
        max_pages=max_pages,
    )


def daily_chart(
    client: KiwoomClient,
    stk_cd: str,
    base_dt: str,
    upd_stkpc_tp: str = "1",
    *,
    max_pages: int = 10,
    warn_on_truncate: bool = True,
) -> list[dict[str, Any]]:
    """주식일봉차트조회요청(ka10081) — 백테스팅용 일봉.

    upd_stkpc_tp="1"이면 수정주가를 적용한다. 액면분할 같은 권리발생일 이후 날짜를
    base_dt로 넣어야 과거 구간까지 수정주가가 반영된다.

    한 페이지에 600거래일이 온다. 최근 구간만 필요하면 max_pages=1에
    warn_on_truncate=False를 함께 넘겨 의도된 절단임을 표시한다.
    """
    return client.collect(
        "ka10081",
        CHART,
        {"stk_cd": stk_cd, "base_dt": base_dt, "upd_stkpc_tp": upd_stkpc_tp},
        rows_key="stk_dt_pole_chart_qry",
        max_pages=max_pages,
        warn_on_truncate=warn_on_truncate,
    )


# 업종(지수) 코드 — ka20006 기준. 지수값은 소수점 2자리를 정수로 편 형태다(6296.38 → 629638).
# 수익률 계산에는 배율이 상쇄되므로 그대로 써도 된다.
INDEX_KOSPI = "001"
INDEX_KOSDAQ = "101"
INDEX_KOSPI200 = "201"


def index_daily_chart(
    client: KiwoomClient,
    inds_cd: str,
    base_dt: str,
    *,
    max_pages: int = 1,
    warn_on_truncate: bool = True,
) -> list[dict[str, Any]]:
    """업종일봉조회요청(ka20006) — 지수 일봉. 백테스팅의 벤치마크로 쓴다.

    inds_cd: 001=종합(KOSPI), 101=종합(KOSDAQ), 201=KOSPI200
    """
    return client.collect(
        "ka20006",
        CHART,
        {"inds_cd": inds_cd, "base_dt": base_dt},
        rows_key="inds_dt_pole_qry",
        max_pages=max_pages,
        warn_on_truncate=warn_on_truncate,
    )


def etf_all(
    client: KiwoomClient,
    *,
    txon_type: str = "0",
    navpre: str = "0",
    mngmcomp: str = "0000",
    txon_yn: str = "0",
    trace_idex: str = "0",
    stex_tp: str = "3",
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """ETF전체시세요청(ka40004) — 조건에 맞는 ETF 전 종목 시세.

    mngmcomp: 0000=전체, 3020=KODEX, 3027=KOSEF, 3191=TIGER, 3228=KINDEX, 3023=KStar
    stex_tp: 1=KRX, 2=NXT, 3=통합
    """
    return client.collect(
        "ka40004",
        ETF,
        {
            "txon_type": txon_type,
            "navpre": navpre,
            "mngmcomp": mngmcomp,
            "txon_yn": txon_yn,
            "trace_idex": trace_idex,
            "stex_tp": stex_tp,
        },
        rows_key="etfall_mrpr",
        max_pages=max_pages,
    )


def etf_info(client: KiwoomClient, stk_cd: str) -> dict[str, Any]:
    """ETF종목정보요청(ka40002) — NAV, 추적지수, 괴리율 등."""
    return client.request("ka40002", ETF, {"stk_cd": stk_cd}).data
