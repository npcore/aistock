#!/usr/bin/env python3
"""장이 **열리는 그 시각에 딱 한 번만** 시세를 보낸다.

왜 래퍼가 필요한가. `aistock ticker --once --market us` 는 '미국장이 열려 있으면'
보낸다 — 개장 시각인지는 보지 않는다. 그런데 미국 개장은 서머타임에 따라 한국시각
**22:30(3~11월) / 23:30(11~3월)** 로 옮겨 다녀서, launchd 에 두 시각을 다 걸어두면
여름엔 22:30 에 한 번, 23:30 에 또 한 번(장중이라) 나간다. 그래서 여기서
**뉴욕 현지시각이 09:30 언저리일 때만** 실행한다 — 서머타임 계산은 zoneinfo 가 한다.

  python3 market_open_run.py --kr    한국장 개장(평일 09:00 KST)
  python3 market_open_run.py --us    미국장 개장(뉴욕 09:30 ET)
  python3 market_open_run.py --us --dry-run   보내지 않고 판정만
"""
import argparse
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

AISTOCK = "/Users/hansc/projects/aistock/.venv/bin/aistock"
NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
WINDOW_MIN = 25          # 개장 시각으로 인정할 폭(launchd 지연·부팅 직후 대비)


def _near(now, hh, mm):
    """지금이 hh:mm 로부터 WINDOW_MIN 분 안인가(개장 직후만 True)."""
    mins = now.hour * 60 + now.minute - (hh * 60 + mm)
    return 0 <= mins <= WINDOW_MIN


def should_run(market):
    """지금 보내야 하는가 → (보낼지, 이유)."""
    if market == "us":
        now = datetime.now(NY)
        if now.weekday() >= 5:
            return False, f"뉴욕 주말({now:%m/%d %a %H:%M} ET)"
        if not _near(now, 9, 30):
            return False, f"개장 시각이 아님({now:%m/%d %H:%M} ET)"
        return True, f"미국장 개장({now:%m/%d %H:%M} ET · 한국 {datetime.now(KST):%H:%M})"
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return False, f"한국 주말({now:%m/%d %a %H:%M} KST)"
    if not _near(now, 9, 0):
        return False, f"개장 시각이 아님({now:%m/%d %H:%M} KST)"
    return True, f"한국장 개장({now:%m/%d %H:%M} KST)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kr", action="store_true")
    ap.add_argument("--us", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    market = "us" if a.us else "domestic"
    ok, why = should_run("us" if a.us else "kr")
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    if not ok:
        print(f"[{stamp}] 건너뜀 — {why}")
        return 0
    cmd = [AISTOCK, "ticker", "--once", "--telegram", "--fx", "--chart",
           "--market", market]
    print(f"[{stamp}] 발송 — {why}")
    if a.dry_run:
        print("  (dry-run) " + " ".join(cmd))
        return 0
    # 휴장일 판정은 aistock 이 한다(--once 가 휴장이면 스스로 건너뛴다).
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
