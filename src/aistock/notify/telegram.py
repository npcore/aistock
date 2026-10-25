"""텔레그램 알림.

사람이 읽고 매수·매도를 직접 판단하는 채널이다. 자동 주문 경로가 아니므로,
판단에 필요한 근거(수치·리스크·원문 링크)를 빠뜨리지 않는 쪽을 우선한다.
"""

from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import dataclass
from typing import Self

import httpx

from ..config import load_env

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# 텔레그램 메시지 상한은 4096자. 여유를 두고 자른다.
MAX_TEXT = 3900
# 사진 캡션은 본문보다 훨씬 짧다(1024자).
MAX_CAPTION = 950


class TelegramError(Exception):
    """전송 실패."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> TelegramConfig:
        load_env()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다. "
                "@BotFather로 봇을 만들고 .env에 넣어주세요."
            )
        return cls(token, chat_id)

    @staticmethod
    def available() -> bool:
        load_env()
        return bool(
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            and os.getenv("TELEGRAM_CHAT_ID", "").strip()
        )


def escape(text: str) -> str:
    """HTML parse_mode에서 깨지는 문자를 이스케이프한다.

    회사명이나 공시 제목에 `<`, `>`, `&`가 들어가면 메시지 전체가 400으로 거절된다.
    """
    return html.escape(str(text), quote=False)


class TelegramNotifier:
    """텔레그램 봇으로 메시지를 보낸다.

    >>> notifier = TelegramNotifier.create()
    >>> notifier.send("테스트")
    """

    def __init__(self, config: TelegramConfig, *, timeout: float = 15.0) -> None:
        self.config = config
        self._client = httpx.Client(timeout=timeout)
        self.sent = 0

    @classmethod
    def create(cls, **kwargs: object) -> Self:
        return cls(TelegramConfig.from_env(), **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send(
        self,
        text: str,
        *,
        preview: bool = False,
        buttons: list[list[tuple[str, str]]] | None = None,
        reply_to: int = 0,
    ) -> int:
        """메시지 한 건. 상한을 넘으면 자르되 잘렸음을 본문에 남긴다.

        buttons는 인라인 키보드 [[(라벨, callback_data), ...], ...]. 보낸 message_id를 돌려준다
        — 나중에 버튼을 지우고 결과를 덧붙일 때 쓴다.
        """
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n\n… (길이 제한으로 잘림)"

        body: dict = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
        }
        if buttons:
            body["reply_markup"] = _keyboard(buttons)
        if reply_to:
            body["reply_parameters"] = _reply(reply_to)
        payload = self._call("sendMessage", body, "텔레그램 전송")
        self.sent += 1
        return int((payload.get("result") or {}).get("message_id", 0))

    def edit(self, message_id: int, text: str, *, buttons: list | None = None) -> None:
        """보낸 메시지를 고친다. buttons를 안 주면 버튼은 지워진다 — 결정이 끝난 제안을
        다시 누를 수 없게."""
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n\n… (길이 제한으로 잘림)"
        body: dict = {
            "chat_id": self.config.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            body["reply_markup"] = _keyboard(buttons)
        self._call("editMessageText", body, "메시지 수정")

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """버튼 누름에 응답한다. 안 하면 텔레그램 앱에서 버튼이 계속 로딩 상태로 남는다."""
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:190]},
            "버튼 응답",
        )

    def _call(self, method: str, body: dict, what: str) -> dict:
        try:
            response = self._client.post(f"{API_BASE}/bot{self.config.token}/{method}", json=body)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{what} 중 네트워크 오류: {exc}") from exc

        payload = _safe_json(response)
        if not payload.get("ok"):
            # 실패 메시지에 토큰이 섞이지 않도록 description만 꺼낸다.
            raise TelegramError(
                f"{what} 실패 (HTTP {response.status_code}): "
                f"{payload.get('description', response.text[:200])}"
            )
        return payload

    def send_photo(
        self, image: bytes, caption: str = "", *, filename: str = "chart.png", reply_to: int = 0
    ) -> None:
        """이미지 한 장. 캡션은 1024자 제한이라 본문보다 짧다."""
        if len(caption) > MAX_CAPTION:
            caption = caption[: MAX_CAPTION - 20] + "\n… (캡션 잘림)"
        data = {"chat_id": self.config.chat_id, "caption": caption, "parse_mode": "HTML"}
        if reply_to:
            data["reply_parameters"] = json.dumps(_reply(reply_to))
        try:
            response = self._client.post(
                f"{API_BASE}/bot{self.config.token}/sendPhoto",
                data=data,
                files={"photo": (filename, image, "image/png")},
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"텔레그램 이미지 전송 중 네트워크 오류: {exc}") from exc

        payload = _safe_json(response)
        if not payload.get("ok"):
            raise TelegramError(
                f"텔레그램 이미지 전송 실패 (HTTP {response.status_code}): "
                f"{payload.get('description', response.text[:200])}"
            )
        self.sent += 1

    def check(self) -> str:
        """봇 연결 확인. 봇 이름을 돌려준다."""
        try:
            response = self._client.get(f"{API_BASE}/bot{self.config.token}/getMe")
        except httpx.HTTPError as exc:
            raise TelegramError(f"텔레그램 연결 실패: {exc}") from exc
        payload = _safe_json(response)
        if not payload.get("ok"):
            raise TelegramError(f"봇 토큰이 유효하지 않습니다: {payload.get('description', '')}")
        bot = payload.get("result", {})
        return f"@{bot.get('username', '?')} ({bot.get('first_name', '')})"


def _reply(message_id: int) -> dict:
    # 원 메시지가 지워졌어도 보내기는 한다
    return {"message_id": message_id, "allow_sending_without_reply": True}


def _keyboard(buttons: list[list]) -> dict:
    """[(라벨, callback_data), ...] 또는 텔레그램 버튼 dict(예: 복사 버튼)를 섞어 받는다."""
    return {
        "inline_keyboard": [
            [b if isinstance(b, dict) else {"text": b[0], "callback_data": b[1]} for b in row]
            for row in buttons
        ]
    }


def _safe_json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
