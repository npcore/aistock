"""백테스팅용 일봉 조회 · 캐시.

같은 종목을 반복 조회하게 되므로 디스크에 캐싱한다. 키움 일봉은 한 번 요청에 600거래일이
오므로 종목당 1회 조회로 충분하다.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from ..client import KiwoomClient
from ..config import CACHE_DIR
from ..dart.corp_code import normalize_stock_code
from ..errors import KiwoomError
from ..market import domestic

logger = logging.getLogger(__name__)

PRICE_CACHE_DIR = CACHE_DIR / "prices"

KST = timezone(timedelta(hours=9))
# 마지막 봉이 이보다 오래되면 다시 받는다. 연휴(주말+공휴일)를 넘길 만큼은 둬야
# 휴장 중에 불필요한 조회가 반복되지 않는다.
MAX_CACHE_AGE = timedelta(days=4)


@dataclass(frozen=True)
class Bar:
    """일봉 하나.

    국내는 원 단위 정수, 미국은 달러 소수라 float으로 받는다. 국내 값은 정수로 떨어지므로
    표시할 때만 구분하면 된다.
    """

    date: str  # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    volume: int


def parse_price(value: Any) -> float:
    """키움 가격 문자열을 숫자로.

    일봉 OHLC에는 부호가 없지만 ETF·미국 시세 등 일부 TR은 `+9875` / `-74.2700` 형태로 준다.
    그 부호는 전일대비 방향 표시이지 음수 가격이 아니다 — 주가는 음수가 될 수 없으므로
    부호를 떼고 절댓값으로 읽는다. 이걸 놓치면 하락 종목 가격이 음수가 되어
    수익률이 통째로 뒤집힌다.

    미국 주가는 소수(74.27)라 정수로 자르면 안 된다. 국내 값은 소수부가 0이라 그대로 쓴다.
    """
    text = str(value).strip().replace(",", "")
    if not text:
        raise ValueError("빈 가격 값")
    if text[0] in "+-":
        text = text[1:]
    return float(text)


class PriceHistory:
    """한 종목의 일봉. 날짜 오름차순으로 정렬해 보관한다."""

    def __init__(self, stock_code: str, bars: list[Bar]) -> None:
        self.stock_code = stock_code
        self.bars = sorted(bars, key=lambda b: b.date)
        self._dates = [b.date for b in self.bars]

    def __len__(self) -> int:
        return len(self.bars)

    def index_on_or_after(self, date: str) -> int | None:
        """`date` 이상인 첫 거래일의 인덱스. 휴장일·주말을 자동으로 건너뛴다."""
        i = bisect_left(self._dates, date)
        return i if i < len(self.bars) else None

    def bar_on_or_after(self, date: str) -> Bar | None:
        i = self.index_on_or_after(date)
        return self.bars[i] if i is not None else None

    def bar_after(self, date: str) -> Bar | None:
        """`date`를 **넘는** 첫 거래일. 다음 거래일 진입에 쓴다."""
        i = bisect_left(self._dates, date)
        while i < len(self.bars) and self.bars[i].date <= date:
            i += 1
        return self.bars[i] if i < len(self.bars) else None

    def nth_after(self, date: str, n: int) -> Bar | None:
        """`date` 이상인 첫 거래일로부터 n거래일 뒤."""
        i = self.index_on_or_after(date)
        if i is None:
            return None
        j = i + n
        return self.bars[j] if j < len(self.bars) else None


def bars_from_rows(rows: list[dict[str, Any]], label: str) -> list[Bar]:
    """키움 일봉 응답 행들을 Bar로. 종목 일봉과 지수 일봉이 같은 필드명을 쓴다."""
    bars = []
    for row in rows:
        try:
            bars.append(
                Bar(
                    date=row["dt"],
                    open=parse_price(row["open_pric"]),
                    high=parse_price(row["high_pric"]),
                    low=parse_price(row["low_pric"]),
                    close=parse_price(row["cur_prc"]),
                    volume=int(str(row.get("trde_qty", "0")).replace(",", "") or 0),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.debug("일봉 행을 건너뜁니다 (%s): %s", label, exc)
    return bars


class PriceCache:
    """종목코드 → PriceHistory. 디스크 캐시를 우선 사용한다."""

    def __init__(self, client: KiwoomClient | None = None, *, base_dt: str | None = None) -> None:
        self.client = client
        self.base_dt = base_dt
        self._memory: dict[str, PriceHistory | None] = {}
        PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, stock_code: str, *, exchange: str = "") -> PriceHistory | None:
        """일봉을 돌려준다. 조회 불가(상폐·비상장 등)면 None.

        exchange(NY/NA/ND)를 주면 미국 종목으로 보고 다른 TR을 쓴다.
        """
        code = normalize_stock_code(stock_code)
        key = f"us_{code}" if exchange else code
        if key in self._memory:
            return self._memory[key]

        history = self._from_disk(key) or (
            self._us_from_api(code, exchange) if exchange else self._from_api(code)
        )
        self._memory[key] = history
        return history

    def _us_from_api(self, code: str, exchange: str) -> PriceHistory | None:
        """미국 일별주가(usa20590). 한 페이지에 20건뿐이라 여러 장 받아야 한다."""
        if self.client is None:
            return None
        from ..market import overseas

        try:
            rows = overseas.daily_price(
                self.client, code, self.base_dt or _today(), exchange, max_pages=8
            )
        except KiwoomError as exc:
            logger.warning("미국 일봉 조회 실패 (%s/%s): %s", code, exchange, exc)
            return None

        bars = bars_from_rows(rows, code)
        if not bars:
            logger.warning("미국 일봉이 비어 있습니다: %s", code)
            return None
        history = PriceHistory(code, bars)
        self._save(f"us_{code}", history)
        logger.info("미국 일봉 %d일치 확보: %s (%s)", len(history), code, exchange)
        return history

    def _path(self, code: str) -> Path:
        return PRICE_CACHE_DIR / f"{code}.json"

    def _save(self, code: str, history: PriceHistory) -> None:
        self._path(code).write_text(
            json.dumps([asdict(b) for b in history.bars], ensure_ascii=False), encoding="utf-8"
        )

    def _from_disk(self, code: str) -> PriceHistory | None:
        """캐시를 읽되 오래됐으면 버린다.

        만료가 없으면 매일 도는 차트가 몇 주 전 데이터를 계속 그린다. 실제로 캐시가
        9일 묵은 채 알림이 나가고 있었다. base_dt를 지정한 백테스팅은 과거 구간을 보는
        것이므로 만료 판정에서 제외한다.
        """
        path = self._path(code)
        if not path.exists():
            return None
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            history = PriceHistory(code, [Bar(**row) for row in rows])
        except (ValueError, TypeError) as exc:
            logger.warning("일봉 캐시를 읽을 수 없어 다시 받습니다 (%s): %s", code, exc)
            return None

        if self.base_dt is None and self._is_stale(history):
            logger.info(
                "일봉 캐시가 오래되어 갱신합니다 (%s, 마지막 %s)", code, history.bars[-1].date
            )
            return None
        return history

    def _is_stale(self, history: PriceHistory) -> bool:
        """마지막 봉이 직전 거래일보다 오래됐으면 갱신 대상.

        주말·공휴일에는 새 봉이 없으므로, '오늘'이 아니라 '며칠 지났는지'로 본다.
        """
        if not history.bars:
            return True
        from datetime import datetime

        last = datetime.strptime(history.bars[-1].date, "%Y%m%d").replace(tzinfo=KST)
        return (datetime.now(KST) - last) > MAX_CACHE_AGE

    def _from_api(self, code: str) -> PriceHistory | None:
        if self.client is None:
            logger.warning("일봉 캐시가 없고 키움 클라이언트도 없습니다: %s", code)
            return None
        try:
            # 600거래일이면 백테스팅에 충분하다 — 의도된 절단이므로 경고를 끈다.
            rows = domestic.daily_chart(
                self.client,
                code,
                self.base_dt or _today(),
                max_pages=1,
                warn_on_truncate=False,
            )
        except KiwoomError as exc:
            logger.warning("일봉 조회 실패 (%s): %s", code, exc)
            return None

        bars = bars_from_rows(rows, code)
        if not bars:
            logger.warning("일봉이 비어 있습니다: %s", code)
            return None

        history = PriceHistory(code, bars)
        self._save(code, history)
        logger.info("일봉 %d일치 확보: %s", len(history), code)
        return history


def _today() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
