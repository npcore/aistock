"""기술적 지표. 순수 함수만 둔다 — 네트워크·파일 없이 테스트할 수 있어야 한다."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise


def sma(values: Sequence[float], n: int) -> float | None:
    """단순이동평균. 데이터가 n개 미만이면 None."""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(values: Sequence[float], n: int = 14) -> float | None:
    """Wilder RSI. 데이터가 n+1개 미만이면 None.

    단순평균 RSI와 값이 다르다. 증권사 HTS가 쓰는 쪽(Wilder 평활)에 맞췄다.
    """
    if n <= 0 or len(values) < n + 1:
        return None
    gains, losses = [], []
    for prev, cur in pairwise(values):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:], strict=False):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
