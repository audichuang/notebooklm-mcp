"""``source_ids`` 選取:能力在 MCP,選哪幾筆的政策留 host(ADR-0007)。

SDK 的 ``generate_audio`` 早就吃 ``source_ids``,不傳就 fallback 抓筆記本全部來源。
我們只是把那個洞往上開;「重生 EP05 時排除 EP06+ 的來源」這種 Audicast 操作政策
不進 capability layer。
"""
import json

import pytest
from test_generation_input_bundle import _write_bundle

from notebooklm_mcp import tools_basic as b
from notebooklm_mcp import tools_podcast as p


def _audio_call(fake_client):
    return next(
        call for call in fake_client.artifacts.calls if call[0] == "generate_audio"
    )[1]


async def test_generate_audio_forwards_the_caller_selection(fake_client):
    fake_client.sources.seed("EP05 題目", "EP04 心法篇")
    await b.generate_audio("nb-1", source_ids=["src-1", "src-2"])
    assert _audio_call(fake_client)["source_ids"] == ["src-1", "src-2"]


async def test_generate_audio_without_selection_keeps_sdk_fallback(fake_client):
    """不傳 = 現行行為(SDK 自己抓全部來源),向後相容。"""
    await b.generate_audio("nb-1")
    assert _audio_call(fake_client)["source_ids"] is None


@pytest.mark.parametrize(
    "bad", [[], ["src-1", "src-1"], ["src-1", ""], ["src-1", 2], "src-1"]
)
async def test_generate_audio_rejects_a_malformed_selection(fake_client, bad):
    """空清單/重複/非字串在打 RPC 之前就退——生成是燒配額的不可逆副作用。"""
    with pytest.raises(ValueError):
        await b.generate_audio("nb-1", source_ids=bad)
    assert not fake_client.artifacts.calls


async def test_episode_pins_the_selection_into_attempt_settings(fake_client, tmp_path):
    """source_ids 是生成輸入,得跟 language/format/length 一樣進 attempt settings,
    否則斷線 resume 可能用不同的來源集合續生同一集。"""
    fake_client.sources.seed("EP05 題目", "EP04 心法篇")
    manifest_path = tmp_path / "series_manifest.json"

    await p.podcast_episode(
        "nb-1",
        episode_n=5,
        title="心法篇",
        brief="第五集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=["src-1", "src-2"],
    )

    assert _audio_call(fake_client)["source_ids"] == ["src-1", "src-2"]
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["settings"]["source_ids"] == ["src-1", "src-2"]


async def test_unpinned_settings_stay_shaped_like_pre_source_ids_manifests(
    fake_client, tmp_path
):
    """沒選來源時 settings 不得多長出 key —— `podcast_series` 的 prepared-attempt
    比對是逐字等值,多一個 key 會讓既有 manifest 被判成「設定變了」。"""
    manifest_path = tmp_path / "series_manifest.json"

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert set(attempt["settings"]) == {"language", "audio_format", "audio_length"}
    assert _audio_call(fake_client)["source_ids"] is None


async def test_episode_rejects_an_absent_source_before_burning_a_generation(
    fake_client, tmp_path
):
    """打錯/已被刪掉的 source_id 會靜默生出「看不到那幾筆來源」的音檔,燒完 20 分鐘
    才發現(同 `_validate_report_prompt` 的情境)。上線前用 source_list 對帳。"""
    fake_client.sources.seed("EP05 題目")
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(ValueError, match="src-9"):
        await p.podcast_episode(
            "nb-1",
            episode_n=5,
            title="心法篇",
            brief="第五集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            source_ids=["src-1", "src-9"],
        )

    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)
    assert not manifest_path.exists()


async def test_frozen_bundle_replay_rejects_a_changed_selection(
    fake_client, tmp_path, monkeypatch
):
    """frozen bundle 重跑沿用同一 attempt,但換了來源集合就不再是同一次生成——
    binding 會在說謊。"""
    fake_client.sources.seed("EP01 題目", "EP01 附錄")
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    real_list = fake_client.artifacts.list
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionError("baseline unavailable")
        return await real_list(*args, **kwargs)

    monkeypatch.setattr(fake_client.artifacts, "list", fail_once)
    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(workspace / "output"),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )
    with pytest.raises(ConnectionError, match="baseline unavailable"):
        await p.podcast_episode("nb-1", source_ids=["src-1"], **args)

    with pytest.raises(ValueError, match="frozen generation input"):
        await p.podcast_episode("nb-1", source_ids=["src-1", "src-2"], **args)
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)

    # 同一組選取重跑才續得下去,且不重建 attempt。
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["attempts"][0]["attempt_id"]
    await p.podcast_episode("nb-1", source_ids=["src-1"], **args)
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [a["attempt_id"] for a in final["episodes"][0]["attempts"]] == [attempt_id]
    assert _audio_call(fake_client)["source_ids"] == ["src-1"]


# ---- v0.7.1 真實驗收 F-8:配額拒絕之後的死鎖 -----------------------------------
# 情境:QA 拒收重生(podcast_episode + source_ids)剛好撞到每日配額。attempt 落在
# not_accepted(從未 dispatch、沒有 artifact),而當時**五條出路全是死的**:
#   podcast_episode  → 「already has durable active attempt」
#   reconcile        → 「state 'not_accepted' cannot be reconciled」
#   resume           → 必填 artifact_id,而它是 null
#   podcast_series   → 「settings changed」(整季不吃 per-episode source_ids)
#   retract          → 只認已 promote 的 output
# 唯一脫出是手改 manifest —— ADR-0009 明令禁止。修法:讓建立它的那支工具原樣重呼即可
# 沿用同一個 attempt 重送。

