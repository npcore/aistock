"""반자동 매매 한도. 전부 .env로 조정한다.

기본값은 보수적으로 잡았다. 실계좌라 처음엔 작게 돌려보고 올리는 쪽이 맞다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..config import load_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingPolicy:
    kr_enabled: bool = True  # 국내 주문·공시 제안·국내 손절익절 (추가 계정은 보통 끈다)
    budget_per_trade: int = 1_000_000  # 매수 1건 예산(원)
    min_confidence: int = 70  # 이 미만 신호는 제안하지 않는다
    max_daily_buys: int = 3  # 하루 국내 매수 제안 상한 (승인 대기·주문된 것만 센다)
    proposal_ttl_min: int = 10  # 이 시간이 지나면 승인 버튼을 눌러도 주문하지 않는다
    max_drift_pct: float = 2.0  # 제안가 대비 현재가가 이만큼 오르면(매수) 주문하지 않는다
    stop_loss_pct: float = 5.0  # 보유 수익률이 -이 값 이하면 매도 제안. 0이면 끔
    take_profit_pct: float = 10.0  # 보유 수익률이 이 값 이상이면 매도 제안. 0이면 끔
    exit_check_min: int = 1  # 보유 종목(국내) 점검 주기(분) — 실시간 제안
    max_gap_pct: float = 5.0  # 장 마감 후 신호: 다음 장에서 이만큼 갭상승했으면 매수 안 함
    deferred_max_days: int = 3  # 장 마감 후 신호를 이 날수 넘게 들고 있지 않는다
    release_after: str = "09:05"  # 장 마감 후 신호를 재평가하는 시각 (동시호가 직후는 피한다)

    # --- 미국 (TQQQ 등). 3배 레버리지는 하루 5~10% 움직이므로 국내보다 기준을 넓게 잡았다 ---
    us_symbols: tuple[str, ...] = field(default_factory=lambda: ("TQQQ",))
    us_budget_usd: float = 0.0  # 매수 제안 1건 예산(USD). 0이면 외화 주문가능금액 전부
    us_stop_loss_pct: float = 10.0  # 0이면 끔
    us_take_profit_pct: float = 20.0  # 0이면 끔
    us_extended_hours: bool = True  # 승인한 주문은 장외(프리·애프터마켓)에도 보낸다
    add_on_pullback: bool = True  # 보유 종목도 상승 추세 중 눌림이면 추가 매수 제안
    brief_kr_at: str = "08:40"  # 국내 관심·보유 브리핑 (한국 시각, 평일). 비우면 끔
    brief_us_at: str = "09:00"  # 미국 관심·보유 브리핑 (뉴욕 시각, 미국 거래일). 비우면 끔
    us_resting: bool = True  # 매일 신호 가격에 지정가를 걸어둘지 묻는다
    us_check_sec: int = 60  # 미국 종목 점검 주기(초). 신호가 뜨면 바로 알린다
    us_realert_min: int = 15  # 만료된 매수 제안을, 조건이 그대로면 이만큼 지나 다시 알린다
    us_check_after: str = "10:00"  # 뉴욕 시각. 개장 직후 30분의 급등락은 신호로 쓰지 않는다
    us_trend_sma: int = 200  # 추세 판단 이동평균
    us_sma_buffer_pct: float = 3.0  # 이동평균 ±이 범위 안의 출렁임은 돌파·이탈로 보지 않는다
    us_pullback_rsi: float = 40.0  # 상승 추세 중 RSI가 이 이하로 내려오면 '눌림' 매수
    us_event_hold: bool = (
        True  # FOMC·CPI·고용보고서 발표 전에는 미국 매수 제안 보류 (trading/events.py)
    )
    # 레버리지 ETF → 기초지수 ETF. 기초지수가 상승 추세(가격·50일선 > 200일선)일 때만 매수 제안
    us_underlying: tuple[tuple[str, str], ...] = (
        ("TQQQ", "QQQ"),
        ("QLD", "QQQ"),
        ("SOXL", "SOXX"),
        ("UPRO", "SPY"),
        ("SPXL", "SPY"),
        ("SSO", "SPY"),
        ("TECL", "XLK"),
    )
    review_proposals: bool = True  # 제안마다 3개월 차트와 경제 봇 의견(찬성·주의·반대)을 붙인다

    @classmethod
    def from_env(cls) -> TradingPolicy:
        load_env()
        d = cls()
        return cls(
            kr_enabled=_flag("TRADE_KR_ENABLED", d.kr_enabled),
            budget_per_trade=_int("TRADE_BUDGET", d.budget_per_trade),
            min_confidence=_int("TRADE_MIN_CONFIDENCE", d.min_confidence),
            max_daily_buys=_int("TRADE_MAX_DAILY_BUYS", d.max_daily_buys),
            proposal_ttl_min=_int("TRADE_PROPOSAL_TTL_MIN", d.proposal_ttl_min),
            max_drift_pct=_float("TRADE_MAX_DRIFT_PCT", d.max_drift_pct),
            stop_loss_pct=_float("TRADE_STOP_LOSS_PCT", d.stop_loss_pct),
            take_profit_pct=_float("TRADE_TAKE_PROFIT_PCT", d.take_profit_pct),
            exit_check_min=_int("TRADE_EXIT_CHECK_MIN", d.exit_check_min),
            max_gap_pct=_float("TRADE_MAX_GAP_PCT", d.max_gap_pct),
            deferred_max_days=_int("TRADE_DEFERRED_MAX_DAYS", d.deferred_max_days),
            release_after=os.getenv("TRADE_RELEASE_AFTER", "").strip() or d.release_after,
            us_symbols=tuple(
                x.strip().upper()
                for x in (os.getenv("TRADE_US_SYMBOLS") or ",".join(d.us_symbols)).split(",")
                if x.strip()
            ),
            us_budget_usd=_float("TRADE_US_BUDGET_USD", d.us_budget_usd),
            us_stop_loss_pct=_float("TRADE_US_STOP_LOSS_PCT", d.us_stop_loss_pct),
            us_take_profit_pct=_float("TRADE_US_TAKE_PROFIT_PCT", d.us_take_profit_pct),
            us_extended_hours=os.getenv("TRADE_US_EXTENDED_HOURS", "1").strip()
            not in ("0", "false", "no"),
            add_on_pullback=_flag("TRADE_ADD_ON_PULLBACK", d.add_on_pullback),
            brief_kr_at=os.getenv("TRADE_BRIEF_KR_AT", d.brief_kr_at).strip(),
            brief_us_at=os.getenv("TRADE_BRIEF_US_AT", d.brief_us_at).strip(),
            us_resting=os.getenv("TRADE_US_RESTING", "1").strip() not in ("0", "false", "no"),
            us_check_sec=_int("TRADE_US_CHECK_SEC", d.us_check_sec),
            us_realert_min=_int("TRADE_US_REALERT_MIN", d.us_realert_min),
            us_check_after=os.getenv("TRADE_US_CHECK_AFTER", "").strip() or d.us_check_after,
            us_trend_sma=_int("TRADE_US_TREND_SMA", d.us_trend_sma),
            us_sma_buffer_pct=_float("TRADE_US_SMA_BUFFER_PCT", d.us_sma_buffer_pct),
            us_pullback_rsi=_float("TRADE_US_PULLBACK_RSI", d.us_pullback_rsi),
            review_proposals=_flag("TRADE_REVIEW", d.review_proposals),
            us_event_hold=_flag("TRADE_US_EVENT_HOLD", d.us_event_hold),
            us_underlying=_pairs("TRADE_US_UNDERLYING", d.us_underlying),
        )

    def underlying_of(self, symbol: str) -> str:
        return dict(self.us_underlying).get(symbol.upper(), "")


def _pairs(key: str, default: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """ "TQQQ:QQQ,SOXL:SOXX". 비우면 기본값, 0이면 끔."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    if raw.lower() in ("0", "false", "no", "off", "none"):
        return ()
    out = []
    for part in raw.split(","):
        lev, _, base = part.partition(":")
        if lev.strip() and base.strip():
            out.append((lev.strip().upper(), base.strip().upper()))
    return tuple(out)


def _flag(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw.replace(",", "").replace("_", ""))
    except ValueError:
        logger.warning("%s를 읽을 수 없어 기본값 %s를 씁니다: %r", key, default, raw)
        return default


def _float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s를 읽을 수 없어 기본값 %s를 씁니다: %r", key, default, raw)
        return default
