import io
import zipfile

import pytest

from aistock.dart.client import _looks_like_error_xml, _parse_error_xml
from aistock.dart.corp_code import (
    Corp,
    CorpCodeIndex,
    _extract_single,
    _parse,
    normalize_stock_code,
)

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list>
    <corp_code>00126380</corp_code>
    <corp_name>삼성전자</corp_name>
    <stock_code>005930</stock_code>
    <modify_date>20260701</modify_date>
  </list>
  <list>
    <corp_code>00164779</corp_code>
    <corp_name>비상장회사</corp_name>
    <stock_code>         </stock_code>
    <modify_date>20260701</modify_date>
  </list>
</result>
""".encode()


def test_parse_reads_listed_company():
    corps = _parse(SAMPLE_XML)
    assert corps[0] == Corp("00126380", "삼성전자", "005930", "20260701")


def test_parse_treats_whitespace_stock_code_as_unlisted():
    """비상장사의 stock_code는 공백으로 채워져 온다. strip 없으면 상장사로 오인한다."""
    corps = _parse(SAMPLE_XML)
    assert corps[1].stock_code == ""
    assert not corps[1].is_listed


def test_index_only_maps_listed_companies():
    index = CorpCodeIndex(_parse(SAMPLE_XML), "2026-08-05T00:00:00+00:00")
    assert len(index.corps) == 2
    assert len(index.listed) == 1
    assert index.by_stock_code("005930").corp_name == "삼성전자"


def test_lookup_strips_kiwoom_exchange_suffix():
    """키움은 SOR/NXT 종목을 005930_AL, 005930_NX로 준다. DART는 6자리만 안다."""
    index = CorpCodeIndex(_parse(SAMPLE_XML), "2026-08-05T00:00:00+00:00")
    assert index.by_stock_code("005930_AL").corp_code == "00126380"
    assert index.by_stock_code("005930_NX").corp_code == "00126380"


@pytest.mark.parametrize(
    "raw,expected", [("005930", "005930"), ("005930_NX", "005930"), (" 005930 ", "005930")]
)
def test_normalize_stock_code(raw, expected):
    assert normalize_stock_code(raw) == expected


def test_unknown_stock_code_raises():
    index = CorpCodeIndex(_parse(SAMPLE_XML), "2026-08-05T00:00:00+00:00")
    with pytest.raises(KeyError):
        index.by_stock_code("999999")


def test_get_returns_none_for_unknown():
    index = CorpCodeIndex(_parse(SAMPLE_XML), "2026-08-05T00:00:00+00:00")
    assert index.get("999999") is None


def test_search_by_name_defaults_to_listed_only():
    index = CorpCodeIndex(_parse(SAMPLE_XML), "2026-08-05T00:00:00+00:00")
    assert [c.corp_name for c in index.search("회사")] == []
    assert [c.corp_name for c in index.search("회사", listed_only=False)] == ["비상장회사"]


def test_extract_single_reads_xml_from_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", SAMPLE_XML)
    assert _extract_single(buffer.getvalue()) == SAMPLE_XML


def test_zip_bytes_are_not_mistaken_for_an_error():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", SAMPLE_XML)
    assert not _looks_like_error_xml(buffer.getvalue())


def test_error_xml_is_detected_and_parsed():
    body = b'<?xml version="1.0"?><result><status>020</status><message>\xed\x95\x9c\xeb\x8f\x84\xec\xb4\x88\xea\xb3\xbc</message></result>'
    assert _looks_like_error_xml(body)
    assert _parse_error_xml(body) == ("020", "한도초과")
