"""스펙 파일이 우리가 감싼 TR들과 실제로 일치하는지 확인한다.

여기가 깨지면 코드에 하드코딩한 경로/파라미터가 스펙과 어긋난 것이다.
"""

import pytest

from aistock import spec
from aistock.market import domestic, overseas

WRAPPED = {
    "ka10001": domestic.STKINFO,
    "ka10004": domestic.MRKCOND,
    "ka10086": domestic.MRKCOND,
    "ka10081": domestic.CHART,
    "ka40002": domestic.ETF,
    "ka40004": domestic.ETF,
    "usa20100": overseas.US_MRKCOND,
    "usa20101": overseas.US_MRKCOND,
    "usa20590": overseas.US_MRKCOND,
}


@pytest.mark.parametrize("api_id,path", WRAPPED.items())
def test_wrapped_tr_paths_match_spec(api_id, path):
    assert spec.url_of(api_id) == path


@pytest.mark.parametrize("api_id", ["ka10001", "ka10004", "ka10081"])
def test_domestic_tr_takes_stk_cd(api_id):
    assert "stk_cd" in {row["element"] for row in spec.params_of(api_id)}


def test_us_quote_requires_exchange_and_code():
    required = {row["element"] for row in spec.params_of("usa20100") if row.get("required") == "Y"}
    assert required == {"stex_tp", "stk_cd"}


def test_us_exchange_codes_are_letter_based():
    """미국 stex_tp는 NY/ND/NA 문자 코드. 국내처럼 1/2/3 숫자를 넣으면 안 된다."""
    desc = next(r for r in spec.params_of("usa20100") if r["element"] == "stex_tp")["description"]
    for code, name in overseas.EXCHANGES.items():
        assert code in desc and name in desc


def test_etf_all_required_params_are_all_supplied():
    """ka40004는 6개 파라미터가 전부 필수 — 하나라도 빠지면 에러가 난다."""
    required = {row["element"] for row in spec.params_of("ka40004") if row.get("required") == "Y"}
    assert required == {"txon_type", "navpre", "mngmcomp", "txon_yn", "trace_idex", "stex_tp"}


def test_search_finds_by_korean_name():
    assert any(api_id == "ka10001" for api_id, _, _ in spec.search("주식기본정보"))


def test_unknown_api_id_raises():
    with pytest.raises(KeyError):
        spec.get("nope99999")
