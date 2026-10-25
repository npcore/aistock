"""알림 검증. 텔레그램 API를 호출하지 않고 돈다."""

from datetime import datetime, timedelta, timezone

import pytest

from aistock.dart.disclosure import Disclosure
from aistock.notify.format import headline_message, signal_message, watchlist_digest
from aistock.notify.handler import TelegramSignalHandler
from aistock.notify.telegram import MAX_TEXT, escape
from aistock.pipeline.events import DisclosureEvent, Stage
from aistock.signal.schema import Action, DisclosureSignal, Horizon, Impact
from aistock.watchlist import Watchlist

KST = timezone(timedelta(hours=9))

ROW = {
    "corp_cls": "Y",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "주요사항보고서(유상증자결정)",
    "rcept_no": "20260806000123",
    "flr_nm": "삼성전자",
    "rcept_dt": "20260806",
    "rm": "",
}


def _event(stage=Stage.DOCUMENT, **overrides) -> DisclosureEvent:
    return DisclosureEvent(
        stage=stage,
        disclosure=Disclosure.from_row({**ROW, **overrides}),
        detected_at=datetime.now(KST),
        text="본문",
        pblntf_ty="B",
    )


def _signal(**overrides) -> DisclosureSignal:
    base = {
        "action": Action.SELL,
        "confidence": 74,
        "impact": Impact.NEGATIVE,
        "horizon": Horizon.DAYS,
        "event_type": "유상증자결정",
        "rationale": "시가총액 대비 큰 규모의 증자로 희석 부담이 있습니다.",
        "key_facts": ["발행금액 500억원", "기존 주식수 대비 12.3%"],
        "risks": ["자금 용도가 시설투자면 영향이 달라질 수 있음"],
    }
    return DisclosureSignal(**{**base, **overrides})


class StubNotifier:
    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self.fail = fail

    def send(self, text, *, preview=False):
        if self.fail:
            from aistock.notify.telegram import TelegramError

            raise TelegramError("전송 실패")
        self.messages.append(text)


# --- 이스케이프 ---


@pytest.mark.parametrize(
    "raw,expected",
    [("A & B", "A &amp; B"), ("<주>한국", "&lt;주&gt;한국"), ("정상", "정상")],
)
def test_escape_protects_html_parse_mode(raw, expected):
    """회사명에 <, >, &가 들어가면 메시지 전체가 400으로 거절된다."""
    assert escape(raw) == expected


# --- 메시지 조립 ---


def test_signal_message_carries_the_decision_basis():
    """사람이 이 메시지만 보고 판단하므로 근거가 다 들어가야 한다."""
    msg = signal_message(_event(), _signal())
    assert "매도" in msg and "74%" in msg
    assert "삼성전자" in msg and "005930" in msg
    assert "희석 부담" in msg
    assert "발행금액 500억원" in msg  # key_facts
    assert "자금 용도가 시설투자면" in msg  # risks
    assert "dsaf001/main.do?rcpNo=20260806000123" in msg  # 원문 링크


def test_signal_message_includes_quote_when_available():
    msg = signal_message(
        _event(), _signal(), quote={"cur_prc": "-246000", "flu_rt": "-2.50", "trde_qty": "1234567"}
    )
    assert "246000" in msg
    assert "▼" in msg
    assert "1,234,567" in msg


def test_signal_message_survives_missing_quote():
    assert "삼성전자" in signal_message(_event(), _signal(), quote=None)


def test_signal_message_escapes_company_name():
    msg = signal_message(_event(corp_name="<주>테스트&컴퍼니"), _signal())
    assert "&lt;주&gt;테스트&amp;컴퍼니" in msg
    assert "<주>" not in msg


def test_watchlist_note_is_shown():
    msg = signal_message(_event(), _signal(), note="반도체 대장주")
    assert "반도체 대장주" in msg


def test_headline_message_marks_analysis_pending():
    """원문 전 속보이므로 분석이 아직임을 밝혀야 오해가 없다."""
    msg = headline_message(_event(Stage.HEADLINE))
    assert "속보" in msg
    assert "주요사항보고서(유상증자결정)" in msg
    assert "원문 확보 후" in msg


def test_watchlist_digest_lists_each_stock():
    msg = watchlist_digest(
        [
            {
                "code": "005930",
                "name": "삼성전자",
                "quote": {"cur_prc": "+246000", "flu_rt": "+2.5"},
            },
            {"code": "035720", "name": "카카오", "quote": {}},
        ]
    )
    assert "005930" in msg and "035720" in msg
    assert "삼성전자" in msg and "카카오" in msg


