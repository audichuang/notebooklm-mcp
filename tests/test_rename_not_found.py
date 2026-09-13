"""``rename(return_object=False)`` 在 0.8.0 起不再是 fire-and-forget(#1362):兩種
``return_object`` 模式都做存在性檢查,查不到就 ``raise``——見
``_web/artifacts.py::WebArtifactsAPI.rename`` / ``_web/sources/__init__.py::WebSourcesAPI.rename``
與 ``tests/test_contracts.py::test_rename_false_no_longer_short_circuits``。

``tests/conftest.py`` 的 fake 原本對查無 id 的 rename **回 None**(0.7.x 行為),與
實裝 0.8.2 不同形。這支檔案先直接釘住修好後的 fake 本身(不然沒有任何測試真的在
驗 conftest 這條修復),再釘 finalize 撞到這個真實形狀時走的分支:**「rename 失敗
不連坐下載」**——``audio_finalize.finalize_attempt`` 的 rename 例外處理只在事後
用 ``_artifact_title_state`` 驗證 postcondition 之後才決定要不要 fail-closed,不把
download 的狀態一起弄髒(download 從沒被碰過,不能被算成「失敗」)。

比照 ``tests/test_reliability_followups.py:43`` 用 ``ConnectionError`` 模擬回應遺失
的寫法,這裡換成 ``ArtifactNotFoundError``(艙面已知的真形狀,不是猜的)。

**為什麼用 monkeypatch 而不是讓 conftest 的修復自然觸發**:``audio_finalize.
finalize_attempt`` 在呼叫 rename **之前**就先用 ``_artifact_title_state`` 查一次
(:571),查不到就直接 `raise RuntimeError("... cannot be verified ...")`(:575)。
若在 dispatch 完就把 artifact 從 ``fake_client.artifacts.artifacts`` 移除,會撞到
這個更早的分支而不是 rename 的例外處理——所以要在 rename 呼叫的當下才讓 artifact
消失(模擬「rename 當下才發現遠端已經不在了」),唯一辦法是直接 monkeypatch
``rename`` 本身,而不是單靠 conftest 修好的預設行為。
"""
import json

import pytest
from notebooklm.exceptions import ArtifactNotFoundError, SourceNotFoundError

from notebooklm_mcp import tools_podcast as p


async def test_fake_artifacts_rename_raises_not_found_for_unknown_id(fake_client):
    """先釘住 conftest 修復本身:查無此 id 要 raise,不是回 None。"""
    with pytest.raises(ArtifactNotFoundError):
        await fake_client.artifacts.rename("nb-1", "no-such-artifact", "x", return_object=False)


async def test_fake_sources_rename_raises_not_found_for_unknown_id(fake_client):
    with pytest.raises(SourceNotFoundError):
        await fake_client.sources.rename("nb-1", "no-such-source", "x", return_object=False)


async def test_rename_not_found_marks_outcome_unknown_without_touching_download(
    fake_client, tmp_path, monkeypatch
):
    manifest_path = tmp_path / "series_manifest.json"

    async def vanished_at_rename_time(
        notebook_id, artifact_id, new_title, *, return_object=True
    ):
        # 模擬「rename 當下才發現遠端已經不在了」:先讓 list() 也看不到它
        # (與真實 0.8.2 的 miss-detection 同形),再 raise。
        fake_client.artifacts.artifacts[:] = [
            a for a in fake_client.artifacts.artifacts if a.id != artifact_id
        ]
        fake_client.artifacts.calls.append(
            (
                "rename",
                dict(artifact_id=artifact_id, new_title=new_title,
                     return_object=return_object),
            )
        )
        raise ArtifactNotFoundError(artifact_id)

    monkeypatch.setattr(fake_client.artifacts, "rename", vanished_at_rename_time)

    with pytest.raises(ArtifactNotFoundError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    finalize = stored["episodes"][0]["attempts"][0]["finalize"]
    assert finalize["artifact_rename"]["status"] == "outcome_unknown"
    assert finalize["download"]["status"] == "not_started", (
        "rename 失敗不連坐下載——download 從沒被嘗試過,不能標成失敗"
    )
