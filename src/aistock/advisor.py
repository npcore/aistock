"""경제 봇 — 텔레그램에서 시장·매매 타이밍을 대화로 논의한다.

봇에 `/`로 시작하지 않는 메시지를 보내면 여기로 온다. Claude가 필요하면 스스로

    get_market_data   종목 시세·이동평균·RSI·고점 대비 낙폭 (미국은 분할 보정)
    get_account       국내·미국 잔고와 예수금
    save_note         "81달러에 매도함" 같은 사실을 기억
    web_search        뉴스·금리·실적 일정 등 최신 정보

를 불러 근거를 모은 뒤 답한다.

**이 모듈에는 주문 경로가 없다.** 의견만 낸다. 사겠다고 정하면 사람이 `/buy`를 보내고
[승인] 버튼을 눌러야 한다 — 대화 중 오해("그럼 사줘")로 주문이 나가는 일이 없게 했다.

대화 기록은 `data/cache/advisor_chat.json`에 최근 MAX_TURNS 턴만 텍스트로 남긴다.
지난 턴의 thinking 블록은 다시 보내지 않는다(텍스트만 보존). 한 턴 안의 도구 루프에서는
응답 content를 그대로 돌려보낸다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from .config import CACHE_DIR, load_env
from .market_hours import KST, NY, in_session, in_us_session, is_us_trading_day

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
EFFORT = "high"  # 매매 판단은 근거를 따져야 하는 작업이라 기본값(medium)보다 올렸다
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8
MAX_TURNS = 20  # 기억할 대화 턴 수 (user+assistant 한 쌍이 1턴)
MAX_NOTES = 30
FALLBACK_BETA = "server-side-fallback-2026-07-01"

CHAT_PATH = CACHE_DIR / "advisor_chat.json"

SYSTEM = """당신은 텔레그램에서 한 명의 개인 투자자와 대화하는 시장 분석가입니다. 이 사용자는 키움증권 계좌로 \
한국 주식과 미국 TQQQ(나스닥100 3배 레버리지 ETF)를 거래합니다. 주로 "언제 사고팔지"를 함께 따져보고 싶어 합니다.

일하는 방식:
- 의견을 내기 전에 도구로 근거를 확인하세요. 시세·지표는 get_market_data, 잔고는 get_account, \
최근 뉴스·금리·실적 발표 일정·지수 흐름은 web_search로 봅니다. 기억에 의존해 가격을 말하지 마세요.
- TQQQ를 논할 때는 기초지수(QQQ)도 함께 보면 좋습니다. 3배 레버리지는 횡보·급변 구간에서 \
변동성 손실이 크다는 점을 판단에 반영하세요.
- 사용자가 자신의 매매 사실·평단·원칙·선호를 말하면 save_note로 남기세요. 이미 저장된 메모는 \
메시지 앞의 [메모]에 있습니다.
- 결론을 분명히 주세요: 지금 사는 쪽인지 기다리는 쪽인지, 기다린다면 어떤 가격·조건·이벤트를 \
보고 들어갈지(예: 분할 매수 구간, 다시 확인할 지표, 일정). 시나리오별로 나눠도 좋습니다.
- 확신의 정도를 솔직하게 말하세요. 시장은 예측할 수 없고, 틀릴 수 있는 이유도 짧게 덧붙입니다. \
다만 매번 같은 면책 문구를 반복하지는 마세요.
- 당신은 주문을 내거나 취소할 수 없습니다. 대신 텔레그램 명령을 정확히 안내하세요:
  · 매수: /buy TQQQ 수량 가격 (또는 /buy TQQQ 전량 가격) → [승인] 버튼으로 확정
  · 매도: /sell TQQQ 수량 가격 (또는 /sell TQQQ 전량)
  · 취소: /cancel 주문번호, /cancel TQQQ (그 종목 미체결 전부), /cancel 전부. /cancel 만 보내면 목록이 나옵니다.
  · 키움 앱으로 가라고 하지 마세요 — 위 명령으로 텔레그램 안에서 다 됩니다.
  · 제안하는 주문은 실행 가능한 명령 그대로(예: /buy TQQQ 54 71.5) 한 줄에 하나씩 쓰세요. \
