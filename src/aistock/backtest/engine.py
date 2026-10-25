"""신호 → 체결 가정 → 수익률.

## look-ahead bias

이 모듈에서 제일 조심할 지점이다. DART 공시는 07:30~18:00 아무 때나 접수되고, 우리는
접수 **날짜**만 알 뿐 시각을 모른다(접수번호의 일련번호는 시각이 아니다). 15:30 장마감
뒤에 올라온 공시를 그날 종가로 체결했다고 치면, 실제로는 불가능했던 거래를 성사시킨 셈이
되어 성과가 부풀려진다.

그래서 기본 진입 시점은 **다음 거래일 시가**다. 당일 반응을 통째로 놓치므로 신호의 가치를
과소평가하지만, 과대평가보다는 낫다. `SAME_CLOSE`도 고를 수 있게 열어뒀으나 그건 낙관적
상한선이며 실제 달성 가능한 수치가 아니다 — 둘을 같이 돌려 격차를 보는 용도로 쓸 것.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .benchmark import INDEX_NAMES, BenchmarkCache
from .costs import CostModel
from .prices import Bar, PriceCache

logger = logging.getLogger(__name__)


class Entry(str, Enum):
    NEXT_OPEN = "next_open"  # 공시일 다음 거래일 시가 — 기본값, look-ahead 없음
    SAME_CLOSE = "same_close"  # 공시일 종가 — 낙관적 상한선, 장마감 후 공시면 실행 불가


class SkipReason(str, Enum):
    NO_STOCK_CODE = "종목코드 없음"
    NO_PRICE_DATA = "일봉 조회 불가"
    NO_ENTRY_BAR = "진입일 일봉 없음"
    NO_EXIT_BAR = "청산일 일봉 없음"
    NOT_DIRECTIONAL = "방향성 신호 아님"
    BELOW_CONFIDENCE = "confidence 미달"
    ZERO_PRICE = "진입가 0"


@dataclass(frozen=True)
class Trade:
    rcept_no: str
    stock_code: str
    corp_name: str
    action: str
    confidence: int
    event_type: str
    signal_date: str
    entry_date: str
    entry_price: int
    exit_date: str
    exit_price: int
    corp_cls: str = "Y"  # 시장 구분 — 증권거래세율과 비교 지수가 다르다
    cost: CostModel = field(default_factory=CostModel)
    # 같은 기간 지수 수익률(%). 지수 일봉이 없으면 None.
    benchmark_pct: float | None = None
    benchmark_name: str = ""

    @property
    def raw_return_pct(self) -> float:
        """주가 자체 변화율. 비용 미반영."""
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def signed_return_pct(self) -> float:
        """신호 방향 기준 **총**수익률. 비용 미반영.

        sell은 공매도를 가정한 부호 반전이다. 국내 개인은 공매도가 사실상 어려우므로
        이 값은 '방향을 맞췄는가'의 지표로 읽어야지, 실현 가능한 수익으로 읽으면 안 된다.
        """
        return self.raw_return_pct if self.action == "buy" else -self.raw_return_pct

    @property
    def net_return_pct(self) -> float:
        """수수료·거래세·슬리피지를 뺀 순수익률. 실제로 봐야 할 숫자."""
        return self.cost.net_return_pct(
            self.action, self.entry_price, self.exit_price, self.corp_cls
        )

    @property
    def cost_drag_pct(self) -> float:
        """비용이 갉아먹은 폭(%p)."""
        return self.signed_return_pct - self.net_return_pct

    @property
    def hit(self) -> bool:
        """비용까지 뺀 뒤에도 이익인가. 총수익 기준 적중은 의미가 없다."""
        return self.net_return_pct > 0

    @property
    def benchmark_signed_pct(self) -> float | None:
        """신호 방향에서 본 지수 수익률.

        매수는 지수가 오르면 그만큼 넘어야 하고, 공매도는 지수가 내리면 그만큼 넘어야 한다.
        """
        if self.benchmark_pct is None:
            return None
        return self.benchmark_pct if self.action == "buy" else -self.benchmark_pct

    @property
    def excess_return_pct(self) -> float | None:
        """지수 대비 초과수익(%p).

        전략은 비용 차감 후, 지수는 비용 미반영이다. 지수를 ETF로 들고 있어도 비용이 드니
        이 비교는 전략에 불리한 쪽으로 기울어 있다 — 보수적인 방향이라 그대로 둔다.
        """
        signed = self.benchmark_signed_pct
        if signed is None:
            return None
        return self.net_return_pct - signed

    @property
    def beat_market(self) -> bool | None:
        excess = self.excess_return_pct
        return None if excess is None else excess > 0


@dataclass(frozen=True)
class Skipped:
    rcept_no: str
    stock_code: str
    reason: SkipReason


def load_signals(path: Path) -> list[dict[str, Any]]:
    """signals.jsonl을 읽는다."""
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            logger.warning("%s:%d 파싱 실패, 건너뜁니다: %s", path, line_no, exc)
    return rows


def evaluate(
    row: dict[str, Any],
    prices: PriceCache,
    *,
    horizon: int,
    entry: Entry = Entry.NEXT_OPEN,
    min_confidence: int = 0,
    cost: CostModel | None = None,
    benchmark: BenchmarkCache | None = None,
) -> Trade | Skipped:
    """신호 한 건을 체결 가정에 넣어 수익률을 낸다."""
    rcept_no = row.get("rcept_no", "")
    stock_code = (row.get("stock_code") or "").strip()
    action = row.get("action", "")
    confidence = int(row.get("confidence", 0) or 0)

    if action not in ("buy", "sell"):
        return Skipped(rcept_no, stock_code, SkipReason.NOT_DIRECTIONAL)
    if confidence < min_confidence:
        return Skipped(rcept_no, stock_code, SkipReason.BELOW_CONFIDENCE)
    if not stock_code:
        return Skipped(rcept_no, stock_code, SkipReason.NO_STOCK_CODE)

    history = prices.get(stock_code)
    if history is None:
        return Skipped(rcept_no, stock_code, SkipReason.NO_PRICE_DATA)

    signal_date = row.get("rcept_dt") or rcept_no[:8]

    entry_bar: Bar | None
    if entry is Entry.SAME_CLOSE:
        entry_bar = history.bar_on_or_after(signal_date)
        entry_price = entry_bar.close if entry_bar else 0
    else:
        entry_bar = history.bar_after(signal_date)
        entry_price = entry_bar.open if entry_bar else 0

    if entry_bar is None:
        return Skipped(rcept_no, stock_code, SkipReason.NO_ENTRY_BAR)
    if entry_price <= 0:
        return Skipped(rcept_no, stock_code, SkipReason.ZERO_PRICE)

    exit_bar = history.nth_after(entry_bar.date, horizon)
    if exit_bar is None:
        # 아직 horizon만큼 시간이 지나지 않은 최근 신호도 여기로 온다 — 실패가 아니다.
        return Skipped(rcept_no, stock_code, SkipReason.NO_EXIT_BAR)

    corp_cls = str(row.get("corp_cls") or "Y").upper()
    benchmark_pct, benchmark_name = _benchmark_for(
        benchmark, corp_cls, entry_bar.date, exit_bar.date, entry
    )

    return Trade(
        rcept_no=rcept_no,
        stock_code=stock_code,
        corp_name=row.get("corp_name", ""),
        action=action,
        confidence=confidence,
        event_type=row.get("event_type", ""),
        signal_date=signal_date,
        entry_date=entry_bar.date,
        entry_price=entry_price,
        exit_date=exit_bar.date,
        exit_price=exit_bar.close,
        corp_cls=corp_cls,
        cost=cost or CostModel(),
        benchmark_pct=benchmark_pct,
        benchmark_name=benchmark_name,
    )


def _benchmark_for(
    benchmark: BenchmarkCache | None,
    corp_cls: str,
    entry_date: str,
    exit_date: str,
    entry: Entry,
) -> tuple[float | None, str]:
    """같은 날짜·같은 진입 방식으로 지수 수익률을 계산한다."""
    if benchmark is None:
        return None, ""
    history = benchmark.for_market(corp_cls)
    if history is None:
        return None, ""

    entry_bar = history.bar_on_or_after(entry_date)
    exit_bar = history.bar_on_or_after(exit_date)
    if entry_bar is None or exit_bar is None or entry_bar.date != entry_date:
        # 지수 일봉에 그 날짜가 없으면 억지로 근사하지 않고 비교를 포기한다.
        return None, ""

    # 종목이 시가에 들어갔으면 지수도 시가 기준으로 맞춘다.
    base = entry_bar.open if entry is Entry.NEXT_OPEN else entry_bar.close
    if base <= 0:
        return None, ""
    name = INDEX_NAMES.get(benchmark.index_code(corp_cls), "")
    return (exit_bar.close - base) / base * 100, name


def dedupe(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """같은 종목·같은 날의 신호를 하나로 줄이고, 줄인 개수를 함께 돌려준다.

    한 회사가 하루에 공시를 여러 건 내는 일은 흔하다(유상증자와 전환사채를 같은 날 공시 등).
    그대로 두면 진입·청산이 동일한 거래가 여러 번 계산되어 표본 수가 부풀고, 공시가 잦은
    종목이 통계를 과대 대표한다.

    남길 하나는 **방향성 우선, 그다음 confidence** 순으로 고른다. confidence만 보면
    `watch 80%`가 `buy 75%`를 밀어내 매매 가능한 신호가 사라진다 — 실제로 한 종목이 같은 날
    watch 두 건과 ignore 한 건을 낸 사례에서 ignore가 남는 것을 확인했다.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        code = (row.get("stock_code") or "").strip()
        date = row.get("rcept_dt") or str(row.get("rcept_no", ""))[:8]
        key = (code, date)
        current = best.get(key)
        if current is None or _dedupe_rank(row) > _dedupe_rank(current):
            best[key] = row
    return list(best.values()), len(rows) - len(best)


