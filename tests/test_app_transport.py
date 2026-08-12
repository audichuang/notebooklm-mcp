import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notebooklm_mcp import app


def test_remote_http_bind_requires_explicit_insecure_opt_in(monkeypatch):
    fake_mcp = MagicMock(settings=SimpleNamespace(host=None, port=None))
    monkeypatch.setattr(app, "mcp", fake_mcp)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notebooklm-mcp", "--transport", "streamable-http", "--host", "0.0.0.0"],
    )

    with pytest.raises(SystemExit, match="allow-insecure-remote"):
        app.main()
    fake_mcp.run.assert_not_called()


def test_remote_http_bind_allows_explicit_insecure_opt_in(monkeypatch):
    fake_mcp = MagicMock(settings=SimpleNamespace(host=None, port=None))
    monkeypatch.setattr(app, "mcp", fake_mcp)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "notebooklm-mcp",
            "--transport",
            "sse",
            "--host",
            "::",
            "--allow-insecure-remote",
        ],
    )

    app.main()

    fake_mcp.run.assert_called_once_with(transport="sse")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.42.0.9", "::1"])
def test_http_bind_allows_loopback_hosts_without_opt_in(monkeypatch, host):
    fake_mcp = MagicMock(settings=SimpleNamespace(host=None, port=None))
    monkeypatch.setattr(app, "mcp", fake_mcp)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notebooklm-mcp", "--transport", "streamable-http", "--host", host],
    )

    app.main()

    assert fake_mcp.settings.host == host
    fake_mcp.run.assert_called_once_with(transport="streamable-http")