# --- 핸들러 ---


def test_handler_sends_directional_signals():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub)
    h(_event(), _signal())
    assert h.stats["sent"] == 1
    assert len(stub.messages) == 1


@pytest.mark.parametrize("action", [Action.WATCH, Action.IGNORE])
def test_handler_filters_non_directional_by_default(action):
    """하루 수백 건이라 필터 없이 켜면 알림이 쏟아져 중요한 걸 놓친다."""
    stub = StubNotifier()
    h = TelegramSignalHandler(stub)
    h(_event(), _signal(action=action))
    assert h.stats["sent"] == 0 and h.stats["filtered"] == 1


def test_handler_filters_by_confidence():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub, min_confidence=80)
    h(_event(), _signal(confidence=74))
    assert h.stats["filtered"] == 1


def test_handler_can_include_watch_signals():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub, actions=(Action.BUY, Action.SELL, Action.WATCH))
    h(_event(), _signal(action=Action.WATCH))
    assert h.stats["sent"] == 1


def test_send_failure_does_not_raise():
    """전송 실패가 수집 루프를 멈추면 안 된다."""
    h = TelegramSignalHandler(StubNotifier(fail=True))
    h(_event(), _signal())
    assert h.stats["failed"] == 1


def test_headlines_are_off_by_default():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub)
    h.on_disclosure(_event(Stage.HEADLINE))
    assert stub.messages == []


def test_headlines_can_be_enabled():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub, send_headlines=True)
    h.on_disclosure(_event(Stage.HEADLINE))
    assert len(stub.messages) == 1


def test_document_stage_is_not_sent_as_headline():
    stub = StubNotifier()
    h = TelegramSignalHandler(stub, send_headlines=True)
    h.on_disclosure(_event(Stage.DOCUMENT))
    assert stub.messages == []


def test_long_message_is_truncated_with_a_marker(tmp_path):
    from aistock.notify.telegram import TelegramConfig, TelegramNotifier

    n = TelegramNotifier(TelegramConfig("t", "c"))
    captured = {}

    class FakeClient:
        def post(self, url, json):
            captured["text"] = json["text"]

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"ok": True}

            return R()

    n._client = FakeClient()
    n.send("가" * (MAX_TEXT + 500))
    assert len(captured["text"]) <= MAX_TEXT + 40
    assert "잘림" in captured["text"]


# --- 관심종목 ---


def test_watchlist_round_trip(tmp_path):
    path = tmp_path / "watchlist.txt"
    wl = Watchlist([], path)
    assert wl.add("005930", "삼성전자")
    assert wl.add("035720", "카카오")
    wl.save()

    loaded = Watchlist.load(path)
    assert loaded.codes == ["005930", "035720"]
    assert loaded.note_for("035720") == "카카오"


def test_watchlist_rejects_duplicates(tmp_path):
    wl = Watchlist([], tmp_path / "w.txt")
    assert wl.add("005930")
    assert not wl.add("005930")
    assert len(wl) == 1


def test_watchlist_preserves_order(tmp_path):
    """사용자가 적은 순서가 화면 순서다."""
    path = tmp_path / "w.txt"
    path.write_text("035720  # 카카오\n005930  # 삼성전자\n", encoding="utf-8")
    assert Watchlist.load(path).codes == ["035720", "005930"]


def test_watchlist_strips_exchange_suffix(tmp_path):
    wl = Watchlist([], tmp_path / "w.txt")
    wl.add("005930_AL")
    assert wl.codes == ["005930"]


def test_watchlist_skips_malformed_lines(tmp_path, caplog):
    path = tmp_path / "w.txt"
    path.write_text("005930\n# 주석만\nAAPL\n12345\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        wl = Watchlist.load(path)
    assert wl.codes == ["005930"]
    assert "종목코드 형식이 아니라" in caplog.text


def test_watchlist_remove(tmp_path):
    wl = Watchlist([], tmp_path / "w.txt")
    wl.add("005930")
    assert wl.remove("005930")
    assert not wl.remove("005930")
    assert len(wl) == 0


def test_missing_watchlist_file_is_empty(tmp_path):
    assert len(Watchlist.load(tmp_path / "none.txt")) == 0
