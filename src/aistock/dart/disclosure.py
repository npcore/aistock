"""공시 검색 · 기업개황 · 공시원문 조회."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import DartClient
from .corp_code import CorpCodeIndex
from .errors import DartError

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# pblntf_ty — 공시유형 대분류
PBLNTF_TY: dict[str, str] = {
    "A": "정기공시",
    "B": "주요사항보고",
    "C": "발행공시",
    "D": "지분공시",
    "E": "기타공시",
    "F": "외부감사관련",
    "G": "펀드공시",
    "H": "자산유동화",
    "I": "거래소공시",
    "J": "공정위공시",
}

# 주가에 즉각 반응이 오는 유형. 뉴스 기반 전략의 1차 필터로 쓴다.
MARKET_MOVING_TYPES = ("B", "C", "D", "I")

CORP_CLS: dict[str, str] = {"Y": "유가", "K": "코스닥", "N": "코넥스", "E": "기타"}

MAX_PAGE_COUNT = 100

# 원문이 아직 안 올라온 상태. 실패가 아니라 '나중에 다시 시도'로 다뤄야 한다.
# 014=파일 없음, 013=접수번호 오류(접수 직후 아직 색인 전인 건도 여기로 온다).
# 확인 결과 유형·제출주체와 무관하게 당일 최신 건에서만 발생하고, 전일 이전은 전부 제공된다.
DOCUMENT_PENDING_STATUSES = {"013", "014"}


@dataclass(frozen=True)
class Disclosure:
    """공시 한 건."""

    rcept_no: str  # 접수번호 — 원문 조회 키
    corp_code: str
    corp_name: str
    stock_code: str
    corp_cls: str
    report_nm: str  # 보고서명
    flr_nm: str  # 제출인
    rcept_dt: str  # 접수일자 YYYYMMDD
    rm: str  # 비고

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Disclosure:
        return cls(
            rcept_no=row.get("rcept_no", ""),
            corp_code=row.get("corp_code", ""),
            corp_name=row.get("corp_name", ""),
            stock_code=(row.get("stock_code") or "").strip(),
            corp_cls=row.get("corp_cls", ""),
            report_nm=row.get("report_nm", "").strip(),
            flr_nm=row.get("flr_nm", ""),
            rcept_dt=row.get("rcept_dt", ""),
            rm=row.get("rm", ""),
        )

    @property
    def market(self) -> str:
        return CORP_CLS.get(self.corp_cls, self.corp_cls)

    @property
    def url(self) -> str:
        """DART 웹 뷰어 링크."""
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={self.rcept_no}"

    @property
    def submitted_at(self) -> datetime | None:
        """접수번호 앞 8자리가 접수일자다. 일 단위까지만 알 수 있다."""
        try:
            return datetime.strptime(self.rcept_dt, "%Y%m%d").replace(tzinfo=KST)
        except ValueError:
            return None


def search(
    client: DartClient,
    *,
    corp_code: str | None = None,
    bgn_de: str | None = None,
    end_de: str | None = None,
    pblntf_ty: str | None = None,
    pblntf_detail_ty: str | None = None,
    corp_cls: str | None = None,
    last_reprt_at: str = "N",
    sort: str = "date",
    sort_mth: str = "desc",
    max_pages: int = 10,
    page_count: int = MAX_PAGE_COUNT,
) -> list[Disclosure]:
    """공시검색(list.json). 페이지를 끝까지 따라가며 모은다.

    corp_code 없이 조회하면 기간이 3개월로 제한된다.
    """
    if page_count > MAX_PAGE_COUNT:
        raise ValueError(f"page_count는 최대 {MAX_PAGE_COUNT}입니다: {page_count}")

    params = {
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": pblntf_ty,
        "pblntf_detail_ty": pblntf_detail_ty,
        "corp_cls": corp_cls,
        "last_reprt_at": last_reprt_at,
        "sort": sort,
        "sort_mth": sort_mth,
        "page_count": str(page_count),
    }

    out: list[Disclosure] = []
    for page_no in range(1, max_pages + 1):
        data = client.get_json("list.json", {**params, "page_no": str(page_no)})
        # 013은 실패가 아니라 '해당 조건에 공시 없음'이다. 조용히 끝낸다.
        if str(data.get("status")) == "013":
            return out
        out.extend(Disclosure.from_row(row) for row in data.get("list", []))

        total_page = int(data.get("total_page", 1) or 1)
        if page_no >= total_page:
            return out

    logger.warning(
        "max_pages=%d 도달 — 남은 공시를 가져오지 않았습니다 (현재 %d건).", max_pages, len(out)
    )
    return out


def recent_for_stock(
    client: DartClient,
    index: CorpCodeIndex,
    stock_code: str,
    *,
    days: int = 30,
    pblntf_ty: str | None = None,
    max_pages: int = 5,
) -> list[Disclosure]:
    """키움 종목코드로 최근 N일 공시를 가져온다."""
    corp = index.by_stock_code(stock_code)
    today = datetime.now(KST)
    return search(
        client,
        corp_code=corp.corp_code,
        bgn_de=(today - timedelta(days=days)).strftime("%Y%m%d"),
        end_de=today.strftime("%Y%m%d"),
        pblntf_ty=pblntf_ty,
        max_pages=max_pages,
    )


def market_moving(
    client: DartClient,
    *,
    date: str | None = None,
    corp_cls: str | None = None,
    max_pages: int = 5,
) -> list[Disclosure]:
    """당일(또는 지정일) 주가에 영향을 줄 만한 유형의 공시만 모은다.

    DART는 유형 다중 선택을 지원하지 않아 유형별로 나눠 호출한 뒤 접수번호로 중복을 제거한다.
    """
    day = date or datetime.now(KST).strftime("%Y%m%d")
    seen: dict[str, Disclosure] = {}
    for ty in MARKET_MOVING_TYPES:
        for item in search(
            client,
            bgn_de=day,
            end_de=day,
            pblntf_ty=ty,
            corp_cls=corp_cls,
            max_pages=max_pages,
        ):
            seen.setdefault(item.rcept_no, item)
    return sorted(seen.values(), key=lambda d: d.rcept_no, reverse=True)


def company(client: DartClient, corp_code: str) -> dict[str, Any]:
    """기업개황(company.json) — 대표자, 업종, 결산월, 홈페이지 등."""
    return client.get_json("company.json", {"corp_code": corp_code})


def document_files(client: DartClient, rcept_no: str) -> dict[str, bytes]:
    """공시서류 원문(document.xml)을 받아 zip 안의 파일들을 그대로 돌려준다."""
    blob = client.get_bytes("document.xml", {"rcept_no": rcept_no})
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def document_text(client: DartClient, rcept_no: str) -> str:
    """공시 원문을 LLM에 넣을 수 있는 평문으로 뽑아낸다.

    DART 원문은 자체 마크업이 섞인 XML이라 표준 파서로는 깨진다. 태그를 제거하고
    공백을 정리하는 편이 실전에서 안정적이다.
    """
    parts = []
    for name, raw in document_files(client, rcept_no).items():
        if not name.lower().endswith((".xml", ".html", ".htm")):
            continue
        parts.append(_strip_markup(raw.decode("utf-8", errors="replace")))
    return "\n\n".join(p for p in parts if p)


def try_document_text(client: DartClient, rcept_no: str) -> str | None:
    """원문이 아직 안 올라왔으면 None을 돌려준다.

    공시 목록에는 접수 즉시 뜨지만 원문 파일은 시차를 두고 올라온다(그 사이 014 응답).
    실시간 폴링 루프는 이걸 실패가 아니라 '나중에 다시'로 다뤄야 한다.
    보고서명(`report_nm`)은 목록 단계에서 이미 확보되므로, 급한 신호는 그것만으로도 낼 수 있다.
    """
    try:
        return document_text(client, rcept_no)
    except DartError as exc:
        if exc.status in DOCUMENT_PENDING_STATUSES:
            logger.debug("원문 미제공 (재시도 대상): %s", rcept_no)
            return None
        raise


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _strip_markup(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    lines = (_WS.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
