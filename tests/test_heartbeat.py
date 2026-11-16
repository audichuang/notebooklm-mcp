"""`_heartbeat`:工具呼叫期間送 progress,且心跳的任何失敗都不碰工具本體。

全部走 in-memory client/server(真的 JSON-RPC 往返),工具註冊在一次性的 FastMCP 上。
"""

import asyncio

import anyio
import mcp.types as types
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.server.session import ServerSession
from mcp.shared.memory import create_connected_server_and_client_session

from notebooklm_mcp import _heartbeat


@pytest.fixture(autouse=True)
def _fast_interval(monkeypatch):
    monkeypatch.setattr(_heartbeat, "INTERVAL", 0.05)


def _server(**tools) -> FastMCP:
    srv = FastMCP("hb-test")
    for name, fn in tools.items():
        srv.tool(name=name)(fn)
    _heartbeat.install(srv._tool_manager.list_tools())
    return srv


def _text(result: types.CallToolResult) -> str:
    return result.content[0].text


async def test_slow_tool_emits_increasing_progress():
    async def slow() -> str:
        await asyncio.sleep(0.3)
        return "done"

    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with create_connected_server_and_client_session(_server(slow=slow)) as client:
        result = await client.call_tool("slow", {}, progress_callback=on_progress)

    assert not result.isError and _text(result) == "done"
    assert len(seen) >= 2
    values = [p for p, _, _ in seen]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert all(total is None and message for _, total, message in seen)


@pytest.mark.parametrize("mode", ["raise", "hang"])
async def test_send_failure_or_hang_does_not_affect_result(monkeypatch, mode):
    calls = 0

    async def broken_send(self, *a, **kw):
        nonlocal calls
        calls += 1
        if mode == "raise":
            raise RuntimeError("transport gone")
        await asyncio.Event().wait()  # 永遠卡住

    monkeypatch.setattr(ServerSession, "send_progress_notification", broken_send)

    async def slow() -> str:
        await asyncio.sleep(0.25)
        return "ok"

    async with create_connected_server_and_client_session(_server(slow=slow)) as client:
        with anyio.fail_after(2):
            result = await client.call_tool("slow", {}, progress_callback=lambda *a: None)

    assert calls >= 1
    assert not result.isError and _text(result) == "ok"


async def test_tool_exception_propagates_unchanged_and_heartbeat_cleaned_up():
    tasks: list[asyncio.Task] = []

    async def boom() -> str:
        await asyncio.sleep(0.12)
        tasks.extend(t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_beat")
        raise ValueError("本體失敗")

    async def boom_plain() -> str:
        raise ValueError("本體失敗")

    srv = _server(boom=boom)
    plain = FastMCP("plain")
    plain.tool(name="boom")(boom_plain)

    async with create_connected_server_and_client_session(srv) as client:
        wrapped = await client.call_tool("boom", {}, progress_callback=lambda *a: None)
        await asyncio.sleep(0)
    async with create_connected_server_and_client_session(plain) as client:
        unwrapped = await client.call_tool("boom", {})

    assert wrapped.isError and _text(wrapped) == _text(unwrapped)
    assert len(tasks) == 1 and tasks[0].done()


async def test_client_cancellation_cancels_tool_as_before():
    started = asyncio.Event()
    request_ids: list = []
    cancelled = asyncio.Event()
    beats: list[asyncio.Task] = []

    async def forever() -> str:
        request_ids.append(request_ctx.get().request_id)
        beats.extend(t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_beat")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    async with (
        create_connected_server_and_client_session(_server(forever=forever)) as client,
        anyio.create_task_group() as tg,
    ):

        async def call():
            try:
                await client.call_tool("forever", {}, progress_callback=lambda *a: None)
            except Exception:  # noqa: BLE001 —— 取消後 server 回 error,這裡只關心本體
                pass

        tg.start_soon(call)
        with anyio.fail_after(2):
            await started.wait()
        await client.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=request_ids[0])
                )
            )
        )
        with anyio.fail_after(2):
            await cancelled.wait()
        tg.cancel_scope.cancel()

    await asyncio.sleep(0)
    assert cancelled.is_set()
    assert len(beats) == 1 and beats[0].done()


async def test_no_progress_token_sends_nothing(monkeypatch):
    sent = 0

    async def counting_send(self, *a, **kw):
        nonlocal sent
        sent += 1

    monkeypatch.setattr(ServerSession, "send_progress_notification", counting_send)

    async def slow() -> str:
        assert not [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_beat"]
        await asyncio.sleep(0.2)
        return "quiet"

    async with create_connected_server_and_client_session(_server(slow=slow)) as client:
        result = await client.call_tool("slow", {})

    assert not result.isError and _text(result) == "quiet"
    assert sent == 0


async def test_outside_request_context_just_calls_through():
    async def f(x: int) -> int:
        return x + 1

    assert await _heartbeat.wrap(f)(1) == 2


def test_every_production_tool_is_wrapped():
    from notebooklm_mcp.app import mcp

    tools = mcp._tool_manager.list_tools()
    assert len(tools) >= 37
    assert all(getattr(t.fn, "__heartbeat__", False) for t in tools)
