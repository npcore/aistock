"""공시 파이프라인이 내보내는 이벤트.

공시는 두 단계로 도착한다. 목록에는 접수 즉시 뜨지만 원문은 시차를 두고 올라온다.
그래서 한 건의 공시가 이벤트를 **두 번** 낼 수 있다.

    HEADLINE  — 새 공시 감지. 보고서명까지만 안다. 지연 없음.
    DOCUMENT  — 같은 공시의 원문이 확보됨. 몇 초~몇 분 뒤.

급한 신호는 HEADLINE에서 내고, 정밀 분석은 DOCUMENT를 기다린다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..dart.disclosure import Disclosure


class Stage(str, Enum):
    HEADLINE = "headline"
    DOCUMENT = "document"


@dataclass(frozen=True)
class DisclosureEvent:
    stage: Stage
    disclosure: Disclosure
    detected_at: datetime
    text: str | None = None  # DOCUMENT 단계에서만 채워진다
    pblntf_ty: str | None = None  # 공시유형 대분류. 목록 응답에는 없어 폴러가 채운다.

    @property
    def rcept_no(self) -> str:
        return self.disclosure.rcept_no

    @property
    def headline(self) -> str:
        """LLM 프롬프트에 그대로 넣을 수 있는 한 줄 요약."""
        d = self.disclosure
        who = f"{d.corp_name}({d.stock_code})" if d.stock_code else d.corp_name
        return f"[{d.market}] {who} {d.report_nm}"

    def to_dict(self) -> dict[str, Any]:
        d = self.disclosure
        return {
            "stage": self.stage.value,
            "detected_at": self.detected_at.isoformat(),
            "rcept_no": d.rcept_no,
            "corp_code": d.corp_code,
            "corp_name": d.corp_name,
            "stock_code": d.stock_code,
            "market": d.market,
            "report_nm": d.report_nm,
            "flr_nm": d.flr_nm,
            "rcept_dt": d.rcept_dt,
            "pblntf_ty": self.pblntf_ty,
            "url": d.url,
            "text_len": len(self.text) if self.text else 0,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
