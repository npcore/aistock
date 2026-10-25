"""경제 봇 검증. Claude·키움·텔레그램을 호출하지 않는다."""

from types import SimpleNamespace

import pytest

from aistock import advisor as A
from aistock.advisor import Advisor, to_telegram_html


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def _resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


class FakeClient:
    """미리 정한 응답을 차례로 돌려주고, 받은 요청을 기록한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


def _advisor(tmp_path, *responses):
    return Advisor(None, client=FakeClient(*responses), path=tmp_path / "chat.json")


def test_plain_answer_is_saved_to_history(tmp_path):
    adv = _advisor(tmp_path, _resp("end_turn", _text("지금은 기다리세요.")))
    assert adv.ask("TQQQ 살까?") == "지금은 기다리세요."
    assert adv.history == [
        {"role": "user", "content": "TQQQ 살까?"},
        {"role": "assistant", "content": "지금은 기다리세요."},
    ]
    # 재시작해도 이어진다
    assert Advisor(None, client=FakeClient(), path=tmp_path / "chat.json").history == adv.history


def test_context_block_sent_but_not_stored(tmp_path):
    """시세 맥락은 매 턴 새로 붙인다. 기록에 남기면 오래된 시각·장 상태가 쌓인다."""
    adv = _advisor(tmp_path, _resp("end_turn", _text("ok")))
    adv.ask("질문")
    sent = adv.client.calls[0]["messages"][-1]["content"]
    assert sent.startswith("[지금]") and sent.endswith("질문")
    assert adv.history[0]["content"] == "질문"


def test_tool_loop_runs_tools_and_returns_final_text(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "market_data", lambda k, s: {"symbol": s, "price": 68.21})
    adv = _advisor(
        tmp_path,
        _resp("tool_use", _text("확인해볼게요"), _tool("get_market_data", {"symbol": "TQQQ"})),
        _resp("end_turn", _text("**68달러**에 있습니다.")),
    )
    assert adv.ask("TQQQ 어때?") == "**68달러**에 있습니다."
    second = adv.client.calls[1]["messages"]
    [result] = second[-1]["content"]
    assert result["type"] == "tool_result" and '"price": 68.21' in result["content"]


def test_tool_error_is_reported_to_model_not_raised(tmp_path, monkeypatch):
    def boom(k, s):
        raise RuntimeError("키움 점검 중")

    monkeypatch.setattr(A, "market_data", boom)
    adv = _advisor(
        tmp_path,
        _resp("tool_use", _tool("get_market_data", {"symbol": "TQQQ"})),
        _resp("end_turn", _text("지금은 시세를 못 봤습니다.")),
    )
    assert "못 봤습니다" in adv.ask("TQQQ?")
    [result] = adv.client.calls[1]["messages"][-1]["content"]
    assert result["is_error"] and "키움 점검 중" in result["content"]


def test_pause_turn_resumes_without_extra_user_message(tmp_path):
    """웹 검색이 길면 pause_turn으로 끊긴다. 'continue' 같은 메시지를 넣지 않고 그대로 다시 보낸다."""
    adv = _advisor(
        tmp_path,
        _resp("pause_turn", SimpleNamespace(type="server_tool_use")),
        _resp("end_turn", _text("뉴스를 보니…")),
    )
    assert adv.ask("최근 뉴스?") == "뉴스를 보니…"
    resumed = adv.client.calls[1]["messages"]
    assert resumed[-1]["role"] == "assistant"


def test_save_note_persists_and_appears_in_context(tmp_path):
    adv = _advisor(
        tmp_path,
        _resp("tool_use", _tool("save_note", {"note": "TQQQ를 $81에 전량 매도"})),
        _resp("end_turn", _text("기억해둘게요.")),
        _resp("end_turn", _text("81달러 기준으로 보면…")),
    )
    adv.ask("나 TQQQ 81불에 팔았어")
    assert adv.notes and "$81" in adv.notes[0]
    adv.ask("다시 살까?")
    assert "[메모]" in adv.client.calls[2]["messages"][-1]["content"]
    assert Advisor(None, client=FakeClient(), path=tmp_path / "chat.json").notes == adv.notes


def test_history_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "MAX_TURNS", 2)
    adv = _advisor(tmp_path, *[_resp("end_turn", _text(f"a{i}")) for i in range(3)])
    for i in range(3):
        adv.ask(f"q{i}")
    assert [m["content"] for m in adv.history] == ["q1", "a1", "q2", "a2"]


def test_refusal_returns_message(tmp_path):
    adv = _advisor(tmp_path, _resp("refusal"))
    assert "답하지 못했습니다" in adv.ask("?")


def test_no_order_tool_exposed():
    """경제 봇은 의견만 낸다. 주문 도구가 끼어들면 대화 중 오해로 주문이 나갈 수 있다."""
    names = {t["name"] for t in A.TOOLS}
    assert names == {"get_market_data", "get_account", "save_note", "web_search"}


def test_request_uses_current_model_with_fallbacks(tmp_path):
    adv = _advisor(tmp_path, _resp("end_turn", _text("ok")))
    adv.ask("?")
    call = adv.client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and A.FALLBACK_BETA in call["betas"]


# --- 텔레그램 표시 ---


def test_html_escapes_and_bolds():
    assert to_telegram_html("**매수** 구간 <60달러 & 대기") == "<b>매수</b> 구간 &lt;60달러 &amp; 대기"


def test_html_strips_headings():
    assert to_telegram_html("## 결론\n기다림") == "결론\n기다림"


# --- 봇 연결 ---


def test_bot_rejects_second_question_while_busy(tmp_path):
    from aistock.bot import TelegramBot
    from aistock.notify.telegram import TelegramConfig

    bot = TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    bot._advisor = SimpleNamespace(ask=lambda q: "답")
    sent = []
    notifier = SimpleNamespace(send=lambda text, **kw: sent.append(text) or 1)
    bot._advisor_busy.acquire()
    bot._ask(SimpleNamespace(kiwoom=None), notifier, "두 번째 질문")
    assert "분석 중" in sent[-1]


@pytest.mark.parametrize("text", ["TQQQ 언제 살까?", "안녕"])
def test_bot_routes_free_text_to_advisor(tmp_path, text):
    from aistock.bot import TelegramBot
    from aistock.notify.telegram import TelegramConfig

    bot = TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    asked = []
    bot._ask = lambda ctx, n, q: asked.append(q)
    update = {"message": {"chat": {"id": 111}, "text": text}}
    bot._dispatch(SimpleNamespace(), SimpleNamespace(), update)
    assert asked == [text]


def test_advisor_knows_cancel_and_sees_open_orders():
    """'키움 앱에서 취소하세요'라고 하거나 미체결을 추측하던 문제 (2026-10-21)."""
    assert "/cancel" in A.SYSTEM and "open_orders" in A.SYSTEM
    account_tool = next(t for t in A.TOOLS if t["name"] == "get_account")
    assert "미체결" in account_tool["description"]


# --- 답 속 명령 → 버튼 ---


ANSWER = """**계획**
• 1차 /buy TQQQ 54 71.5 (약 $3,861)
• 2차 /buy TQQQ 59 $65.50
• 기존 주문은 /cancel TQQQ 로 정리
• 전부 사려면 /buy TQQQ 전량 65
• 국내는 /buy 005930 10 230000
• 다시 /buy TQQQ 54 71.5"""


def test_extract_commands():
    assert A.extract_commands(ANSWER) == [
        "/buy TQQQ 54 71.5",
        "/buy TQQQ 59 65.50",
        "/cancel TQQQ",
        "/buy TQQQ 전량 65",
        "/buy 005930 10 230000",
    ]


def test_commands_become_tap_to_copy_code():
    """그냥 두면 텔레그램이 '/buy'까지만 링크로 잡아, 누르면 인자 없는 /buy가 간다."""
    html = to_telegram_html("1차 /buy TQQQ 54 71.5 로 시작")
    assert "<code>/buy TQQQ 54 71.5</code>" in html


def test_buttons_us_run_and_copy_kr_copy_only():
    from aistock.bot import command_buttons

    rows = command_buttons(["/buy TQQQ 54 71.5", "/buy 005930 10 230000", "/cancel TQQQ"])
    us, kr, cancel = rows
    assert us[0] == ("▶ /buy TQQQ 54 71.5", "cmd:/buy TQQQ 54 71.5")
    assert us[1]["copy_text"]["text"] == "/buy TQQQ 54 71.5"
    # 국내 /buy는 확인 없이 즉시 체결되는 경로 — 실행 버튼을 만들지 않는다
    assert len(kr) == 1 and kr[0]["copy_text"]["text"] == "/buy 005930 10 230000"
    assert cancel[0][1] == "cmd:/cancel TQQQ"


def _bot_with_ctx(tmp_path, monkeypatch):
    from aistock.bot import TelegramBot
    from aistock.notify.telegram import TelegramConfig
    from aistock.trading import us_trader
    from aistock.trading.policy import TradingPolicy

    bot = TelegramBot(TelegramConfig("t", "111"), kiwoom=None, offset_path=tmp_path / "o")
    made = []
    monkeypatch.setattr(us_trader, "manual_proposal", lambda *a, **kw: made.append(kw))
    ctx = SimpleNamespace(kiwoom=None, notifier=None, store=None, policy=TradingPolicy())
    answers, sent = [], []
    notifier = SimpleNamespace(
        answer_callback=lambda cid, text="": answers.append(text),
        send=lambda text, **kw: sent.append(text) or 1,
        edit=lambda *a, **kw: None,
    )
    return bot, ctx, notifier, made, answers


def _press(bot, ctx, notifier, data, user=111):
    q = {"id": "q", "from": {"id": user}, "message": {"chat": {"id": user}}, "data": data}
    bot._dispatch(ctx, notifier, {"callback_query": q})


def test_run_button_creates_confirmation_not_order(tmp_path, monkeypatch):
    bot, ctx, notifier, made, answers = _bot_with_ctx(tmp_path, monkeypatch)
    _press(bot, ctx, notifier, "cmd:/buy TQQQ 54 71.5")
    assert made == [{"side": "buy", "symbol": "TQQQ", "quantity": 54, "price": 71.5}]
    assert "[승인]" in answers[0]


def test_forged_kr_buy_button_refused(tmp_path, monkeypatch):
    """버튼 데이터는 위조될 수 있다. 국내 즉시 주문은 버튼 경로로 절대 실행하지 않는다."""
    from aistock import bot as B

    bot, ctx, notifier, made, answers = _bot_with_ctx(tmp_path, monkeypatch)
    monkeypatch.setitem(B.COMMANDS, "buy", lambda *a: pytest.fail("국내 주문이 실행됐다"))
    _press(bot, ctx, notifier, "cmd:/buy 005930 10 230000")
    assert "실행할 수 없습니다" in answers[0]


def test_command_button_from_stranger_ignored(tmp_path, monkeypatch):
    bot, ctx, notifier, made, answers = _bot_with_ctx(tmp_path, monkeypatch)
    _press(bot, ctx, notifier, "cmd:/buy TQQQ 54 71.5", user=999)
    assert not made and not answers


def test_long_command_gets_copy_only():
    """callback_data는 64바이트까지. 넘으면 실행 버튼 없이 복사만."""
    from aistock.bot import command_buttons

    cmd = "/buy " + "A" * 10 + " 전량 " + "9" * 60
    [row] = command_buttons([cmd])
    assert len(row) == 1 and "copy_text" in row[0]
