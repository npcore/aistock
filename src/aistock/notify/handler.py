"""텔레그램 전송 핸들러.

폴러·신호 생성기에 붙여 쓴다. 전송 실패가 수집 루프를 멈추면 안 되므로 여기서 삼킨다 —
알림을 놓치는 것보다 수집이 멈추는 쪽이 나쁘다.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client import KiwoomClient
from ..errors import KiwoomError
from ..market import domestic
from ..pipeline.events import DisclosureEvent, Stage
from ..signal.schema import Action, DisclosureSignal
from ..watchlist import Watchlist
from .format import headline_message, signal_message
from .telegram import TelegramError, TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramSignalHandler:
    """신호를 텔레그램으로 보낸다.

    `min_confidence`와 `actions`로 알림 양을 조절한다. 하루 주요 공시가 수백 건이라
    필터 없이 켜면 알림이 쏟아져 정작 중요한 걸 놓친다.
    """

    def __init__(
        self,
        notifier: TelegramNotifier,
        *,
        kiwoom: KiwoomClient | None = None,
        watchlist: Watchlist | None = None,
        min_confidence: int = 0,
        actions: tuple[Action, ...] = (Action.BUY, Action.SELL),
        send_headlines: bool = False,
    ) -> None:
        self.notifier = notifier
        self.kiwoom = kiwoom
        self.watchlist = watchlist
        self.min_confidence = min_confidence
        self.actions = tuple(actions)
        self.send_headlines = send_headlines
        self.stats = {"sent": 0, "filtered": 0, "failed": 0}

    # --- 신호 콜백 (SignalHandler.callbacks에 연결) ---

    def __call__(self, event: DisclosureEvent, signal: DisclosureSignal) -> None:
        if signal.action not in self.actions or signal.confidence < self.min_confidence:
            self.stats["filtered"] += 1
            return

        note = self.watchlist.note_for(event.disclosure.stock_code) if self.watchlist else ""
        text = signal_message(
            event, signal, quote=self._quote(event.disclosure.stock_code), note=note
        )
        self._deliver(text, event.rcept_no)

    # --- 공시 속보 (DisclosurePoller.handlers에 연결) ---

    def on_disclosure(self, event: DisclosureEvent) -> None:
        """HEADLINE 단계에서 바로 보내는 속보. 원문 분석은 몇 분 뒤에 온다."""
        if not self.send_headlines or event.stage is not Stage.HEADLINE:
            return
        note = self.watchlist.note_for(event.disclosure.stock_code) if self.watchlist else ""
        self._deliver(headline_message(event, note), event.rcept_no)

    # --- 내부 ---

    def _quote(self, stock_code: str) -> dict[str, Any] | None:
        """현재가를 붙인다. 실패해도 알림 자체는 나가야 하므로 조용히 건너뛴다."""
        if self.kiwoom is None or not stock_code:
            return None
        try:
            return domestic.stock_info(self.kiwoom, stock_code)
        except KiwoomError as exc:
            logger.debug("현재가 조회 실패 (%s): %s", stock_code, exc)
            return None

    def _deliver(self, text: str, rcept_no: str) -> None:
        try:
            self.notifier.send(text)
            self.stats["sent"] += 1
        except TelegramError as exc:
            self.stats["failed"] += 1
            logger.error("텔레그램 전송 실패 (%s): %s", rcept_no, exc)
