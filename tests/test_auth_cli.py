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
