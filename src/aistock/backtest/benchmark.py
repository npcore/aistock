"""지수 대비 초과수익.

지수가 오른 기간이면 아무 종목이나 사도 평균은 양수가 된다. 신호가 실제로 가치가 있는지는
'같은 기간 지수를 그냥 들고 있었을 때'와 비교해야 알 수 있다.

비교는 **같은 날짜, 같은 진입 방식**으로 맞춘다. 신호가 다음 거래일 시가에 들어갔으면
지수도 그날 시가에 들어간 것으로 계산한다. 그래야 시장 타이밍이 아니라 종목 선택의
기여만 남는다.
"""

from __future__ import annotations

import logging

from ..config import CACHE_DIR
from ..market import domestic
from .prices import PriceCache, PriceHistory

logger = logging.getLogger(__name__)

INDEX_CACHE_DIR = CACHE_DIR / "prices"

# corp_cls → 비교할 지수. 코넥스는 전용 지수가 ka20006에 없어 코스닥으로 대신한다.
INDEX_BY_MARKET: dict[str, str] = {
    "Y": domestic.INDEX_KOSPI,
    "K": domestic.INDEX_KOSDAQ,
    "N": domestic.INDEX_KOSDAQ,
    "E": domestic.INDEX_KOSPI,
}
DEFAULT_INDEX = domestic.INDEX_KOSPI

INDEX_NAMES = {
    domestic.INDEX_KOSPI: "KOSPI",
    domestic.INDEX_KOSDAQ: "KOSDAQ",
    domestic.INDEX_KOSPI200: "KOSPI200",
}


class BenchmarkCache(PriceCache):
    """업종(지수) 일봉 캐시. 종목 캐시와 같은 구조를 쓰되 조회 TR만 다르다."""

    def index_code(self, corp_cls: str) -> str:
        return INDEX_BY_MARKET.get((corp_cls or "").upper(), DEFAULT_INDEX)

    def for_market(self, corp_cls: str) -> PriceHistory | None:
        return self.get(f"idx{self.index_code(corp_cls)}")

    def _from_api(self, code: str) -> PriceHistory | None:  # type: ignore[override]
        if self.client is None:
            logger.warning("지수 캐시가 없고 키움 클라이언트도 없습니다: %s", code)
            return None
        from ..errors import KiwoomError
        from .prices import bars_from_rows

        inds_cd = code.removeprefix("idx")
        try:
            # 600거래일이면 충분하다 — 의도된 절단이므로 경고를 끈다.
            rows = domestic.index_daily_chart(
                self.client,
                inds_cd,
                self.base_dt or _today(),
                max_pages=1,
                warn_on_truncate=False,
            )
        except KiwoomError as exc:
            logger.warning("지수 일봉 조회 실패 (%s): %s", inds_cd, exc)
            return None

        bars = bars_from_rows(rows, code)
        if not bars:
            logger.warning("지수 일봉이 비어 있습니다: %s", inds_cd)
            return None

        history = PriceHistory(code, bars)
        self._save(code, history)
        logger.info(
            "지수 일봉 %d일치 확보: %s (%s)",
            len(history),
            INDEX_NAMES.get(inds_cd, inds_cd),
            inds_cd,
        )
        return history


def _today() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
