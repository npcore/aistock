"""신호 생성 검증. Claude API를 호출하지 않고 스텁으로 돈다."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aistock.dart.disclosure import Disclosure
from aistock.pipeline.events import DisclosureEvent, Stage
from aistock.signal.analyzer import SignalAnalyzer, SignalError, Usage, _headline_block
from aistock.signal.handler import SignalHandler
from aistock.signal.schema import Action, DisclosureSignal, Horizon, Impact, Triage

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


def _disclosure(**overrides) -> Disclosure:
    return Disclosure.from_row({**ROW, **overrides})


def _event(stage: Stage, *, text: str | None = None, **overrides) -> DisclosureEvent:
    return DisclosureEvent(
        stage=stage,
        disclosure=_disclosure(**overrides),
        detected_at=datetime.now(KST),
        text=text,
        pblntf_ty="B",
    )


def _signal(**overrides) -> DisclosureSignal:
    base = {
        "action": Action.SELL,
        "confidence": 75,
        "impact": Impact.NEGATIVE,
        "horizon": Horizon.DAYS,
        "event_type": "유상증자결정",
        "rationale": "시가총액 대비 큰 규모의 주주배정 증자로 희석 부담이 있습니다.",
        "key_facts": ["발행금액 500억원"],
        "risks": ["자금 용도가 시설투자면 영향이 달라질 수 있음"],
    }
    return DisclosureSignal(**{**base, **overrides})


# --- 스키마 ---


def test_actionable_requires_direction_and_confidence():
    assert _signal(action=Action.SELL, confidence=70).actionable
    assert not _signal(action=Action.SELL, confidence=69).actionable
    assert not _signal(action=Action.WATCH, confidence=95).actionable
    assert not _signal(action=Action.IGNORE, confidence=99).actionable


@pytest.mark.parametrize("bad", [-1, 101])
def test_confidence_is_bounded(bad):
    """0~100 밖의 confidence는 스키마에서 막는다."""
    with pytest.raises(ValidationError):
        _signal(confidence=bad)


def test_optional_lists_default_empty():
    signal = DisclosureSignal(
        action=Action.IGNORE,
        confidence=10,
        impact=Impact.NEUTRAL,
        horizon=Horizon.UNKNOWN,
        event_type="정기보고서",
        rationale="주가와 무관합니다.",
    )
    assert signal.key_facts == [] and signal.risks == []


def test_headline_block_includes_report_name():
    block = _headline_block(_disclosure())
    assert "주요사항보고서(유상증자결정)" in block
    assert "005930" in block


def test_headline_block_marks_unlisted():
    assert "(비상장)" in _headline_block(_disclosure(stock_code=""))


# --- 사용량 집계 ---


class _FakeUsage:
    input_tokens = 1000
    output_tokens = 200
    cache_read_input_tokens = 800


def test_usage_accumulates():
    usage = Usage()
    usage.add(_FakeUsage())
    usage.add(_FakeUsage())
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (2000, 400, 1600)


def test_usage_tolerates_missing_cache_field():
    class Bare:
        input_tokens = 5
        output_tokens = 1

    usage = Usage()
    usage.add(Bare())
    assert usage.cache_read_tokens == 0


# --- 분석기 ---


class StubAnalyzer(SignalAnalyzer):
    """API를 타지 않는 분석기. _parse만 가로챈다."""

    def __init__(self, *, triage: Triage | None = None, signal: DisclosureSignal | None = None):
        self._triage = triage or Triage(
            worth_reading=True,
            event_type="유상증자결정",
            expected_impact=Impact.NEGATIVE,
            reason="자본구조에 영향",
        )
        self._signal = signal or _signal()
        self.calls: list[str] = []
        self.last_user = ""
        self.usage = Usage()
        self.max_document_chars = 100

    def _parse(self, *, system, user, schema, effort, max_tokens):
        self.calls.append(schema.__name__)
        self.last_user = user
        return self._triage if schema is Triage else self._signal


def test_analyze_rejects_empty_text():
    with pytest.raises(SignalError):
        StubAnalyzer().analyze(_disclosure(), "   ")


def test_long_document_is_truncated_with_a_warning(caplog):
    """조용히 자르면 근거가 빠진 줄 모르고 신호를 믿게 된다."""
    analyzer = StubAnalyzer()
    with caplog.at_level("WARNING"):
        analyzer.analyze(_disclosure(), "가" * 500)
    assert "원문이 길어" in caplog.text
    body = analyzer.last_user.split("## 공시 원문\n\n", 1)[1]
    assert body == "가" * 100


def test_short_document_is_not_truncated(caplog):
    analyzer = StubAnalyzer()
    with caplog.at_level("WARNING"):
        analyzer.analyze(_disclosure(), "가" * 50)
    assert caplog.text == ""
    assert analyzer.last_user.split("## 공시 원문\n\n", 1)[1] == "가" * 50


# --- 핸들러 ---


def _handler(tmp_path, **kwargs) -> SignalHandler:
    return SignalHandler(path=tmp_path / "signals.jsonl", **kwargs)


def test_headline_runs_triage_only(tmp_path):
    analyzer = StubAnalyzer()
    handler = _handler(tmp_path, analyzer=analyzer)
    handler(_event(Stage.HEADLINE))
    assert analyzer.calls == ["Triage"]
    assert handler.stats["triaged"] == 1


def test_document_is_skipped_when_triage_rejected(tmp_path):
    """비싼 건 원문 분석이라, 여기서 거른 만큼이 그대로 비용 절감이다."""
    analyzer = StubAnalyzer(
        triage=Triage(
            worth_reading=False,
            event_type="임원소유상황보고",
            expected_impact=Impact.NEUTRAL,
            reason="주가와 무관",
        )
    )
    handler = _handler(tmp_path, analyzer=analyzer)
    handler(_event(Stage.HEADLINE))
    handler(_event(Stage.DOCUMENT, text="본문"))
    assert analyzer.calls == ["Triage"]
    assert handler.stats["analyzed"] == 0
    assert handler.stats["skipped"] == 1


def test_document_is_analyzed_when_triage_passed(tmp_path):
    analyzer = StubAnalyzer()
    handler = _handler(tmp_path, analyzer=analyzer)
    handler(_event(Stage.HEADLINE))
    handler(_event(Stage.DOCUMENT, text="본문"))
    assert analyzer.calls == ["Triage", "DisclosureSignal"]
    assert handler.stats["analyzed"] == 1


def test_document_without_triage_record_is_analyzed(tmp_path):
    """재시작으로 분류 기록이 없으면 거르지 않는다 — 신호를 놓치는 쪽이 더 나쁘다."""
    analyzer = StubAnalyzer()
    handler = _handler(tmp_path, analyzer=analyzer)
    handler(_event(Stage.DOCUMENT, text="본문"))
    assert analyzer.calls == ["DisclosureSignal"]


def test_signal_is_written_to_jsonl(tmp_path):
    import json

    handler = _handler(tmp_path, analyzer=StubAnalyzer())
    handler(_event(Stage.DOCUMENT, text="본문"))
    row = json.loads((tmp_path / "signals.jsonl").read_text(encoding="utf-8").strip())
    assert row["action"] == "sell"
    assert row["stock_code"] == "005930"
    assert row["actionable"] is True
    assert row["rcept_no"] == "20260806000123"


def test_callbacks_receive_the_signal(tmp_path):
    received = []
    handler = _handler(
        tmp_path, analyzer=StubAnalyzer(), callbacks=[lambda e, s: received.append((e, s))]
    )
    handler(_event(Stage.DOCUMENT, text="본문"))
    assert len(received) == 1
    assert received[0][1].action is Action.SELL


def test_analysis_failure_does_not_propagate(tmp_path):
    """핸들러가 예외를 흘리면 수집 루프가 멈춘다."""

    class Broken(StubAnalyzer):
        def _parse(self, **kwargs):
            raise SignalError("모델 오류")

    handler = _handler(tmp_path, analyzer=Broken())
    handler(_event(Stage.DOCUMENT, text="본문"))
    assert handler.stats["failed"] == 1


def test_triage_can_be_disabled(tmp_path):
    analyzer = StubAnalyzer()
    handler = _handler(tmp_path, analyzer=analyzer, triage_enabled=False)
    handler(_event(Stage.HEADLINE))
    assert analyzer.calls == []


def test_document_without_text_is_ignored(tmp_path):
    analyzer = StubAnalyzer()
    handler = _handler(tmp_path, analyzer=analyzer)
    handler(_event(Stage.DOCUMENT, text=None))
    assert analyzer.calls == []
