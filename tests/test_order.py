"""주문 검증. 실제 주문은 내지 않는다.

여기가 틀리면 실계좌에서 손실이 난다. 특히 '막아야 하는데 통과되는' 경우를 집중적으로 본다.
"""

from types import SimpleNamespace

import pytest

from aistock.order import (
    DEFAULT_MAX_ORDER_AMOUNT,
    OrderError,
    OrderRequest,
    OrderType,
    PendingOrder,
    place,
    validate,
)


def _req(**overrides) -> OrderRequest:
    base = {
        "side": "buy",
        "stock_code": "005930",
        "quantity": 10,
        "price": 230_000,
        "order_type": OrderType.LIMIT,
    }
    return OrderRequest(**{**base, **overrides})


class StubClient:
    """주문 요청 본문만 받아두는 가짜 클라이언트."""

    def __init__(self, order_no: str = "1234567"):
        self.calls: list[tuple[str, dict]] = []
        self.order_no = order_no

    def request(self, api_id, path, body=None, **kwargs):
        self.calls.append((api_id, body or {}))
        return SimpleNamespace(data={"ord_no": self.order_no, "dmst_stex_tp": "KRX"})


# --- 검증: 막아야 하는 것 ---


@pytest.mark.parametrize("qty", [0, -1, -100])
def test_non_positive_quantity_is_rejected(qty):
    with pytest.raises(OrderError, match="1주 이상"):
        validate(_req(quantity=qty))


@pytest.mark.parametrize("code", ["", "AAPL", "00593", "005930A"])
def test_bad_stock_code_is_rejected(code):
    with pytest.raises(OrderError, match="종목코드"):
        validate(_req(stock_code=code))


def test_limit_order_without_price_is_rejected():
    """지정가인데 가격이 없으면 조용히 시장가로 나가면 안 된다."""
    with pytest.raises(OrderError, match="주문단가"):
        validate(_req(price=0))


def test_bad_side_is_rejected():
    with pytest.raises(OrderError):
        validate(_req(side="hold"))


def test_amount_over_limit_is_rejected(monkeypatch):
    """수량에 0을 하나 더 붙이는 실수가 계좌를 비울 수 있다."""
    monkeypatch.setenv("MAX_ORDER_AMOUNT", "1000000")
    with pytest.raises(OrderError, match="상한"):
        validate(_req(quantity=100, price=230_000))  # 2,300만원


def test_amount_at_limit_is_allowed(monkeypatch):
    monkeypatch.setenv("MAX_ORDER_AMOUNT", "2300000")
    validate(_req(quantity=10, price=230_000))  # 정확히 230만원


def test_zero_limit_disables_the_cap(monkeypatch):
    monkeypatch.setenv("MAX_ORDER_AMOUNT", "0")
    validate(_req(quantity=10_000, price=230_000))


def test_malformed_limit_falls_back_to_default(monkeypatch):
    """상한 설정이 깨졌다고 상한 없이 주문이 나가면 안 된다."""
    monkeypatch.setenv("MAX_ORDER_AMOUNT", "십만원")
    with pytest.raises(OrderError, match="상한"):
        validate(_req(quantity=1000, price=230_000))


def test_default_limit_applies_when_unset(monkeypatch):
    monkeypatch.delenv("MAX_ORDER_AMOUNT", raising=False)
    over = DEFAULT_MAX_ORDER_AMOUNT // 230_000 + 10
    with pytest.raises(OrderError, match="상한"):
        validate(_req(quantity=over, price=230_000))


# --- 검증: 통과해야 하는 것 ---


def test_valid_limit_order_passes():
    validate(_req())


def test_market_order_needs_no_price():
    validate(_req(order_type=OrderType.MARKET, price=0))


def test_market_order_skips_the_amount_cap(monkeypatch):
    """시장가는 체결가를 모르므로 금액 상한을 걸 수 없다 — 그래서 기본값이 아니다."""
    monkeypatch.setenv("MAX_ORDER_AMOUNT", "1000")
    validate(_req(order_type=OrderType.MARKET, price=0, quantity=1000))


# --- 전송 본문 ---


def test_buy_uses_the_buy_tr():
    client = StubClient()
    place(client, _req(side="buy"))
    assert client.calls[0][0] == "kt10000"


def test_sell_uses_the_sell_tr():
    client = StubClient()
    place(client, _req(side="sell"))
    assert client.calls[0][0] == "kt10001"


def test_limit_order_sends_price():
    client = StubClient()
    place(client, _req(price=230_000, quantity=10))
    body = client.calls[0][1]
    assert body["trde_tp"] == "0"
    assert body["ord_uv"] == "230000"
    assert body["ord_qty"] == "10"
    assert body["stk_cd"] == "005930"


def test_market_order_omits_price():
    """시장가에 단가를 함께 보내면 거래소가 거절하거나 의도와 다르게 처리될 수 있다."""
    client = StubClient()
    place(client, _req(order_type=OrderType.MARKET, price=0))
    body = client.calls[0][1]
    assert body["trde_tp"] == "3"
    assert "ord_uv" not in body


def test_place_revalidates_even_if_caller_skipped_it():
    """호출자가 validate를 잊어도 주문이 나가면 안 된다."""
    with pytest.raises(OrderError):
        place(StubClient(), _req(quantity=0))


def test_missing_order_number_is_an_error():
    class NoOrderNo(StubClient):
        def request(self, api_id, path, body=None, **kwargs):
            return SimpleNamespace(data={"return_code": 0})

    with pytest.raises(OrderError, match="주문번호"):
        place(NoOrderNo(), _req())


def test_place_returns_the_order_number():
    result = place(StubClient("7654321"), _req())
    assert result.order_no == "7654321"


# --- 표시 ---


def test_describe_shows_amount_for_limit():
    text = _req(quantity=10, price=230_000).describe()
    assert "매수" in text and "10주" in text and "2,300,000원" in text


def test_describe_omits_amount_for_market():
    text = _req(order_type=OrderType.MARKET, price=0).describe()
    assert "MARKET" in text
    assert "원 =" not in text


# --- 미체결 파싱 ---


def test_pending_order_parses_padded_numbers():
    """계좌 TR과 마찬가지로 0패딩이 온다."""
    row = {
        "ord_no": "0001234",
        "stk_cd": "A005930",
        "stk_nm": "삼성전자",
        "io_tp_nm": "매수",
        "ord_qty": "000000000000010",
        "ord_pric": "000000000230000",
        "oso_qty": "000000000000007",
        "ord_stt": "접수",
        "tm": "093015",
    }
    p = PendingOrder.from_row(row)
    assert p.order_no == "0001234"
    assert p.stock_code == "005930"  # A 접두사 제거
    assert (p.quantity, p.price, p.remaining) == (10, 230_000, 7)
