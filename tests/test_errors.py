"""Coverage for error / edge paths: bad language, generation timeout, malformed input."""
import json
import os

import pytest
from notebooklm.exceptions import ClientError, NetworkError

from notebooklm_mcp import _errors
from notebooklm_mcp import tools_basic as t
from notebooklm_mcp import tools_podcast as p


async def test_bad_language_raises_before_any_sdk_call(fake_client):
    with pytest.raises(ValueError) as exc:
        await t.generate_audio("nb-1", language="zh-TW")
    msg = str(exc.value)
    assert "zh_Hant" in msg or "underscore" in msg.lower()
    # The bad code is rejected up front — no generation was triggered.
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)


async def test_series_generation_timeout_surfaces_with_partial_manifest(fake_client, tmp_path):
    # Episode 2's wait_for_completion raises (episode 1's wait is call #1, episode 2's is #2).
    fake_client.artifacts.fail_wait_on = 2
    eps = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
        {"title": "收尾篇", "brief": "3"},
    ]

    out = await p.podcast_series(
        "nb-1", episodes=eps, output_dir=str(tmp_path), start=1
    )
    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["observed_state"] == "pending"
    assert out["safe_next_action"] == "podcast_series"

    # EP2 的 accepted attempt 必須留下，讓下次重呼 wait 同一 artifact；
    # 只有 EP1 已 promotion，EP3 尚未產生任何 attempt。
    manifest = json.load(open(os.path.join(str(tmp_path), "series_manifest.json"), encoding="utf-8"))
    assert [e["episode"] for e in manifest["episodes"]] == [1, 2]
    assert manifest["episodes"][0]["output_attempt_id"]
    assert "output_attempt_id" not in manifest["episodes"][1]
    attempt = manifest["episodes"][1]["attempts"][0]
    assert attempt["remote"]["artifact_id"] == "task-124"
    assert attempt["remote"]["status"] == "pending"
    assert fake_client.sources.titles() == ["EP01 心法篇"]


async def test_series_resume_network_error_returns_structured_partial(
    fake_client, tmp_path
):
    fake_client.artifacts.fail_wait_on = 1
    eps = [{"title": "心法篇", "brief": "1"}]

    first = await p.podcast_series(
        "nb-1", episodes=eps, output_dir=str(tmp_path)
    )
    assert first["complete"] is False
    assert first["observed_state"] == "pending"

    fake_client.artifacts.fail_wait_on = 2
    fake_client.artifacts.wait_exc = NetworkError("resume network down")
    resumed = await p.podcast_series(
        "nb-1", episodes=eps, output_dir=str(tmp_path)
    )

    assert resumed["complete"] is False
    assert resumed["stopped_at_episode"] == 1
    assert resumed["observed_state"] == "pending"
    assert resumed["safe_next_action"] == "podcast_series"
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["remote"]["artifact_id"] == "task-123"
    assert attempt["remote"]["status"] == "pending"


async def test_malformed_episodes_fail_fast_with_clear_error(fake_client, tmp_path):
    with pytest.raises(ValueError) as exc:
        await p.podcast_series("nb-1", episodes=["just a string"], output_dir=str(tmp_path))
    assert "brief" in str(exc.value)
    # Failed validation before any generation.
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)


