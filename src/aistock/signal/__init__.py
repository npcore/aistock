"""LLM 기반 매매 신호 생성.

DART 공시를 Claude로 분석해 구조화된 신호로 바꾼다. 신호는 기록만 되며,
주문으로 이어지는 경로는 아직 없다 — 백테스팅으로 검증하기 전까지 그대로 둘 것.
"""

from .analyzer import SignalAnalyzer, SignalError
from .handler import SignalHandler, console_signal
from .schema import Action, DisclosureSignal, Horizon, Impact, Triage

__all__ = [
    "Action",
    "DisclosureSignal",
    "Horizon",
    "Impact",
    "SignalAnalyzer",
    "SignalError",
    "SignalHandler",
    "Triage",
    "console_signal",
]
