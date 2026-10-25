"""장 운영시간 판정.

시간별 알림이 휴장일이나 장 마감 후에 계속 울리면 알림 자체를 무시하게 된다.
그래서 '요일·시각'과 '실제 거래일인지'를 나눠서 본다 — 앞은 계산으로 알 수 있지만
공휴일은 달력 없이 알 수 없어 일봉에 오늘 날짜가 있는지로 확인한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .client import KiwoomClient
from .errors import KiwoomError
from .market import domestic

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

OPEN = time(9, 0)
CLOSE = time(15, 30)

# 휴장일 판정용 기준 종목. 거래정지 가능성이 사실상 없는 대형주를 쓴다.
PROBE_CODE = "005930"


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday(at: datetime | None = None) -> bool:
    return (at or now_kst()).weekday() < 5


def in_session(at: datetime | None = None) -> bool:
    """정규장 시간대인가. 공휴일 여부는 보지 않는다."""
    at = at or now_kst()
    return is_weekday(at) and OPEN <= at.time() <= CLOSE


def is_trading_day(client: KiwoomClient, at: datetime | None = None) -> bool:
    """오늘이 실제 거래일인가.

    일봉에 오늘 날짜 봉이 있으면 장이 열린 것이다. 공휴일 달력을 따로 두지 않아도 되고,
    임시 휴장에도 자동으로 맞는다. 조회에 실패하면 '열렸다'로 보고 넘어간다 —
    조회 실패 때문에 알림이 통째로 멈추는 쪽이 더 나쁘다.
    """
    at = at or now_kst()
    if not is_weekday(at):
        return False
    today = at.strftime("%Y%m%d")
    try:
        rows = domestic.daily_chart(client, PROBE_CODE, today, max_pages=1, warn_on_truncate=False)
    except KiwoomError as exc:
        logger.warning("거래일 확인 실패, 장중으로 간주합니다: %s", exc)
        return True
    return bool(rows) and rows[0].get("dt") == today


def next_tick(interval_minutes: int, at: datetime | None = None) -> datetime:
    """다음 알림 시각. 정각에 맞춰 떨어지도록 올림한다.

    60분이면 10:00, 11:00처럼 정각에 오고, 30분이면 10:00, 10:30에 온다.
    """
    at = at or now_kst()
    if interval_minutes <= 0:
        raise ValueError("interval_minutes는 1 이상이어야 합니다.")
    minutes = at.hour * 60 + at.minute
    next_minutes = ((minutes // interval_minutes) + 1) * interval_minutes
    base = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=next_minutes)


# --- 미국장 ---

NY = ZoneInfo("America/New_York")
US_OPEN = time(9, 30)
US_CLOSE = time(16, 0)


def us_now() -> datetime:
    return datetime.now(NY)


def in_us_session(at: datetime | None = None) -> bool:
    """미국 정규장(09:30~16:00 ET) 시간대인가.

    한국 시각으로는 서머타임에 따라 22:30~05:00 또는 23:30~06:00이라 고정 시각으로는
    판정할 수 없다. 뉴욕 현지 시각으로 보면 서머타임이 자동으로 처리된다.
    미국 공휴일(추수감사절 등)은 여기서 걸러지지 않는다 — 그건 시세 응답으로 판단한다.
    """
    ny = (at or now_kst()).astimezone(NY)
    return ny.weekday() < 5 and US_OPEN <= ny.time() <= US_CLOSE


# NYSE 휴장일. 휴장일에도 in_us_session()은 True라서, 이걸 안 보면 휴장일 밤에 매매 제안이 나가고
# 승인하면 주문이 거부된다. **매년 갱신해야 한다** — 목록에 없는 해는 경고를 남기고 거래일로 본다.
# 출처: NYSE Holidays & Trading Hours (nyse.com/markets/hours-calendars)
US_HOLIDAYS = {
    # 2026
    "20260101",
    "20260119",
    "20260216",
    "20260403",
    "20260525",
    "20260619",
    "20260703",
    "20260907",
    "20261126",
    "20261225",
    # 2027
    "20270101",
    "20270118",
    "20270215",
    "20270326",
    "20270531",
    "20270618",
    "20270705",
    "20270906",
    "20271125",
    "20271224",
}
_US_HOLIDAY_YEARS = {d[:4] for d in US_HOLIDAYS}


def is_us_trading_day(at: datetime | None = None) -> bool:
    """뉴욕 날짜 기준 거래일인가 (주말·NYSE 휴장일 제외)."""
    ny = (at or now_kst()).astimezone(NY)
    if ny.weekday() >= 5:
        return False
    day = ny.strftime("%Y%m%d")
    if day[:4] not in _US_HOLIDAY_YEARS:
        logger.warning(
            "%s년 미국 휴장일 목록이 없습니다. market_hours.US_HOLIDAYS를 갱신하세요.", day[:4]
        )
    return day not in US_HOLIDAYS


def us_session_label(at: datetime | None = None) -> str:
    ny = (at or now_kst()).astimezone(NY)
    return f"{ny:%m/%d %H:%M} ET"


def session_bounds(at: datetime | None = None) -> tuple[datetime, datetime]:
    at = at or now_kst()
    day = at.replace(second=0, microsecond=0)
    return day.replace(hour=OPEN.hour, minute=OPEN.minute), day.replace(
        hour=CLOSE.hour, minute=CLOSE.minute
    )
