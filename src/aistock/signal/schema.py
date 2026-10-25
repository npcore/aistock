"""매매 신호 스키마.

Claude가 이 구조로만 응답하도록 강제한다(structured outputs). 자유 텍스트를 파싱하면
장중에 형식이 어긋나는 순간 신호가 통째로 날아가므로, 스키마 검증을 API 레벨에 맡긴다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Action(str, Enum):
    """신호가 제안하는 행동. 실제 주문은 사람이 검토한 뒤에만 나간다."""

    BUY = "buy"
    SELL = "sell"
    WATCH = "watch"  # 관심 등록, 추가 정보 대기
    IGNORE = "ignore"  # 주가와 무관


class Impact(str, Enum):
    STRONG_POSITIVE = "strong_positive"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    STRONG_NEGATIVE = "strong_negative"


class Horizon(str, Enum):
    INTRADAY = "intraday"  # 당일 반응
    DAYS = "days"  # 수일
    WEEKS = "weeks"  # 수주 이상
    UNKNOWN = "unknown"


class Triage(BaseModel):
    """1단계 — 보고서명만 보고 하는 분류. 원문을 기다리지 않는다."""

    worth_reading: bool = Field(description="원문까지 분석할 가치가 있는 공시인가")
    event_type: str = Field(description="사건 유형 (예: 유상증자결정, 전환사채발행, 자기주식취득)")
    expected_impact: Impact = Field(description="보고서명만으로 추정한 주가 영향")
    reason: str = Field(description="한 문장 근거")


class DisclosureSignal(BaseModel):
    """2단계 — 공시 원문까지 읽고 내는 신호."""

    action: Action
    confidence: int = Field(ge=0, le=100, description="0~100. 근거가 약하면 낮게.")
    impact: Impact
    horizon: Horizon
    event_type: str = Field(description="사건 유형")
    rationale: str = Field(description="판단 근거. 한국어 2~3문장.")
    key_facts: list[str] = Field(
        default_factory=list,
        description="원문에서 뽑은 핵심 수치. 예: '발행금액 500억원', '기존 주식수 대비 12.3%'",
    )
    risks: list[str] = Field(default_factory=list, description="이 판단이 틀릴 수 있는 이유")

    @property
    def actionable(self) -> bool:
        """주문 후보로 볼 만한 신호인가. 임계값은 백테스팅으로 정할 것."""
        return self.action in (Action.BUY, Action.SELL) and self.confidence >= 70
