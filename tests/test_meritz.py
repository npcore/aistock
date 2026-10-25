"""메리츠증권 연결 검증. 실제 서버를 부르지 않는다 — 공식 명세(openapi.json)의 형태로 흉내낸다."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from aistock.brokers import meritz as M


@pytest.fixture(autouse=True)
def token_path(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "TOKEN_PATH", tmp_path / "meritz_token.json")
    monkeypatch.setattr(M, "MIN_INTERVAL", 0)
    return tmp_path / "meritz_token.json"


class FakeMeritz:
    """경로별 응답을 정해두고, 받은 요청을 기록한다."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert form["grant_type"] == ["client_credentials"] and form["scope"] == ["login"]
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "43200",
                                             "token_type": "Bearer", "scope": "login"})
        handler = self.routes[request.url.path]
        if callable(handler):
            status, body = handler(request)
        elif isinstance(handler, tuple):
            status, body = handler
        else:
            status, body = 200, handler
        return httpx.Response(status, json=body)


def _client(routes):
    fake = FakeMeritz(routes)
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return M.MeritzClient(M.MeritzConfig("app-key-123456", "secret"), http=http), fake


DEPOSIT = {"rsp_cd": "5766", "rsp_msg": "조회 완료", "tr_cont": "0",
           "data": {"tfam": 5000000, "d2_aft_tfam": 4800000, "oder_able_cash_amt": 4700000,
                    "drng_able_amt": 4600000}}


def test_token_is_form_encoded_and_cached(token_path):
    client, fake = _client({"/accounts/v1/me/deposit": DEPOSIT})
    client.deposit()
    client.deposit()
    assert fake.token_calls == 1
    # 디스크 캐시: 새 클라이언트도 토큰을 다시 받지 않는다
    client2, fake2 = _client({"/accounts/v1/me/deposit": DEPOSIT})
    client2.deposit()
    assert fake2.token_calls == 0
    assert oct(token_path.stat().st_mode)[-3:] == "600"


def test_token_cache_ignored_for_other_app_key(token_path):
    client, _ = _client({"/accounts/v1/me/deposit": DEPOSIT})
    client.deposit()
    other = M.MeritzClient(
        M.MeritzConfig("different-key-999999", "s"),
        http=httpx.Client(transport=httpx.MockTransport(FakeMeritz({"/accounts/v1/me/deposit": DEPOSIT}))),
    )
    other.deposit()
    assert other._token == "tok" and json.loads(token_path.read_text())["key_tail"] == "999999"


def test_required_headers():
    """mac_address 헤더가 없으면 게이트웨이가 IGW50024로 거절한다."""
    client, fake = _client({"/accounts/v1/me/deposit": DEPOSIT})
    client.deposit()
    req = fake.requests[-1]
    assert req.headers["authorization"] == "Bearer tok"
    mac = req.headers["mac_address"]
    assert len(mac) == 12 and all(c in "0123456789ABCDEF" for c in mac)


def test_deposit_parsed():
    client, _ = _client({"/accounts/v1/me/deposit": DEPOSIT})
    d = client.deposit()
    assert (d.cash, d.d2_cash, d.orderable, d.withdrawable) == (5000000, 4800000, 4700000, 4600000)


def test_business_error_comes_as_http_500():
    """업무 오류도 HTTP 500 + rsp_cd로 온다. 사유가 메시지에 보여야 고칠 수 있다."""
    client, _ = _client({"/accounts/v1/me/deposit": (500, {"rsp_cd": "0760", "rsp_msg": "권한이 없습니다.",
                                                           "rsp_sub_msg": "계좌 관리점 확인"})})
    with pytest.raises(M.MeritzError, match="0760.*권한이 없습니다.*계좌 관리점"):
        client.deposit()


def test_unknown_rsp_cd_with_200_is_failure():
    client, _ = _client({"/accounts/v1/me/deposit": {"rsp_cd": "9999", "rsp_msg": "이상"}})
    with pytest.raises(M.MeritzError, match="9999"):
        client.deposit()


