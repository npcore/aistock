"""시장을 크게 흔드는 미국 경제 일정 — 이 앞에서는 자동 매수 제안을 보류한다.

    FOMC 금리 결정   둘째 날 14:00 ET  (federalreserve.gov/monetarypolicy/fomccalendars.htm)
    CPI 소비자물가    08:30 ET         (bls.gov/schedule/news_release/cpi.htm)
    고용보고서(NFP)   08:30 ET         (bls.gov/schedule/news_release/empsit.htm)

3배 레버리지는 발표 직후 하루 5~10%가 움직인다. 결과를 모르는 채 전액을 사는 것은
규칙(추세·눌림)이 아니라 동전 던지기라, **발표 전 거래일 0시부터 발표 30분 뒤까지** 매수
제안을 내지 않는다. 매도(손절·익절·추세 이탈)는 막지 않는다.

    FOMC (화·수, 수 14:00)  → 화요일 하루 + 수요일 14:30까지
    CPI·고용 (08:30, 개장 전) → 전 거래일 하루 (발표 당일 10:00 점검 때는 이미 결과가 나와 있다)

2026-10-25에 위 공식 일정표에서 옮겼다. 2027 FOMC는 '직전 회의에서 확정'이라 바뀔 수 있다.
연도가 바뀌기 전에 갱신한다 — 목록이 끝나면 경고를 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..market_hours import NY, is_us_trading_day

logger = logging.getLogger(__name__)

HOLD_AFTER = timedelta(minutes=30)  # 발표 직후 첫 반응은 넘기고

FOMC = [  # 결정 발표일 (회의 둘째 날)
    "20261028",
    "20261209",
    "20270127",
    "20270317",
    "20270428",
    "20270609",
    "20270728",
    "20270915",
    "20271027",
    "20271208",
]
CPI = [
    "20261110",
    "20261210",
    "20270113",
    "20270211",
    "20270310",
    "20270413",
    "20270512",
    "20270610",
    "20270714",
    "20270811",
    "20270914",
    "20271014",
    "20271110",
    "20271210",
]
JOBS = [
    "20261106",
    "20261204",
    "20270108",
    "20270205",
    "20270305",
    "20270402",
    "20270507",
    "20270604",
    "20270702",
    "20270806",
    "20270903",
    "20271008",
    "20271105",
    "20271203",
]


@dataclass(frozen=True)
class Event:
    name: str
    at: datetime  # 발표 시각 (뉴욕)

    @property
    def label(self) -> str:
        return f"{self.name} {self.at:%m/%d %H:%M} ET"


def _events() -> list[Event]:
    out = []
    for name, days, hhmm in (
        ("FOMC 금리 결정", FOMC, time(14, 0)),
        ("CPI 발표", CPI, time(8, 30)),
        ("고용보고서", JOBS, time(8, 30)),
    ):
        for d in days:
            day = date(int(d[:4]), int(d[4:6]), int(d[6:]))
            out.append(Event(name, datetime.combine(day, hhmm, tzinfo=NY)))
    return sorted(out, key=lambda e: e.at)


EVENTS = _events()


def _prev_trading_day(day: datetime) -> datetime:
    d = day - timedelta(days=1)
    while not is_us_trading_day(d.replace(hour=12)):
        d -= timedelta(days=1)
    return d


def hold_window(e: Event) -> tuple[datetime, datetime]:
    start = _prev_trading_day(e.at).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, e.at + HOLD_AFTER


def active_event(now: datetime, events: list[Event] | None = None) -> Event | None:
    """지금이 보류 구간이면 그 이벤트."""
    now = now.astimezone(NY)
    events = EVENTS if events is None else events
    if events and now > events[-1].at:
        logger.warning("경제 일정 목록이 끝났습니다. trading/events.py를 갱신하세요.")
    for e in events:
        start, end = hold_window(e)
        if start <= now <= end:
            return e
    return None


def upcoming(now: datetime, days: int = 7, events: list[Event] | None = None) -> list[Event]:
    """앞으로 days일 안의 일정 (브리핑 표시용)."""
    now = now.astimezone(NY)
    until = now + timedelta(days=days)
    return [e for e in (EVENTS if events is None else events) if now <= e.at <= until]
