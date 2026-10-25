"""계정 프로필 — 키움 계좌·텔레그램 봇을 계정마다 따로 돌린다.

    profiles/<이름>/.env          이 계정만의 앱키·계좌·봇 토큰 (메인 .env를 이어받지 않음)
    profiles/<이름>/data/cache/   토큰·주문 제안·경제 봇 메모·봇 offset
    profiles/<이름>/logs/bot.log

같은 코드로 `AISTOCK_PROFILE=<이름> aistock bot`을 띄운다. launchd 서비스 이름은
`com.hansc.aistock.bot.<이름>`.

**섞이면 남의 계좌로 주문이 나간다.** 그래서 시작 전에

    - 앱키·봇 토큰이 메인이나 다른 계정과 겹치지 않는지
    - 실제로 토큰 발급과 봇 연결이 되는지

를 확인하고, 하나라도 어긋나면 서비스를 올리지 않는다.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .config import PROFILES_DIR, PROJECT_ROOT

LABEL_PREFIX = "com.hansc.aistock.bot"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
AISTOCK_BIN = PROJECT_ROOT / ".venv" / "bin" / "aistock"

# 계정마다 반드시 달라야 하는 값. 하나라도 메인·다른 계정과 같으면 시작하지 않는다.
UNIQUE_KEYS = ("KIWOOM_APP_KEY", "TELEGRAM_BOT_TOKEN")
REQUIRED_KEYS = ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

TEMPLATE = """# 계정 프로필: {name}
# 이 파일의 값만 씁니다 — 메인 .env를 이어받지 않습니다 (빈 칸이 메인 계좌로 채워지면 안 되니까).
# 다 채운 뒤: aistock profile start {name}

# 키움 REST API — 이 계좌 명의로 발급한 앱키 (https://openapi.kiwoom.com)
KIWOOM_MODE=real
KIWOOM_APP_KEY=
KIWOOM_APP_SECRET=
KIWOOM_ACCOUNT_NO=

# 이 계정 전용 텔레그램 봇 — @BotFather에서 새로 만든 봇의 토큰 (메인 봇과 달라야 함)
TELEGRAM_BOT_TOKEN=
# 비워두면: 새 봇에게 아무 메시지나 보낸 뒤 `aistock profile start {name}` → 찾아서 알려줌
TELEGRAM_CHAT_ID=

# 경제 봇 (Claude). 비워두면 경제 봇만 꺼짐. 질문마다 이 키의 주인에게 비용 발생
ANTHROPIC_API_KEY={anthropic}
ADVISOR_DAILY_LIMIT=40

# 기능 범위: TQQQ + 경제 봇. 국내 주문·공시 제안·국내 손절익절은 끔
TRADE_KR_ENABLED=0

