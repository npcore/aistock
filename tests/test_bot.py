"""봇 검증. 텔레그램 API를 호출하지 않는다."""

import pytest

from aistock.bot import COMMANDS, TelegramBot
from aistock.notify.telegram import TelegramConfig


def _bot(tmp_path, chat_id="123456789") -> TelegramBot:
    return TelegramBot(
        TelegramConfig("token", chat_id), kiwoom=None, offset_path=tmp_path / "offset.txt"
    )


# --- offset 내구성 ---


def test_offset_persists_across_restarts(tmp_path):
    """주문 처리 도중 죽으면 재시작 시 같은 /buy가 다시 실행돼 중복 체결이 난다."""
    bot = _bot(tmp_path)
    assert bot._offset == 0
    bot._offset = 42
    bot._save_offset()

    revived = _bot(tmp_path)
    assert revived._offset == 42


def test_missing_offset_file_starts_at_zero(tmp_path):
    assert _bot(tmp_path)._offset == 0


def test_corrupt_offset_file_starts_at_zero(tmp_path):
    (tmp_path / "offset.txt").write_text("깨진값")
    assert _bot(tmp_path)._offset == 0


def test_offset_save_failure_does_not_crash(tmp_path, caplog):
    """저장에 실패해도 봇이 죽으면 안 된다 — 경고만 남기고 계속 돈다."""
    bot = _bot(tmp_path)
    bot.offset_path = tmp_path / "없는디렉터리" / "x" / "offset.txt"
    bot.offset_path.parent.parent.write_text("파일이라 하위 경로를 만들 수 없다")
    with caplog.at_level("WARNING"):
        bot._save_offset()
    assert "offset 저장 실패" in caplog.text


# --- 접근 제어 ---


def test_only_configured_chat_is_allowed(tmp_path):
    """봇 사용자명은 검색으로 찾을 수 있다. 화이트리스트가 없으면 누구나 잔고를 본다."""
    bot = _bot(tmp_path)
    assert "123456789" in bot.allowed
    assert "999999999" not in bot.allowed


def test_extra_chat_ids_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111, 222")
    bot = _bot(tmp_path)
    assert {"123456789", "111", "222"} <= bot.allowed


# --- 명령 등록 ---


@pytest.mark.parametrize(
    "name", ["help", "quote", "watchlist", "balance", "orders", "trend", "buy", "sell", "cancel"]
)
def test_command_is_registered(name):
    assert name in COMMANDS


def test_start_maps_to_help():
    assert COMMANDS["start"] is COMMANDS["help"]


# --- 주문 명령 인자 파싱 ---


class StubCtx:
    from aistock.trading.policy import TradingPolicy

    kiwoom = None
    notifier = None
    watchlist = None
    policy = TradingPolicy()


@pytest.mark.parametrize("args", [[], ["005930"], ["005930", "10"]])
def test_order_requires_three_arguments(args):
    assert "형식" in COMMANDS["buy"](StubCtx(), args)


def test_order_rejects_non_numeric_quantity():
    assert "수량" in COMMANDS["buy"](StubCtx(), ["005930", "열주", "230000"])


def test_order_rejects_non_numeric_price():
    assert "가격" in COMMANDS["buy"](StubCtx(), ["005930", "10", "비싸게"])


# --- 폴링 실패 시 백오프 ---


def test_backoff_grows_on_consecutive_failures(tmp_path, monkeypatch):
    """502가 이어질 때 같은 간격으로 계속 때리면 429로 번진다."""
    import aistock.bot as bot_mod

    slept = []
    monkeypatch.setattr(bot_mod.time, "sleep", slept.append)
    bot = _bot(tmp_path)
    for _ in range(4):
        bot._backoff("실패")
    assert slept == sorted(slept)  # 단조 증가
    assert slept[0] < slept[-1]


def test_backoff_is_capped(tmp_path, monkeypatch):
    """장애가 풀렸을 때 너무 늦게 복귀하면 안 된다."""
    import aistock.bot as bot_mod

    slept = []
    monkeypatch.setattr(bot_mod.time, "sleep", slept.append)
    bot = _bot(tmp_path)
    for _ in range(20):
        bot._backoff("실패")
    assert max(slept) <= bot_mod.MAX_BACKOFF


def test_retry_after_is_honored(tmp_path, monkeypatch):
    """429는 텔레그램이 대기 시간을 알려준다. 무시하면 제한이 길어진다."""
    import aistock.bot as bot_mod

    slept = []
    monkeypatch.setattr(bot_mod.time, "sleep", slept.append)
    bot = _bot(tmp_path)
    bot._backoff("429", wait=5.0)
    assert slept == [5.0]


def test_failure_counter_resets_after_success(tmp_path, monkeypatch):
    """복구 후에도 긴 대기가 남아 있으면 응답이 계속 느리다."""
    import aistock.bot as bot_mod

    monkeypatch.setattr(bot_mod.time, "sleep", lambda _: None)
    bot = _bot(tmp_path)
    for _ in range(5):
        bot._backoff("실패")
    assert bot._failures == 5

    class FakeResponse:
        @staticmethod
        def json():
            return {"ok": True, "result": []}

    monkeypatch.setattr(bot._client, "get", lambda *a, **k: FakeResponse())
    bot._poll()
    assert bot._failures == 0


# --- 설명서 ---


