"""거래비용 모델 — 위탁수수료 · 증권거래세 · 슬리피지.

국내 주식은 **매도할 때만** 증권거래세를 낸다. 손실이 나도 붙는다.
왕복 비용이 0.23% 수준이라, 5거래일 보유 전략의 기대수익이 그 아래면 총수익이 양수여도
실제로는 손실이다. 백테스팅에서 비용을 빼먹으면 없는 우위를 있다고 착각하게 된다.

세율 근거 (2026-01-01 시행, 금융투자소득세 폐지에 따른 환원):

    코스피  증권거래세 0.05% + 농어촌특별세 0.15% = 0.20%
    코스닥  증권거래세 0.20%                      = 0.20%
    코넥스  증권거래세 0.10% + 농어촌특별세 0.10% = 0.20%

2025년에는 코스피·코스닥 모두 0.15%였다. **세율은 자주 바뀐다** — 과거 구간을 백테스팅할
때 현재 세율을 그대로 쓰면 비용을 과대·과소 계상하게 되므로, 기간이 길면 `STT_RATES`를
그 시점 기준으로 바꿔 돌려볼 것.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# corp_cls 코드 → 매도 시 세율 (증권거래세 + 농어촌특별세 합계)
STT_RATES: dict[str, float] = {
    "Y": 0.0020,  # 유가증권 (코스피)
    "K": 0.0020,  # 코스닥
    "N": 0.0020,  # 코넥스 — 출처에 따라 0.10%로도 표기된다. 물량이 적어 영향은 미미.
    "E": 0.0020,  # 기타
}
DEFAULT_STT_RATE = 0.0020

# 키움 온라인(HTS·홈페이지) 현금주식 매매 기준. 매수·매도 각각 부과된다.
# 비대면 계좌 이벤트나 지점 계좌면 달라지므로 본인 계좌 기준으로 바꿔서 쓸 것.
DEFAULT_COMMISSION_RATE = 0.00015


@dataclass(frozen=True)
class CostModel:
    """왕복 거래비용.

    슬리피지 기본값은 0이다. 어떤 값을 넣어도 근거 없는 추정치가 되므로, 넣을지 말지는
    쓰는 사람이 정하게 두고 기본은 '넣지 않았음'을 명확히 한다.
    """

    commission_rate: float = DEFAULT_COMMISSION_RATE  # 매수/매도 각각
    slippage_rate: float = 0.0  # 진입·청산 각각 불리한 방향으로
    stt_rates: dict[str, float] = field(default_factory=lambda: dict(STT_RATES))

    @classmethod
    def free(cls) -> CostModel:
        """비용 0. 총수익(gross)을 볼 때 쓴다."""
        return cls(commission_rate=0.0, slippage_rate=0.0, stt_rates={})

    @property
    def enabled(self) -> bool:
        return bool(self.commission_rate or self.slippage_rate or self.stt_rates)

    def tax_rate(self, corp_cls: str) -> float:
        if not self.stt_rates:
            return 0.0
        return self.stt_rates.get((corp_cls or "").upper(), DEFAULT_STT_RATE)

    def net_return_pct(
        self, action: str, entry_price: float, exit_price: float, corp_cls: str
    ) -> float:
        """비용을 뺀 수익률(%).

        매수 진입은 체결가가 불리하게 밀리고 수수료가 붙는다. 매도 청산은 수수료에 더해
        증권거래세까지 붙는다. 공매도는 방향이 반대일 뿐 같은 구조로 계산한다.
        """
        c = self.commission_rate
        s = self.slippage_rate
        t = self.tax_rate(corp_cls)

        if action == "buy":
            # 살 때 더 비싸게, 팔 때 더 싸게 체결된다고 본다.
            paid = entry_price * (1 + s) * (1 + c)
            received = exit_price * (1 - s) * (1 - c - t)
            return (received - paid) / paid * 100

        # 공매도: 먼저 팔고(세금·수수료) 나중에 되산다(수수료).
        received = entry_price * (1 - s) * (1 - c - t)
        paid = exit_price * (1 + s) * (1 + c)
        return (received - paid) / received * 100

    def round_trip_pct(self, corp_cls: str = "Y") -> float:
        """왕복 비용의 대략적 크기(%). 요약 출력용."""
        return (2 * self.commission_rate + 2 * self.slippage_rate + self.tax_rate(corp_cls)) * 100

    def describe(self) -> str:
        if not self.enabled:
            return "비용 미반영 (총수익)"
        parts = [f"수수료 {self.commission_rate * 100:.3f}% ×2"]
        if self.stt_rates:
            parts.append(f"거래세 {DEFAULT_STT_RATE * 100:.2f}% (매도시)")
        if self.slippage_rate:
            parts.append(f"슬리피지 {self.slippage_rate * 100:.3f}% ×2")
        else:
            parts.append("슬리피지 미반영")
        return " · ".join(parts) + f" → 왕복 약 {self.round_trip_pct():.3f}%"
