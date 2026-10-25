"""키움 앱(영웅문) 관심종목 조회.

앱에서 등록한 관심종목을 그대로 읽어온다. `watchlist.txt`(로컬 파일)와 달리 별도 관리가
필요 없고, 앱에서 종목을 추가하면 여기에도 바로 반영된다.

국내와 미국이 TR·응답 필드가 다르다. 미국은 `stex_tp`(NY/NA/ND)가 함께 오는데, 시세 조회에
그 값이 필요하므로 반드시 같이 들고 다녀야 한다 — 거래소를 모르면 미국 종목은 조회할 수 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .client import KiwoomClient
from .errors import KiwoomError

logger = logging.getLogger(__name__)

DOMESTIC_PATH = "/api/dostk/watchlist"
US_PATH = "/api/us/watchlist"

GROUPS_DOMESTIC = "ka01300"
DETAIL_DOMESTIC = "ka01301"
GROUPS_US = "usa20200"
DETAIL_US = "usa20201"


@dataclass(frozen=True)
class AppGroup:
    code: str  # gcod
    name: str
    market: str  # "domestic" | "us"


@dataclass(frozen=True)
class AppStock:
    code: str
    market: str  # "domestic" | "us"
    exchange: str = ""  # 미국만: NY/NA/ND
    group: str = ""

    @property
    def is_us(self) -> bool:
        return self.market == "us"


def groups(client: KiwoomClient, *, market: str = "domestic") -> list[AppGroup]:
    """관심종목 그룹 목록."""
    api, path = (GROUPS_US, US_PATH) if market == "us" else (GROUPS_DOMESTIC, DOMESTIC_PATH)
    data = client.request(api, path, {}).data
    return [
        AppGroup(code=str(g.get("gcod", "")), name=str(g.get("name", "")), market=market)
        for g in (data.get("nofi") or [])
        if g.get("gcod")
    ]


def stocks_in_group(
    client: KiwoomClient, group_code: str, *, market: str = "domestic", group_name: str = ""
) -> list[AppStock]:
    """그룹에 담긴 종목."""
    api, path = (DETAIL_US, US_PATH) if market == "us" else (DETAIL_DOMESTIC, DOMESTIC_PATH)
    data = client.request(api, path, {"arn_grp_id": group_code}).data
    out = []
    for row in data.get("nofj") or []:
        code = str(row.get("cod2", "")).strip()
        if not code:
            continue
        out.append(
            AppStock(
                code=code,
                market=market,
                exchange=str(row.get("stex_tp", "")).strip(),
                group=group_name or group_code,
            )
        )
    return out


def fetch_all(client: KiwoomClient) -> list[AppStock]:
    """국내·미국 관심종목 전체.

    한쪽 조회가 실패해도 나머지는 돌려준다 — 미국 관심종목이 비어 있다고 국내까지
    못 보게 되면 곤란하다.
    """
    out: list[AppStock] = []
    for market in ("domestic", "us"):
        try:
            for group in groups(client, market=market):
                out.extend(
                    stocks_in_group(client, group.code, market=market, group_name=group.name)
                )
        except KiwoomError as exc:
            logger.warning("%s 관심종목 조회 실패: %s", market, exc)
    return out
