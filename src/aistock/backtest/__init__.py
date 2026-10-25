"""신호 백테스팅.

`logs/signals.jsonl`의 신호를 키움 일봉과 대조해 실제 수익성을 본다.
`actionable` 임계값(confidence ≥ 70)은 근거 없는 임시값이므로, 여기서 나오는 구간별
성적으로 정해야 한다.
"""

from .benchmark import INDEX_BY_MARKET, INDEX_NAMES, BenchmarkCache
from .costs import STT_RATES, CostModel
from .engine import (
    Entry,
    Skipped,
    SkipReason,
    Trade,
    dedupe,
    evaluate,
    load_signals,
    run,
)
from .prices import Bar, PriceCache, PriceHistory, parse_price
from .report import Stats, render

__all__ = [
    "INDEX_BY_MARKET",
    "INDEX_NAMES",
    "STT_RATES",
    "Bar",
    "BenchmarkCache",
    "CostModel",
    "Entry",
    "PriceCache",
    "PriceHistory",
    "SkipReason",
    "Skipped",
    "Stats",
    "Trade",
    "dedupe",
    "evaluate",
    "load_signals",
    "parse_price",
    "render",
    "run",
]
