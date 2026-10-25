"""원/달러 환율 조회와 이력 축적.

키움 환율 TR(`ust31301`)은 **현재 고시환율만** 준다. 과거 시계열이 없어서 그래프를 그리려면
조회할 때마다 값을 직접 쌓아야 한다. 그래서 조회 시점에 자동으로 기록하고, 차트는 그 기록으로
그린다 — 처음에는 점 몇 개뿐이고 알림이 돌수록 선이 채워진다.

매수/매도 환율이 따로 있다. 달러를 살 때는 매수환율(비쌈), 팔 때는 매도환율(쌈)이 적용된다.
그래프에는 둘의 중간값(중간환율)을 쓴다 — 어느 한쪽만 보면 스프레드만큼 왜곡된다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import KiwoomClient
from .config import CACHE_DIR

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
EXCHANGE_PATH = "/api/us/exchange"
RATE_TR = "ust31301"
HISTORY_PATH = CACHE_DIR / "fx_history.jsonl"

# 같은 값을 계속 쌓지 않도록 최소 간격을 둔다. 고시환율은 자주 바뀌지 않는다.
MIN_RECORD_GAP = timedelta(minutes=20)


@dataclass(frozen=True)
class FxRate:
    """원/달러 고시환율."""

    at: str  # ISO8601 KST
    buy: float  # 달러 살 때 적용 (원화→달러). 공개 API 는 매수/매도가 없어 중간값을 넣는다
    sell: float  # 달러 팔 때 적용 (달러→원화)
    label: str = ""  # 환율구분명
    src: str = ""  # 출처("ECB"·"er-api"·""=키움 고시환율)

    @property
    def mid(self) -> float:
        """중간환율. 한쪽만 보면 스프레드만큼 치우친다."""
        if self.buy and self.sell:
            return (self.buy + self.sell) / 2
        return self.buy or self.sell

    @property
    def spread(self) -> float:
        return abs(self.buy - self.sell) if self.buy and self.sell else 0.0

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)


def _rate(client: KiwoomClient, exch_tp: str) -> dict:
    return client.request(RATE_TR, EXCHANGE_PATH, {"exch_tp": exch_tp}).data


def _num(value: object) -> float:
    text = str(value or "").strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch(client: KiwoomClient, *, record: bool = True) -> FxRate:
    """현재 고시환율. `record=True`면 이력에 남긴다."""
    krw_to_usd = _rate(client, "1")  # 달러 살 때
    usd_to_krw = _rate(client, "2")  # 달러 팔 때
    rate = FxRate(
        at=datetime.now(KST).isoformat(timespec="seconds"),
        buy=_num(krw_to_usd.get("aplc_exrt")),
        sell=_num(usd_to_krw.get("aplc_exrt")),
        label=str(krw_to_usd.get("exrt_tp_nm", "")).strip(),
    )
    if record:
        append_history(rate)
    return rate


def append_history(rate: FxRate, path: Path = HISTORY_PATH) -> bool:
    """이력에 추가. 최근 기록과 간격이 짧거나 값이 같으면 건너뛴다."""
    history = load_history(path)
    if history:
        last = history[-1]
        if rate.when - last.when < MIN_RECORD_GAP and abs(rate.mid - last.mid) < 0.01:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(rate), ensure_ascii=False) + "\n")
    return True


def load_history(path: Path = HISTORY_PATH) -> list[FxRate]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(FxRate(**json.loads(line)))
        except (ValueError, TypeError) as exc:
            logger.debug("환율 이력 한 줄을 건너뜁니다: %s", exc)
    return sorted(out, key=lambda r: r.at)


# ── 공개 환율 API(달러·엔화 공통) ───────────────────────────────────────
# 2026-09-17 사용자 지시로 **달러도 공개 API 로 통일**했다. 그 전에는 달러만
# 키움 고시환율이었는데, 엔화(시장 중간환율)와 기준이 달라 나란히 못 읽었다.
# ⚠️두 값의 차이는 작지 않다 — 실측 키움 고시 중간 1,379.90 vs ECB 1,367.80(12원).
# 키움 고시환율은 스프레드가 30원이라 중간값이 시장가 위에 있다.
_UA = {"User-Agent": "aistock/1.0"}


def _get_json(url: str, timeout: float) -> dict:
    """⚠️User-Agent 를 안 보내면 frankfurter 는 Cloudflare 가 403 으로 막는다
    (curl 은 통과해서 처음엔 못 알아채고 2순위로 떨어졌다)."""
    import json as _json
    import urllib.request

    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return _json.loads(fh.read().decode("utf-8"))


# ⚠️순서가 곧 우선순위다. er-api 를 앞에 둔다(2026-09-17 사용자 지시) —
# ECB(frankfurter)는 **하루 한 번 고시**라 오늘 값이 어제 종가다. er-api 쪽이
# 더 자주 갱신된다. ECB 는 er-api 가 죽었을 때의 대비책으로 남긴다.
USD_SOURCES = [
    ("er-api", "https://open.er-api.com/v6/latest/USD"),
    ("ECB", "https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW"),
]


def fetch_usd(
    client: KiwoomClient | None = None, *, record: bool = True, timeout: float = 12.0
) -> FxRate | None:
    """원/달러 시장환율. 공개 API 두 곳을 차례로, 다 실패하면 키움으로 물러선다."""
    for name, url in USD_SOURCES:
        try:
            rate_krw = float(_get_json(url, timeout)["rates"]["KRW"])
            if rate_krw <= 0:
                continue
            # 공개 API 는 매수/매도가 없다. 중간값을 양쪽에 넣어 mid·차트가 그대로 돌게 한다.
            rate = FxRate(
                at=datetime.now(KST).isoformat(timespec="seconds"),
                buy=rate_krw,
                sell=rate_krw,
                label="시장환율",
                src=name,
            )
            if record:
                append_history(rate)
            return rate
        except Exception as exc:  # noqa: BLE001
            logger.debug("달러 환율 %s 실패: %s", name, exc)
    if client is not None:
        logger.warning("공개 환율 API 실패 — 키움 고시환율로 대체합니다")
        return fetch(client, record=record)
    logger.warning("달러 환율을 가져오지 못했습니다")
    return None


# ── 엔화 ────────────────────────────────────────────────────────────────
# 키움 환율 TR(ust31301)은 **달러 전용**이다 — exch_tp 는 1/2(매수·매도)뿐이고
# 통화를 고를 수 없다(실측: 3 이상은 "환전 구분값은 1 또는 2만 입력 가능").
# 그래서 엔화만 공개 환율 API 로 가져온다. ⚠️달러는 키움 **고시환율**(매수/매도),
# 엔화는 **시장 중간환율**이라 기준이 다르다 — 표시에 출처를 밝힌다.
JPY_HISTORY_PATH = CACHE_DIR / "fx_jpy_history.jsonl"
JPY_SOURCES = [  # 달러와 같은 순서로 둔다 — 두 줄의 출처가 엇갈리면 헷갈린다
    ("er-api", "https://open.er-api.com/v6/latest/JPY", lambda d: float(d["rates"]["KRW"])),
    (
        "ECB",
        "https://api.frankfurter.dev/v1/latest?base=JPY&symbols=KRW",
        lambda d: float(d["rates"]["KRW"]),
    ),
]


@dataclass(frozen=True)
class JpyRate:
    """원/엔 환율. 한국 관행대로 **100엔 기준**으로 보여 준다."""

    at: str  # ISO8601 KST
    per100: float  # 100엔당 원
    src: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)


def fetch_jpy(*, record: bool = True, timeout: float = 12.0) -> JpyRate | None:
    """엔화 환율. 두 곳을 차례로 시도하고 다 안 되면 None(달러 줄은 그대로 나간다)."""
    for name, url, pick in JPY_SOURCES:
        try:
            per1 = pick(_get_json(url, timeout))
            if per1 <= 0:
                continue
            rate = JpyRate(
                at=datetime.now(KST).isoformat(timespec="seconds"), per100=per1 * 100, src=name
            )
            if record:
                append_jpy(rate)
            return rate
        except Exception as exc:  # noqa: BLE001
            logger.debug("엔화 환율 %s 실패: %s", name, exc)
    logger.warning("엔화 환율을 가져오지 못했습니다")
    return None


def append_jpy(rate: JpyRate, path: Path = JPY_HISTORY_PATH) -> bool:
    history = load_jpy_history(path)
    if history:
        last = history[-1]
        if rate.when - last.when < MIN_RECORD_GAP and abs(rate.per100 - last.per100) < 0.01:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(rate), ensure_ascii=False) + "\n")
    return True


def load_jpy_history(path: Path = JPY_HISTORY_PATH) -> list[JpyRate]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(JpyRate(**json.loads(line)))
        except (ValueError, TypeError) as exc:
            logger.debug("엔화 이력 한 줄을 건너뜁니다: %s", exc)
    return sorted(out, key=lambda r: r.at)


def summary(rate: FxRate, history: list[FxRate] | None = None) -> str:
    """텔레그램용 한 줄 요약. 이력이 있으면 변화폭도 함께."""
    parts = [f"💵 <b>원/달러</b>  {rate.mid:,.2f}원"]
    history = history or []
    if len(history) >= 2:
        first = history[0]
        delta = rate.mid - first.mid
        sign = "+" if delta > 0 else ""
        span = rate.when - first.when
        unit = f"{span.days}일" if span.days else f"{span.seconds // 3600}시간"
        parts.append(f"({sign}{delta:,.2f}, 최근 {unit})")
    out = " ".join(parts)
    if rate.spread > 0.005:  # 키움 고시환율일 때만 뜻이 있다
        out += f"\n   매수 {rate.buy:,.2f} / 매도 {rate.sell:,.2f}"

    # 엔화 한 줄(2026-09-17 사용자 요청 "엔화도 같이 보여줘").
    # 못 가져와도 달러 줄은 그대로 나가야 한다.
    jpy = fetch_jpy()
    if jpy:
        line = f"\n💴 <b>원/100엔</b>  {jpy.per100:,.2f}원"
        jh = load_jpy_history()
        if len(jh) >= 2:
            d = jpy.per100 - jh[0].per100
            span = jpy.when - jh[0].when
            unit = f"{span.days}일" if span.days else f"{span.seconds // 3600}시간"
            line += f" ({'+' if d > 0 else ''}{d:,.2f}, 최근 {unit})"
        out += line
    # 출처 표시. 둘이 같으면 맨 끝에 한 번만, 다르면(키움 폴백 등) 줄마다 밝힌다 —
    # 한쪽만 적으면 다른 쪽도 그 기준인 줄 오해한다.
    usd_src = rate.src or "키움 고시환율"
    jpy_src = jpy.src if jpy else ""
    if jpy and usd_src == jpy_src:
        out += f"\n   <i>시장 중간환율 · {usd_src}</i>"
    else:
        note = f"\n   <i>달러 {usd_src}</i>"
        if jpy:
            note += f" · <i>엔 시장중간 {jpy_src}</i>"
        out += note
    return out
