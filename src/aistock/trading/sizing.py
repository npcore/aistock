"""주문 수량·가격 계산.

호가단위에 맞지 않는 지정가는 키움이 거부한다. 2023년 1월부터 코스피·코스닥이 같은
호가단위 표를 쓴다.
"""

from __future__ import annotations

# (상한 미만, 호가단위)
TICK_TABLE = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
TOP_TICK = 1_000


def tick_size(price: int) -> int:
    for bound, tick in TICK_TABLE:
        if price < bound:
            return tick
    return TOP_TICK


def round_to_tick(price: float, *, up: bool) -> int:
    """호가단위로 맞춘다. 매수는 올림(체결 우선), 매도는 내림."""
    p = int(price)
    tick = tick_size(p)
    if p % tick == 0 and p == price:
        return p
    down = p - p % tick
    if not up:
        return down
    nxt = down + tick
    # 올린 결과가 다음 구간으로 넘어가면 그 구간 단위로 다시 맞춘다 (예: 1,999 → 2,000).
    return nxt if nxt % tick_size(nxt) == 0 else nxt + tick_size(nxt) - nxt % tick_size(nxt)


def quantity_for(budget: int, price: int) -> int:
    """예산 안에서 살 수 있는 최대 수량."""
    if price <= 0 or budget <= 0:
        return 0
    return budget // price