async def _quota_blocked_episode(fake_client, tmp_path):
    """製造一個「帶 source_ids、撞到配額拒絕」的 active attempt,回傳呼叫參數。"""
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"   # podcast_episode 會自己建
    fake_client.sources.seed("EP01 題目", "EP01 補充")
    args = dict(
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    with pytest.raises(RateLimitError):
        await p.podcast_episode("nb-1", source_ids=["src-1", "src-2"], **args)
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["remote"]["artifact_id"] is None
    return args, manifest_path, attempt["attempt_id"]


async def test_refusal_error_tells_the_caller_how_to_resume(fake_client, tmp_path):
    """F-9:拒絕分支不能裸拋。呼叫端只看到 SDK 的 rate-limit 字串時,既不知道 attempt
    已被持久化,也不知道下一步——而姊妹分支(acceptance_unknown)8 行外就把 attempt_id
    與確切指令塞進訊息了。兩個分支的待遇要一致。"""
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.seed("EP01 題目")
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")

    with pytest.raises(RateLimitError) as caught:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            source_ids=["src-1"],
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["attempts"][0]["attempt_id"]
    message = str(caught.value)
    assert attempt_id in message, "訊息沒帶 attempt_id"
    assert "podcast_episode" in message, "訊息沒說下一步用哪支工具"
    assert "沒有" in message and "artifact" in message, "訊息沒說清楚遠端什麼都沒建"


async def test_identical_recall_resends_the_quota_refused_attempt(
    fake_client, tmp_path
):
    """原樣重呼 = 沿用同一個 attempt 重送,不建新的、不多燒一次配額。"""
    args, manifest_path, attempt_id = await _quota_blocked_episode(fake_client, tmp_path)

    fake_client.artifacts.generate_audio_exc = None
    out = await p.podcast_episode("nb-1", source_ids=["src-1", "src-2"], **args)

    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = final["episodes"][0]
    # 只能有一個 attempt,而且就是原來那個 —— 新建 attempt 等於承認死鎖沒修好。
    assert [a["attempt_id"] for a in episode["attempts"]] == [attempt_id]
    assert out["episode"] == 1
    assert _audio_call(fake_client)["source_ids"] == ["src-1", "src-2"]
    # 被拒的原因不能因為重送就消失(F-7 的同一條紀律):remote 清回乾淨的 prepared,
    # 但**為什麼被拒**留在 errors[](只 append、從不清除)。
    # 註:errors[].type 是寫死的 "not_accepted"(例外型別名放在 remote.error_code),
    # 所以這裡驗訊息本身,不驗型別欄位。
    refusals = [
        e for e in episode["attempts"][0]["errors"] if e["phase"] == "dispatch"
    ]
    assert any("配額" in e["message"] for e in refusals), refusals


async def test_recall_with_different_selection_still_fails_loud(fake_client, tmp_path):
    """**只有逐字相同的請求**才沿用。設定變了還沿用 = 靜默改掉生成輸入,比死鎖更糟。"""
    args, manifest_path, _ = await _quota_blocked_episode(fake_client, tmp_path)
    fake_client.artifacts.generate_audio_exc = None

    with pytest.raises(ValueError, match="already has durable active attempt"):
        await p.podcast_episode("nb-1", source_ids=["src-1"], **args)
    # brief 變了也一樣不沿用
    with pytest.raises(ValueError, match="already has durable active attempt"):
        await p.podcast_episode(
            "nb-1", source_ids=["src-1", "src-2"], **{**args, "brief": "改過的 brief"}
        )
    # 只有 helper 那一次;兩次被擋的呼叫都不該打到 RPC。
    assert sum(1 for c in fake_client.artifacts.calls if c[0] == "generate_audio") == 1


async def test_series_names_podcast_episode_as_the_owner(fake_client, tmp_path):
    """整季接不了帶 source_ids 的 attempt —— 但訊息要**指出誰接得了**。

    驗收當時 troubleshooting 對 not_accepted 的指示是「重呼 podcast_series」,而那對
    這種 attempt 恰好是唯一會把狀態改壞的動作。訊息只講「設定變了」會把人卡死。
    """
    args, manifest_path, _ = await _quota_blocked_episode(fake_client, tmp_path)
    fake_client.artifacts.generate_audio_exc = None

    with pytest.raises(ValueError, match="podcast_episode"):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),   # series 由 output_dir 推導 manifest 路徑
        )


async def test_failed_series_call_does_not_wipe_the_refusal_evidence(
    fake_client, tmp_path
):
    """F-7:mutate-before-validate。註定失敗的 series 呼叫**不得**先把診斷抹掉。

    舊行為:rearm 先跑(清掉 remote.error / error_code / dispatched_at、把 status 設成
    unknown),settings 才驗、才 raise。抹完的 manifest 長得正好像「分類從未生效過」,
    真相只剩 errors[]。
    """
    args, manifest_path, _ = await _quota_blocked_episode(fake_client, tmp_path)
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    before_remote = before["episodes"][0]["attempts"][0]["remote"]
    assert before_remote["error_code"] == "RateLimitError"

    with pytest.raises(ValueError):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),   # series 由 output_dir 推導 manifest 路徑
        )

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    after_attempt = after["episodes"][0]["attempts"][0]
    assert after_attempt["remote"] == before_remote, "失敗的呼叫抹掉了拒絕證據"
    assert after_attempt["dispatch"]["status"] == "not_accepted"