def test_pagination_sends_key_as_query():
    """연속조회 키를 헤더로 보내면 방화벽이 막는다. 쿼리스트링으로, 한 글자도 바꾸지 않고."""
    key = "ABC  123   XYZ"  # 공백이 긴 키

    def holdings(req):
        if req.url.params.get("tr_cont") == "1":
            assert req.url.params.get("tr_cont_key") == key
            assert "tr_cont_key" not in req.headers
            return 200, {"rsp_cd": "5766", "tr_cont": "0", "data": [_kr_row("000660", 2)]}
        return 200, {"rsp_cd": "5766", "tr_cont": "1", "tr_cont_key": key,
                     "data": [_kr_row("005930", 10)]}

    client, fake = _client({"/accounts/v1/me/holdings": holdings})
    rows = client.domestic_holdings()
    assert [h.code for h in rows] == ["005930", "000660"]


def _kr_row(code, qty, cls="1"):
    return {"iscd": f"A{code}", "isnm": "종목", "pros_trgt_cls_code": cls, "mksc_cls_name": "주식일반",
            "rmnd_qty": qty, "byng_unpr": "70000.00000000", "psnt_prc": "75000.00000000",
            "vltn_amt": qty * 75000, "vltn_prls_amt": qty * 5000, "ert": "7.14286"}


def test_domestic_holdings_skip_overseas_rows_and_zero_qty():
    client, _ = _client({"/accounts/v1/me/holdings": {
        "rsp_cd": "5766", "tr_cont": "0",
        "data": [_kr_row("005930", 10), _kr_row("TQQQ.OQ", 5, cls="J"), _kr_row("035720", 0)]}})
    [h] = client.domestic_holdings()
    assert h.code == "005930" and h.quantity == 10 and h.profit_rate == pytest.approx(7.14286)


OVERSEAS = {"rsp_cd": "5766", "tr_cont": "0", "data": [{
    "iscd": "TQQQ.OQ", "ovrs_stck_symb_iscd": "TQQQ", "isnm": "PROSHARES ULTRAPRO QQQ",
    "crcd": "USD", "rmnd_dqty": "100.0000000000", "seln_able_dqty": "100.0000000000",
    "avrg_unpr": "68.5000", "psnt_prc": "72.1900", "vltn_prls_camt": "-369.0000",
    "vltn_ert": "-5.3870", "frcr_vltn_amt": "7219.000", "exrt": "1330.50"}]}


def test_overseas_holdings_parse_decimal_strings():
    client, _ = _client({"/accounts/v1/me/overseas/holdings": OVERSEAS})
    [h] = client.overseas_holdings()
    assert (h.market, h.code, h.quantity, h.avg_price, h.price) == ("US", "TQQQ", 100.0, 68.5, 72.19)
    assert h.profit == -369.0 and h.profit_rate == pytest.approx(-5.387)  # 손실 부호 유지


def test_no_data_is_empty_not_error():
    client, _ = _client({"/accounts/v1/me/overseas/holdings": {"rsp_cd": "5820", "rsp_msg": "조회할 자료 없음",
                                                               "tr_cont": "0", "data": []}})
    assert client.overseas_holdings() == []


def test_snapshot_and_message():
    from aistock.notify.format import meritz_message

    client, _ = _client({
        "/accounts/v1/me/deposit": DEPOSIT,
        "/accounts/v1/me/holdings": {"rsp_cd": "5766", "tr_cont": "0", "data": [_kr_row("005930", 10)]},
        "/accounts/v1/me/overseas/holdings": OVERSEAS,
        "/accounts/v1/me/overseas/currencies": {"rsp_cd": "5766", "tr_cont": "0",
                                                "data": [{"crcd": "USD", "frcr_amt": "1234.56"}]},
    })
    snap = client.snapshot()
    text = meritz_message(snap)
    assert "메리츠증권" in text and "TQQQ" in text and "USD 1,234.56" in text
    assert "조회 전용" in text


def test_no_order_functions():
    """조회 전용. 주문 경로가 생기면 이 테스트가 먼저 깨져서 의도적으로 열었는지 확인하게 된다."""
    names = [n for n in dir(M.MeritzClient) if not n.startswith("__")]
    assert not [n for n in names if any(w in n for w in ("order", "buy", "sell", "cancel"))]
    src = open(M.__file__, encoding="utf-8").read()
    assert "/trading/" not in src


def test_unavailable_without_keys(monkeypatch):
    monkeypatch.setattr(M, "load_env", lambda: None)
    monkeypatch.delenv("MERITZ_APP_KEY", raising=False)
    monkeypatch.delenv("MERITZ_APP_SECRET", raising=False)
    assert not M.MeritzConfig.available()
