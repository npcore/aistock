"""폴러 상태 영속화.

프로세스가 죽었다 살아나도 같은 공시를 다시 내보내거나, 원문 대기 중이던 건을
잃어버리면 안 된다. 두 가지를 디스크에 남긴다.

    seen     — 이미 HEADLINE을 낸 접수번호
    pending  — 원문을 아직 못 받아 재시도해야 할 건
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR

logger = logging.getLogger(__name__)

STATE_PATH = CACHE_DIR / "dart_poller_state.json"

# seen이 무한히 자라지 않도록 이 기간이 지난 접수번호는 버린다.
# 접수번호 앞 8자리가 날짜라 날짜만으로 정리할 수 있다.
SEEN_RETENTION = timedelta(days=7)

# 원문이 끝내 안 올라오는 건도 있다. 이만큼 시도하면 포기한다.
MAX_DOCUMENT_ATTEMPTS = 40


@dataclass
class PendingDocument:
    """원문 대기 중인 공시."""

    rcept_no: str
    row: dict[str, Any]  # Disclosure 복원용 원본 행
    first_seen: str
    attempts: int = 0
    last_attempt: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_DOCUMENT_ATTEMPTS


@dataclass
class PollerState:
    seen: set[str] = field(default_factory=set)
    pending: dict[str, PendingDocument] = field(default_factory=dict)
    last_poll: str | None = None
    # 어디에 저장할지는 상태 자신이 안다. 폴러가 매번 경로를 넘기지 않아도 되고,
    # 테스트가 실제 캐시를 덮어쓰는 사고도 막는다.
    path: Path = STATE_PATH

    # --- 영속화 ---

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> PollerState:
        if not path.exists():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                seen=set(payload.get("seen", [])),
                pending={k: PendingDocument(**v) for k, v in payload.get("pending", {}).items()},
                last_poll=payload.get("last_poll"),
                path=path,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("폴러 상태를 읽을 수 없어 새로 시작합니다: %s", exc)
            return cls(path=path)

    def save(self) -> None:
        payload = {
            "seen": sorted(self.seen),
            "pending": {k: asdict(v) for k, v in self.pending.items()},
            "last_poll": self.last_poll,
        }
        # 중간에 죽어도 기존 상태가 깨지지 않도록 임시파일에 쓰고 바꿔치기한다.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # --- 조작 ---

    def is_new(self, rcept_no: str) -> bool:
        return rcept_no not in self.seen

    def mark_seen(self, rcept_no: str) -> None:
        self.seen.add(rcept_no)

    def add_pending(self, rcept_no: str, row: dict[str, Any]) -> None:
        if rcept_no in self.pending:
            return
        self.pending[rcept_no] = PendingDocument(
            rcept_no=rcept_no, row=row, first_seen=datetime.now(UTC).isoformat()
        )

    def record_attempt(self, rcept_no: str) -> PendingDocument | None:
        item = self.pending.get(rcept_no)
        if item is None:
            return None
        item.attempts += 1
        item.last_attempt = datetime.now(UTC).isoformat()
        return item

    def resolve_pending(self, rcept_no: str) -> None:
        self.pending.pop(rcept_no, None)

    def prune(self, retention: timedelta = SEEN_RETENTION) -> int:
        """오래된 seen 항목과 포기한 pending을 정리하고 버린 개수를 돌려준다."""
        cutoff = (datetime.now(UTC) - retention).strftime("%Y%m%d")
        before = len(self.seen) + len(self.pending)
        self.seen = {r for r in self.seen if r[:8] >= cutoff}
        for rcept_no, item in list(self.pending.items()):
            if item.exhausted or rcept_no[:8] < cutoff:
                logger.info("원문 확보 포기: %s (%d회 시도)", rcept_no, item.attempts)
                del self.pending[rcept_no]
        return before - len(self.seen) - len(self.pending)
