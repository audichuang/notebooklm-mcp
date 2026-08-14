import io
import json
import os
import stat
import sys

import pytest

from notebooklm_mcp import auth_cli


def _valid_storage_state() -> dict:
    return {
        "cookies": [
            {"name": name, "value": "secret", "domain": ".google.com", "path": "/"}
            for name in ("SID", "__Secure-1PSIDTS", "OSID")
        ]
    }


@pytest.mark.parametrize("state", [{"cookies": []}, {"cookies": [{"name": "SID"}]}])
def test_invalid_cookie_state_does_not_touch_existing_secret(tmp_path, monkeypatch, state):
    target = tmp_path / "storage_state.json"
    target.write_text("old-secret", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["notebooklm-auth", "--out", str(target)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(state)))

    with pytest.raises(SystemExit, match="Invalid storage_state"):
        auth_cli.main()

    assert target.read_text(encoding="utf-8") == "old-secret"


def test_atomic_write_failure_preserves_existing_secret(tmp_path, monkeypatch):
    target = tmp_path / "storage_state.json"
    target.write_text("old-secret", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["notebooklm-auth", "--out", str(target)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_valid_storage_state())))

    def fail_replace(*_args):
        raise OSError("boom")

    monkeypatch.setattr(auth_cli.os, "replace", fail_replace)

    with pytest.raises(OSError, match="boom"):
        auth_cli.main()

    assert target.read_text(encoding="utf-8") == "old-secret"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_existing_secret_is_atomically_replaced_with_mode_0600(tmp_path, monkeypatch):
    target = tmp_path / "storage_state.json"
    target.write_text("old-secret", encoding="utf-8")
    target.chmod(0o644)
    state = _valid_storage_state()
    monkeypatch.setattr(sys, "argv", ["notebooklm-auth", "--out", str(target)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(state)))

    auth_cli.main()

    assert json.loads(target.read_text(encoding="utf-8")) == state
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_auth_cli_and_pool_precheck_share_one_cookie_policy(tmp_path, monkeypatch):
    """**兩個入口的接受條件只能有一份。**

    `app._write_credential_file` 那份由
    `test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 守著「與 SDK
    strict loader 等價」;auth CLI 之前是自己抄一份,那條 tripwire 照不到它 —— 上游改
    `MINIMUM_REQUIRED_COOKIES` 或 `extract_cookies_from_storage` 的語義時,只有 pool
    那邊會被改到。共用 `_cookies` 之後,同一個 tripwire 自然涵蓋兩處,而這條測試鎖住
    「真的共用」這件事本身(有人把判準抄回 CLI 裡就紅)。
    """
    from notebooklm_mcp import app
    from notebooklm_mcp import _cookies

    assert auth_cli.assert_usable_storage_state is _cookies.assert_usable_storage_state
    assert app.assert_usable_storage_state is _cookies.assert_usable_storage_state

    target = tmp_path / "storage_state.json"
    target.write_text("old-secret", encoding="utf-8")
    states = [
        (
            {
                "cookies": [
                    {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                ]
            },
            "Missing required cookies",
        ),
        (
            {
                "cookies": [
                    {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                    {"name": "__Secure-1PSIDTS", "value": "", "domain": ".google.com", "path": "/"},
                ]
            },
            None,
        ),
    ]
    for state, upstream_message in states:
        with pytest.raises(ValueError, match="必要 cookie 缺少或值是空的") as exc_info:
            _cookies.assert_usable_storage_state(state)
        if upstream_message:
            assert upstream_message in str(exc_info.value.__cause__)

        with pytest.raises(RuntimeError, match="必要 cookie 缺少或值是空的"):
            app._write_credential_file(json.dumps(state), tmp_path / "pool.json", 1)

        monkeypatch.setattr(sys, "argv", ["notebooklm-auth", "--out", str(target)])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(state)))
        with pytest.raises(SystemExit, match="Invalid storage_state: 必要 cookie"):
            auth_cli.main()
    assert target.read_text(encoding="utf-8") == "old-secret"
