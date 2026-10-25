"""폴러에 붙이는 신호 생성 핸들러.

`DisclosurePoller(handlers=[...])`에 그대로 넣으면 공시가 감지될 때마다 분석이 돈다.
핸들러에서 예외가 나도 폴러는 계속 돈다(폴러가 격리한다).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from ..config import LOG_DIR
from ..pipeline.events import DisclosureEvent, Stage
from .analyzer import SignalAnalyzer, SignalError
from .schema import DisclosureSignal, Triage

logger = logging.getLogger(__name__)

SIGNALS_PATH = LOG_DIR / "signals.jsonl"

SignalCallback = Callable[[DisclosureEvent, DisclosureSignal], None]


class SignalHandler:
    """공시 이벤트 → Claude 분석 → 신호.

    HEADLINE 단계에서 걸러낸 공시는 DOCUMENT 단계에서 분석하지 않는다.
    비싼 건 원문 분석 쪽이라, 여기서 거르는 만큼이 그대로 비용 절감이다.
    """

    def __init__(
        self,
        analyzer: SignalAnalyzer | None = None,
        *,
        callbacks: Sequence[SignalCallback] = (),
        path: Path = SIGNALS_PATH,
        triage_enabled: bool = True,
    ) -> None:
        self.analyzer = analyzer or SignalAnalyzer()
        self.callbacks = list(callbacks)
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.triage_enabled = triage_enabled
        self._triaged: dict[str, Triage] = {}
        self.stats = {"triaged": 0, "skipped": 0, "analyzed": 0, "failed": 0}

    def __call__(self, event: DisclosureEvent) -> None:
        try:
            if event.stage is Stage.HEADLINE:
                self._on_headline(event)
            else:
                self._on_document(event)
        except SignalError as exc:
            self.stats["failed"] += 1
            logger.error("신호 생성 실패 (%s): %s", event.rcept_no, exc)

    # --- 단계별 ---

    def _on_headline(self, event: DisclosureEvent) -> None:
        if not self.triage_enabled:
            return
        triage = self.analyzer.triage(event.disclosure)
        self._triaged[event.rcept_no] = triage
        self.stats["triaged"] += 1
        if not triage.worth_reading:
            self.stats["skipped"] += 1
            logger.info(
                "원문 분석 생략: %s %s — %s",
                event.disclosure.corp_name,
                triage.event_type,
                triage.reason,
            )

    def _on_document(self, event: DisclosureEvent) -> None:
        triage = self._triaged.pop(event.rcept_no, None)
        # triage 기록이 없으면(재시작 등) 거르지 않고 분석한다.
        # 비용을 조금 더 쓰는 쪽이, 신호를 놓치는 쪽보다 낫다.
        if triage is not None and not triage.worth_reading:
            return
        if not event.text:
            return

        signal = self.analyzer.analyze(event.disclosure, event.text)
        self.stats["analyzed"] += 1
        self._record(event, signal)
        for callback in self.callbacks:
            callback(event, signal)

    # --- 출력 ---

    def _record(self, event: DisclosureEvent, signal: DisclosureSignal) -> None:
        row = {
            "detected_at": event.detected_at.isoformat(),
            "analyzed_at": datetime.now(event.detected_at.tzinfo).isoformat(),
            "rcept_no": event.rcept_no,
            "corp_name": event.disclosure.corp_name,
            "stock_code": event.disclosure.stock_code,
            # 시장별로 증권거래세율이 다르므로 백테스팅에서 쓴다.
            "corp_cls": event.disclosure.corp_cls,
            "market": event.disclosure.market,
            "rcept_dt": event.disclosure.rcept_dt,
            "report_nm": event.disclosure.report_nm,
            "url": event.disclosure.url,
            **signal.model_dump(mode="json"),
            "actionable": signal.actionable,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def console_signal(event: DisclosureEvent, signal: DisclosureSignal) -> None:
    """사람이 보라고 신호를 찍는다."""
    mark = "★" if signal.actionable else " "
    who = f"{event.disclosure.corp_name}({event.disclosure.stock_code})"
    print(
        f"  {mark} [{signal.action.value.upper():<6}] {signal.confidence:>3}%  {who}  {signal.event_type}"
    )
    print(f"      {signal.rationale}")
    for fact in signal.key_facts[:3]:
        print(f"      · {fact}")