답 아래에 그 명령을 바로 실행하는 버튼이 붙습니다. 가격은 달러 숫자만($ 없이도 됨).
- 걸려 있는 주문은 get_account의 open_orders에 있습니다. 주문가능금액이 예수금보다 작으면 \
미체결 주문이 묶고 있는 것이니 open_orders의 실제 수량·가격을 보고 말하세요. 추측하지 마세요. 봇의 자동 규칙\
(200일선 추세·RSI 눌림·손절 -10%·익절 +20%)도 별도로 돌고 있습니다.

답변 형식 (텔레그램 메시지):
- 한국어, 휴대폰에서 읽기 좋게 짧은 문단과 • 글머리표. 보통 15줄 안팎, 길어도 30줄 이내.
- 표·제목(#)·코드블록은 쓰지 마세요. 강조는 **굵게**만 씁니다.
- 출처가 있는 최신 정보는 무엇을 근거로 했는지 한 줄로 밝히세요(예: "10/18 로이터").
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_market_data",
        "description": (
            "종목의 현재가, 전일 대비 등락, 20/50/200일 이동평균, RSI14, 52주 고점·저점과 고점 대비 낙폭, "
            "최근 10거래일 종가를 돌려줍니다. 미국 종목은 액면분할을 보정한 값이고, TQQQ 같은 자동매매 "
            "대상이면 봇 규칙의 현재 판단도 함께 줍니다. 미국은 TQQQ·QQQ 같은 티커, 한국은 6자리 코드."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "예: TQQQ, QQQ, 005930"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_account",
        "description": (
            "사용자 계좌의 국내·미국 보유종목, 평가손익, 원화·달러 예수금과 주문가능금액, "
            "그리고 걸려 있는 미체결 주문(open_orders: 주문번호·종목·수량·가격·잔량). "
            "메리츠증권 계좌가 연결돼 있으면 meritz 항목에 그 계좌 잔고도 함께 온다(조회 전용)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "save_note",
        "description": (
            "사용자에 대해 앞으로도 기억할 사실을 저장합니다. 매매 기록(언제 얼마에 사고팔았는지), "
            "평균단가, 투자 원칙·선호 등. 한 문장으로. 저장 날짜는 자동으로 붙으니 쓰지 마세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
]


class Advisor:
    """대화 상태(기록·메모)를 들고 질문에 답한다. 봇 프로세스에서 한 번에 하나만 돈다."""

    def __init__(
        self,
        kiwoom: Any,
        *,
        client: anthropic.Anthropic | None = None,
        path: Path = CHAT_PATH,
        model: str = MODEL,
    ) -> None:
        self.kiwoom = kiwoom
        self.client = client or anthropic.Anthropic()
        self.path = path
        self.model = model
        self.lock = threading.Lock()  # 대화 기록을 동시에 고치지 않게
        state = self._load()
        self.history: list[dict[str, str]] = state.get("history", [])
        self.notes: list[str] = state.get("notes", [])

    @staticmethod
    def has_credentials() -> bool:
        load_env()
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())

    # --- 대화 ---

    def ask(self, question: str) -> str:
        """질문 하나에 답한다. 실패하면 사람이 읽을 오류 문장을 돌려준다."""
        with self.lock:
            user_text = f"{self._context()}\n\n{question}"
            messages: list[dict[str, Any]] = [*self.history, {"role": "user", "content": user_text}]
            try:
                answer = self._run(messages)
            except anthropic.RateLimitError:
                return "요청이 많아 잠시 막혔습니다. 1분쯤 뒤에 다시 물어봐 주세요."
            except anthropic.APIStatusError as exc:
                logger.error("경제 봇 API 오류 %s: %s", exc.status_code, exc.message)
                return (
                    f"분석 중 오류가 났습니다 (API {exc.status_code}). 잠시 후 다시 시도해 주세요."
                )
            except anthropic.APIConnectionError:
                return "Claude API에 연결하지 못했습니다. 네트워크를 확인해 주세요."

            # 다음 턴에는 맥락 블록 없이 질문 원문만 남긴다 — 시세는 매번 새로 붙인다.
            self.history += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            self.history = self.history[-MAX_TURNS * 2 :]
            self._save()
            return answer

    def reset(self) -> None:
        with self.lock:
            self.history = []
            self._save()

    def forget_notes(self) -> None:
        with self.lock:
            self.notes = []
            self._save()

    # --- 내부 ---

    def _run(self, messages: list[dict[str, Any]]) -> str:
        return run_tool_loop(
            self.client,
            model=self.model,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            call_tool=self._call_tool,
        )

    def _call_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "get_market_data":
            symbol = str(args.get("symbol", "")).strip()
            if not symbol:
                raise ValueError("symbol이 비었습니다")
            return json.dumps(market_data(self.kiwoom, symbol), ensure_ascii=False)
        if name == "get_account":
            return json.dumps(account_data(self.kiwoom), ensure_ascii=False)
        if name == "save_note":
            note = str(args.get("note", "")).strip()
            if not note:
                raise ValueError("note가 비었습니다")
            stamp = datetime.now(KST).strftime("%Y-%m-%d")
            self.notes = [*self.notes, f"[{stamp}] {note}"][-MAX_NOTES:]
            self._save()
            return "저장했습니다."
        raise ValueError(f"모르는 도구: {name}")

    def _context(self) -> str:
        now = datetime.now(KST)
        ny = now.astimezone(NY)
        kr = "열림" if in_session(now) else "닫힘"
        us = "열림" if in_us_session(now) and is_us_trading_day(now) else "닫힘"
        lines = [
            f"[지금] 한국 {now:%Y-%m-%d %a %H:%M} (국내장 {kr}) · 뉴욕 {ny:%m-%d %a %H:%M} (미국장 {us})"
        ]
        if self.notes:
            lines.append("[메모]\n" + "\n".join(f"- {n}" for n in self.notes))
        return "\n".join(lines)

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError:
            logger.warning("경제 봇 대화 기록을 읽을 수 없어 새로 시작합니다: %s", self.path)
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"history": self.history, "notes": self.notes}, ensure_ascii=False, indent=1
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


def run_tool_loop(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    call_tool: Callable[[str, dict[str, Any]], str],
    effort: str = EFFORT,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """도구를 다 쓸 때까지 돈다. 웹 검색이 길어지면 pause_turn으로 끊겨 오므로 이어 붙인다."""
    response = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            # 안전 분류기가 거절하면 서버가 다른 모델로 이어서 답한다.
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "이 질문에는 답하지 못했습니다. 표현을 바꿔 다시 물어봐 주세요."
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue  # 서버 쪽 검색 루프를 이어서 돌린다. 'continue' 메시지는 넣지 않는다
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                output = call_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            except Exception as exc:  # noqa: BLE001 — 도구 실패는 모델에게 알리고 계속한다
                logger.warning("경제 봇 도구 실패 %s: %s", block.name, exc)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"오류: {exc}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})

    text = "\n".join(b.text for b in (response.content if response else []) if b.type == "text")
    if response is not None and response.stop_reason == "max_tokens":
        text += "\n\n… (답변이 길어 잘렸습니다)"
    return text.strip() or "답변을 만들지 못했습니다. 다시 물어봐 주세요."


# --- 도구 구현 (조회 전용) ---


def market_data(kiwoom: Any, symbol: str) -> dict[str, Any]:
    from .us_order import is_us_symbol

    if is_us_symbol(symbol):
        return _us_market_data(kiwoom, symbol.upper())
    return _kr_market_data(kiwoom, symbol)


def _indicators(closes: list[float], price: float) -> dict[str, Any]:
    from .trading.indicators import rsi, sma

    series = [*closes, price]
    window = series[-252:]
    high, low = max(window), min(window)

    def r(v: float | None) -> float | None:
        return None if v is None else round(v, 2)

    return {
        "sma20": r(sma(series, 20)),
        "sma50": r(sma(series, 50)),
        "sma200": r(sma(series, 200)),
        "rsi14": r(rsi(series, 14)),
        "high_52w": r(high),
        "low_52w": r(low),
        "from_52w_high_pct": r((price / high - 1) * 100),
        "recent_closes": [r(c) for c in closes[-10:]],
        "bars": len(closes),
    }


def _us_market_data(kiwoom: Any, symbol: str) -> dict[str, Any]:
    from .backtest.prices import bars_from_rows, parse_price
    from .market import overseas
    from .trading.policy import TradingPolicy
    from .trading.us_trader import decide, split_adjusted_closes
    from .us_order import resolve_exchange

    exchange, quote = resolve_exchange(kiwoom, symbol)
    price = parse_price(quote.get("cur_prc", "0"))
    today = datetime.now(NY).strftime("%Y%m%d")
    rows = overseas.daily_price(kiwoom, symbol, today, exchange, max_pages=14)
    bars = [b for b in sorted(bars_from_rows(rows, symbol), key=lambda b: b.date) if b.date < today]
    closes = split_adjusted_closes(bars)

    out = {
        "symbol": symbol,
        "name": str(quote.get("stk_nm", "")).strip(),
        "currency": "USD",
        "price": price,
        "change_pct": _num(quote.get("flu_rt")),
        "last_bar_date": bars[-1].date if bars else "",
        "split_adjusted": True,
        **_indicators(closes, price),
    }
    policy = TradingPolicy.from_env()
    if symbol in policy.us_symbols:
        d = decide(closes, price, policy, holding_rate=None)
        out["bot_rule_if_not_holding"] = {"signal": d.side or "none", "reason": d.reason}
    return out


def _kr_market_data(kiwoom: Any, code: str) -> dict[str, Any]:
    from .backtest import PriceCache
    from .backtest.prices import parse_price
    from .market import domestic

    info = domestic.stock_info(kiwoom, code)
    price = parse_price(info.get("cur_prc", "0"))
    history = PriceCache(kiwoom).get(code)
    today = datetime.now(KST).strftime("%Y%m%d")
    closes = [b.close for b in history.bars if b.date < today] if history else []
    return {
        "symbol": code,
        "name": str(info.get("stk_nm", "")).strip(),
        "currency": "KRW",
        "price": price,
        "change_pct": _num(info.get("flu_rt")),
        **_indicators(closes, price),
    }


def account_data(kiwoom: Any) -> dict[str, Any]:
    from .account import fetch_balance, fetch_deposit, fetch_us_balance

    kr = fetch_balance(kiwoom)
    dep = fetch_deposit(kiwoom)
    us = fetch_us_balance(kiwoom)
    return {
        "kr_holdings": [
            {
                "code": h.stock_code,
                "name": h.name,
                "qty": h.quantity,
                "avg": h.avg_price,
                "price": h.current_price,
                "profit_pct": h.profit_rate,
            }
            for h in kr.holdings
        ],
        "kr_cash": {"deposit": dep.cash, "orderable": dep.orderable},
        "us_holdings": [
            {
                "symbol": h.symbol,
                "qty": h.quantity,
                "avg_usd": h.avg_price,
                "price_usd": h.current_price,
                "profit_pct": h.profit_rate,
            }
            for h in us.holdings
        ],
        "us_cash": {"usd": us.usd_cash, "usd_orderable": us.usd_orderable, "krw": us.krw_cash},
        "open_orders": _open_orders(kiwoom),
        **_meritz_account(),
    }


def _meritz_account() -> dict[str, Any]:
    """메리츠증권 계좌(설정돼 있을 때만). 키움과 다른 증권사라 주문 명령은 쓸 수 없다."""
    from .brokers.meritz import MeritzConfig, MeritzError, fetch_snapshot

    if not MeritzConfig.available():
        return {}
    try:
        s = fetch_snapshot()
    except MeritzError as exc:
        return {"meritz": f"조회 실패: {exc}"}
    return {
        "meritz": {
            "note": "메리츠증권 계좌 — 조회 전용. /buy·/sell·/cancel 은 키움 계좌에만 적용됨",
            "holdings": [
                {
                    "market": h.market,
                    "code": h.code,
                    "name": h.name,
                    "qty": h.quantity,
                    "avg": h.avg_price,
                    "price": h.price,
                    "profit_pct": h.profit_rate,
                    "currency": h.currency,
                }
                for h in s.holdings
            ],
            "krw_cash": {"deposit": s.deposit.cash, "orderable": s.deposit.orderable},
            "foreign_cash": s.foreign_cash,
        }
    }


def _open_orders(kiwoom: Any) -> list[dict[str, Any]] | str:
    """미체결 주문. 이게 없으면 '주문가능금액이 왜 적은지'를 모델이 추측하게 된다."""
    from . import us_order
    from .errors import KiwoomError
    from .order import pending_orders

    try:
        us = [
            {
                "market": "US",
                "order_no": o.order_no,
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.quantity,
                "remaining": o.remaining,
                "price_usd": o.price,
                "status": o.status,
                "time": o.time,
                "cancel": f"/cancel {o.order_no}",
            }
            for o in us_order.pending_orders(kiwoom)
        ]
        kr = [
            {
                "market": "KR",
                "order_no": o.order_no,
                "code": o.stock_code,
                "name": o.name,
                "side": o.side,
                "qty": o.quantity,
                "remaining": o.remaining,
                "price_krw": o.price,
                "status": o.status,
                "cancel": f"/cancel {o.order_no}",
            }
            for o in pending_orders(kiwoom)
        ]
    except KiwoomError as exc:
        return f"미체결 조회 실패: {exc}"
    return us + kr


def _num(value: Any) -> float:
    try:
        return float(str(value).strip().replace(",", "").replace("%", "") or 0)
    except ValueError:
        return 0.0


# --- 텔레그램 표시 ---


# 경제 봇 답 속의 실행 가능한 명령. 예: /buy TQQQ 54 71.5 · /buy TQQQ 전량 65 · /cancel TQQQ
COMMAND_RE = re.compile(
    r"/(?:buy|sell|cancel)"
    r"(?:\s+[A-Za-z0-9][A-Za-z0-9.\-]*)"
    r"(?:\s+(?:전량|전부|all|\d[\d,]*))?"
    r"(?:\s+\$?\d+(?:\.\d+)?)?"
)
MAX_COMMAND_BUTTONS = 6


def extract_commands(text: str) -> list[str]:
    """답에 나온 /buy·/sell·/cancel 명령 (순서 유지, 중복 제거). 버튼으로 바로 실행하게 한다."""
    out: list[str] = []
    for m in COMMAND_RE.finditer(text):
        cmd = " ".join(m.group(0).replace("$", "").replace(",", "").split())
        if cmd not in out:
            out.append(cmd)
    return out[:MAX_COMMAND_BUTTONS]


def to_telegram_html(text: str) -> str:
    """모델의 답(평문 + **굵게**)을 텔레그램 HTML로. 다른 마크업은 전부 이스케이프한다.

    모델이 HTML을 직접 쓰게 하면 태그 하나만 어긋나도 메시지 전체가 400으로 거절된다.
    """
    from .notify.telegram import escape

    html = escape(text)
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html, flags=re.DOTALL)
    # 명령은 <code>로 — 텔레그램에서 탭하면 통째로 복사된다. 그냥 두면 '/buy'까지만 링크가 걸려
    # 누르면 인자 없는 /buy가 전송된다.
    html = COMMAND_RE.sub(lambda m: f"<code>{m.group(0)}</code>", html)
    return re.sub(r"^#{1,6}\s*", "", html, flags=re.MULTILINE)