def _balanced(html: str) -> bool:
    import re

    stack = []
    for closing, tag in re.findall(r"<(/?)(\w+)[^>]*>", html):
        if closing:
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


def test_help_pages_are_valid_telegram_html():
    """태그가 하나만 어긋나도 텔레그램이 메시지 전체를 400으로 거절한다."""
    from aistock.bot import HELP, HELP_TOPICS

    for text in [HELP, *HELP_TOPICS.values()]:
        assert _balanced(text) and len(text) < 3900


def test_help_topics():
    help_ = COMMANDS["help"]
    assert "설명서" in help_(None, [])
    assert "TQQQ 주문 자세히" in help_(None, ["미국"])
    assert "경제 봇" in help_(None, ["경제"])  # 앞부분만 쳐도 된다
    assert "자동 제안" in help_(None, ["자동매매"])
    assert "그런 항목이 없습니다" in help_(None, ["없는거"])


def test_menu_commands_exist():
    """메뉴에 있는데 실제로 없는 명령이면 눌렀을 때 '모르는 명령'이 뜬다."""
    from aistock.bot import ADVISOR_COMMANDS, MENU

    for name, desc in MENU:
        assert name in COMMANDS or name in ADVISOR_COMMANDS
        assert name.islower() and len(desc) <= 256


def test_kr_orders_refused_when_disabled():
    """추가 계정(TQQQ 전용)에서 국내 /buy는 즉시 주문 경로라 아예 막는다."""
    from aistock.trading.policy import TradingPolicy

    class Ctx(StubCtx):
        policy = TradingPolicy(kr_enabled=False)

    assert "국내 주식 주문을 쓰지 않습니다" in COMMANDS["buy"](Ctx(), ["005930", "10", "230000"])


# --- /cancel ---


@pytest.fixture
def open_orders(monkeypatch):
    from types import SimpleNamespace as NS

    from aistock import order as kr_order
    from aistock import us_order

    state = NS(
        us=[
            NS(order_no="000012678", symbol="TQQQ", side="매수", remaining=179, quantity=179,
               price=65.5, exchange="", status="접수"),
            NS(order_no="000012999", symbol="SOXL", side="매수", remaining=10, quantity=10,
               price=30.0, exchange="NY", status="접수"),
        ],
        kr=[NS(order_no="7001", stock_code="005930", name="삼성전자", side="매수",
               remaining=3, price=200000)],
        cancelled=[],
    )
    monkeypatch.setattr(us_order, "pending_orders", lambda c: state.us)
    monkeypatch.setattr(kr_order, "pending_orders", lambda c: state.kr)
    monkeypatch.setattr(us_order, "resolve_exchange", lambda c, s: ("ND", {}))
    monkeypatch.setattr(
        us_order, "cancel", lambda c, no, sym, ex: state.cancelled.append((no, sym, ex)) or no
    )
    monkeypatch.setattr(
        kr_order, "cancel", lambda c, no, code, *a, **k: state.cancelled.append((no, code)) or no
    )
    return state


def test_cancel_without_args_lists_orders(open_orders):
    text = COMMANDS["cancel"](StubCtx(), [])
    assert "/cancel 000012678" in text and "/cancel 7001" in text
    assert not open_orders.cancelled


def test_cancel_by_symbol_cancels_only_that_symbol(open_orders):
    text = COMMANDS["cancel"](StubCtx(), ["tqqq"])
    # 미체결 응답의 거래소가 '미국'으로만 와서 비어 있으면 시세로 찾은 ND를 쓴다
    assert open_orders.cancelled == [("000012678", "TQQQ", "ND")]
    assert "취소 접수" in text


def test_cancel_by_order_no(open_orders):
    COMMANDS["cancel"](StubCtx(), ["7001"])
    assert open_orders.cancelled == [("7001", "005930")]


def test_cancel_everything(open_orders):
    COMMANDS["cancel"](StubCtx(), ["전부"])
    assert len(open_orders.cancelled) == 3


def test_cancel_unknown(open_orders):
    assert "취소할 미체결 주문이 없습니다" in COMMANDS["cancel"](StubCtx(), ["NVDA"])
    assert not open_orders.cancelled


def test_cancel_list_reply_gets_buttons(tmp_path, open_orders):
    from types import SimpleNamespace as NS

    bot = _bot(tmp_path, chat_id="111")
    sent = []
    notifier = NS(send=lambda text, **kw: sent.append(kw.get("buttons")) or 1)
    bot._dispatch(StubCtx(), notifier, {"message": {"chat": {"id": 111}, "text": "/cancel"}})
    [buttons] = sent
    assert ("▶ /cancel 000012678", "cmd:/cancel 000012678") in [row[0] for row in buttons]


def test_bot_cancel_updates_record(tmp_path, open_orders):
    from aistock.trading import proposals as P

    store = P.ProposalStore(tmp_path / "p.json")
    store.add(P.Proposal(id="x", created_at="2026-10-20T19:47:03+09:00", side="buy",
                         stock_code="TQQQ", name="TQQQ", quantity=179, limit_price=0,
                         confidence=0, reason="", status=P.ORDERED, order_no="000012678",
                         market="us", exchange="ND", price_usd=65.5))

    class Ctx(StubCtx):
        pass

    ctx = Ctx()
    ctx.store = store
    COMMANDS["cancel"](ctx, ["TQQQ"])
    assert store.get("x").status == P.CANCELLED
