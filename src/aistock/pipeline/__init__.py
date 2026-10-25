"""공시 수집 파이프라인.

DART 공시를 상시 감지해 이벤트로 흘려보낸다. 다음 단계(LLM 신호 생성)는
`handlers`에 핸들러를 하나 더 붙이는 형태로 연결한다.
"""

from .events import DisclosureEvent, Stage
from .handlers import DocumentArchiver, JsonlWriter, console
from .poller import DisclosurePoller
from .state import PollerState

__all__ = [
    "DisclosureEvent",
    "DisclosurePoller",
    "DocumentArchiver",
    "JsonlWriter",
    "PollerState",
    "Stage",
    "console",
]
