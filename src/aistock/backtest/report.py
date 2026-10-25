"""백테스팅 결과 집계·출력.

표의 수치는 **순수익**(비용 차감 후) 기준이다. 총수익으로 판단하면 왕복 0.23%의 비용을
못 넘는 전략을 수익 전략으로 오인하게 된다.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass

from .costs import CostModel
from .engine import Skipped, Trade, iter_confidence_buckets


@dataclass(frozen=True)
class Stats:
    count: int
    hit_rate: float  # 비용 차감 후에도 이익인 비율 (%)
    mean_return: float  # 순수익 평균 (%)
    median_return: float
    best: float
    worst: float
    mean_gross: float  # 비용 차감 전 평균 — 비용이 얼마나 갉아먹는지 보려고
    mean_benchmark: float | None = None  # 같은 기간 지수 (신호 방향 기준)
    mean_excess: float | None = None  # 지수 대비 초과수익 (%p)
    beat_rate: float | None = None  # 지수를 이긴 비율 (%)

    @classmethod
    def of(cls, trades: list[Trade]) -> Stats:
        if not trades:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        net = [t.net_return_pct for t in trades]
        benched = [t for t in trades if t.excess_return_pct is not None]
        return cls(
            count=len(trades),
            hit_rate=sum(t.hit for t in trades) / len(trades) * 100,
            mean_return=statistics.fmean(net),
            median_return=statistics.median(net),
            best=max(net),
            worst=min(net),
            mean_gross=statistics.fmean([t.signed_return_pct for t in trades]),
            mean_benchmark=(
                statistics.fmean([t.benchmark_signed_pct for t in benched]) if benched else None
            ),
            mean_excess=(
                statistics.fmean([t.excess_return_pct for t in benched]) if benched else None
            ),
            beat_rate=(
                sum(bool(t.beat_market) for t in benched) / len(benched) * 100 if benched else None
            ),
        )


def _row(label: str, stats: Stats, *, benched: bool = False) -> str:
    if stats.count == 0:
        return f"  {label:<12} {'-':>6}"
    line = (
        f"  {label:<12} {stats.count:>6}  {stats.hit_rate:>6.1f}%  "
        f"{stats.mean_return:>+7.2f}%  {stats.median_return:>+7.2f}%"
    )
    if benched and stats.mean_excess is not None:
        line += (
            f"  {stats.mean_benchmark:>+7.2f}%  {stats.mean_excess:>+7.2f}%p"
            f"  {stats.beat_rate:>6.1f}%"
        )
    else:
        line += f"  {stats.best:>+7.2f}%  {stats.worst:>+7.2f}%"
    return line


def render(
    trades: list[Trade],
    skipped: list[Skipped],
    *,
    horizon: int,
    entry: str,
    sample_limit: int = 10,
    merged: int = 0,
    cost: CostModel | None = None,
) -> str:
    """결과를 사람이 읽을 텍스트로."""
    cost = cost or CostModel()
    lines: list[str] = []
    overall = Stats.of(trades)

    head = f"체결 {len(trades)}건 / 제외 {len(skipped)}건"
    if merged:
        head += f" / 중복 병합 {merged}건"
    lines.append(f"{head}  (보유 {horizon}거래일, 진입 {entry})")
    lines.append(f"비용: {cost.describe()}")
    lines.append("")

    if not trades:
        lines.append("  평가할 신호가 없습니다.")
        _append_skips(lines, skipped)
        return "\n".join(lines)

    benched = overall.mean_excess is not None
    if benched:
        names = sorted({t.benchmark_name for t in trades if t.benchmark_name})
        header = (
            f"  {'구분':<12} {'건수':>6}  {'적중률':>7}  {'평균':>8}  {'중앙':>8}"
            f"  {'지수':>8}  {'초과':>9}  {'승률':>7}"
        )
        lines.append(f"  벤치마크: {'/'.join(names)} (같은 날짜·같은 진입 방식, 비용 미반영)")
    else:
        header = (
            f"  {'구분':<12} {'건수':>6}  {'적중률':>7}  {'평균':>8}  {'중앙':>8}"
            f"  {'최고':>8}  {'최저':>8}"
        )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    lines.append(_row("전체", overall, benched=benched))

    if cost.enabled:
        drag = overall.mean_gross - overall.mean_return
        lines.append(
            f"  {'(비용 전)':<12} {'':>6}  {'':>7}  {overall.mean_gross:>+7.2f}%"
            f"   → 비용이 {drag:.3f}%p 를 갉아먹음"
        )

    lines.append("")
    for action in ("buy", "sell"):
        subset = [t for t in trades if t.action == action]
        if subset:
            lines.append(_row(action, Stats.of(subset), benched=benched))

    lines.append("")
    lines.append("  confidence 구간별")
    for label, bucket in iter_confidence_buckets(trades):
        if bucket:
            lines.append(_row(label, Stats.of(bucket), benched=benched))

    events = Counter(t.event_type for t in trades)
    if len(events) > 1:
        lines.append("")
        lines.append("  사건 유형별 (5건 이상)")
        for event, n in events.most_common():
            if n < 5:
                continue
            lines.append(
                _row(
                    event[:12],
                    Stats.of([t for t in trades if t.event_type == event]),
                    benched=benched,
                )
            )

    worst = sorted(trades, key=lambda t: t.net_return_pct)[:sample_limit]
    if worst:
        lines.append("")
        lines.append(f"  손실 상위 {len(worst)}건")
        for t in worst:
            lines.append(
                f"    {t.signal_date}  {t.corp_name[:12]:<14} {t.action:<5} "
                f"{t.confidence:>3}%  {t.net_return_pct:>+7.2f}%  {t.event_type[:20]}"
            )

    _append_skips(lines, skipped)
    lines.append("")
    lines.append("  주의: sell 수익률은 공매도 가정의 부호 반전이다. 국내 개인은 실행이 어려우므로")
    lines.append("        '방향 적중' 지표로만 읽을 것.")
    if benched:
        lines.append(
            "        초과수익은 전략(비용 차감) − 지수(비용 미반영)라 전략에 불리한 쪽으로"
        )
        lines.append("        기울어 있다. 판단 기준은 평균이 아니라 '초과'와 '승률'이다.")
    return "\n".join(lines)


def _append_skips(lines: list[str], skipped: list[Skipped]) -> None:
    if not skipped:
        return
    lines.append("")
    lines.append("  제외 사유")
    for reason, n in Counter(s.reason.value for s in skipped).most_common():
        lines.append(f"    {reason:<20} {n:>5}건")
