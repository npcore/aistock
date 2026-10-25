"""주문 제안 저장소.

제안은 공시 감시 프로세스(`dart watch`)가 만들고, 승인은 봇 프로세스(`bot`)가 처리한다.
두 프로세스가 같은 파일을 쓰므로 **파일 잠금 아래에서 읽고-고치고-쓴다.**

상태 전이가 곧 중복 주문 방지 장치다. 승인 버튼을 두 번 누르거나 텔레그램이 같은 콜백을
다시 보내도, `pending → executing` 전이는 한 번만 성공한다.

    pending ─승인→ executing ─→ ordered | failed | stale
       │
       ├─거절→ rejected
       └─시간초과→ expired
    blocked  (기술적 필터·한도에 걸려 버튼 없이 기록만 된 제안)

    deferred ─다음 장 시작 후 재평가→ pending | blocked | expired
             (장 마감 후 신호. 그날 가격으로 바로 제안하지 않고 다음 장에서 다시 본다)
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR
from ..market_hours import KST

PROPOSALS_PATH = CACHE_DIR / "proposals.json"

PENDING = "pending"
EXECUTING = "executing"
ORDERED = "ordered"
FAILED = "failed"
STALE = "stale"
REJECTED = "rejected"
EXPIRED = "expired"
BLOCKED = "blocked"
DEFERRED = "deferred"
QUEUED = "queued"  # 승인했지만 키움이 장종료로 거부 → 봇이 보관했다가 주문을 받기 시작하면 넣는다

CANCELLED = "cancelled"  # 주문이 나갔다가 /cancel로 취소됨

OPEN_STATES = (PENDING, EXECUTING, DEFERRED, QUEUED)

# 이보다 오래된 종결 제안은 파일에서 지운다. 파일이 끝없이 커지지 않게.
KEEP_DAYS = 30


@dataclass(frozen=True)
class Proposal:
    id: str
    created_at: str  # ISO, KST
    side: str  # buy | sell
    stock_code: str
    name: str
    quantity: int
    limit_price: int
    confidence: int
    reason: str  # 사람이 읽는 근거 (공시 사건 + 신호 요약)
    rcept_no: str = ""
    technical: str = ""  # 기술적 지표 요약
    blocks: list[str] = field(default_factory=list)
    status: str = PENDING
    message_id: int = 0
    order_no: str = ""
    decided_at: str = ""
    note: str = ""  # 실패·보류 사유
    source: str = "disclosure"  # disclosure | stop_loss | take_profit | trend | manual
    signal_at: str = ""  # 신호가 처음 나온 시각 (지연 제안은 created_at과 다르다)
    ref_price: int = 0  # 신호 시점 가격. 지연 제안의 갭 판단 기준
    market: str = "kr"  # kr | us
    exchange: str = ""  # 미국만: ND/NY/NA
    price_usd: float = 0.0  # 미국만: 지정가(USD). 국내는 limit_price(원)를 쓴다
    queued_at: str = ""  # 예약한 시각 (예약 만료 계산)

    @property
    def is_us(self) -> bool:
        return self.market == "us"

    @property
    def amount_usd(self) -> float:
        return round(self.quantity * self.price_usd, 2)

    def price_text(self) -> str:
        if self.is_us:
            return f"{self.quantity:,}주 × ${self.price_usd:,.2f} = ${self.amount_usd:,.2f}"
        return f"{self.quantity:,}주 × {self.limit_price:,}원 = {self.amount:,}원"

    @property
    def amount(self) -> int:
        return self.quantity * self.limit_price

    @property
    def created(self) -> datetime:
        return datetime.fromisoformat(self.created_at)

    def expired(self, ttl_min: int, now: datetime | None = None) -> bool:
        return (now or datetime.now(KST)) - self.created > timedelta(minutes=ttl_min)


def new_id() -> str:
    return secrets.token_hex(4)


class ProposalStore:
    def __init__(self, path: Path = PROPOSALS_PATH) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    # --- 조회 ---

    def get(self, proposal_id: str) -> Proposal | None:
        with self._locked() as items:
            return items.get(proposal_id)

    def all(self) -> list[Proposal]:
        with self._locked() as items:
            return sorted(items.values(), key=lambda p: p.created_at)

    def today(self, now: datetime | None = None) -> list[Proposal]:
        day = (now or datetime.now(KST)).date()
        return [p for p in self.all() if p.created.date() == day]

    def open_for(self, stock_code: str, side: str) -> Proposal | None:
        """같은 종목·방향으로 아직 결정 안 된 제안. 공시 정정·첨부추가로 중복 제안되는 걸 막는다."""
        for p in self.all():
            if p.stock_code == stock_code and p.side == side and p.status in OPEN_STATES:
                return p
        return None

    # --- 변경 ---

    def add(self, proposal: Proposal) -> None:
        with self._locked(write=True) as items:
            items[proposal.id] = proposal

    def update(self, proposal_id: str, **changes: Any) -> Proposal | None:
        with self._locked(write=True) as items:
            current = items.get(proposal_id)
            if current is None:
                return None
            items[proposal_id] = replace(current, **changes)
            return items[proposal_id]

    def mark_cancelled(self, order_no: str) -> Proposal | None:
        """키움에서 취소한 주문의 제안 기록을 맞춘다. 안 맞추면 봇은 아직 매수 중이라고 본다."""
        with self._locked(write=True) as items:
            for pid, p in items.items():
                if order_no and p.order_no == order_no and p.status == ORDERED:
                    items[pid] = replace(p, status=CANCELLED, note=f"주문 취소 ({order_no})")
                    return items[pid]
        return None

    def transition(
        self, proposal_id: str, expected: str, new: str, **changes: Any
    ) -> Proposal | None:
        """현재 상태가 expected일 때만 new로 바꾼다. 실패하면 None — 누군가 먼저 처리했다."""
        with self._locked(write=True) as items:
            current = items.get(proposal_id)
            if current is None or current.status != expected:
                return None
            items[proposal_id] = replace(current, status=new, **changes)
            return items[proposal_id]

    # --- 내부 ---

    @contextmanager
    def _locked(self, *, write: bool = False) -> Iterator[dict[str, Proposal]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                items = self._read()
                yield items
                if write:
                    self._write(items)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Proposal]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {r["id"]: Proposal(**r) for r in rows}

    def _write(self, items: dict[str, Proposal]) -> None:
        cutoff = datetime.now(KST) - timedelta(days=KEEP_DAYS)
        keep = [
            asdict(p)
            for p in sorted(items.values(), key=lambda p: p.created_at)
            if p.status in OPEN_STATES or p.created >= cutoff
        ]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
