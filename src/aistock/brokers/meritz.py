"""메리츠증권 Open API — **조회 전용** (예수금·국내 보유·해외 보유·외화 잔고).

이 모듈에는 주문 경로가 없다. 해외 주문 지원은 확인됐지만(베타), 모의계좌가 없어 처음부터
실계좌로 시험해야 하므로 조회부터 붙인다.

공식 명세(github.com/meritz-securities/open-api 의 openapi.json)에서 확인한 규칙:

- 도메인 https://openapi.imeritz.com:9443 (운영만 있음)
- 토큰: POST /oauth2/token, **form-urlencoded**, client_credentials + scope "login".
  유효기간 expires_in 초(개인 12시간). 앱키가 계좌 단위라 계좌번호 파라미터가 없다(/me).
- 모든 조회에 `mac_address` 헤더(12자리 16진수)가 **필수**. 없으면 IGW50024로 거절된다.
- 성공 판정은 HTTP 200만으로 하지 않는다. rsp_cd "5766"(조회 완료)·"5820"(자료 없음) 등.
  업무 오류도 HTTP 500 + rsp_cd로 온다 — 상태코드만 보면 "권한 없음"이 전송 장애처럼 보인다.
- 연속조회: 응답 tr_cont "1"이면 tr_cont_key를 **쿼리스트링으로** 그대로 다시 보낸다.
  헤더로 보내면 공백이 긴 키를 방화벽이 막는다(공식 클라이언트 주석).
- 수량·단가가 문자열(소수 10자리 등)로 온다. 숫자로 바꿔 쓴다.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from ..config import CACHE_DIR, load_env

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.imeritz.com:9443"
TOKEN_PATH = CACHE_DIR / "meritz_token.json"
USER_AGENT = "aistock/0.1"
OK_CODES = {"0000", "0001", "5620", "5762", "5766", "5820", "5822"}
MAX_PAGES = 10
MIN_INTERVAL = 0.12  # 조회 API 초당 10건 한도 — 여유를 둔다


class MeritzError(Exception):
    """메리츠 API 오류. 메시지에 rsp_cd·rsp_msg를 담는다(키·토큰은 담지 않는다)."""


@dataclass(frozen=True)
class MeritzConfig:
    app_key: str
    app_secret: str
    base_url: str = BASE_URL

    @classmethod
    def from_env(cls) -> MeritzConfig:
        load_env()
        key = os.getenv("MERITZ_APP_KEY", "").strip()
        secret = os.getenv("MERITZ_APP_SECRET", "").strip()
        if not key or not secret:
            raise MeritzError(".env에 MERITZ_APP_KEY / MERITZ_APP_SECRET이 없습니다.")
        base = (os.getenv("MERITZ_BASE_URL", "").strip() or BASE_URL).rstrip("/")
        if not base.startswith("https://"):
            raise MeritzError(f"MERITZ_BASE_URL은 https여야 합니다: {base}")
        return cls(key, secret, base)

    @staticmethod
    def available() -> bool:
        load_env()
        return bool(
            os.getenv("MERITZ_APP_KEY", "").strip() and os.getenv("MERITZ_APP_SECRET", "").strip()
        )


def mac_address() -> str:
    """게이트웨이 필수 헤더. 공식 클라이언트와 같은 방식(uuid.getnode, 대문자 12자리)."""
    n = uuid.getnode()
    return "".join(f"{(n >> s) & 0xFF:02X}" for s in range(40, -1, -8))


def num(value: Any) -> float:
    """'0000123.4500000000' · '-12.3' · 123 → float. 빈 값은 0."""
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# --- 결과 모델 ---


@dataclass(frozen=True)
class MeritzDeposit:
    cash: int  # 예수금(원화 현금)
    d2_cash: int
    orderable: int  # 주문 가능 현금
    withdrawable: int


@dataclass(frozen=True)
class MeritzHolding:
    market: str  # KR | US(해외)
    code: str  # 국내 6자리 / 해외 심볼(거래소 접미어 뗌)
    name: str
    quantity: float  # 해외 소수점 보유가 있어 float
    sellable: float
    avg_price: float  # 국내 원, 해외 거래통화
    price: float
    profit: float  # 평가손익 (국내 원, 해외 거래통화)
    profit_rate: float  # %
    eval_amount: float
    currency: str = "KRW"


@dataclass(frozen=True)
class MeritzSnapshot:
    deposit: MeritzDeposit
    holdings: list[MeritzHolding] = field(default_factory=list)
    foreign_cash: dict[str, float] = field(default_factory=dict)  # 통화 → 외화 예수금


# --- 클라이언트 ---


class MeritzClient:
    def __init__(self, config: MeritzConfig, *, http: httpx.Client | None = None) -> None:
        self.config = config
        self._http = http or httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT})
        self._token = ""
        self._expires = 0.0
        self._last = 0.0

    @classmethod
    def create(cls) -> MeritzClient:
        return cls(MeritzConfig.from_env())

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 토큰 (디스크 캐시: 12시간짜리를 매 실행 새로 받지 않게) ---

    def token(self) -> str:
        if self._token and time.time() < self._expires:
            return self._token
        cached = self._load_token()
        if cached:
            self._token, self._expires = cached
            return self._token
        try:
            r = self._http.post(
                f"{self.config.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.app_key,
                    "client_secret": self.config.app_secret,
                    "scope": "login",
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise MeritzError(f"메리츠 토큰 서버에 연결하지 못했습니다: {exc}") from None
        body = _json(r)
        if r.status_code != 200 or not body.get("access_token"):
            # 키·시크릿은 메시지에 넣지 않는다
            code = body.get("rsp_cd") or body.get("error") or ""
            msg = body.get("rsp_msg") or body.get("error_description") or r.text[:200]
            raise MeritzError(f"메리츠 토큰 발급 실패 (HTTP {r.status_code} {code}): {msg}")
        ttl = int(num(body.get("expires_in")) or 43200)
        self._token = body["access_token"]
        self._expires = time.time() + ttl - 60  # 만료 직전에 쓰다 실패하지 않게
        self._save_token()
        return self._token

    def _load_token(self) -> tuple[str, float] | None:
        try:
            d = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # 앱키가 바뀌었으면(계좌 교체) 예전 토큰을 쓰지 않는다
        if d.get("key_tail") != self.config.app_key[-6:] or time.time() >= d.get("expires", 0):
            return None
        return d["token"], d["expires"]

    def _save_token(self) -> None:
        try:
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(
                json.dumps(
                    {
                        "token": self._token,
                        "expires": self._expires,
                        "key_tail": self.config.app_key[-6:],
                    }
                ),
                encoding="utf-8",
            )
            TOKEN_PATH.chmod(0o600)
        except OSError as exc:
            logger.warning("메리츠 토큰 캐시 저장 실패: %s", exc)

    # --- 조회 ---

    def get(self, path: str, params: dict[str, str] | None = None) -> list[dict] | dict:
        """GET + 성공 판정 + 연속조회. data가 목록이면 모든 페이지를 이어 붙여 돌려준다."""
        params = dict(params or {})
        pages: list[dict] = []
        for _ in range(MAX_PAGES):
            body = self._get_once(path, params)
            data = body.get("data")
            if not isinstance(data, list):
                return data or {}
            pages.extend(d for d in data if isinstance(d, dict))
            if str(body.get("tr_cont", "0")) != "1" or not body.get("tr_cont_key"):
                return pages
            params.update(tr_cont="1", tr_cont_key=str(body["tr_cont_key"]))
        logger.warning("메리츠 %s: %d페이지에서 멈춤 (남은 자료 있음)", path, MAX_PAGES)
        return pages

    def _get_once(self, path: str, params: dict[str, str]) -> dict:
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        headers = {
            "authorization": f"Bearer {self.token()}",
            "mac_address": mac_address(),
            "content-type": "application/json; charset=utf-8",
        }
        try:
            r = self._http.get(f"{self.config.base_url}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise MeritzError(f"메리츠 조회 중 네트워크 오류 ({path}): {exc}") from None
        finally:
            self._last = time.monotonic()
        body = _json(r)
        code = str(body.get("rsp_cd", ""))
        if r.status_code == 401:
            self._token, self._expires = "", 0.0
            TOKEN_PATH.unlink(missing_ok=True)
        if r.status_code != 200 or (code and code not in OK_CODES):
            msg = body.get("rsp_msg") or r.text[:200]
            sub = body.get("rsp_sub_msg") or ""
            raise MeritzError(
                f"메리츠 {path} 실패 (HTTP {r.status_code} {code}): {msg}{' · ' + sub if sub else ''}"
            )
        if not body:
            raise MeritzError(f"메리츠 {path}: JSON이 아닌 응답 (HTTP {r.status_code})")
        return body

    # --- 묶음 ---

    def deposit(self) -> MeritzDeposit:
        d = self.get("/accounts/v1/me/deposit")
        d = d if isinstance(d, dict) else {}
        return MeritzDeposit(
            cash=int(num(d.get("tfam"))),
            d2_cash=int(num(d.get("d2_aft_tfam"))),
            orderable=int(num(d.get("oder_able_cash_amt"))),
            withdrawable=int(num(d.get("drng_able_amt"))),
        )

    def domestic_holdings(self) -> list[MeritzHolding]:
        rows = self.get("/accounts/v1/me/holdings")
        out = []
        # 국내·해외가 한 목록에 섞여 온다. 해외는 전용 API로 따로 받으므로 국내("1")만.
        for r in rows if isinstance(rows, list) else []:
            if str(r.get("pros_trgt_cls_code", "")) != "1":
                continue
            qty = num(r.get("rmnd_qty"))
            if qty <= 0:
                continue
            out.append(
                MeritzHolding(
                    market="KR",
                    code=str(r.get("iscd", "")).strip().lstrip("A"),
                    name=str(r.get("isnm", "")).strip(),
                    quantity=qty,
                    sellable=qty,
                    avg_price=num(r.get("byng_unpr")),
                    price=num(r.get("psnt_prc")),
                    profit=num(r.get("vltn_prls_amt")),
                    profit_rate=num(r.get("ert")),
                    eval_amount=num(r.get("vltn_amt")),
                )
            )
        return out

    def overseas_holdings(self) -> list[MeritzHolding]:
        rows = self.get("/accounts/v1/me/overseas/holdings")
        out = []
        for r in rows if isinstance(rows, list) else []:
            qty = num(r.get("rmnd_dqty"))
            if qty <= 0:
                continue
            symbol = str(r.get("ovrs_stck_symb_iscd") or r.get("iscd", "")).strip().split(".")[0]
            out.append(
                MeritzHolding(
                    market="US",
                    code=symbol,
                    name=str(r.get("isnm", "")).strip(),
                    quantity=qty,
                    sellable=num(r.get("seln_able_dqty")),
                    avg_price=num(r.get("avrg_unpr")),
                    price=num(r.get("psnt_prc")),
                    profit=num(r.get("vltn_prls_camt")),
                    profit_rate=num(r.get("vltn_ert")),
                    eval_amount=num(r.get("frcr_vltn_amt")),
                    currency=str(r.get("crcd", "USD")).strip() or "USD",
                )
            )
        return out

    def foreign_cash(self) -> dict[str, float]:
        rows = self.get("/accounts/v1/me/overseas/currencies")
        return {
            str(r.get("crcd", "")).strip(): num(r.get("frcr_amt"))
            for r in (rows if isinstance(rows, list) else [])
            if str(r.get("crcd", "")).strip()
        }

    def snapshot(self) -> MeritzSnapshot:
        return MeritzSnapshot(
            deposit=self.deposit(),
            holdings=self.domestic_holdings() + self.overseas_holdings(),
            foreign_cash=self.foreign_cash(),
        )


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def fetch_snapshot() -> MeritzSnapshot:
    with MeritzClient.create() as client:
        return client.snapshot()
