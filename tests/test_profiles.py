"""계정 프로필 검증. 키움·텔레그램·launchd를 건드리지 않는다.

여기서 막는 것은 '다른 계좌로 주문이 나가는' 사고다.
"""

import subprocess
import sys

import pytest

from aistock import profiles


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KIWOOM_APP_KEY=main-key\nTELEGRAM_BOT_TOKEN=main-bot\nANTHROPIC_API_KEY=sk-main\n")
    monkeypatch.setattr(profiles, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path / "profiles")
    return tmp_path


def _fill(root, name, **values):
    env = root / "profiles" / name / ".env"
    text = env.read_text()
    for k, v in values.items():
        text = text.replace(f"\n{k}=\n", f"\n{k}={v}\n")
    env.write_text(text)


def test_create_writes_private_template(root):
    env = profiles.create("wife")
    assert env.exists() and oct(env.stat().st_mode)[-3:] == "600"
    text = env.read_text()
    assert "TRADE_KR_ENABLED=0" in text and "ANTHROPIC_API_KEY=\n" in text
    assert profiles.names() == ["wife"]


def test_create_can_copy_anthropic_key(root):
    profiles.create("isa", copy_anthropic_key=True)
    assert "ANTHROPIC_API_KEY=sk-main" in (root / "profiles" / "isa" / ".env").read_text()


def test_create_refuses_existing(root):
    profiles.create("wife")
    with pytest.raises(profiles.ProfileError, match="이미"):
        profiles.create("wife")


def test_bad_name_rejected(root):
    with pytest.raises(profiles.ProfileError):
        profiles.create("../main")


def test_same_app_key_as_main_is_a_conflict(root):
    """메인 앱키를 그대로 넣으면 이 계정 봇이 메인 계좌로 주문한다."""
    profiles.create("wife")
    _fill(root, "wife", KIWOOM_APP_KEY="main-key", TELEGRAM_BOT_TOKEN="wife-bot")
    found = profiles.conflicts("wife", profiles.read_env("wife"))
    assert any("KIWOOM_APP_KEY" in f and "메인" in f for f in found)


def test_same_bot_token_between_profiles_is_a_conflict(root):
    """두 프로세스가 같은 봇 토큰으로 폴링하면 메시지를 서로 빼앗는다."""
    profiles.create("a")
    profiles.create("b")
    _fill(root, "a", KIWOOM_APP_KEY="a-key", TELEGRAM_BOT_TOKEN="shared-bot")
    _fill(root, "b", KIWOOM_APP_KEY="b-key", TELEGRAM_BOT_TOKEN="shared-bot")
    assert any("TELEGRAM_BOT_TOKEN" in f for f in profiles.conflicts("b", profiles.read_env("b")))


def test_verify_lists_missing_values_before_touching_network(root):
    profiles.create("wife")
    problems = profiles.verify("wife")
    assert "KIWOOM_APP_KEY가 비어 있습니다" in problems
    assert "TELEGRAM_CHAT_ID가 비어 있습니다" in problems


def test_profile_env_does_not_inherit_main(tmp_path):
    """추가 계정 .env에 앱키가 없을 때 메인 것을 이어받으면 메인 계좌로 주문이 나간다."""
    code = (
        "import os\n"
        "from aistock import config\n"
        "print(config.CACHE_DIR)\n"
        "config.load_env()\n"
        "print(repr(os.getenv('KIWOOM_APP_KEY', '')))\n"
    )
    env = {
        "AISTOCK_PROFILE": "ptest",
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # 실제 profiles/ 아래에 임시 프로필을 만든다 (끝나면 지운다)
    from aistock.config import PROFILES_DIR

    home = PROFILES_DIR / "ptest"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    finally:
        (home / ".env").unlink()
        for sub in sorted(home.rglob("*"), reverse=True):
            sub.rmdir() if sub.is_dir() else sub.unlink()
        home.rmdir()
    assert out[0].endswith("profiles/ptest/data/cache")
    assert out[1] == "''"