def _dedupe_rank(row: dict[str, Any]) -> tuple[int, int, str]:
    """방향성 > confidence > 접수번호(늦은 것). 접수번호까지 보는 건 결정적으로 고르기 위해."""
    directional = 1 if row.get("action") in ("buy", "sell") else 0
    return directional, int(row.get("confidence", 0) or 0), str(row.get("rcept_no", ""))


def run(
    rows: list[dict[str, Any]],
    prices: PriceCache,
    *,
    horizon: int = 5,
    entry: Entry = Entry.NEXT_OPEN,
    min_confidence: int = 0,
    deduplicate: bool = True,
    cost: CostModel | None = None,
    benchmark: BenchmarkCache | None = None,
) -> tuple[list[Trade], list[Skipped], int]:
    """신호 목록 전체를 평가해 (체결, 제외, 중복제거 건수)로 나눈다."""
    merged = 0
    if deduplicate:
        rows, merged = dedupe(rows)

    trades: list[Trade] = []
    skipped: list[Skipped] = []
    for row in rows:
        result = evaluate(
            row,
            prices,
            horizon=horizon,
            entry=entry,
            min_confidence=min_confidence,
            cost=cost,
            benchmark=benchmark,
        )
        (trades if isinstance(result, Trade) else skipped).append(result)  # type: ignore[arg-type]
    return trades, skipped, merged


def iter_confidence_buckets(trades: list[Trade]) -> Iterator[tuple[str, list[Trade]]]:
    """confidence 구간별로 묶는다. 임계값이 실제로 의미가 있는지 보려는 것."""
    bounds = [(0, 59), (60, 69), (70, 79), (80, 100)]
    for low, high in bounds:
        bucket = [t for t in trades if low <= t.confidence <= high]
        yield f"{low}~{high}", bucket
