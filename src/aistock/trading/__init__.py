"""반자동 매매 — 공시 신호 + 기술적 필터 → 텔레그램 승인 → 주문.

자동으로 주문이 나가는 경로는 없다. 사람이 승인 버튼을 눌러야 `executor.approve()`가
주문을 낸다.
"""

from .policy import TradingPolicy
from .proposals import Proposal, ProposalStore
from .proposer import TradeProposer

__all__ = ["Proposal", "ProposalStore", "TradeProposer", "TradingPolicy"]
