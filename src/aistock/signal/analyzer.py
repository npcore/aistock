"""Claude 기반 공시 분석기.

폴러의 2단계 구조를 그대로 따른다.

    HEADLINE → triage()   보고서명만. 원문을 기다리지 않는다.
    DOCUMENT → analyze()  원문까지 읽고 신호를 낸다.

응답은 structured outputs로 스키마를 강제하므로 자유 텍스트 파싱이 없다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from ..config import load_env
from ..dart.disclosure import Disclosure
from .prompts import ANALYSIS_SYSTEM, TRIAGE_SYSTEM
from .schema import DisclosureSignal, Triage

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# 안전 분류기가 요청을 거절하면 서버가 대체 모델로 다시 태운다.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 원문이 아주 긴 공시(회사합병 등은 12만자를 넘는다)를 그대로 넣으면 비용이 급증한다.
# 자르는 경우 반드시 로그로 알린다 — 조용히 잘라내면 근거가 빠진 줄 모르고 신호를 믿게 된다.
MAX_DOCUMENT_CHARS = 60_000

T = TypeVar("T", bound=BaseModel)


class SignalError(Exception):
    """분석 실패."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0


class SignalAnalyzer:
    """공시를 매매 신호로 바꾼다.

    >>> analyzer = SignalAnalyzer()
    >>> signal = analyzer.analyze(disclosure, text)
    >>> signal.action, signal.confidence
    (<Action.SELL: 'sell'>, 75)
    """

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        model: str = MODEL,
        max_document_chars: int = MAX_DOCUMENT_CHARS,
    ) -> None:
        load_env()
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_document_chars = max_document_chars
        self.usage = Usage()

    @staticmethod
    def has_credentials() -> bool:
        load_env()
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    # --- 1단계: 보고서명만 ---

    def triage(self, disclosure: Disclosure) -> Triage:
        """보고서명만으로 원문을 읽을 가치가 있는지 판단한다. 지연 없이 도는 단계라 effort는 낮게."""
        return self._parse(
            system=TRIAGE_SYSTEM,
            user=_headline_block(disclosure),
            schema=Triage,
            effort="low",
            max_tokens=2000,
        )

    # --- 2단계: 원문까지 ---

    def analyze(self, disclosure: Disclosure, text: str) -> DisclosureSignal:
        """공시 원문을 읽고 신호를 낸다."""
        if not text.strip():
            raise SignalError(f"원문이 비어 있습니다: {disclosure.rcept_no}")

        body = text
        if len(text) > self.max_document_chars:
            logger.warning(
                "원문이 길어 앞 %d자만 분석합니다 (%s, 전체 %d자). 뒷부분 근거는 빠집니다.",
                self.max_document_chars,
                disclosure.rcept_no,
                len(text),
            )
            body = text[: self.max_document_chars]

        user = f"{_headline_block(disclosure)}\n\n## 공시 원문\n\n{body}"
        return self._parse(
            system=ANALYSIS_SYSTEM,
            user=user,
            schema=DisclosureSignal,
            effort="high",
            max_tokens=8000,
        )

    # --- 공통 ---

    def _parse(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        effort: str,
        max_tokens: int,
    ) -> T:
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                # 시스템 프롬프트는 요청마다 동일하므로 캐시 대상이다.
                # 변동 값(종목·원문)은 전부 user 메시지에 있어 접두사가 깨지지 않는다.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                output_format=schema,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIStatusError as exc:
            raise SignalError(f"Claude API 오류 ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise SignalError(f"Claude API 연결 실패: {exc}") from exc

        # 거절은 HTTP 200으로 온다. content를 먼저 읽으면 엉뚱한 곳에서 터진다.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise SignalError(f"안전 분류기가 요청을 거절했습니다 (category={category})")
        if response.stop_reason == "max_tokens":
            raise SignalError(f"응답이 max_tokens({max_tokens})에서 잘렸습니다")

        self.usage.add(response.usage)
        parsed = response.parsed_output
        if parsed is None:
            raise SignalError("응답을 스키마로 파싱하지 못했습니다")
        return parsed


def _headline_block(disclosure: Disclosure) -> str:
    """공시 메타데이터를 프롬프트용 텍스트로."""
    lines = [
        "## 공시 정보",
        f"- 회사: {disclosure.corp_name}",
        f"- 종목코드: {disclosure.stock_code or '(비상장)'}",
        f"- 시장: {disclosure.market}",
        f"- 보고서명: {disclosure.report_nm}",
        f"- 제출인: {disclosure.flr_nm}",
        f"- 접수일자: {disclosure.rcept_dt}",
    ]
    if disclosure.rm:
        lines.append(f"- 비고: {disclosure.rm}")
    return "\n".join(lines)
