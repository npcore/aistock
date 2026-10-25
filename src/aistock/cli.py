"""연동 확인용 CLI.

$ aistock check                       # 토큰 발급 + 연결 확인
$ aistock quote 005930                # 국내 현재가
$ aistock book 005930                 # 국내 호가
$ aistock chart 005930 --days 20      # 국내 일봉
$ aistock us NVDA --exchange ND       # 미국 현재가
$ aistock etf --mngmcomp 3020         # ETF 전체시세 (KODEX)
$ aistock dart sync                   # DART 고유번호 인덱스 갱신
$ aistock dart list 005930 --days 30  # 종목별 최근 공시
$ aistock dart today                  # 당일 주요 공시
$ aistock dart doc 20260805000123     # 공시 원문 텍스트
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dart
from .client import KiwoomClient
from .config import LOG_DIR
from .dart import CorpCodeIndex, DartClient, DartError
from .errors import KiwoomError
from .market import domestic, overseas
from .pipeline import DisclosurePoller, DocumentArchiver, JsonlWriter, console
from .pipeline.handlers import JSONL_PATH
from .signal.handler import SIGNALS_PATH

KST = timezone(timedelta(hours=9))


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _show(data: dict, fields: dict[str, str]) -> None:
    width = max(len(label) for label in fields.values())
    for key, label in fields.items():
        value = data.get(key)
        if value not in (None, ""):
            print(f"  {label:<{width}}  {value}")


def cmd_check(client: KiwoomClient, args: argparse.Namespace) -> int:
    settings = client.settings
    print(f"모드      : {settings.mode} ({'실전투자' if settings.is_real else '모의투자'})")
    print(f"base_url  : {settings.base_url}")
    print(f"계좌번호  : {settings.account_no or '(미설정)'}")

    record = client.auth.current()
    print(f"토큰      : 발급 성공 (만료 {record.expiry.astimezone(KST):%Y-%m-%d %H:%M:%S} KST)")

    data = domestic.stock_info(client, "005930")
    print(f"시세 확인 : {data.get('stk_nm')} 현재가 {data.get('cur_prc')}")
    print("\n연동 정상입니다.")
    return 0


def cmd_quote(client: KiwoomClient, args: argparse.Namespace) -> int:
    data = domestic.stock_info(client, args.code)
    print(f"{data.get('stk_nm')} ({args.code})")
    _show(
        data,
        {
            "cur_prc": "현재가",
            "pred_pre": "전일대비",
            "flu_rt": "등락률",
            "open_pric": "시가",
            "high_pric": "고가",
            "low_pric": "저가",
            "trde_qty": "거래량",
            "mac": "시가총액",
            "per": "PER",
            "pbr": "PBR",
            "eps": "EPS",
            "roe": "ROE",
            "250hgst": "250일최고",
            "250lwst": "250일최저",
        },
    )
    return 0


def _bid_fields(side: str, tier: int) -> tuple[str, str]:
    """최우선호가(1단계)만 `sel_fpr_bid` 형태이고 2~10단계는 `sel_2th_pre_bid` 형태다."""
    if tier == 1:
        return f"{side}_fpr_bid", f"{side}_fpr_req"
    return f"{side}_{tier}th_pre_bid", f"{side}_{tier}th_pre_req"


def cmd_book(client: KiwoomClient, args: argparse.Namespace) -> int:
    data = domestic.order_book(client, args.code)
    print(f"{args.code} 호가 (기준 {data.get('bid_req_base_tm')})")
    for tier in range(args.depth, 0, -1):
        bid, req = _bid_fields("sel", tier)
        print(f"  매도{tier}  {data.get(bid, ''):>10}  {data.get(req, ''):>12}")
    print("  " + "-" * 28)
    for tier in range(1, args.depth + 1):
        bid, req = _bid_fields("buy", tier)
        print(f"  매수{tier}  {data.get(bid, ''):>10}  {data.get(req, ''):>12}")
    return 0


def cmd_chart(client: KiwoomClient, args: argparse.Namespace) -> int:
    rows = domestic.daily_chart(client, args.code, _today(), max_pages=1)
    print(f"{args.code} 일봉 최근 {args.days}일")
    print(f"  {'일자':<10} {'시가':>9} {'고가':>9} {'저가':>9} {'종가':>9} {'거래량':>12}")
    for row in rows[: args.days]:
        print(
            f"  {row.get('dt', ''):<10} {row.get('open_pric', ''):>9} {row.get('high_pric', ''):>9} "
            f"{row.get('low_pric', ''):>9} {row.get('cur_prc', ''):>9} {row.get('trde_qty', ''):>12}"
        )
    return 0


def cmd_us(client: KiwoomClient, args: argparse.Namespace) -> int:
    data = overseas.quote(client, args.code, args.exchange)
    print(
        f"{data.get('stk_nm') or data.get('stk_enm')} ({args.code} / {overseas.EXCHANGES[args.exchange]})"
    )
    _show(
        data,
        {
            "cur_prc": "현재가",
            "pred_pre": "전일대비",
            "flu_rt": "등락률",
            "acc_trde_qty": "누적거래량",
            "mac": "시가총액",
            "52wk_hgst_pric": "52주최고",
            "52wk_lwst_pric": "52주최저",
        },
    )
    return 0


def cmd_etf(client: KiwoomClient, args: argparse.Namespace) -> int:
    rows = domestic.etf_all(client, mngmcomp=args.mngmcomp, max_pages=args.pages)
    print(f"ETF {len(rows)}종목")
    for row in rows[: args.limit]:
        print(
            f"  {row.get('stk_cd', ''):<10} {row.get('stk_nm', '')[:26]:<28} "
            f"{row.get('close_pric', ''):>9} {row.get('pre_rt', ''):>8}  NAV {row.get('nav', ''):>10}"
        )
    return 0


def _print_disclosures(items: list[dart.Disclosure], limit: int) -> None:
    if not items:
        print("  (공시 없음)")
        return
    for item in items[:limit]:
        label = item.stock_code or "비상장"
        print(f"  {item.rcept_dt}  {label:<8} {item.corp_name[:14]:<16} {item.report_nm[:44]}")
        print(f"             {item.flr_nm[:20]:<22} {item.url}")
    if len(items) > limit:
        print(f"  … 외 {len(items) - limit}건")


def cmd_dart_sync(client: DartClient, args: argparse.Namespace) -> int:
    index = CorpCodeIndex.refresh(client)
    print(f"고유번호 인덱스 갱신 완료 — 전체 {len(index.corps):,}개, 상장 {len(index.listed):,}개")
    return 0


def cmd_dart_list(client: DartClient, args: argparse.Namespace) -> int:
    index = CorpCodeIndex.load(client=client)
    corp = index.by_stock_code(args.code)
    items = dart.recent_for_stock(client, index, args.code, days=args.days, pblntf_ty=args.type)
    print(
        f"{corp.corp_name} ({corp.stock_code} / corp_code {corp.corp_code}) 최근 {args.days}일 공시 {len(items)}건"
    )
    _print_disclosures(items, args.limit)
    return 0


def cmd_dart_today(client: DartClient, args: argparse.Namespace) -> int:
    items = dart.market_moving(client, date=args.date, corp_cls=args.market)
    day = args.date or "오늘"
    types = ", ".join(f"{t}={dart.PBLNTF_TY[t]}" for t in dart.MARKET_MOVING_TYPES)
    print(f"{day} 주요 공시 {len(items)}건  [{types}]")
    _print_disclosures(items, args.limit)
    return 0


def cmd_dart_search(client: DartClient, args: argparse.Namespace) -> int:
    index = CorpCodeIndex.load(client=client)
    hits = index.search(args.keyword, listed_only=not args.all)
    print(f"'{args.keyword}' 검색 {len(hits)}건")
    for corp in hits[: args.limit]:
        print(f"  {corp.stock_code or '      '}  {corp.corp_code}  {corp.corp_name}")
    return 0


def cmd_dart_doc(client: DartClient, args: argparse.Namespace) -> int:
    text = dart.document_text(client, args.rcept_no)
    if args.save:
        path = LOG_DIR / f"dart_{args.rcept_no}.txt"
        path.write_text(text, encoding="utf-8")
        print(f"저장: {path} ({len(text):,}자)")
        return 0
    print(text[: args.chars])
    if len(text) > args.chars:
        print(
            f"\n… 전체 {len(text):,}자 중 {args.chars:,}자만 표시 (--chars 로 조정, --save 로 저장)"
        )
    return 0


def cmd_dart_watch(client: DartClient, args: argparse.Namespace) -> int:
    deadline_seconds = None
    if args.weekdays:
        from .market_hours import is_weekday

        if not is_weekday():
            print("주말이라 감시하지 않고 종료합니다.")
            return 0
    if args.until:
        from datetime import datetime

        from .market_hours import KST

        hh, mm = (int(x) for x in args.until.split(":"))
        now = datetime.now(KST)
        deadline_seconds = (
            now.replace(hour=hh, minute=mm, second=0, microsecond=0) - now
        ).total_seconds()
        if deadline_seconds <= 0:
            print(f"이미 {args.until}이 지났습니다. 감시하지 않고 종료합니다.")
            return 0

    handlers = [console, JsonlWriter()]
    if not args.no_archive:
        handlers.append(DocumentArchiver())

    from .watchlist import Watchlist

    watchlist = Watchlist.load()
    # --watchlist는 관심종목 파일을, --stock은 명령줄 인자를 쓴다. 둘 다 없으면 전 종목.
    stock_codes = list(args.stock or [])
    if args.watchlist:
        if not watchlist:
            print(
                "관심종목이 비어 있습니다. `aistock watchlist add <종목코드>` 로 추가하세요.",
                file=sys.stderr,
            )
            return 2
        stock_codes = watchlist.codes

    telegram = None
    if args.telegram:
        from .notify import TelegramConfig, TelegramNotifier, TelegramSignalHandler

        if not TelegramConfig.available():
            print(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다. `aistock telegram` 을 먼저 실행해 보세요.",
                file=sys.stderr,
            )
            return 2
        telegram = TelegramSignalHandler(
            TelegramNotifier.create(),
            kiwoom=KiwoomClient.create(),
            watchlist=watchlist,
            min_confidence=args.notify_confidence,
            send_headlines=args.notify_headlines,
        )
        handlers.append(telegram.on_disclosure)

    signal_handler = None
    if args.signals:
        from .signal import SignalAnalyzer, SignalHandler, console_signal

        if not SignalAnalyzer.has_credentials():
            print(
                "ANTHROPIC_API_KEY가 없습니다. --signals 없이 실행하거나 .env에 키를 넣어주세요.",
                file=sys.stderr,
            )
            return 2
        callbacks = [console_signal] + ([telegram] if telegram else [])
        if args.propose:
            from .notify import TelegramConfig, TelegramNotifier
            from .trading import TradeProposer, TradingPolicy

            if not TelegramConfig.available():
                print("--propose 는 텔레그램 승인이 필요합니다.", file=sys.stderr)
                return 2
            policy = TradingPolicy.from_env()
            callbacks.append(
                TradeProposer(KiwoomClient.create(), TelegramNotifier.create(), policy=policy)
            )
            print(
                f"주문 제안 켜짐 — 확신 {policy.min_confidence}% 이상, "
                f"1건 {policy.budget_per_trade:,}원, 하루 매수 {policy.max_daily_buys}건, 승인 유효 {policy.proposal_ttl_min}분"
            )
            print("  승인은 `aistock bot` 프로세스가 처리합니다. 봇이 떠 있어야 합니다.")
        signal_handler = SignalHandler(callbacks=callbacks)
        handlers.append(signal_handler)
    elif args.propose:
        print("--propose 는 --signals 와 함께 써야 합니다.", file=sys.stderr)
        return 2
    elif telegram is not None and not args.notify_headlines:
        print(
            "경고: --telegram만 켜면 보낼 것이 없습니다. --signals 또는 --notify-headlines 를 함께 쓰세요.",
            file=sys.stderr,
        )

    poller = DisclosurePoller(
        client,
        handlers=handlers,
        types=None if args.all_types else tuple(args.types or dart.MARKET_MOVING_TYPES),
        stock_codes=stock_codes or None,
        interval=args.interval,
    )

    scope = "전체 유형" if args.all_types else ", ".join(poller.types)
    watch = f"{len(stock_codes)}개 종목" if stock_codes else "전 종목"
    print(f"공시 감시 시작 — {scope} / {watch} / {args.interval:g}초 간격")
    print(f"기록: {JSONL_PATH}")

    if not args.replay:
        skipped = poller.bootstrap()
        print(f"기존 공시 {skipped}건은 건너뜁니다. (--replay 로 포함)")
    print("중단하려면 Ctrl+C\n")

    def _stop(*_: object) -> None:
        print("\n중단 요청 — 상태를 저장합니다.")
        poller.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if deadline_seconds is not None:
        import threading

        timer = threading.Timer(deadline_seconds, poller.stop)
        timer.daemon = True
        timer.start()
        print(f"{args.until}(KST)에 자동 종료합니다.")

    poller.run(max_cycles=args.cycles)
    print(
        f"\n종료. 요청 {poller.requests_made}건, 원문 대기 {len(poller.state.pending)}건이 "
        f"다음 실행에 이어집니다."
    )
    if telegram is not None:
        t = telegram.stats
        print(f"텔레그램: 전송 {t['sent']}건 / 필터 {t['filtered']}건 / 실패 {t['failed']}건")
    if signal_handler is not None:
        stats = signal_handler.stats
        usage = signal_handler.analyzer.usage
        print(
            f"신호: 분류 {stats['triaged']}건 (생략 {stats['skipped']}) / "
            f"분석 {stats['analyzed']}건 / 실패 {stats['failed']}건"
        )
        print(
            f"토큰: 입력 {usage.input_tokens:,} (캐시읽기 {usage.cache_read_tokens:,}) "
            f"/ 출력 {usage.output_tokens:,}"
        )
    return 0


def _confirm(prompt: str) -> bool:
    """실계좌 주문 직전 확인. 플래그 하나로 통과되면 사고가 난다."""
    if not sys.stdin.isatty():
        print("확인 입력이 필요합니다. 터미널에서 직접 실행하세요.", file=sys.stderr)
        return False
    try:
        answer = input(f"{prompt}\n진행하려면 '주문' 을 입력하세요 (취소: Enter): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "주문"


def cmd_order(client: KiwoomClient, args: argparse.Namespace) -> int:
    """매수·매도 주문. 실계좌에 즉시 반영된다."""
    from .order import OrderError, OrderRequest, OrderType, max_order_amount, place, validate

    order_type = OrderType.MARKET if args.market else OrderType.LIMIT
    price = args.price or 0
    if order_type is OrderType.LIMIT and not price:
        # 지정가인데 가격을 안 주면 현재가를 제안한다 — 조용히 시장가로 바꾸지 않는다.
        try:
            cur = domestic.stock_info(client, args.code).get("cur_prc", "")
            suggest = abs(int(str(cur).lstrip("+-") or 0))
            print(
                f"현재가 {suggest:,}원. --price 로 지정가를 주거나 --market 으로 시장가를 쓰세요.",
                file=sys.stderr,
            )
        except KiwoomError:
            print("지정가 주문은 --price 가 필요합니다.", file=sys.stderr)
        return 2

    request = OrderRequest(
        side=args.side,
        stock_code=args.code,
        quantity=args.quantity,
        price=price,
        order_type=order_type,
        exchange=args.exchange,
    )
    try:
        validate(request)
    except OrderError as exc:
        print(f"주문 거부: {exc}", file=sys.stderr)
        return 2

    settings = client.settings
    mode = "실전투자" if settings.is_real else "모의투자"
    try:
        quote = domestic.stock_info(client, args.code)
        name = quote.get("stk_nm", args.code)
        cur = quote.get("cur_prc", "-")
    except KiwoomError:
        name, cur = args.code, "-"

    print(f"\n  계좌   {settings.account_no} ({mode})")
    print(f"  종목   {name} ({args.code})  현재가 {cur}")
    print(f"  주문   {request.describe()}")
    print(f"  상한   {max_order_amount():,}원\n")
    if settings.is_real:
        print("  ⚠️  실전투자 계좌입니다. 되돌릴 수 없습니다.\n")

    if not args.yes and not _confirm("위 내용으로 주문합니다."):
        print("취소했습니다.")
        return 1

    try:
        result = place(client, request)
    except OrderError as exc:
        print(f"주문 실패: {exc}", file=sys.stderr)
        return 1
    print(f"\n주문 접수 완료 — 주문번호 {result.order_no}")
    print("체결 확인: aistock orders")
    return 0


def cmd_orders(client: KiwoomClient, args: argparse.Namespace) -> int:
    """미체결 주문 조회."""
    from .order import pending_orders

    rows = pending_orders(client, stock_code=args.code or "")
    if not rows:
        print("미체결 주문이 없습니다.")
        return 0
    print(f"미체결 {len(rows)}건\n")
    print(
        f"  {'주문번호':<9} {'종목':<14} {'구분':<6} {'주문수량':>8} {'미체결':>8} {'주문가':>10} {'상태':<10} 시각"
    )
    print("  " + "-" * 82)
    for r in rows:
        print(
            f"  {r.order_no:<9} {r.name[:12]:<14} {r.side:<6} {r.quantity:>8,} {r.remaining:>8,} "
            f"{r.price:>10,} {r.status[:8]:<10} {r.time}"
        )
    return 0


def cmd_cancel(client: KiwoomClient, args: argparse.Namespace) -> int:
    """주문 취소."""
    from .order import OrderError, cancel, pending_orders

    target = next((r for r in pending_orders(client) if r.order_no == args.order_no), None)
    if target is None:
        print(f"미체결 목록에 주문번호 {args.order_no}가 없습니다.", file=sys.stderr)
        return 1

    qty = args.quantity or 0
    label = "잔량 전부" if qty == 0 else f"{qty:,}주"
    print(f"\n  {target.name} ({target.stock_code}) {target.side}")
    print(f"  주문 {target.quantity:,}주 중 미체결 {target.remaining:,}주 · {label} 취소\n")
    if not args.yes and not _confirm("위 주문을 취소합니다."):
        print("취소하지 않았습니다.")
        return 1
    try:
        no = cancel(client, args.order_no, target.stock_code, qty, exchange=args.exchange)
    except OrderError as exc:
        print(f"취소 실패: {exc}", file=sys.stderr)
        return 1
    print(f"취소 접수 완료 — 주문번호 {no}")
    return 0


def cmd_bot(client: KiwoomClient, args: argparse.Namespace) -> int:
    """텔레그램 봇 대기. 명령을 받아 조회·주문을 수행한다."""
    from .bot import TelegramBot
    from .notify import TelegramConfig

    if not TelegramConfig.available():
        print(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다. `aistock telegram` 참고.",
            file=sys.stderr,
        )
        return 2

    bot = TelegramBot(TelegramConfig.from_env(), client)
    print(f"봇 대기 시작 — 허용 chat_id: {', '.join(sorted(bot.allowed))}")
    if client.settings.is_real:
        print("⚠️  실전투자 계좌입니다. /buy · /sell 은 확인 없이 즉시 체결됩니다.")
    print("중단하려면 Ctrl+C\n")

    def _stop(*_):
        print("\n중단합니다.")
        bot.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        bot.run(max_cycles=args.cycles)
    finally:
        bot.close()
        print(
            f"처리 {bot.stats['handled']}건 / 거부 {bot.stats['rejected']}건 / 오류 {bot.stats['errors']}건"
        )
    return 0


def cmd_models(client: KiwoomClient, args: argparse.Namespace) -> int:
    """사용 가능한 분석 모델 목록."""
    from .signal.analyzer import SignalError
    from .signal.ollama import OllamaAnalyzer

    print("Ollama (사내망)")
    try:
        with OllamaAnalyzer() as o:
            print(f"  {o.base_url}  현재 모델: {o.model}")
            for name in o.models():
                mark = " ←" if name == o.model else ""
                print(f"    {name}{mark}")
    except SignalError as exc:
        print(f"  연결 실패: {exc}")
    return 0


def cmd_trend(client: KiwoomClient, args: argparse.Namespace) -> int:
    """관심종목 추이 차트. 기간 등락률과 52주 위치를 함께 본다."""
    from .backtest import PriceCache
    from .chart import compute_stats, render_trends, stats_text
    from .watchlist import Watchlist

    wl = Watchlist.load()
    codes = args.codes or wl.codes
    if not codes:
        print("종목을 지정하거나 관심종목을 추가하세요.", file=sys.stderr)
        return 2

    prices = PriceCache(client)
    items, stats = [], []
    for code in codes:
        history = prices.get(code)
        if history is None:
            print(f"  {code}: 일봉 조회 실패", file=sys.stderr)
            continue
        name = wl.note_for(code) or code
        items.append((history, code, name))
        st = compute_stats(history, code, name)
        if st:
            stats.append(st)

    if not items:
        print("그릴 데이터가 없습니다.", file=sys.stderr)
        return 1

    for s in stats:
        rets = "  ".join(f"{k} {'+' if v > 0 else ''}{v:.1f}%" for k, v in s.returns.items())
        print(f"  {s.name[:14]:<16} {s.last_price:>10,}원  {rets}")
        print(f"  {'':16} 52주 {s.low_52w:,}~{s.high_52w:,} · 위치 {s.position_52w:.0f}%")

    png = render_trends(items, days=args.days, title=f"최근 {args.days}거래일 추이")
    out = Path(args.out) if args.out else LOG_DIR / "trend.png"
    out.write_bytes(png)
    print(f"\n차트 저장: {out}")

    if args.telegram:
        from .notify import TelegramConfig, TelegramError, TelegramNotifier

        if not TelegramConfig.available():
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다.", file=sys.stderr)
            return 2
        try:
            with TelegramNotifier.create() as n:
                n.send_photo(png, stats_text(stats))
            print("텔레그램으로 전송했습니다.")
        except TelegramError as exc:
            print(f"텔레그램 전송 실패: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_ticker(client: KiwoomClient, args: argparse.Namespace) -> int:
    """관심종목 시세를 장중에 주기적으로 알린다."""
    import time as _time

    from . import market_hours as mh
    from .app_watchlist import fetch_all
    from .notify import TelegramConfig, TelegramError, TelegramNotifier, ticker_message
    from .watchlist import Watchlist

    # 기본은 키움 앱 관심종목이다. 앱에서 종목을 추가하면 여기에도 바로 반영된다.
    targets: list[dict] = []
    if args.local:
        wl = Watchlist.load()
        targets = [
            {"code": e.code, "note": e.note, "market": "domestic", "section": "국내"}
            for e in wl.entries
        ]
    else:
        for s in fetch_all(client):
            targets.append(
                {
                    "code": s.code,
                    "note": "",
                    "market": s.market,
                    "exchange": s.exchange,
                    "section": "🇺🇸 미국" if s.is_us else "🇰🇷 국내",
                }
            )
    if not targets:
        where = "watchlist.txt" if args.local else "키움 앱 관심종목"
        print(f"{where}가 비어 있습니다.", file=sys.stderr)
        return 2

    notifier = None
    if args.telegram:
        if not TelegramConfig.available():
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다.", file=sys.stderr)
            return 2
        notifier = TelegramNotifier.create()

    def _lookup_name(t: dict) -> str:
        """닫힌 장 종목의 이름. 시세를 안 받았으면 여기서 한 번 더 조회한다."""
        try:
            if t["market"] == "us":
                q = overseas.quote(client, t["code"], t.get("exchange") or "ND")
                return q.get("stk_nm") or q.get("stk_enm") or t["code"]
            return domestic.stock_info(client, t["code"]).get("stk_nm") or t["code"]
        except KiwoomError:
            return t["code"]

    def _send_chart(notifier, picked: list[dict], names: dict[str, str]) -> None:
        """6개월 추이 차트. 시세 알림만으로는 흐름을 알 수 없다."""
        from .backtest import PriceCache
        from .chart import compute_stats, render_trends, stats_text

        prices = PriceCache(client)
        items, stats = [], []
        for t in picked[:6]:  # 종목이 많으면 이미지가 세로로 길어져 읽기 어렵다
            history = prices.get(t["code"], exchange=t.get("exchange", ""))
            if history is None:
                continue
            # 앱 관심종목에는 이름이 없다. 방금 조회한 시세에서 받은 이름을 쓰되,
            # 닫힌 장 종목은 시세를 안 받았으므로 별도로 조회한다.
            name = names.get(t["code"]) or t["note"] or _lookup_name(t)
            items.append((history, t["code"], name))
            st = compute_stats(history, t["code"], name)
            if st:
                stats.append(st)
        if not items:
            return
        try:
            png = render_trends(
                items, days=args.chart_days, title=f"최근 {args.chart_days}거래일 추이"
            )
            notifier.send_photo(png, stats_text(stats))
        except (TelegramError, ValueError) as exc:
            print(f"  차트 전송 실패: {exc}", file=sys.stderr)

    def _send_fx_chart(notifier) -> None:
        """환율 추이. 키움은 현재값만 주므로 조회할 때마다 쌓은 이력으로 그린다."""
        from . import fx as fx_mod
        from .chart import render_fx

        history = fx_mod.load_history()
        if len(history) < 2:
            return  # 아직 선을 그릴 만큼 쌓이지 않았다
        try:
            notifier.send_photo(render_fx(history), "원/달러 환율 추이")
        except (TelegramError, ValueError) as exc:
            print(f"  환율 차트 전송 실패: {exc}", file=sys.stderr)

    def active_targets() -> list[dict]:
        """지금 열려 있는 장의 종목만. 닫힌 장의 종가를 반복해 보내면 알림이 무의미해진다."""
        if args.market == "domestic":
            return [t for t in targets if t["market"] == "domestic"]
        if args.market == "us":
            return [t for t in targets if t["market"] == "us"]
        if args.force:
            # 강제 실행은 "지금 전부 보고 싶다"는 뜻이다. 장 구분으로 걸러내지 않는다.
            return targets
        picked = []
        if mh.in_session():
            picked += [t for t in targets if t["market"] == "domestic"]
        if mh.in_us_session():
            picked += [t for t in targets if t["market"] == "us"]
        # 어느 장도 안 열렸으면(--force 등) 전부 보여준다.
        return picked or targets

    def snapshot() -> list[dict]:
        rows = []
        for t in active_targets():
            try:
                if t["market"] == "us":
                    quote = overseas.quote(client, t["code"], t.get("exchange") or "ND")
                    name = quote.get("stk_nm") or quote.get("stk_enm") or t["code"]
                else:
                    quote = domestic.stock_info(client, t["code"])
                    name = quote.get("stk_nm") or t["note"]
            except KiwoomError as exc:
                logging.getLogger(__name__).warning("시세 조회 실패 (%s): %s", t["code"], exc)
                quote, name = {}, t["note"] or t["code"]
            rows.append({"code": t["code"], "name": name, "quote": quote, "section": t["section"]})
        return rows

    def emit(closing: bool = False) -> None:
        now = mh.now_kst()
        rows = snapshot()
        label = f"{now:%H:%M}"
        if mh.in_us_session(now) and not mh.in_session(now):
            label += f"  ·  {mh.us_session_label(now)}"
        text = ticker_message(rows, at=label, closing=closing)

        fx_rate = None
        if args.fx:
            from . import fx as fx_mod

            try:
                # 달러도 공개 API 로(2026-09-17). 실패하면 키움으로 물러선다.
                fx_rate = fx_mod.fetch_usd(client)
                text += "\n\n" + fx_mod.summary(fx_rate, fx_mod.load_history())
            except KiwoomError as exc:
                logging.getLogger(__name__).warning("환율 조회 실패: %s", exc)
        for r in rows:
            q = r["quote"]
            print(
                f"  {now:%H:%M}  {r['code']}  {r['name'][:12]:<14} {q.get('cur_prc', '-'):>10} ({q.get('flu_rt', '-')}%)"
            )
        if fx_rate is not None:
            print(
                (
                    # 공개 API 는 매수/매도가 없어 같은 값이 두 번 찍힌다 → 한 값만.
                    f"  {now:%H:%M}  환율    {fx_rate.mid:,.2f}"
                    + (
                        f" (매수 {fx_rate.buy:,.2f} / 매도 {fx_rate.sell:,.2f})"
                        if fx_rate.spread > 0.005
                        else ""
                    )
                    + (f" · {fx_rate.src}" if fx_rate.src else "")
                )
            )
        if notifier is not None:
            try:
                notifier.send(text)
            except TelegramError as exc:
                print(f"  텔레그램 전송 실패: {exc}", file=sys.stderr)
            if args.chart:
                # 차트는 과거 추이라 장 개폐와 무관하다. 열린 장만 그리면
                # 09:00·15:30 알림에서 미국 종목이 통째로 빠진다.
                _send_chart(notifier, targets, {r["code"]: r["name"] for r in rows})

    if args.once:
        # launchd는 고정 시각만 안다. 서머타임에 따라 미국장 시각이 1시간 밀리므로
        # 실제로 장이 열렸는지는 여기서 판정한다.
        if not args.force:
            kr_open = mh.in_session() and mh.is_trading_day(client)
            us_open = mh.in_us_session()
            if args.market == "domestic" and not kr_open:
                print("국내장 시간이 아니라 건너뜁니다.")
                return 0
            if args.market == "us" and not us_open:
                print(f"미국장 시간이 아니라 건너뜁니다. (현지 {mh.us_session_label()})")
                return 0
            if args.market == "auto" and not kr_open and not us_open:
                print("열려 있는 장이 없어 건너뜁니다.")
                return 0
        emit()
        return 0

    src = "watchlist.txt" if args.local else "키움 앱"
    print(f"{src} 관심종목 {len(targets)}개 · {args.interval}분 간격 · 장중(09:00~15:30)에만 전송")
    print("중단하려면 Ctrl+C\n")
    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True
        print("\n중단합니다.")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    sent_close = False
    while not stop["now"]:
        now = mh.now_kst()
        if not mh.in_session(now) or not mh.is_trading_day(client, now):
            # 휴장이면 다음 확인까지 길게 쉰다. 짧게 반복하면 토큰만 축낸다.
            _time.sleep(min(600, args.interval * 60))
            sent_close = False
            continue

        emit()
        nxt = mh.next_tick(args.interval, now)
        _, close_at = mh.session_bounds(now)
        if nxt > close_at and not sent_close:
            # 마지막 틱이 장 마감을 넘기면 마감 시각에 한 번 더 보낸다.
            nxt, sent_close = close_at, True
        while not stop["now"] and mh.now_kst() < nxt:
            _time.sleep(min(20, max(1, (nxt - mh.now_kst()).total_seconds())))
        if stop["now"]:
            break
        if sent_close:
            emit(closing=True)
            sent_close = False

    if notifier is not None:
        notifier.close()
    return 0


def cmd_meritz(args: argparse.Namespace) -> int:
    """메리츠증권 조회. 키움 토큰이 필요 없어 별도 경로로 돈다."""
    from .brokers.meritz import MeritzClient, MeritzConfig, MeritzError, mac_address

    if not MeritzConfig.available():
        print(".env에 MERITZ_APP_KEY / MERITZ_APP_SECRET을 넣어주세요.", file=sys.stderr)
        return 2
    try:
        with MeritzClient.create() as client:
            if args.meritz_command == "check":
                client.token()
                d = client.deposit()
                print(f"메리츠 연결 확인 — 토큰 발급 OK · 예수금 {d.cash:,}원")
                print(f"  mac_address 헤더: {mac_address()[:4]}…{mac_address()[-2:]}")
                return 0
            snap = client.snapshot()
    except MeritzError as exc:
        print(f"메리츠 조회 실패: {exc}", file=sys.stderr)
        return 1
    d = snap.deposit
    print(
        f"[메리츠] 예수금 {d.cash:,}원 · D+2 {d.d2_cash:,}원 · 주문가능 {d.orderable:,}원 · 출금가능 {d.withdrawable:,}원"
    )
    for c, amt in snap.foreign_cash.items():
        print(f"  외화 {c} {amt:,.2f}")
    if not snap.holdings:
        print("  보유 종목이 없습니다.")
    for h in snap.holdings:
        unit = "$" if h.market == "US" else ""
        print(
            f"  {h.market} {h.code:<8} {h.name[:14]:<16} {h.quantity:>10,.4g}주  "
            f"매입 {unit}{h.avg_price:,.2f} → {unit}{h.price:,.2f}  {h.profit:+,.2f} ({h.profit_rate:+.2f}%)"
        )
    return 0


def cmd_balance(client: KiwoomClient, args: argparse.Namespace) -> int:
    """보유종목 시세·평가손익. 조회만 하며 주문은 나가지 않는다."""
    from .account import fetch_balance, fetch_deposit, fetch_us_balance

    balance = fetch_balance(client, exchange=args.exchange)
    # 예수금·미국 잔고는 실패해도 국내 잔고는 보여준다.
    try:
        deposit = fetch_deposit(client)
    except (KiwoomError, ValueError) as exc:
        print(f"(예수금 조회 실패: {exc})", file=sys.stderr)
        deposit = None
    try:
        us = fetch_us_balance(client)
    except (KiwoomError, ValueError) as exc:
        print(f"(미국 잔고 조회 실패: {exc})", file=sys.stderr)
        us = None

    _print_domestic_balance(balance)
    if deposit is not None:
        print(
            f"\n  예수금 {deposit.cash:,}원 · D+2 {deposit.d2_cash:,}원 · "
            f"주문가능 {deposit.orderable:,}원 · 출금가능 {deposit.withdrawable:,}원"
        )
    if us is not None:
        print("\n[미국]")
        if not us.holdings:
            print("  보유 종목이 없습니다.")
        for h in us.holdings:
            print(
                f"  {h.symbol:<8} {h.quantity:>6,}주  매입 ${h.avg_price:,.2f} → ${h.current_price:,.2f}  "
                f"{h.profit:>+,.2f}$ ({h.profit_rate:+.2f}%) · {h.profit_krw:+,}원"
            )
        print(f"  외화예수금 ${us.usd_cash:,.2f} · 주문가능 ${us.usd_orderable:,.2f}")
        if us.krw_cash:
            print(f"  원화예수금(해외) {us.krw_cash:,}원")

    if args.telegram:
        from .notify import TelegramConfig, TelegramError, TelegramNotifier, account_message

        if not TelegramConfig.available():
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다.", file=sys.stderr)
            return 2
        try:
            with TelegramNotifier.create() as n:
                n.send(account_message(balance, deposit, us))
            print("\n텔레그램으로 전송했습니다.")
        except TelegramError as exc:
            print(f"텔레그램 전송 실패: {exc}", file=sys.stderr)
            return 1
    return 0


def _print_domestic_balance(balance) -> None:
    print("[국내]")
    if not balance.holdings:
        print("  보유 종목이 없습니다.")
        return

    print(f"보유 {len(balance.holdings)}종목\n")
    print(
        f"  {'종목':<16} {'수량':>7} {'매입가':>10} {'현재가':>10} {'평가손익':>13} {'수익률':>8} {'비중':>6}"
    )
    print("  " + "-" * 78)
    for h in balance.holdings:
        print(
            f"  {h.name[:14]:<16} {h.quantity:>7,} {h.avg_price:>10,} {h.current_price:>10,} "
            f"{h.eval_profit:>+13,} {h.profit_rate:>+7.2f}% {h.weight:>5.1f}%"
        )
    print("  " + "-" * 78)
    print(
        f"  {'합계':<16} {'':>7} {balance.total_purchase:>10,} {balance.total_eval:>10,} "
        f"{balance.total_profit:>+13,} {balance.total_profit_rate:>+7.2f}%"
    )
    if balance.estimated_assets:
        print(f"  추정예탁자산 {balance.estimated_assets:,}원")


def cmd_watchlist(client: KiwoomClient, args: argparse.Namespace) -> int:
    from .watchlist import Watchlist

    wl = Watchlist.load()

    if args.watchlist_command == "add":
        added = []
        for code in args.codes:
            note = ""
            try:
                note = domestic.stock_info(client, code).get("stk_nm", "")
            except KiwoomError:
                pass
            if wl.add(code, note):
                added.append(f"{code} {note}".strip())
        wl.save()
        print(f"추가 {len(added)}건" + (f": {', '.join(added)}" if added else " (이미 있음)"))
        return 0

    if args.watchlist_command == "remove":
        removed = [c for c in args.codes if wl.remove(c)]
        wl.save()
        print(f"삭제 {len(removed)}건" + (f": {', '.join(removed)}" if removed else " (없음)"))
        return 0

    # list
    if not wl:
        print("관심종목이 비어 있습니다. `aistock watchlist add 005930` 으로 추가하세요.")
        print(f"파일: {wl.path}")
        return 0
    print(f"관심종목 {len(wl)}개 · {wl.path}\n")
    for e in wl.entries:
        line = f"  {e.code}  {e.note}"
        if args.quotes:
            try:
                d = domestic.stock_info(client, e.code)
                line = f"  {e.code}  {d.get('stk_nm', e.note)[:14]:<16} {d.get('cur_prc', ''):>10} ({d.get('flu_rt', '')}%)"
            except KiwoomError as exc:
                line += f"  (시세 조회 실패: {exc})"
        print(line)
    return 0


def cmd_telegram(client: KiwoomClient, args: argparse.Namespace) -> int:
    from .notify import TelegramConfig, TelegramError, TelegramNotifier

    if not TelegramConfig.available():
        print(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다.\n"
            "  1) 텔레그램에서 @BotFather 에게 /newbot → 토큰 발급\n"
            "  2) 만든 봇과 대화를 시작하고 아무 메시지나 전송\n"
            "  3) https://api.telegram.org/bot<토큰>/getUpdates 에서 chat.id 확인\n"
            "  4) .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 입력",
            file=sys.stderr,
        )
        return 2
    try:
        with TelegramNotifier.create() as n:
            print(f"봇 연결: {n.check()}")
            n.send(
                "✅ <b>aistock 연결 확인</b>\n\n"
                "공시 신호를 이 채널로 보냅니다.\n"
                "<i>주문은 자동으로 나가지 않습니다 — 직접 판단해 주세요.</i>"
            )
            print("테스트 메시지를 보냈습니다. 텔레그램을 확인해 주세요.")
    except (TelegramError, ValueError) as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_backtest(client: KiwoomClient, args: argparse.Namespace) -> int:
    from .backtest import (
        BenchmarkCache,
        CostModel,
        Entry,
        PriceCache,
        load_signals,
        render,
        run,
    )

    path = Path(args.signals) if args.signals else SIGNALS_PATH
    rows = load_signals(path)
    if not rows:
        print(f"신호 파일이 비어 있습니다: {path}", file=sys.stderr)
        print("먼저 `aistock dart watch --signals`로 신호를 쌓으세요.", file=sys.stderr)
        return 1

    prices = PriceCache(client, base_dt=args.base_date)
    benchmark = None if args.no_benchmark else BenchmarkCache(client, base_dt=args.base_date)
    modes = [Entry.NEXT_OPEN, Entry.SAME_CLOSE] if args.compare_entry else [Entry(args.entry)]
    cost = (
        CostModel.free()
        if args.gross
        else CostModel(
            commission_rate=args.commission / 100,
            slippage_rate=args.slippage / 100,
        )
    )

    print(f"신호 {len(rows)}건 · {path}\n")
    for mode in modes:
        trades, skipped, merged = run(
            rows,
            prices,
            horizon=args.horizon,
            entry=mode,
            min_confidence=args.min_confidence,
            deduplicate=not args.keep_duplicates,
            cost=cost,
            benchmark=benchmark,
        )
        print(
            render(
                trades,
                skipped,
                horizon=args.horizon,
                entry=mode.value,
                merged=merged,
                cost=cost,
            )
        )
        print()
    if args.compare_entry:
        print("  next_open과 same_close의 격차가 곧 '당일 반응을 잡을 수 있느냐'의 가치다.")
        print("  same_close는 장마감 후 공시에서는 달성 불가능한 상한선이다.")
    return 0


def cmd_signal(client: DartClient, args: argparse.Namespace) -> int:
    """공시 하나를 Claude로 분석해 신호를 뽑는다. 프롬프트 튜닝용."""
    from .signal import SignalAnalyzer, SignalError

    if not SignalAnalyzer.has_credentials():
        print(
            "ANTHROPIC_API_KEY가 없습니다. https://console.anthropic.com 에서 발급 후 .env에 넣어주세요.",
            file=sys.stderr,
        )
        return 2

    index = CorpCodeIndex.load(client=client)
    items = dart.search(client, bgn_de=args.rcept_no[:8], end_de=args.rcept_no[:8], max_pages=10)
    disclosure = next((d for d in items if d.rcept_no == args.rcept_no), None)
    if disclosure is None:
        print(f"해당 날짜 공시 목록에서 {args.rcept_no}를 찾지 못했습니다.", file=sys.stderr)
        return 1
    corp = index.get(disclosure.stock_code)

    analyzer = SignalAnalyzer(model=args.model)
    print(f"{disclosure.corp_name} — {disclosure.report_nm}")
    print(f"{disclosure.url}\n")

    try:
        triage = analyzer.triage(disclosure)
        print(f"[1단계 분류] {triage.event_type} / 영향추정 {triage.expected_impact.value}")
        print(f"  원문 분석 가치: {'있음' if triage.worth_reading else '없음'} — {triage.reason}\n")

        if not triage.worth_reading and not args.force:
            print("(원문 분석을 생략했습니다. --force 로 강제 실행)")
            return 0

        text = dart.document_text(client, args.rcept_no)
        print(f"[2단계 분석] 원문 {len(text):,}자")
        signal = analyzer.analyze(disclosure, text)
    except SignalError as exc:
        print(f"분석 실패: {exc}", file=sys.stderr)
        return 1

    print(f"\n  판단      {signal.action.value.upper()}  (confidence {signal.confidence}%)")
    print(f"  영향/기간 {signal.impact.value} / {signal.horizon.value}")
    print(
        f"  종목      {disclosure.corp_name}({disclosure.stock_code}) {corp.corp_code if corp else ''}"
    )
    print(f"\n  {signal.rationale}")
    if signal.key_facts:
        print("\n  핵심 수치")
        for fact in signal.key_facts:
            print(f"    · {fact}")
    if signal.risks:
        print("\n  리스크")
        for risk in signal.risks:
            print(f"    · {risk}")
    usage = analyzer.usage
    print(
        f"\n  토큰 입력 {usage.input_tokens:,} (캐시읽기 {usage.cache_read_tokens:,}) "
        f"/ 출력 {usage.output_tokens:,}"
    )
    print(f"  주문 후보: {'예' if signal.actionable else '아니오'}")
    return 0


def _add_dart_commands(sub: argparse._SubParsersAction) -> None:
    dart_parser = sub.add_parser("dart", help="DART 전자공시")
    dart_sub = dart_parser.add_subparsers(dest="dart_command", required=True)

    p = dart_sub.add_parser("sync", help="고유번호 인덱스 갱신")
    p.set_defaults(func=cmd_dart_sync, backend="dart")

    p = dart_sub.add_parser("list", help="종목별 최근 공시")
    p.add_argument("code", help="키움 종목코드 (예: 005930)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--type", choices=sorted(dart.PBLNTF_TY), help="공시유형 대분류")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_dart_list, backend="dart")

    p = dart_sub.add_parser("today", help="당일 주요 공시 (주가 영향 유형만)")
    p.add_argument("--date", help="YYYYMMDD, 생략 시 오늘")
    p.add_argument("--market", choices=["Y", "K", "N", "E"], help="Y=유가, K=코스닥")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_dart_today, backend="dart")

    p = dart_sub.add_parser("search", help="회사명으로 종목코드/corp_code 찾기")
    p.add_argument("keyword")
    p.add_argument("--all", action="store_true", help="비상장 포함")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_dart_search, backend="dart")

    p = dart_sub.add_parser("doc", help="공시 원문 텍스트")
    p.add_argument("rcept_no", help="접수번호 14자리")
    p.add_argument("--chars", type=int, default=3000)
    p.add_argument("--save", action="store_true", help="logs/ 에 전문 저장")
    p.set_defaults(func=cmd_dart_doc, backend="dart")

    p = dart_sub.add_parser("watch", help="공시 상시 감시 (폴링 루프)")
    p.add_argument("--interval", type=float, default=30.0, help="폴링 주기(초), 기본 30")
    p.add_argument(
        "--types",
        nargs="+",
        choices=sorted(dart.PBLNTF_TY),
        help=f"감시할 공시유형, 기본 {' '.join(dart.MARKET_MOVING_TYPES)}",
    )
    p.add_argument(
        "--all-types", action="store_true", help="유형 구분 없이 전체 (요청수 1/4, 유형 미표시)"
    )
    p.add_argument("--stock", nargs="+", metavar="CODE", help="관심 종목코드만 감시")
    p.add_argument("--replay", action="store_true", help="이미 떠 있는 당일 공시도 이벤트로 처리")
    p.add_argument("--no-archive", action="store_true", help="원문 전문 파일 저장 안 함")
    p.add_argument("--signals", action="store_true", help="Claude로 매매 신호 생성 (API 비용 발생)")
    p.add_argument("--watchlist", action="store_true", help="관심종목 파일의 종목만 감시")
    p.add_argument("--telegram", action="store_true", help="신호를 텔레그램으로 전송")
    p.add_argument(
        "--notify-confidence", type=int, default=0, help="이 confidence 이상만 텔레그램 전송"
    )
    p.add_argument("--notify-headlines", action="store_true", help="원문 분석 전 공시 속보도 전송")
    p.add_argument(
        "--propose",
        action="store_true",
        help="신호를 기술적 필터로 거른 뒤 텔레그램 [승인] 버튼이 달린 주문 제안으로 전송",
    )
    p.add_argument("--until", metavar="HH:MM", help="이 시각(KST)에 상태를 저장하고 종료")
    p.add_argument("--weekdays", action="store_true", help="주말이면 바로 종료 (launchd용)")
    p.add_argument("--cycles", type=int, help="지정 횟수만 돌고 종료 (테스트용)")
    p.set_defaults(func=cmd_dart_watch, backend="dart")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aistock", description="키움 시세 + DART 공시 조회")
    parser.add_argument("--mode", choices=["real", "demo"], help=".env의 KIWOOM_MODE보다 우선")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="연동 확인").set_defaults(func=cmd_check)

    p = sub.add_parser("quote", help="국내 종목 현재가")
    p.add_argument("code")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("book", help="국내 종목 호가")
    p.add_argument("code")
    p.add_argument("--depth", type=int, default=5, choices=range(1, 11), metavar="1-10")
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("chart", help="국내 종목 일봉")
    p.add_argument("code")
    p.add_argument("--days", type=int, default=20)
    p.set_defaults(func=cmd_chart)

    p = sub.add_parser("us", help="미국 종목 현재가")
    p.add_argument("code")
    p.add_argument("--exchange", choices=["NY", "ND", "NA"], default="ND")
    p.set_defaults(func=cmd_us)

    p = sub.add_parser("etf", help="ETF 전체시세")
    p.add_argument("--mngmcomp", default="0000", help="0000=전체, 3020=KODEX, 3191=TIGER ...")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_etf)

    for side, label in (("buy", "매수"), ("sell", "매도")):
        p = sub.add_parser(side, help=f"{label} 주문 (실계좌)")
        p.add_argument("code", metavar="CODE")
        p.add_argument("quantity", type=int, metavar="QTY")
        p.add_argument("--price", type=int, help="지정가 (원)")
        p.add_argument("--market", action="store_true", help="시장가로 주문")
        p.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
        p.add_argument("--yes", action="store_true", help="확인 입력 생략 (권장하지 않음)")
        p.set_defaults(func=cmd_order, side=side)

    p = sub.add_parser("orders", help="미체결 주문 조회")
    p.add_argument("--code", help="종목코드로 필터")
    p.set_defaults(func=cmd_orders)

    p = sub.add_parser("cancel", help="주문 취소")
    p.add_argument("order_no", metavar="ORDER_NO")
    p.add_argument("--quantity", type=int, help="부분 취소 수량, 생략하면 잔량 전부")
    p.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
    p.add_argument("--yes", action="store_true", help="확인 입력 생략 (권장하지 않음)")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("bot", help="텔레그램 봇 대기 (명령 수행)")
    p.add_argument("--cycles", type=int, help="지정 횟수만 폴링 (테스트용)")
    p.set_defaults(func=cmd_bot)

    p = sub.add_parser("models", help="사용 가능한 분석 모델")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("trend", help="관심종목 추이 차트 (월간·분기 흐름)")
    p.add_argument("codes", nargs="*", metavar="CODE", help="생략하면 관심종목 전체")
    p.add_argument("--days", type=int, default=126, help="거래일 수, 기본 126 (약 6개월)")
    p.add_argument("--telegram", action="store_true", help="차트를 텔레그램으로 전송")
    p.add_argument("--out", help="PNG 저장 경로")
    p.set_defaults(func=cmd_trend)

    p = sub.add_parser("ticker", help="관심종목 시세를 장중 주기 알림")
    p.add_argument("--interval", type=int, default=60, help="분 단위, 기본 60 (매시 정각)")
    p.add_argument("--telegram", action="store_true", help="텔레그램으로 전송")
    p.add_argument("--once", action="store_true", help="지금 한 번만 (휴장일이면 건너뜀)")
    p.add_argument("--force", action="store_true", help="휴장·장외에도 강제 전송")
    p.add_argument("--local", action="store_true", help="키움 앱 대신 watchlist.txt 사용")
    p.add_argument(
        "--market",
        choices=["auto", "domestic", "us"],
        default="auto",
        help="대상 시장. auto=열려 있는 장만 (기본)",
    )
    p.add_argument("--chart", action="store_true", help="6개월 추이 차트도 함께 전송")
    p.add_argument("--fx", action="store_true", help="원/달러 환율도 함께 (--chart 시 그래프도)")
    p.add_argument("--chart-days", type=int, default=126, help="차트 기간(거래일), 기본 126")
    p.set_defaults(func=cmd_ticker)

    p = sub.add_parser("balance", help="보유종목 시세·평가손익 (조회 전용)")
    p.add_argument("--telegram", action="store_true", help="텔레그램으로 전송")
    p.add_argument("--exchange", choices=["KRX", "NXT"], default="KRX")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("watchlist", help="관심종목 관리")
    wl_sub = p.add_subparsers(dest="watchlist_command")
    q = wl_sub.add_parser("list", help="목록 보기")
    q.add_argument("--quotes", action="store_true", help="현재가도 함께")
    q = wl_sub.add_parser("add", help="추가")
    q.add_argument("codes", nargs="+", metavar="CODE")
    q = wl_sub.add_parser("remove", help="삭제")
    q.add_argument("codes", nargs="+", metavar="CODE")
    p.set_defaults(func=cmd_watchlist, watchlist_command="list", quotes=False)

    p = sub.add_parser("telegram", help="텔레그램 봇 연결 확인")
    p.set_defaults(func=cmd_telegram)

    p = sub.add_parser("meritz", help="메리츠증권 계좌 (조회 전용)")
    mz = p.add_subparsers(dest="meritz_command", required=True)
    mz.add_parser("check", help="앱키로 토큰 발급·예수금 조회 확인")
    mz.add_parser("balance", help="메리츠 잔고·예수금·외화")
    p.set_defaults(backend="meritz")

    p = sub.add_parser("profile", help="계정 추가·관리 (계정마다 키움 계좌·텔레그램 봇 따로)")
    prof = p.add_subparsers(dest="profile_command", required=True)
    q = prof.add_parser("create", help="새 계정 설정 파일 만들기")
    q.add_argument("name", help="계정 이름 (영문·숫자, 예: wife, isa)")
    q.add_argument(
        "--copy-anthropic-key",
        action="store_true",
        help="경제 봇용 Claude 키를 메인 것으로 채움 (비용은 메인 키 주인에게)",
    )
    prof.add_parser("list", help="계정 목록·실행 상태")
    q = prof.add_parser("start", help="설정 검증 후 이 계정 봇을 상시 실행")
    q.add_argument("name")
    q = prof.add_parser("stop", help="이 계정 봇 멈추기")
    q.add_argument("name")
    p.set_defaults(backend="none")

    _add_dart_commands(sub)

    p = sub.add_parser("backtest", help="신호를 일봉과 대조해 수익성 검증")
    p.add_argument("--signals", help=f"신호 파일, 기본 {SIGNALS_PATH}")
    p.add_argument("--horizon", type=int, default=5, help="보유 거래일 수, 기본 5")
    p.add_argument(
        "--entry",
        choices=["next_open", "same_close"],
        default="next_open",
        help="진입 시점. next_open=다음 거래일 시가(기본, 안전), same_close=당일 종가(낙관적)",
    )
    p.add_argument("--compare-entry", action="store_true", help="두 진입 시점을 모두 계산해 비교")
    p.add_argument("--min-confidence", type=int, default=0)
    p.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="같은 종목·같은 날 신호를 병합하지 않음 (기본은 병합 — 표본 부풀림 방지)",
    )
    p.add_argument("--base-date", help="일봉 조회 기준일 YYYYMMDD, 기본 오늘")
    p.add_argument(
        "--commission",
        type=float,
        default=0.015,
        help="위탁수수료율 %%, 매수·매도 각각. 기본 0.015 (키움 온라인)",
    )
    p.add_argument(
        "--slippage", type=float, default=0.0, help="슬리피지 %%, 진입·청산 각각. 기본 0"
    )
    p.add_argument("--gross", action="store_true", help="비용 미반영 총수익으로 계산")
    p.add_argument("--no-benchmark", action="store_true", help="지수 대비 비교 생략")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("signal", help="공시 하나를 Claude로 분석 (프롬프트 튜닝용)")
    p.add_argument("rcept_no", help="접수번호 14자리")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--force", action="store_true", help="1단계에서 걸러져도 원문 분석 강행")
    p.set_defaults(func=cmd_signal, backend="dart")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )
    # httpx는 INFO에서 전체 URL을 찍는다. DART는 인증키를 쿼리스트링으로 받으므로
    # 그대로 두면 키가 터미널과 로그 파일에 그대로 남는다.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # DART 명령은 키움 토큰이 필요 없다. 괜히 발급하면 요청만 낭비된다.
    backend = getattr(args, "backend", "kiwoom")
    if backend == "none":
        from .profiles import main_cli

        return main_cli(args)
    if backend == "meritz":
        return cmd_meritz(args)
    opener = (
        (lambda: DartClient.create())
        if backend == "dart"
        else (lambda: KiwoomClient.create(args.mode))
    )

    try:
        with opener() as client:
            return args.func(client, args)
    except (KiwoomError, DartError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"조회 실패: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
