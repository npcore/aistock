"""기술적 필터 — 공시 신호를 주문 후보로 올리기 전에 차트로 한 번 더 거른다.

공시가 좋아도 이미 상한가 근처까지 올랐거나 긴 하락 추세 한가운데라면 진입 타이밍이
나쁘다. 여기서는 **매수만 막는다.** 매도는 리스크 회피라 차트가 나빠도 막지 않는다 —
악재 공시를 보고도 차트 때문에 못 파는 쪽이 더 위험하다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .indicators import rsi, sma

MIN_BARS = 60  # SMA60을 계산할 수 있어야 추세를 본다
RSI_OVERHEATED = 75.0
CHASE_LIMIT_PCT = 15.0  # 당일 이미 이만큼 올랐으면 뉴스가 가격에 반영된 것으로 본다


@dataclass(frozen=True)
class TechnicalView:
    price: float
    change_pct: float  # 전일 종가 대비 등락률
    sma20: float | None
    sma60: float | None
    rsi14: float | None
    bars: int
    blocks: list[str] = field(default_factory=list)  # 매수를 막는 이유

    @property
    def buy_ok(self) -> bool:
        return not self.blocks

    def summary(self) -> str:
        parts = [f"등락 {self.change_pct:+.1f}%"]
        if self.rsi14 is not None:
            parts.append(f"RSI {self.rsi14:.0f}")
        if self.sma20 is not None:
            parts.append(f"20일선 {'위' if self.price >= self.sma20 else '아래'}")
        if self.sma60 is not None:
            parts.append(f"60일선 {'위' if self.price >= self.sma60 else '아래'}")
        return " · ".join(parts)


def evaluate(closes: Sequence[float], price: float, change_pct: float) -> TechnicalView:
    """closes는 과거 일봉 종가(오래된 것부터), price는 현재가.

    현재가를 마지막 값으로 붙여 계산한다. 장중에는 오늘 봉이 아직 확정되지 않았으므로
    어제까지의 종가만 쓰면 오늘의 급등락이 지표에 빠진다.
    """
    series = [*closes, price]
    view = TechnicalView(
        price=price,
        change_pct=change_pct,
        sma20=sma(series, 20),
        sma60=sma(series, 60),
        rsi14=rsi(series, 14),
        bars=len(closes),
    )
    blocks = view.blocks
    if len(closes) < MIN_BARS:
        blocks.append(f"일봉 {len(closes)}개뿐 — 추세 판단 불가(신규상장 등)")
    sma20, sma60 = view.sma20, view.sma60
    if sma20 is not None and sma60 is not None and price < sma60 and sma20 < sma60:
        blocks.append("하락 추세 (현재가·20일선 모두 60일선 아래)")
    if view.rsi14 is not None and view.rsi14 >= RSI_OVERHEATED:
        blocks.append(f"과열 (RSI {view.rsi14:.0f})")
    if change_pct >= CHASE_LIMIT_PCT:
        blocks.append(f"당일 이미 {change_pct:+.1f}% 급등 — 추격 매수")
    return view
