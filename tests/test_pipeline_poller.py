"""폴러 동작 검증. 네트워크 없이 가짜 DartClient로 돌린다."""

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from aistock.pipeline.poller import DisclosurePoller
from aistock.pipeline.state import PollerState

KST = timezone(timedelta(hours=9))


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _row(seq: int, *, corp="삼성전자", stock="005930", report="주요사항보고서(유상증자결정)"):
    return {
        "corp_cls": "Y",
        "corp_name": corp,
        "corp_code": "00126380",
        "stock_code": stock,
        "report_nm": report,
        "rcept_no": f"{_today()}{seq:06d}",
        "flr_nm": corp,
        "rcept_dt": _today(),
        "rm": "",
    }


class FakeDart:
    """get_json / get_bytes만 흉내내는 가짜 클라이언트.

    docs에 없는 접수번호는 '원문 아직 안 올라옴'(014)으로 응답한다.
    """

    def __init__(self, rows, docs=None):
        self.rows = rows
        self.docs = docs or {}
        self.requests_made = 0
        self.list_calls = 0  # 목록 조회
        self.doc_calls = 0  # 원문 조회

    def get_json(self, path, params=None):
        self.requests_made += 1
        self.list_calls += 1
        return {
            "status": "000",
            "list": self.rows,
            "total_page": 1,
            "page_no": 1,
        }

    def get_bytes(self, path, params=None):
        from aistock.dart.errors import DartError

        self.requests_made += 1
        self.doc_calls += 1
        rcept_no = (params or {}).get("rcept_no")
        if rcept_no not in self.docs:
            raise DartError("014")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("doc.xml", self.docs[rcept_no].encode())
        return buffer.getvalue()


def _poller(client, tmp_path, **kwargs):
    kwargs.setdefault("types", ("B",))
    return DisclosurePoller(client, state=PollerState(path=tmp_path / "state.json"), **kwargs)


def test_new_disclosure_emits_headline(tmp_path):
    client = FakeDart([_row(1)])
    events = _poller(client, tmp_path).poll_once()
    headlines = [e for e in events if e.stage.value == "headline"]
    assert len(headlines) == 1
    assert "삼성전자(005930)" in headlines[0].headline
    assert headlines[0].pblntf_ty == "B"


def test_same_disclosure_is_not_emitted_twice(tmp_path):
    client = FakeDart([_row(1)])
    poller = _poller(client, tmp_path)
    assert len(poller.poll_once()) >= 1
    assert poller.poll_once() == []


def test_document_arrives_on_a_later_cycle(tmp_path):
    """1사이클: 원문 없음 → HEADLINE만. 2사이클: 원문 도착 → DOCUMENT."""
    row = _row(1)
    client = FakeDart([row])
    poller = _poller(client, tmp_path)

    first = poller.poll_once()
    assert [e.stage.value for e in first] == ["headline"]
    assert row["rcept_no"] in poller.state.pending

    client.docs[row["rcept_no"]] = "<p>유상증자 결정</p>"
    second = poller.poll_once()
    assert [e.stage.value for e in second] == ["document"]
    assert second[0].text == "유상증자 결정"
    assert row["rcept_no"] not in poller.state.pending


def test_document_available_immediately_emits_both_stages(tmp_path):
    row = _row(1)
    client = FakeDart([row], docs={row["rcept_no"]: "<p>본문</p>"})
    stages = [e.stage.value for e in _poller(client, tmp_path).poll_once()]
    assert stages == ["headline", "document"]


def test_pending_survives_restart(tmp_path):
    """프로세스가 죽어도 원문 대기 건을 잃으면 안 된다."""
    row = _row(1)
    client = FakeDart([row])
    _poller(client, tmp_path).poll_once()

    revived = DisclosurePoller(
        FakeDart([row], docs={row["rcept_no"]: "<p>본문</p>"}),
        state=PollerState.load(tmp_path / "state.json"),
        types=("B",),
    )
    assert [e.stage.value for e in revived.poll_once()] == ["document"]


def test_stock_filter_excludes_other_companies(tmp_path):
    client = FakeDart([_row(1), _row(2, corp="카카오", stock="035720")])
    events = _poller(client, tmp_path, stock_codes=["005930"]).poll_once()
    assert {e.disclosure.corp_name for e in events} == {"삼성전자"}


def test_bootstrap_marks_seen_without_emitting(tmp_path):
    client = FakeDart([_row(1), _row(2)])
    poller = _poller(client, tmp_path)
    assert poller.bootstrap() == 2
    assert poller.poll_once() == []


def test_handler_failure_does_not_stop_the_loop(tmp_path):
    delivered = []

    def broken(event):
        raise RuntimeError("핸들러 폭발")

    client = FakeDart([_row(1)])
    poller = _poller(client, tmp_path, handlers=[broken, delivered.append])
    poller.poll_once()
    assert len(delivered) == 1


def test_types_none_queries_once_without_type(tmp_path):
    """--all-types: 목록 조회는 1회로 줄지만 유형은 알 수 없다."""
    client = FakeDart([_row(1)])
    events = _poller(client, tmp_path, types=None).poll_once()
    assert client.list_calls == 1
    assert events[0].pblntf_ty is None


def test_each_type_costs_a_list_request(tmp_path):
    """유형 4개를 보면 목록 조회도 4배가 된다. 일 한도 계산의 근거."""
    client = FakeDart([_row(1)])
    _poller(client, tmp_path, types=("B", "C", "D", "I")).poll_once()
    assert client.list_calls == 4


def test_pending_document_costs_one_request_per_cycle(tmp_path):
    """원문 재시도도 한도를 먹는다. 목록 조회와 별개로 센다."""
    client = FakeDart([_row(1)])
    poller = _poller(client, tmp_path)
    poller.poll_once()
    assert client.doc_calls == 1
    poller.poll_once()  # 아직 원문 없음 → 또 한 번
    assert client.doc_calls == 2


def test_requests_counter_tracks_client_not_items(tmp_path):
    """한 번의 조회로 100건이 와도 요청은 1건이다."""
    client = FakeDart([_row(i) for i in range(1, 101)])
    poller = _poller(client, tmp_path)
    poller.poll_once()
    assert poller.requests_made == client.requests_made


@pytest.mark.parametrize("stage_index,expected", [(0, "headline"), (1, "document")])
def test_event_serialization_round_trip(tmp_path, stage_index, expected):
    row = _row(1)
    client = FakeDart([row], docs={row["rcept_no"]: "<p>본문</p>"})
    events = _poller(client, tmp_path).poll_once()
    payload = events[stage_index].to_dict()
    assert payload["stage"] == expected
    assert payload["rcept_no"] == row["rcept_no"]
    assert payload["stock_code"] == "005930"
