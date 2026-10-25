import io
import zipfile

import pytest

from aistock.dart.disclosure import (
    MARKET_MOVING_TYPES,
    PBLNTF_TY,
    Disclosure,
    _strip_markup,
    try_document_text,
)
from aistock.dart.errors import DartError

ROW = {
    "corp_cls": "Y",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "주요사항보고서(자기주식취득결정) ",
    "rcept_no": "20260805000123",
    "flr_nm": "삼성전자",
    "rcept_dt": "20260805",
    "rm": "",
}


def test_from_row_maps_fields():
    item = Disclosure.from_row(ROW)
    assert item.rcept_no == "20260805000123"
    assert item.corp_code == "00126380"
    assert item.report_nm == "주요사항보고서(자기주식취득결정)"


def test_market_label():
    assert Disclosure.from_row(ROW).market == "유가"


def test_url_points_at_dart_viewer():
    assert Disclosure.from_row(ROW).url.endswith("rcpNo=20260805000123")


def test_submitted_at_parsed_in_kst():
    at = Disclosure.from_row(ROW).submitted_at
    assert at is not None
    assert (at.year, at.month, at.day) == (2026, 8, 5)
    assert at.utcoffset().total_seconds() == 9 * 3600


def test_submitted_at_none_on_bad_date():
    assert Disclosure.from_row({**ROW, "rcept_dt": ""}).submitted_at is None


def test_unlisted_company_has_blank_stock_code():
    assert Disclosure.from_row({**ROW, "stock_code": " "}).stock_code == ""


def test_market_moving_types_are_all_valid():
    assert set(MARKET_MOVING_TYPES) <= set(PBLNTF_TY)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<p>자기주식 취득</p>", "자기주식 취득"),
        ("<TABLE><TR><TD>금액</TD></TR></TABLE>", "금액"),
        ("A &amp; B &lt;주&gt;", "A & B <주>"),
        ("<style>td{color:red}</style>본문", "본문"),
        ("<script>var a=1;</script>본문", "본문"),
    ],
)
def test_strip_markup(raw, expected):
    assert _strip_markup(raw) == expected


def test_strip_markup_collapses_blank_lines():
    assert _strip_markup("가\n\n\n\n나") == "가\n\n나"


class _StubClient:
    """document_text가 부르는 get_bytes만 흉내낸다."""

    def __init__(self, error: DartError | None = None) -> None:
        self.error = error

    def get_bytes(self, path, params=None):
        if self.error:
            raise self.error
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("doc.xml", "<p>유상증자 결정</p>".encode())
        return buffer.getvalue()


def test_try_document_text_returns_text_when_available():
    assert try_document_text(_StubClient(), "20260805000001") == "유상증자 결정"


@pytest.mark.parametrize("status", ["013", "014"])
def test_try_document_text_returns_none_while_pending(status):
    """공시 목록에는 즉시 뜨지만 원문은 시차를 두고 올라온다. 폴링 루프가 죽으면 안 된다."""
    assert try_document_text(_StubClient(DartError(status)), "20260806000999") is None


def test_try_document_text_still_raises_real_errors():
    with pytest.raises(DartError):
        try_document_text(_StubClient(DartError("020")), "20260806000999")
