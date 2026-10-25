"""이벤트 핸들러.

폴러는 이벤트를 만들기만 하고, 무엇을 할지는 핸들러가 정한다.
LLM 신호 생성은 다음 단계에서 여기에 핸들러 하나를 더 붙이면 된다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import LOG_DIR
from .events import DisclosureEvent, Stage

logger = logging.getLogger(__name__)

JSONL_PATH = LOG_DIR / "disclosures.jsonl"
TEXT_DIR = LOG_DIR / "documents"


def console(event: DisclosureEvent) -> None:
    """사람이 보라고 한 줄 찍는다."""
    at = event.detected_at.strftime("%H:%M:%S")
    if event.stage is Stage.HEADLINE:
        print(f"{at}  [신규] {event.headline}")
    else:
        print(f"{at}  [원문] {event.headline}  ({len(event.text or ''):,}자)")


class JsonlWriter:
    """이벤트를 JSONL로 append 한다. 나중에 백테스팅·재분석에 쓴다."""

    def __init__(self, path: Path = JSONL_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: DisclosureEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")


class DocumentArchiver:
    """원문 전문을 파일로 남긴다. JSONL에는 길이만 들어가므로 본문은 여기에 둔다."""

    def __init__(self, directory: Path = TEXT_DIR) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: DisclosureEvent) -> None:
        if event.stage is not Stage.DOCUMENT or not event.text:
            return
        (self.directory / f"{event.rcept_no}.txt").write_text(event.text, encoding="utf-8")
