"""`_dispatch_prepared_attempt` 的介面層測試:每一種結果形狀都從它的回傳值驗證。

這支 module 把「claim → failover 派送 → 讀回 manifest 分類 → bind」收成一處,
`_run_episode` 與 `podcast_series` 只翻譯結果。這裡不經過任何一個呼叫端,
直接鎖住結果本身:呼叫端對它的任何誤讀都該先在這裡紅。
"""

import asyncio
import json

import pytest
from notebooklm.exceptions import RateLimitError

from notebooklm_mcp import runtime, tools_podcast as p


def _prepared(tmp_path):
    path = tmp_path / "m.json"
    store = p.ManifestStore(str(path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="t",
        brief="b",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    return path, store, attempt_id


def _dispatch_status(path):
    return json.loads(path.read_text(encoding="utf-8"))["episodes"][0]["attempts"][0]["dispatch"][
        "status"
    ]


async def _run(store, attempt_id, generate, client):
    runtime.set_clients([("solo@x", client)])
    return await p._dispatch_prepared_attempt(
        store,
        "nb-1",
        1,
        attempt_id,
        generate,
        account="solo@x",
        client=client,
        wait_timeout=1200.0,
    )


async def test_accepted_returns_dispatching_identity_and_binds(fake_client, tmp_path):
    path, store, attempt_id = _prepared(tmp_path)

    async def generate(client):
        return await client.artifacts.generate_audio("nb-1")

    outcome = await _run(store, attempt_id, generate, fake_client)

    assert isinstance(outcome, p.DispatchAccepted)
    assert outcome.account == "solo@x"
    assert outcome.client is fake_client
    assert _dispatch_status(path) == "accepted"
    attempt = json.loads(path.read_text(encoding="utf-8"))["episodes"][0]["attempts"][0]
    assert attempt["remote"]["artifact_id"] == outcome.artifact_id


async def test_not_claimed_sends_nothing(fake_client, tmp_path):
    _, store, attempt_id = _prepared(tmp_path)
    assert p.AttemptRef(store, 1, attempt_id).claim_prepared([], account="x", wait_timeout=1.0)
    called = []

    async def generate(client):
        called.append(1)

    outcome = await _run(store, attempt_id, generate, fake_client)

    assert isinstance(outcome, p.DispatchNotClaimed)
    assert called == []


@pytest.mark.parametrize(
    ("make_exc", "expected_recorded"),
    [
        (lambda: RateLimitError("quota"), "not_accepted"),
        # 送出後才被打回的 429:結果不明,不准當乾淨拒絕
        (lambda: _unconfirmed(), "acceptance_unknown"),
        # post-send 的普通 RuntimeError:泛用分支標 acceptance_unknown,型別不是判準
        (lambda: RuntimeError("weird"), "acceptance_unknown"),
        (lambda: asyncio.CancelledError(), "acceptance_unknown"),
    ],
    ids=["refused", "unconfirmed-429", "post-send-runtime", "cancelled"],
)
async def test_stopped_carries_original_exc_and_recorded_state(
    fake_client, tmp_path, make_exc, expected_recorded
):
    path, store, attempt_id = _prepared(tmp_path)
    exc = make_exc()

    async def generate(client):
        raise exc

    outcome = await _run(store, attempt_id, generate, fake_client)

    assert isinstance(outcome, p.DispatchStopped)
    assert outcome.exc is exc, "原例外要原樣交回,不改寫、不包裝"
    assert outcome.recorded == expected_recorded == _dispatch_status(path)


async def test_standalone_has_no_manifest_state(fake_client):
    runtime.set_clients([("solo@x", fake_client)])

    async def refuse(client):
        raise RuntimeError("boom")

    stopped = await p._dispatch_prepared_attempt(
        None, "nb-1", 1, None, refuse, account="solo@x", client=fake_client, wait_timeout=1.0
    )
    assert isinstance(stopped, p.DispatchStopped)
    assert stopped.recorded is None


async def test_unreadable_manifest_yields_none_not_a_new_error(fake_client, tmp_path):
    path, store, attempt_id = _prepared(tmp_path)

    async def generate(client):
        # 派送途中 attempt 被並行 retract 成 tombstone 的替身:讓讀回失敗
        path.write_text("not json", encoding="utf-8")
        raise RateLimitError("quota")

    outcome = await _run(store, attempt_id, generate, fake_client)

    assert isinstance(outcome, p.DispatchStopped)
    assert outcome.recorded is None


def _unconfirmed():
    # 用 SDK 自己的 mark_unconfirmed 造例外 —— 驗的是上游真的會掛上去的那個標記。
    from notebooklm._idempotency import mark_unconfirmed

    return mark_unconfirmed(RateLimitError("429 after send"))
