"""Ollama 기반 분석기.

`SignalAnalyzer`와 같은 인터페이스(triage/analyze)를 제공해 서로 바꿔 끼울 수 있다.
사내망 모델이라 토큰 비용이 들지 않는 대신 응답이 느리고, 구조화 출력의 보증 수준이
Claude보다 낮다 — Ollama의 `format`은 JSON 스키마를 강제하지만 값의 범위(0~100 같은)까지는
지키지 않으므로 받은 뒤 Pydantic으로 다시 검증한다.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Self, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import load_env
from ..dart.disclosure import Disclosure
from .analyzer import MAX_DOCUMENT_CHARS, SignalError, Usage, _headline_block
from .prompts import ANALYSIS_SYSTEM, TRIAGE_SYSTEM
from .schema import DisclosureSignal, Triage

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://172.31.109.242:11434"
DEFAULT_MODEL = "gpt-oss:120b"

T = TypeVar("T", bound=BaseModel)


class OllamaAnalyzer:
    """사내망 Ollama로 공시를 분석한다.

    >>> analyzer = OllamaAnalyzer(model="gpt-oss:120b")
    >>> analyzer.triage(disclosure).worth_reading
    True
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        model: str = "",
        timeout: float = 300.0,
        max_document_chars: int = MAX_DOCUMENT_CHARS,
    ) -> None:
        load_env()
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL") or DEFAULT_MODEL
        self.max_document_chars = max_document_chars
        self.usage = Usage()
        self._client = httpx.Client(timeout=timeout)

    @classmethod
    def has_credentials(cls) -> bool:
        """Ollama는 인증이 없다. 대신 서버가 살아 있는지를 본다."""
        try:
            return cls().ping()
        except (httpx.HTTPError, OSError):
            return False

    def close(self) -> None:
        self._client.close()

    def ping(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/api/version", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def models(self) -> list[str]:
        try:
            r = self._client.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SignalError(f"Ollama 모델 목록 조회 실패: {exc}") from exc
        return [m["name"] for m in r.json().get("models", [])]

    # --- SignalAnalyzer와 같은 인터페이스 ---

    def triage(self, disclosure: Disclosure) -> Triage:
        return self._chat(TRIAGE_SYSTEM, _headline_block(disclosure), Triage)

    def analyze(self, disclosure: Disclosure, text: str) -> DisclosureSignal:
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
        return self._chat(ANALYSIS_SYSTEM, user, DisclosureSignal)

    # --- 내부 ---

    def _chat(self, system: str, user: str, schema: type[T]) -> T:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            # 스키마를 넘기면 그 형태로만 생성한다. 값 범위까지는 보증하지 않는다.
            "format": schema.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = self._client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SignalError(f"Ollama 요청 실패 ({self.model}): {exc}") from exc

        data = response.json()
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise SignalError(f"Ollama 응답이 비어 있습니다: {data.get('done_reason')}")

        self.usage.input_tokens += int(data.get("prompt_eval_count") or 0)
        self.usage.output_tokens += int(data.get("eval_count") or 0)

        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            # 값 범위를 벗어난 경우가 흔하므로 무엇이 어긋났는지 남긴다.
            raise SignalError(
                f"Ollama 응답이 스키마와 맞지 않습니다 ({self.model}): {exc.errors()[:2]}\n"
                f"원문: {content[:200]}"
            ) from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def load_analyzer(backend: str = "", **kwargs: Any):
    """백엔드 이름으로 분석기를 만든다. `claude`(기본) 또는 `ollama`."""
    load_env()
    name = (backend or os.getenv("SIGNAL_BACKEND") or "claude").lower()
    if name == "ollama":
        return OllamaAnalyzer(**kwargs)
    from .analyzer import SignalAnalyzer

    return SignalAnalyzer(**kwargs)


def _describe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
