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
