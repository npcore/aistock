"""알림 채널.

텔레그램으로 신호를 보내 사람이 보고 매수·매도를 판단하게 한다.
주문은 여전히 사람이 직접 낸다 — 이 경로에 자동 주문은 없다.
"""

from .format import (
    account_message,
    headline_message,
    holdings_message,
    signal_message,
    ticker_message,
    watchlist_digest,
)
from .handler import TelegramSignalHandler
from .telegram import TelegramConfig, TelegramError, TelegramNotifier

__all__ = [
    "account_message",
    "TelegramConfig",
    "TelegramError",
    "TelegramNotifier",
    "TelegramSignalHandler",
    "headline_message",
    "holdings_message",
    "signal_message",
    "ticker_message",
    "watchlist_digest",
]
