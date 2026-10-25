"""추이 차트.

시점 시세만 보면 흐름을 알 수 없어 기간 차트를 그린다. 일봉 캐시(600거래일)를 그대로 쓰므로
추가 조회는 종목당 최초 1회뿐이다.

## 설계 근거

**종목마다 패널을 나눈다(small multiples).** 삼성전자 23만원과 SK하이닉스 142만원을 한 축에
겹치면 축 정렬이 임의가 되어 없는 상관관계를 만들어낸다. 축을 두 개 쓰는 것(dual-axis)은
더 나쁘다. 패널을 나누면 각자 자기 축을 갖고, 비교는 '기간 등락률' 숫자로 한다.

색은 종목당 한 계열뿐이라 categorical 1번(파랑) 하나만 쓴다. 계열이 하나면 범례가 필요 없고
제목이 이름을 대신한다. 등락 방향은 색이 아니라 숫자·부호로 표시한다 — 색만으로 정보를
전달하면 색각 이상에서 읽히지 않는다.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")  # 화면 없는 환경에서 렌더링
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager

from .backtest.prices import PriceHistory

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# dataviz 기본 팔레트 (light). validate_palette.js 통과 확인함.
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8880"
SERIES = "#2a78d6"  # categorical slot 1
GRID = "#e6e5e0"

# 한글이 깨지면 차트가 무의미해진다. 있는 것 중 첫 번째를 쓴다.
KOREAN_FONTS = ["Apple SD Gothic Neo", "AppleGothic", "NanumGothic", "Malgun Gothic", "PingFang SC"]

PERIODS: dict[str, int] = {
    "1개월": 21,
    "3개월": 63,
    "6개월": 126,
    "1년": 252,
}


def _fmt_price(value: float, *, with_unit: bool = True) -> str:
    """국내는 정수+원, 미국은 달러 소수. 값의 크기로 구분한다 — 소수부가 있으면 달러다."""
    if abs(value - round(value)) < 1e-9:
        whole = round(value)
        return f"{whole:,}원" if with_unit else f"{whole:,}"
    return f"${value:,.2f}" if with_unit else f"{value:,.2f}"


def _pick_font() -> str | None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in KOREAN_FONTS:
        if name in available:
            return name
    logger.warning("한글 폰트를 찾지 못했습니다. 차트의 한글이 깨질 수 있습니다.")
    return None


@dataclass(frozen=True)
class TrendStats:
    """기간별 등락률과 52주 위치."""

    code: str
    name: str
    last_price: int
    last_date: str
    returns: dict[str, float]  # 기간명 → %
    high_52w: int
    low_52w: int

    @property
    def position_52w(self) -> float:
        """52주 밴드 안에서 현재가의 위치(0~100%). 고점 대비 어디쯤인지 한 숫자로 본다."""
        span = self.high_52w - self.low_52w
        if span <= 0:
            return 50.0
        return (self.last_price - self.low_52w) / span * 100


def compute_stats(history: PriceHistory, code: str, name: str) -> TrendStats | None:
    bars = history.bars
    if not bars:
        return None
    last = bars[-1]
    returns: dict[str, float] = {}
    for label, days in PERIODS.items():
        if len(bars) <= days:
            continue
        base = bars[-1 - days].close
        if base > 0:
            returns[label] = (last.close - base) / base * 100
    window = bars[-min(len(bars), PERIODS["1년"]) :]
    return TrendStats(
        code=code,
        name=name,
        last_price=last.close,
        last_date=last.date,
        returns=returns,
        high_52w=max(b.high for b in window),
        low_52w=min(b.low for b in window),
    )


def render_trends(
    items: list[tuple[PriceHistory, str, str]],
    *,
    days: int = 126,
    title: str = "",
) -> bytes:
    """종목별 추이 차트를 PNG 바이트로. items는 (일봉, 종목코드, 종목명)."""
    if not items:
        raise ValueError("그릴 종목이 없습니다.")

    font = _pick_font()
    if font:
        plt.rcParams["font.family"] = font
    plt.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지

    n = len(items)
    fig, axes = plt.subplots(n, 1, figsize=(8, 2.6 * n + 0.6), facecolor=SURFACE, squeeze=False)
    axes = axes.ravel()

    for ax, (history, code, name) in zip(axes, items, strict=True):
        bars = history.bars[-days:]
        if not bars:
            ax.set_visible(False)
            continue
        xs = [datetime.strptime(b.date, "%Y%m%d").replace(tzinfo=KST) for b in bars]
        ys = [b.close for b in bars]

        ax.set_facecolor(SURFACE)
        # 가로 격자만. 축·격자는 뒤로 물러나 있어야 선이 읽힌다.
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(colors=TEXT_MUTED, labelsize=9, length=0)

        ax.plot(xs, ys, color=SERIES, linewidth=2, zorder=3)
        ax.fill_between(xs, ys, min(ys), color=SERIES, alpha=0.08, zorder=2)
        # 마지막 값에만 마커와 직접 라벨을 단다 — 모든 점에 숫자를 붙이면 읽을 수 없다.
        ax.plot(xs[-1], ys[-1], "o", color=SERIES, markersize=8, zorder=4)

        change = (ys[-1] - ys[0]) / ys[0] * 100 if ys[0] else 0.0
        sign = "+" if change > 0 else ""
        # 미국 종목은 달러 소수라 원 단위 정수 표기를 쓰면 74달러가 74원처럼 보인다.
        ax.set_title(
            f"{name} ({code})   {_fmt_price(ys[-1])}   {sign}{change:.1f}%",
            color=TEXT_PRIMARY,
            fontsize=12,
            fontweight="bold",
            loc="left",
            pad=10,
        )
        ax.annotate(
            _fmt_price(ys[-1], with_unit=False),
            xy=(xs[-1], ys[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            color=TEXT_SECONDARY,
            fontsize=9,
            va="center",
        )
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m월"))
        ax.yaxis.set_major_formatter(lambda v, _: _fmt_price(v, with_unit=False))
        # 오른쪽은 마지막 값 라벨이 들어갈 자리라 여유를 더 준다.
        ax.margins(x=0.02, y=0.12)
        left, right = ax.get_xlim()
        ax.set_xlim(left, right + (right - left) * 0.10)

    if title:
        fig.suptitle(title, color=TEXT_SECONDARY, fontsize=10, y=0.995, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.98 if title else 1))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def render_fx(history: list, *, title: str = "원/달러 환율") -> bytes:
    """환율 추이. 종목 차트와 달리 값의 변동폭이 좁아 y축을 0부터 그리면 선이 평평해진다.

    이력이 쌓인 만큼만 그린다 — 점이 2개면 선분 하나다. 시간이 지나며 채워진다.
    """
    if len(history) < 2:
        raise ValueError("환율 이력이 2건 이상 필요합니다.")

    font = _pick_font()
    if font:
        plt.rcParams["font.family"] = font
    plt.rcParams["axes.unicode_minus"] = False

    xs = [r.when for r in history]
    ys = [r.mid for r in history]

    fig, ax = plt.subplots(figsize=(8, 3.0), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_MUTED, labelsize=9, length=0)

    ax.plot(xs, ys, color=SERIES, linewidth=2, zorder=3)
    ax.fill_between(xs, ys, min(ys), color=SERIES, alpha=0.08, zorder=2)
    ax.plot(xs[-1], ys[-1], "o", color=SERIES, markersize=8, zorder=4)

    change = ys[-1] - ys[0]
    sign = "+" if change > 0 else ""
    ax.set_title(
        f"{title}   {ys[-1]:,.2f}원   {sign}{change:,.2f}",
        color=TEXT_PRIMARY,
        fontsize=12,
        fontweight="bold",
        loc="left",
        pad=10,
    )
    ax.annotate(
        f"{ys[-1]:,.2f}",
        xy=(xs[-1], ys[-1]),
        xytext=(6, 0),
        textcoords="offset points",
        color=TEXT_SECONDARY,
        fontsize=9,
        va="center",
    )
    # 기록 간격에 따라 눈금 단위를 바꾼다. 하루치를 날짜로만 찍으면 눈금이 하나뿐이다.
    span = xs[-1] - xs[0]
    if span.days >= 2:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.1f}")
    ax.margins(x=0.02, y=0.15)
    left, right = ax.get_xlim()
    ax.set_xlim(left, right + (right - left) * 0.10)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def stats_text(stats: list[TrendStats]) -> str:
    """차트에 붙일 캡션. 기간별 등락률은 그래프보다 숫자가 정확하다."""
    lines = []
    for s in stats:
        parts = [f"<b>{s.name}</b> <code>{s.code}</code>  {s.last_price:,}원"]
        rets = "  ".join(
            f"{label} {'+' if v > 0 else ''}{v:.1f}%" for label, v in s.returns.items()
        )
        if rets:
            parts.append(f"   {rets}")
        parts.append(f"   52주 {s.low_52w:,}~{s.high_52w:,} · 현재 위치 {s.position_52w:.0f}%")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


SMA_SHORT = "#eb6834"  # categorical slot 2 — 50일선
SMA_LONG = "#1baf7a"  # categorical slot 3 — 200일선


def render_signal(
    dates: list[str],
    closes: list[float],
    *,
    sma50: list[float | None],
    sma200: list[float | None],
    rsi: list[float | None],
    order_price: float,
    order_label: str,
    title: str,
    rsi_line: float = 40.0,
    krw: bool = False,
) -> bytes:
    """매매 제안용 차트: 위는 종가·50일선·200일선·주문가, 아래는 RSI.

    값의 단위가 다른 RSI를 가격 축에 겹치지 않고(두 축 금지) 아래 패널로 따로 그린다.
    모든 목록은 dates와 길이가 같다(마지막 원소가 오늘·현재가).
    """
    font = _pick_font()
    if font:
        plt.rcParams["font.family"] = font
    plt.rcParams["axes.unicode_minus"] = False

    def fmt(v: float) -> str:
        return f"{v:,.0f}" if krw else f"{v:,.2f}"

    xs = [datetime.strptime(d, "%Y%m%d").replace(tzinfo=KST) for d in dates]
    fig, (ax, ax_rsi) = plt.subplots(
        2,
        1,
        figsize=(8, 5.2),
        facecolor=SURFACE,
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    for a in (ax, ax_rsi):
        a.set_facecolor(SURFACE)
        a.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        a.set_axisbelow(True)
        for side in ("top", "right", "left"):
            a.spines[side].set_visible(False)
        a.spines["bottom"].set_color(GRID)
        a.tick_params(colors=TEXT_MUTED, labelsize=9, length=0)

    def series(values: list[float | None]) -> tuple[list, list]:
        pts = [(x, v) for x, v in zip(xs, values, strict=True) if v is not None]
        return [p[0] for p in pts], [p[1] for p in pts]

    lines = [
        ("종가", closes, SERIES, 2.0),
        ("50일선", sma50, SMA_SHORT, 1.5),
        ("200일선", sma200, SMA_LONG, 1.5),
    ]
    ends: list[tuple[float, str]] = []
    for label, values, color, width in lines:
        lx, ly = series(values)
        if not lx:
            continue
        ax.plot(lx, ly, color=color, linewidth=width, label=label, zorder=3)
        ends.append((ly[-1], f"{label} {fmt(ly[-1])}"))
    ax.plot(xs[-1], closes[-1], "o", color=SERIES, markersize=8, zorder=4)

    ax.axhline(order_price, color=TEXT_SECONDARY, linewidth=1.2, linestyle="--", zorder=2)
    ax.annotate(
        f"{order_label} {fmt(order_price)}",
        xy=(xs[0], order_price),
        xytext=(0, 4),
        textcoords="offset points",
        color=TEXT_PRIMARY,
        fontsize=9,
        fontweight="bold",
        va="bottom",
        bbox={"boxstyle": "square,pad=0.15", "facecolor": SURFACE, "edgecolor": "none"},
        zorder=5,
    )
    ax.set_title(title, color=TEXT_PRIMARY, fontsize=12, fontweight="bold", loc="left", pad=10)
    ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=TEXT_SECONDARY, ncol=3)
    ax.yaxis.set_major_formatter(lambda v, _: fmt(v))
    ax.margins(x=0.02, y=0.15)
    # 끝에 직접 라벨 — 범례를 보지 않아도 어느 선인지 안다. 값이 가까우면 위아래로 벌린다.
    bottom, top = ax.get_ylim()
    gap = (top - bottom) * 0.05
    placed: list[float] = []
    for y, text in sorted(ends):
        ty = max(y, placed[-1] + gap) if placed else y
        placed.append(ty)
        ax.annotate(
            text,
            xy=(xs[-1], y),
            xytext=(8, ty),
            textcoords=("offset points", "data"),
            color=TEXT_SECONDARY,
            fontsize=8,
            va="center",
            bbox={"boxstyle": "square,pad=0.15", "facecolor": SURFACE, "edgecolor": "none"},
            zorder=5,
        )

    rx, ry = series(rsi)
    if rx:
        ax_rsi.plot(rx, ry, color=SERIES, linewidth=1.5, zorder=3)
        ax_rsi.plot(rx[-1], ry[-1], "o", color=SERIES, markersize=6, zorder=4)
        ax_rsi.annotate(
            f"RSI {ry[-1]:.0f}",
            xy=(rx[-1], ry[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            color=TEXT_SECONDARY,
            fontsize=8,
            va="center",
        )
    ax_rsi.axhline(rsi_line, color=TEXT_MUTED, linewidth=1, linestyle="--")
    ax_rsi.axhline(70, color=GRID, linewidth=1, linestyle="--")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_yticks([rsi_line, 70])
    ax_rsi.set_ylabel("RSI", color=TEXT_MUTED, fontsize=9)
    ax_rsi.xaxis.set_major_locator(mdates.MonthLocator())
    ax_rsi.xaxis.set_major_formatter(mdates.DateFormatter("%-m월"))

    # 오른쪽 끝 라벨 자리
    left, right = ax.get_xlim()
    ax.set_xlim(left, right + (right - left) * 0.16)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
