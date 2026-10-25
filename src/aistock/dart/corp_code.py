"""DART 고유번호(corp_code) 인덱스.

DART는 자체 8자리 `corp_code`로 회사를 식별하고, 키움은 6자리 종목코드를 쓴다.
둘을 잇는 매핑이 `corpCode.xml`(zip, 약 10만 건)에 들어 있다. 이 파일이 두 시스템을
연결하는 접착제이므로 받아서 캐싱해두고 쓴다.

전체 10만 건 중 상장사는 4천 건 남짓이다. 매매에 쓰는 건 상장사뿐이라
파싱 결과를 압축 인덱스로 저장해두고, 비상장 포함 검색이 필요할 때만 원본을 뒤진다.
"""

from __future__ import annotations

import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import CACHE_DIR
from .client import DartClient

logger = logging.getLogger(__name__)

RAW_PATH = CACHE_DIR / "dart_corpcode.xml"
INDEX_PATH = CACHE_DIR / "dart_corpcode_index.json"
DEFAULT_MAX_AGE = timedelta(days=7)


@dataclass(frozen=True)
class Corp:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str

    @property
    def is_listed(self) -> bool:
        return bool(self.stock_code)


class CorpCodeIndex:
    """종목코드 ↔ corp_code 조회.

    >>> index = CorpCodeIndex.load()
    >>> index.by_stock_code("005930").corp_code
    '00126380'
    """

    def __init__(self, corps: list[Corp], built_at: str) -> None:
        self.corps = corps
        self.built_at = built_at
        self._by_stock = {c.stock_code: c for c in corps if c.stock_code}
        self._by_code = {c.corp_code: c for c in corps}

    # --- 생성 ---

    @classmethod
    def load(
        cls,
        *,
        client: DartClient | None = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
        force: bool = False,
    ) -> CorpCodeIndex:
        """캐시가 있으면 쓰고, 없거나 오래됐으면 DART에서 새로 받는다."""
        if not force and INDEX_PATH.exists():
            index = cls._from_cache()
            if index is not None and index.age < max_age:
                return index
            logger.info("corp_code 캐시가 오래되어 갱신합니다.")

        owns_client = client is None
        client = client or DartClient.create()
        try:
            return cls.refresh(client)
        finally:
            if owns_client:
                client.close()

    @classmethod
    def refresh(cls, client: DartClient) -> CorpCodeIndex:
        """DART에서 corpCode.zip을 내려받아 인덱스를 다시 만든다."""
        logger.info("DART 고유번호 파일을 내려받는 중…")
        blob = client.get_bytes("corpCode.xml")
        xml_bytes = _extract_single(blob)
        RAW_PATH.write_bytes(xml_bytes)

        corps = _parse(xml_bytes)
        index = cls(corps, datetime.now(UTC).isoformat())
        index._save()
        logger.info(
            "corp_code 인덱스 생성 완료: 전체 %d개 (상장 %d개)", len(corps), len(index._by_stock)
        )
        return index

    @classmethod
    def _from_cache(cls) -> CorpCodeIndex | None:
        try:
            payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            return cls([Corp(**row) for row in payload["corps"]], payload["built_at"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("corp_code 캐시를 읽을 수 없어 새로 받습니다: %s", exc)
            return None

    def _save(self) -> None:
        payload = {"built_at": self.built_at, "corps": [asdict(c) for c in self.corps]}
        INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # --- 조회 ---

    @property
    def age(self) -> timedelta:
        return datetime.now(UTC) - datetime.fromisoformat(self.built_at)

    @property
    def listed(self) -> list[Corp]:
        return list(self._by_stock.values())

    def by_stock_code(self, stock_code: str) -> Corp:
        """키움 종목코드(6자리)로 찾는다. `005930_AL` 같은 거래소 접미사는 떼고 본다."""
        key = normalize_stock_code(stock_code)
        try:
            return self._by_stock[key]
        except KeyError:
            raise KeyError(f"DART에 등록되지 않은 종목코드입니다: {stock_code}") from None

    def get(self, stock_code: str) -> Corp | None:
        return self._by_stock.get(normalize_stock_code(stock_code))

    def by_corp_code(self, corp_code: str) -> Corp:
        try:
            return self._by_code[corp_code]
        except KeyError:
            raise KeyError(f"알 수 없는 corp_code입니다: {corp_code}") from None

    def search(self, keyword: str, *, listed_only: bool = True) -> list[Corp]:
        """회사명 부분일치 검색."""
        pool = self.listed if listed_only else self.corps
        return [c for c in pool if keyword in c.corp_name]


def normalize_stock_code(stock_code: str) -> str:
    """키움의 거래소별 코드(`005930_NX`, `005930_AL`)를 순수 6자리로 정규화한다."""
    return stock_code.split("_", 1)[0].strip()


def _extract_single(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError("corpCode zip이 비어 있습니다.")
        return archive.read(names[0])


def _parse(xml_bytes: bytes) -> list[Corp]:
    root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    corps = []
    for node in root.iterfind("list"):
        corp_code = (node.findtext("corp_code") or "").strip()
        if not corp_code:
            continue
        corps.append(
            Corp(
                corp_code=corp_code,
                corp_name=(node.findtext("corp_name") or "").strip(),
                # 비상장사는 공백 문자열로 채워져 오므로 strip이 필수다.
                stock_code=(node.findtext("stock_code") or "").strip(),
                modify_date=(node.findtext("modify_date") or "").strip(),
            )
        )
    return corps


def cache_paths() -> list[Path]:
    return [RAW_PATH, INDEX_PATH]
