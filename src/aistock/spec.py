"""키움 공식 API 스펙(337개 TR) 조회 유틸.

`reference/kiwoom_api_spec.json`은 키움 공식 레포에서 가져온 전체 스펙이다.
새 TR을 감쌀 때 파라미터를 기억에 의존하지 말고 여기서 확인한다.

    $ python -m aistock.spec ka10001
    $ python -m aistock.spec --search 뉴스
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import PROJECT_ROOT

SPEC_PATH = PROJECT_ROOT / "reference" / "kiwoom_api_spec.json"


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    if not SPEC_PATH.exists():
        raise FileNotFoundError(f"API 스펙 파일이 없습니다: {SPEC_PATH}")
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _index() -> dict[str, dict[str, Any]]:
    """api_id -> 스펙 엔트리."""
    out: dict[str, dict[str, Any]] = {}
    for entry in _raw()["apis"].values():
        api_id = entry.get("meta", {}).get("API ID")
        if api_id:
            out[api_id] = entry
    return out


def get(api_id: str) -> dict[str, Any]:
    """TR 하나의 전체 스펙(meta/request/response/예시)을 돌려준다."""
    try:
        return _index()[api_id]
    except KeyError:
        raise KeyError(f"알 수 없는 API ID: {api_id}") from None


def url_of(api_id: str) -> str:
    return get(api_id)["meta"]["URL"]


def params_of(api_id: str) -> list[dict[str, str]]:
    """요청 Body 파라미터 목록."""
    rows: list[dict[str, str]] = []
    request = get(api_id).get("request", {})
    for section, entries in request.items():
        if "body" not in section.lower():
            continue
        for row in entries if isinstance(entries, list) else []:
            rows.append({k: v for k, v in row.items() if v})
    return rows


def search(keyword: str) -> list[tuple[str, str, str]]:
    """API 명 / 메뉴 위치에서 키워드를 찾아 (api_id, 이름, URL) 목록 반환."""
    hits = []
    for api_id, entry in sorted(_index().items()):
        meta = entry["meta"]
        haystack = f"{meta.get('API 명', '')} {meta.get('메뉴 위치', '')}"
        if keyword.lower() in haystack.lower():
            hits.append((api_id, meta.get("API 명", ""), meta.get("URL", "")))
    return hits


def error_codes() -> dict[str, str]:
    return {row["오류코드"]: row["오류메시지"] for row in _raw()["error_codes"]}


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="키움 API 스펙 조회")
    parser.add_argument("api_id", nargs="?", help="조회할 TR 코드 (예: ka10001)")
    parser.add_argument("--search", "-s", help="이름/메뉴로 TR 검색")
    args = parser.parse_args()

    if args.search:
        for api_id, name, url in search(args.search):
            print(f"{api_id:10s} {name:35s} {url}")
        return

    if not args.api_id:
        parser.error("api_id 또는 --search 중 하나가 필요합니다.")

    entry = get(args.api_id)
    meta = entry["meta"]
    print(f"{meta['API ID']}  {meta['API 명']}")
    print(f"  {meta['Method']} {meta['URL']}")
    print(f"  메뉴: {meta.get('메뉴 위치', '')}")
    print("  요청 파라미터:")
    for row in params_of(args.api_id):
        req = "필수" if row.get("required") == "Y" else "선택"
        desc = row.get("description", "").replace("\n", " ")[:90]
        print(f"    - {row['element']:16s} {req}  {row.get('한글명', '')}  {desc}")


if __name__ == "__main__":
    _main()
