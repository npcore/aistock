"""예수금·미국 잔고 파싱과 미국 주문 검증. 키움을 호출하지 않는다."""

import pytest

from aistock import us_order
from aistock.account import Deposit, UsHolding, parse_usd
from aistock.errors import APIError
from aistock.notify.format import account_message
from aistock.us_order import UsOrderError, UsOrderRequest, is_us_symbol, round_price, validate

# --- 예수금 ---


def test_deposit_from_padded_fields():
    d = Deposit.from_data(
        {"entr": "000000005000000", "d2_entra": "000000004800000",
         "ord_alow_amt": "000000004800000", "pymn_alow_amt": "-00000000000100"}
    )
    assert (d.cash, d.d2_cash, d.orderable, d.withdrawable) == (5_000_000, 4_800_000, 4_800_000, -100)


# --- 미국 잔고 ---


def test_parse_usd_keeps_sign_and_decimals():
    """손익의 부호는 진짜 음수다. 시세 TR의 '방향 부호'처럼 떼면 손실이 이익으로 보인다."""
    assert parse_usd("-12.3400") == -12.34
    assert parse_usd("0068.2100") == 68.21
    assert parse_usd("") == 0.0


def test_us_holding_row():
    h = UsHolding.from_row({
        "stex_nm": "NASDAQ", "stk_cd": "TQQQ", "frgn_stk_nm": "PROSHARES ULTRAPRO QQQ",
        "poss_qty": "000000000010", "sell_alowq": "000000000010",
        "frgn_stk_book_uv": "70.0000", "now_pric": "68.2100", "evlt_amt": "682.1000",
        "pl_amt": "-17.9000", "pl_rt": "-2.56", "evlt_amt_krw": "000000000914000",
        "pl_amt_krw": "-00000000024000",
    })
    assert h.exchange == "ND" and h.quantity == 10 and h.sellable == 10
    assert h.profit == -17.9 and h.profit_rate == -2.56 and h.profit_krw == -24_000


def test_account_message_shows_deposits():
    from types import SimpleNamespace

    domestic = SimpleNamespace(holdings=[], total_profit=0)
    deposit = Deposit(5_000_000, 5_000_000, 4_900_000, 4_900_000)
    us = SimpleNamespace(holdings=[], usd_cash=1200.5, usd_orderable=1100.0, krw_cash=0)
    text = account_message(domestic, deposit, us)
    assert "주문가능 4,900,000원" in text and "$1,100.00" in text


def test_account_message_without_extras():
    from types import SimpleNamespace

    text = account_message(SimpleNamespace(holdings=[], total_profit=0))
    assert "예수금" not in text and "미국" not in text


# --- 미국 주문 검증 (주문은 내지 않는다) ---


def test_symbol_detection():
    assert is_us_symbol("TQQQ") and is_us_symbol("brk.b")
    assert not is_us_symbol("005930")


@pytest.mark.parametrize("price,up,expected", [
    (68.213, True, 68.22), (68.219, False, 68.21), (0.12345, True, 0.1235), (68.2, True, 68.2),
])
def test_round_price(price, up, expected):
    assert round_price(price, up=up) == expected


def _req(**kw):
    base = dict(side="buy", symbol="TQQQ", exchange="ND", quantity=10, price=68.21)
    return UsOrderRequest(**{**base, **kw})


def test_valid_request_passes():
    validate(_req())


@pytest.mark.parametrize("kw,match", [
    ({"price": 68.215}, "호가단위"),
    ({"price": 0}, "지정가"),
    ({"quantity": 0}, "1주"),
    ({"exchange": "KRX"}, "거래소"),
    ({"symbol": "005930"}, "형식"),
])
def test_invalid_requests_rejected(kw, match):
    with pytest.raises(UsOrderError, match=match):
        validate(_req(**kw))


def test_usd_cap(monkeypatch):
    """수량에 0을 하나 더 붙이는 실수를 막는다."""
    monkeypatch.setenv("MAX_ORDER_AMOUNT_USD", "5000")
    with pytest.raises(UsOrderError, match="상한"):
        validate(_req(quantity=100))


class _Client:
    def __init__(self, exc):
        self.exc = exc

    def request(self, *a, **kw):
        raise self.exc


def test_no_pending_error_means_empty():
    """미체결이 없으면 키움은 빈 목록 대신 return_code=20 오류를 준다."""
    exc = APIError(20, "[2000](571758:해당 계좌의미체결내역이 없습니다.)", "ust21050")
    assert us_order.pending_orders(_Client(exc)) == []


def test_other_code_20_error_still_raises():
    with pytest.raises(APIError):
        us_order.pending_orders(_Client(APIError(20, "다른 오류", "ust21050")))


# --- 실제 전송 형식 (2026-10-20 실주문 거부 사례) ---


class _CaptureClient:
    def __init__(self):
        self.bodies = []

    def request(self, api_id, path, body):
        self.bodies.append((api_id, body))
        return type("R", (), {"data": {"ord_no": "0001"}})()


@pytest.mark.parametrize("price,expected", [(70.83, "70.83"), (70.8, "70.80"), (0.1234, "0.1234")])
def test_order_price_digits(price, expected):
    """$1 이상에 '70.8300'을 보내면 키움이 1517로 거부한다."""
    client = _CaptureClient()
    us_order.place(client, UsOrderRequest("buy", "TQQQ", "ND", 1, price))
    [(api_id, body)] = client.bodies
    assert api_id == "ust20000" and body["ord_uv"] == expected and body["trde_tp"] == "00"