async def test_failed_generation_status_fails_fast(fake_client, tmp_path):
    # 0.7.x 風格的拒絕形狀:status(task_id="", is_failed=True)而不 raise。**0.8.0
    # 起同步拒絕改成 raise**(ADR-0019 / #1342,見
    # test_contracts.py::test_generation_kickoff_refuses_by_raising)——這裡驗的是
    # ensure_started 仍防得住這個舊分支(defense-in-depth),不是真實 SDK 現在的形狀。
    fake_client.artifacts.fail_generate = True
    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode("nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path))
    # It stopped right after generate — no wait/download on the empty id.
    kinds = [c[0] for c in fake_client.artifacts.calls]
    assert kinds == ["generate_audio"]


async def test_generate_audio_tool_fails_fast_on_failed_status(fake_client):
    fake_client.artifacts.fail_generate = True
    with pytest.raises(RuntimeError, match="Generation failed"):
        await t.generate_audio("nb-1", instructions="x")


async def test_failure_during_wait_fails_fast(fake_client, tmp_path):
    # Generation can START fine (valid task_id) but FAIL mid-poll. The real 0.3.4
    # wait_for_completion RETURNS that failed status (it only raises on timeout),
    # so ensure_completed must catch is_failed before we rename/download a dead
    # artifact. Without that guard the run would proceed on a failed generation.
    fake_client.artifacts.fail_complete = True
    with pytest.raises(RuntimeError, match="failed while waiting"):
        await p.podcast_episode("nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path))
    # Stopped right after the wait — never reached rename/download/self-upload,
    # so the failed episode left NO orphaned source or artifact rename.
    assert [c[0] for c in fake_client.artifacts.calls] == ["generate_audio", "wait"]
    assert fake_client.sources.titles() == []


async def test_removed_status_during_wait_fails_fast(fake_client, tmp_path):
    # 0.6.0 起:配額耗盡/伺服器下架的 artifact 回 status="removed" 且 is_failed=False
    #(0.4.x 是合成 "failed")。ensure_completed 只看 is_failed 會把它當成功放行,
    # 於是帶著死 artifact 繼續 rename/download,最後以誤導性錯誤爆掉、遮蔽配額真因。
    # 多小時整季生成撞每日配額正是這條路徑,必須 fail-loud 且點出配額。
    fake_client.artifacts.fail_removed = True
    with pytest.raises(RuntimeError, match="removed"):
        await p.podcast_episode("nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path))
    # 停在 wait 之後,沒進 rename/download/自上傳。
    assert [c[0] for c in fake_client.artifacts.calls] == ["generate_audio", "wait"]
    assert fake_client.sources.titles() == []


def _client_error_without_rpc_code() -> ClientError:
    exc = ClientError("permission denied")
    try:
        del exc.rpc_code
    except AttributeError:
        pass
    assert not hasattr(exc, "rpc_code")
    return exc


class _NotAClientError(Exception):
    """形狀跟 ClientError 一樣(有 rpc_code)但型別不對 —— 只有 isinstance guard 擋得住。"""

    rpc_code = 7


@pytest.mark.parametrize(
    ("rpc_code", "expected"),
    [
        (7, True),
        ("7", True),
        (5, False),
        ("5", False),
        (None, False),
        ("", False),
    ],
)
def test_is_permission_denied_normalizes_upstream_rpc_codes(rpc_code, expected):
    exc = ClientError("request failed", rpc_code=rpc_code)

    assert _errors.is_permission_denied(exc) is expected


@pytest.mark.parametrize(
    "exc", [RuntimeError("permission denied"), _NotAClientError()],
    ids=["not-client-error", "not-client-error-with-rpc-code"],
)
def test_is_permission_denied_rejects_non_client_errors_and_missing_codes(exc):
    assert _errors.is_permission_denied(exc) is False


def test_is_permission_denied_rejects_client_error_without_rpc_code():
    """建構在測試執行期,避免上游改變 rpc_code 的屬性形狀時 collection 先炸掉。"""
    assert _errors.is_permission_denied(_client_error_without_rpc_code()) is False


def test_is_permission_denied_uses_upstream_normalizer(monkeypatch):
    seen = []

    def fake_normalize_rpc_code(code):
        seen.append(code)
        return 7

    monkeypatch.setattr(_errors, "normalize_rpc_code", fake_normalize_rpc_code)

    assert _errors.is_permission_denied(ClientError("request failed", rpc_code="7"))
    assert seen == ["7"]


def test_notebook_access_denied_remains_runtime_error():
    assert issubclass(_errors.NotebookAccessDenied, RuntimeError)


def test_permission_denied_message_is_stable_and_names_both():
    """這段話會進 manifest 當稽核紀錄、也會原樣當停點指引回給呼叫端 —— 措辭要釘住。

    v0.9.16 把原本**兩份**逐字相同的文字(`raise_if_access_denied` 指名 notebook、
    `tools_podcast` 的 failover 迴圈指名帳號)合成一支。合併的第一版真的漂了:
    少一個空格、還刪掉結尾的「既有 notebook」,而當時沒有任何測試看得出來
    (既有的只 match「分享」與工具名),由 codex 獨立複審抓出來。
    """
    from notebooklm_mcp._errors import access_denied_error

    exc = Exception("permission denied")
    tail = (
        "多帳號 pool 模式要求 notebook 對 pool 全員可存取 —— 呼叫 "
        "notebook_share_with_pool(notebook_id=...) 補分享給其餘帳號(EDITOR)後再重試;"
        "MCP 自建 notebook(v0.8.1 起)已自動分享,這通常是舊版建立或在網頁上手動建立的"
        "既有 notebook。"
    )
    # 只有帳號(音檔 failover 在沒傳 notebook_id 時的退化形狀)
    assert str(access_denied_error(exc, account="a@x")) == (
        f"permission denied\n帳號 'a@x' 對這個 notebook 沒有存取權。{tail}"
    )
    # 只有 notebook(`raise_if_access_denied` —— notebook_get / _list_sources 走這條)
    assert str(access_denied_error(exc, notebook_id="nb-1")) == (
        f"permission denied\n這個帳號對 notebook nb-1 沒有存取權。{tail}"
    )
    # 兩個都有:合併之後才做得到,比原來任一份都完整
    assert str(access_denied_error(exc, notebook_id="nb-1", account="a@x")) == (
        f"permission denied\n帳號 'a@x' 對 notebook nb-1 沒有存取權。{tail}"
    )


async def test_acceptance_unknown_records_the_real_reason_not_an_object_repr(
    fake_client, tmp_path
):
    """「有 task_id 卻 is_failed」傳給 `_mark_acceptance_unknown` 的是 **status 物件**。

    舊版本用 `type(error).__name__` / `str(error)` 直接處理,落盤成
    `type: "GenerationStatus"` / `message: "<... object at 0x7f...>"` —— 真正的原因整條
    蒸發,而 `dispatch.status` 卻正確地標成 `acceptance_unknown`。卡在這個狀態的人打開
    manifest 只看到一個記憶體地址。**這條路此前沒有任何測試碰過那兩個欄位。**
    """
    import json

    from notebooklm_mcp import runtime
    from notebooklm_mcp import tools_podcast as p

    class _AcceptedThenFailed:
        task_id = "art-123"
        is_failed = True
        status = "failed"
        error = "伺服器端生成失敗:配額不足"
        error_code = "RateLimitError"

    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store, notebook_id="nb-1", episode_n=1, title="t", brief="b",
        language="en", audio_format=None, audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="a@x", wait_timeout=1200.0)
    runtime.set_clients([("a@x", fake_client)])

    async def accepted_then_failed(client):
        return _AcceptedThenFailed()

    with pytest.raises(RuntimeError):
        await p._dispatch_audio_with_failover(
            store, 1, attempt_id, accepted_then_failed,
            account="a@x", client=fake_client,
        )

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    recorded = attempt["errors"][-1]
    assert recorded["type"] == "RateLimitError"
    assert recorded["message"] == "伺服器端生成失敗:配額不足"
    assert "object at 0x" not in recorded["message"]