# 미국 (TQQQ). 예산 0 = 외화 전액. 1건 상한은 비워두면 $7,000, 0이면 상한 없음
TRADE_US_SYMBOLS=TQQQ
TRADE_US_BUDGET_USD=0
MAX_ORDER_AMOUNT_USD=
"""


class ProfileError(Exception):
    """프로필 설정이 잘못돼 시작할 수 없다."""


def profile_dir(name: str) -> Path:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ProfileError(f"계정 이름은 영문·숫자·-·_만 쓸 수 있습니다: {name!r}")
    return PROFILES_DIR / name


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def plist_path(name: str) -> Path:
    return LAUNCH_AGENTS / f"{label(name)}.plist"


def names() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if (p / ".env").exists())


def create(name: str, *, copy_anthropic_key: bool = False) -> Path:
    home = profile_dir(name)
    env = home / ".env"
    if env.exists():
        raise ProfileError(f"이미 있는 계정입니다: {env}")
    anthropic = ""
    if copy_anthropic_key:
        anthropic = (dotenv_values(PROJECT_ROOT / ".env").get("ANTHROPIC_API_KEY") or "").strip()
    (home / "data" / "cache").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    env.write_text(TEMPLATE.format(name=name, anthropic=anthropic), encoding="utf-8")
    env.chmod(0o600)  # 앱키·토큰이 들어간다
    return env


def read_env(name: str) -> dict[str, str]:
    env = profile_dir(name) / ".env"
    if not env.exists():
        raise ProfileError(f"계정 {name!r}이 없습니다. 먼저: aistock profile create {name}")
    return {k: (v or "").strip() for k, v in dotenv_values(env).items()}


def conflicts(name: str, values: dict[str, str]) -> list[str]:
    """메인·다른 계정과 겹치는 값. 겹치면 남의 계좌·남의 봇을 건드린다."""
    others = {"메인": PROJECT_ROOT / ".env"}
    others.update({n: profile_dir(n) / ".env" for n in names() if n != name})
    found = []
    for who, path in others.items():
        if not path.exists():
            continue
        theirs = {k: (v or "").strip() for k, v in dotenv_values(path).items()}
        for key in UNIQUE_KEYS:
            if values.get(key) and values.get(key) == theirs.get(key):
                found.append(f"{key}가 {who} 계정과 같습니다")
    return found


def discover_chat_ids(token: str) -> list[tuple[str, str]]:
    """새 봇에게 메시지를 보낸 사람들의 (chat_id, 이름). TELEGRAM_CHAT_ID를 찾을 때 쓴다."""
    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    payload = r.json()
    if not payload.get("ok"):
        raise ProfileError(f"봇 토큰이 유효하지 않습니다: {payload.get('description', '')}")
    seen: dict[str, str] = {}
    for u in payload.get("result", []):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("id") is not None:
            who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            seen[str(chat["id"])] = who or chat.get("username", "")
    return list(seen.items())


def verify(name: str) -> list[str]:
    """시작해도 되는지. 문제 목록(비어 있으면 통과)."""
    values = read_env(name)
    problems = [f"{k}가 비어 있습니다" for k in REQUIRED_KEYS if not values.get(k)]
    problems += conflicts(name, values)
    if problems:
        return problems

    # 실제로 이 계정 앱키로 토큰이 나오는지 — 같은 코드를 이 프로필로 띄워 확인한다.
    env = {**os.environ, "AISTOCK_PROFILE": name}
    result = subprocess.run(
        [str(AISTOCK_BIN), "check"], env=env, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        problems.append("키움 연결 확인 실패: " + " / ".join(tail))

    r = httpx.get(f"https://api.telegram.org/bot{values['TELEGRAM_BOT_TOKEN']}/getMe", timeout=15)
    if not r.json().get("ok"):
        problems.append("텔레그램 봇 토큰이 유효하지 않습니다")
    return problems


def start(name: str) -> Path:
    """검증을 통과하면 launchd 서비스로 올린다."""
    problems = verify(name)
    if problems:
        raise ProfileError("시작하지 않았습니다:\n  - " + "\n  - ".join(problems))
    home = profile_dir(name)
    plist = {
        "Label": label(name),
        "ProgramArguments": [str(AISTOCK_BIN), "bot"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"AISTOCK_PROFILE": name, "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(home / "logs" / "bot.log"),
        "StandardErrorPath": str(home / "logs" / "bot.log"),
        "ProcessType": "Background",
    }
    path = plist_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    stop(name, quiet=True)
    with path.open("wb") as fh:
        plistlib.dump(plist, fh)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    return path


def stop(name: str, *, quiet: bool = False) -> None:
    path = plist_path(name)
    if not path.exists():
        if not quiet:
            raise ProfileError(f"실행 중인 서비스가 없습니다: {label(name)}")
        return
    _launchctl("bootout", f"gui/{os.getuid()}", str(path), check=not quiet)
    path.unlink()


def running(name: str) -> bool:
    r = subprocess.run(
        ["launchctl", "list", label(name)], capture_output=True, text=True, check=False
    )
    return r.returncode == 0


def _launchctl(*args: str, check: bool = True) -> None:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise ProfileError(f"launchctl {args[0]} 실패: {(r.stderr or r.stdout).strip()}")


def main_cli(args) -> int:  # noqa: ANN001 — argparse.Namespace
    try:
        if args.profile_command == "create":
            env = create(args.name, copy_anthropic_key=args.copy_anthropic_key)
            print(f"만들었습니다: {env}")
            print("1) 이 파일에 키움 앱키·계좌, 새 텔레그램 봇 토큰을 채우세요.")
            print("2) 새 봇에게 텔레그램으로 아무 메시지나 보내세요.")
            print(f"3) aistock profile start {args.name}")
        elif args.profile_command == "list":
            if not names():
                print("추가 계정이 없습니다. aistock profile create <이름>")
            for n in names():
                v = read_env(n)
                acct = v.get("KIWOOM_ACCOUNT_NO") or "-"
                state = "실행 중" if running(n) else "꺼짐"
                print(f"  {n:<12} 계좌 {acct:<14} {state}")
        elif args.profile_command == "start":
            values = read_env(args.name)
            if values.get("TELEGRAM_BOT_TOKEN") and not values.get("TELEGRAM_CHAT_ID"):
                found = discover_chat_ids(values["TELEGRAM_BOT_TOKEN"])
                if not found:
                    print(
                        "TELEGRAM_CHAT_ID가 비어 있습니다. 새 봇에게 텔레그램으로 메시지를 보낸 뒤 다시 실행하세요."
                    )
                    return 2
                print("새 봇에게 메시지를 보낸 사람:")
                for cid, who in found:
                    print(f"  {cid}  {who}")
                print(
                    f"이 중 계좌 주인의 번호를 profiles/{args.name}/.env 의 TELEGRAM_CHAT_ID에 넣고 다시 실행하세요."
                )
                return 2
            path = start(args.name)
            print(f"시작했습니다: {label(args.name)}")
            print(f"  서비스 {path}")
            print(f"  로그   {profile_dir(args.name) / 'logs' / 'bot.log'}")
        elif args.profile_command == "stop":
            stop(args.name)
            print(f"멈췄습니다: {label(args.name)}")
        return 0
    except ProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
