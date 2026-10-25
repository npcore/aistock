from datetime import UTC, datetime, timedelta

from aistock.pipeline.state import MAX_DOCUMENT_ATTEMPTS, PollerState

ROW = {"rcept_no": "20260806000001", "corp_name": "삼성전자", "stock_code": "005930"}


def _state(tmp_path):
    return PollerState(path=tmp_path / "state.json")


def test_new_then_seen(tmp_path):
    state = _state(tmp_path)
    assert state.is_new("20260806000001")
    state.mark_seen("20260806000001")
    assert not state.is_new("20260806000001")


def test_round_trip_preserves_seen_and_pending(tmp_path):
    state = _state(tmp_path)
    state.mark_seen("20260806000001")
    state.add_pending("20260806000002", ROW)
    state.record_attempt("20260806000002")
    state.last_poll = "2026-08-06T17:00:00+09:00"
    state.save()

    restored = PollerState.load(tmp_path / "state.json")
    assert restored.seen == {"20260806000001"}
    assert restored.pending["20260806000002"].attempts == 1
    assert restored.pending["20260806000002"].row == ROW
    assert restored.last_poll == "2026-08-06T17:00:00+09:00"


def test_missing_file_starts_empty(tmp_path):
    state = PollerState.load(tmp_path / "nope.json")
    assert state.seen == set() and state.pending == {}


def test_corrupt_file_starts_empty_instead_of_crashing(tmp_path):
    """상태 파일이 깨졌다고 폴러가 못 뜨면 안 된다."""
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    assert PollerState.load(path).seen == set()


def test_save_is_atomic_leaving_no_temp_file(tmp_path):
    state = _state(tmp_path)
    state.mark_seen("20260806000001")
    state.save()
    assert not (tmp_path / "state.tmp").exists()
    assert (tmp_path / "state.json").exists()


def test_add_pending_is_idempotent(tmp_path):
    state = _state(tmp_path)
    state.add_pending("20260806000002", ROW)
    state.record_attempt("20260806000002")
    state.add_pending("20260806000002", ROW)  # 두 번째는 무시되어야 시도 횟수가 보존된다
    assert state.pending["20260806000002"].attempts == 1


def test_record_attempt_on_unknown_returns_none(tmp_path):
    assert _state(tmp_path).record_attempt("99999999999999") is None


def test_prune_drops_old_seen(tmp_path):
    state = _state(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y%m%d") + "000001"
    fresh = datetime.now(UTC).strftime("%Y%m%d") + "000002"
    state.mark_seen(old)
    state.mark_seen(fresh)
    assert state.prune() == 1
    assert state.seen == {fresh}


def test_prune_gives_up_on_exhausted_pending(tmp_path):
    """원문이 끝내 안 올라오는 건이 큐에 영원히 남으면 안 된다."""
    state = _state(tmp_path)
    rcept_no = datetime.now(UTC).strftime("%Y%m%d") + "000003"
    state.add_pending(rcept_no, ROW)
    for _ in range(MAX_DOCUMENT_ATTEMPTS):
        state.record_attempt(rcept_no)
    assert state.pending[rcept_no].exhausted
    state.prune()
    assert rcept_no not in state.pending


def test_prune_keeps_pending_still_within_budget(tmp_path):
    state = _state(tmp_path)
    rcept_no = datetime.now(UTC).strftime("%Y%m%d") + "000004"
    state.add_pending(rcept_no, ROW)
    state.record_attempt(rcept_no)
    state.prune()
    assert rcept_no in state.pending
