"""관심종목 목록.

`watchlist.txt`에 한 줄에 종목코드 하나. `#` 뒤는 주석이라 종목명을 적어둘 수 있다.

    005930  # 삼성전자
    035720  # 카카오

파일 하나를 모든 명령이 공유하므로 매번 종목코드를 다시 칠 필요가 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT
from .dart.corp_code import normalize_stock_code

logger = logging.getLogger(__name__)

WATCHLIST_PATH = PROJECT_ROOT / "watchlist.txt"


@dataclass(frozen=True)
class Entry:
    code: str
    note: str = ""

    def line(self) -> str:
        return f"{self.code}  # {self.note}" if self.note else self.code


class Watchlist:
    """관심종목 파일. 순서를 보존한다 — 사용자가 적은 순서가 화면 순서다."""

    def __init__(self, entries: list[Entry], path: Path = WATCHLIST_PATH) -> None:
        self.entries = entries
        self.path = path

    @classmethod
    def load(cls, path: Path = WATCHLIST_PATH) -> Watchlist:
        if not path.exists():
            return cls([], path)
        entries: list[Entry] = []
        seen: set[str] = set()
        for raw in path.read_text(encoding="utf-8").splitlines():
            code_part, _, note = raw.partition("#")
            code = normalize_stock_code(code_part.strip())
            if not code or code in seen:
                continue
            if not code.isdigit() or len(code) != 6:
                logger.warning("종목코드 형식이 아니라 건너뜁니다: %r", raw.strip())
                continue
            seen.add(code)
            entries.append(Entry(code, note.strip()))
        return cls(entries, path)

    def save(self) -> None:
        self.path.write_text(
            "\n".join(e.line() for e in self.entries) + ("\n" if self.entries else ""),
            encoding="utf-8",
        )

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.entries]

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, code: str) -> bool:
        return normalize_stock_code(code) in set(self.codes)

    def add(self, code: str, note: str = "") -> bool:
        """새로 추가했으면 True, 이미 있으면 False."""
        code = normalize_stock_code(code)
        if code in self:
            return False
        self.entries.append(Entry(code, note))
        return True

    def remove(self, code: str) -> bool:
        code = normalize_stock_code(code)
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.code != code]
        return len(self.entries) < before

    def note_for(self, code: str) -> str:
        code = normalize_stock_code(code)
        return next((e.note for e in self.entries if e.code == code), "")
