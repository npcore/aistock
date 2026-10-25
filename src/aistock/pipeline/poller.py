"""DART 공시 폴링 루프.

한 사이클에서 두 가지 일을 한다.

1. **신규 감지** — 오늘 공시 목록을 훑어 처음 보는 접수번호마다 HEADLINE 이벤트를 낸다.
2. **원문 회수** — 원문이 아직 없어 대기 중인 건들을 다시 시도해서, 확보되면 DOCUMENT 이벤트를 낸다.

원문은 목록보다 늦게 올라오므로 두 일을 한 사이클에 붙여야 지연 없이 신호를 낼 수 있다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..dart.client import DartClient
from ..dart.disclosure import (
    MARKET_MOVING_TYPES,
    Disclosure,
    search,
    try_document_text,
)
from ..dart.errors import DartRateLimitError
from .events import DisclosureEvent, Stage
from .state import PollerState

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

Handler = Callable[[DisclosureEvent], None]

DEFAULT_INTERVAL = 30.0
# DART 일 한도. 이걸 넘길 설정이면 시작 전에 경고한다.
DAILY_QUOTA = 20_000


class DisclosurePoller:
    """공시를 감지해 이벤트로 흘려보낸다.

    >>> poller = DisclosurePoller(client, handlers=[print_event])
    >>> poller.run(max_cycles=1)
    """

    def __init__(
        self,
        client: DartClient,
        *,
        handlers: Sequence[Handler] = (),
        types: Sequence[str] | None = MARKET_MOVING_TYPES,
        stock_codes: Iterable[str] | None = None,
        interval: float = DEFAULT_INTERVAL,
        state: PollerState | None = None,
        max_pages: int = 3,
    ) -> None:
        self.client = client
        self.handlers = list(handlers)
        # types=None이면 유형 구분 없이 한 번에 조회한다. 요청수는 1/4로 줄지만
        # 목록 응답에 유형 필드가 없어 이벤트의 pblntf_ty를 채울 수 없다.
        self.types = tuple(types) if types else (None,)
        self.stock_codes = set(stock_codes) if stock_codes else None
        self.interval = interval
        self.state = state if state is not None else PollerState.load()
        self.max_pages = max_pages
        self._running = False

    # --- 한 사이클 ---

    def poll_once(self) -> list[DisclosureEvent]:
        """신규 감지 + 원문 회수를 한 번 수행하고 발생한 이벤트를 돌려준다."""
        events = self._detect_new()
        events.extend(self._retry_pending())
        self.state.last_poll = datetime.now(KST).isoformat()
        self.state.save()
        return events

    def _detect_new(self) -> list[DisclosureEvent]:
        today = datetime.now(KST).strftime("%Y%m%d")
        now = datetime.now(KST)
        events: list[DisclosureEvent] = []

        for ty in self.types:
            try:
                items = search(
                    self.client,
                    bgn_de=today,
                    end_de=today,
                    pblntf_ty=ty,
                    max_pages=self.max_pages,
                )
            except DartRateLimitError:
                logger.error("DART 요청 한도 초과 — 이번 사이클을 건너뜁니다.")
                raise

            for item in items:
                if not self._wanted(item) or not self.state.is_new(item.rcept_no):
                    continue
                self.state.mark_seen(item.rcept_no)
                self.state.add_pending(item.rcept_no, _to_row(item, ty))
                events.append(
                    DisclosureEvent(
                        stage=Stage.HEADLINE,
                        disclosure=item,
                        detected_at=now,
                        pblntf_ty=ty,
                    )
                )

        self._emit(events)
        return events

    def _retry_pending(self) -> list[DisclosureEvent]:
        events: list[DisclosureEvent] = []
        now = datetime.now(KST)

        for rcept_no in list(self.state.pending):
            item = self.state.record_attempt(rcept_no)
            if item is None:
                continue
            text = try_document_text(self.client, rcept_no)
            if text is None:
                continue  # 아직 안 올라옴 — 다음 사이클에 다시

            row = dict(item.row)
            ty = row.pop("_pblntf_ty", None)
            events.append(
                DisclosureEvent(
                    stage=Stage.DOCUMENT,
                    disclosure=Disclosure.from_row(row),
                    detected_at=now,
                    text=text,
                    pblntf_ty=ty,
                )
            )
            self.state.resolve_pending(rcept_no)

        self.state.prune()
        self._emit(events)
        return events

    @property
    def requests_made(self) -> int:
        """실제 DART 요청수. 일 20,000건 한도 대비 소진량을 볼 때 쓴다."""
        return self.client.requests_made

    def _wanted(self, item: Disclosure) -> bool:
        if self.stock_codes is None:
            return True
        return item.stock_code in self.stock_codes

    def _emit(self, events: Sequence[DisclosureEvent]) -> None:
        for event in events:
            for handler in self.handlers:
                try:
                    handler(event)
                except Exception:
                    # 핸들러 하나가 터져도 루프는 계속 돌아야 한다.
                    logger.exception("핸들러 오류 (%s %s)", event.stage.value, event.rcept_no)

    # --- 루프 ---

    def bootstrap(self) -> int:
        """지금 떠 있는 공시를 이벤트 없이 seen 처리한다.

        첫 실행에서 당일 공시 수백 건이 한꺼번에 신호로 쏟아지는 걸 막는다.
        """
        today = datetime.now(KST).strftime("%Y%m%d")
        count = 0
        for ty in self.types:
            items = search(
                self.client, bgn_de=today, end_de=today, pblntf_ty=ty, max_pages=self.max_pages
            )
            for item in items:
                if self.state.is_new(item.rcept_no):
                    self.state.mark_seen(item.rcept_no)
                    count += 1
        self.state.save()
        logger.info("부트스트랩: 기존 공시 %d건을 이벤트 없이 처리했습니다.", count)
        return count

    def run(self, *, max_cycles: int | None = None) -> None:
        """중단될 때까지(또는 max_cycles만큼) 폴링한다."""
        self._running = True
        self._warn_if_over_quota()
        cycle = 0
        try:
            while self._running:
                cycle += 1
                started = time.monotonic()
                try:
                    self.poll_once()
                except DartRateLimitError:
                    logger.error("한도 초과로 %d초 쉽니다.", self.interval * 10)
                    time.sleep(self.interval * 10)
                    continue

                if max_cycles is not None and cycle >= max_cycles:
                    return
                # 조회에 걸린 시간을 빼서 주기가 밀리지 않게 한다.
                time.sleep(max(0.0, self.interval - (time.monotonic() - started)))
        finally:
            self._running = False
            self.state.save()

    def stop(self) -> None:
        self._running = False

    def _warn_if_over_quota(self) -> None:
        """장 시간(6.5h) 기준 예상 요청수가 일 한도를 넘는지 알린다."""
        per_cycle = len(self.types)
        cycles = (6.5 * 3600) / self.interval
        estimate = int(per_cycle * cycles)
        if estimate > DAILY_QUOTA * 0.5:
            logger.warning(
                "예상 요청수 약 %d건/장중 (한도 %d). 원문 재시도까지 더해지므로 "
                "--interval을 늘리거나 --types를 줄이세요.",
                estimate,
                DAILY_QUOTA,
            )


def _to_row(item: Disclosure, pblntf_ty: str | None) -> dict[str, Any]:
    """pending에 저장할 형태. Disclosure.from_row로 되살릴 수 있어야 한다."""
    return {
        "rcept_no": item.rcept_no,
        "corp_code": item.corp_code,
        "corp_name": item.corp_name,
        "stock_code": item.stock_code,
        "corp_cls": item.corp_cls,
        "report_nm": item.report_nm,
        "flr_nm": item.flr_nm,
        "rcept_dt": item.rcept_dt,
        "rm": item.rm,
        "_pblntf_ty": pblntf_ty,
    }
