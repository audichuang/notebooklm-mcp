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


async def _remotely_failed_pinned_episode(fake_client, tmp_path):
    """製造「帶 source_ids、已 accepted、遠端回報 failed」的 active attempt。

    跟 `_quota_blocked_episode` 是**兩種不同的形狀**,而差別正是這一輪的缺口:那顆
    從沒離開本機(`dispatch=not_accepted`),這顆已經送出去、伺服器才判定失敗
    (`dispatch=accepted` + `remote=failed`)。series 的接手守門只掛在前者。
    """
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.seed("EP01 題目", "EP01 補充")
    fake_client.artifacts.fail_complete = True
    with pytest.raises(p.TerminalGenerationError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            source_ids=["src-1"],
        )
    fake_client.artifacts.fail_complete = False
    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "attempts"
    ][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["status"] == "failed"
    assert attempt["settings"]["source_ids"] == ["src-1"]
    return manifest_path, attempt["attempt_id"]


async def test_series_refuses_to_supersede_an_attempt_it_does_not_own(
    fake_client, tmp_path
):
    """**內容錯置**:series 不得把指名來源的 attempt supersede 成不指名的。

    實測形狀(v0.9.12 與更早皆可重現):`podcast_episode(source_ids=["src-1"])` 遠端
    失敗後直接重呼 `podcast_series`,兩次 dispatch 送出 `[["src-1"], None]` —— 第二次
    靜默改讀整本筆記本(含後面各集的回錄音檔),而工具回報 `complete=True`。

    `_attempt_next_step()` 的 `settled` 分支註解早就點名這個形狀、也刻意不教人重呼
    series;但**只防了訊息、沒防程式路徑**——真的有人呼進來時 supersede 分支照做。
    接手守門必須擺在「會產生新 dispatch」的**共同上游**,而不是只有 prepared/
    not_accepted 那兩格。
    """
    await _remotely_failed_pinned_episode(fake_client, tmp_path)
    before = sum(
        1 for c in fake_client.artifacts.calls if c[0] == "generate_audio"
    )

    with pytest.raises(ValueError, match="podcast_attempt_retract"):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),
        )

    generated = [
        c[1] for c in fake_client.artifacts.calls if c[0] == "generate_audio"
    ]
    assert len(generated) == before, "series 又送出了一次不指名來源的生成"
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    # 拒絕必須在任何 manifest mutation 之前(F-7 同一條紀律)。
    assert len(stored["episodes"][0]["attempts"]) == 1


async def test_the_refusal_for_a_pinned_attempt_is_actually_walkable(
    fake_client, tmp_path
):
    """訊息必須由 `_attempt_capabilities()` 產生 —— 手寫的那句在這一格是**假的**。

    `_assert_series_owns_attempt` 原本的 owner_hint 無條件說「re-calling
    podcast_episode with the identical arguments … resends that same attempt」,
    但 `accepted`+`failed` 的 `can_resend` 是 False(`_is_resendable_same_request`
    只收 `_NEVER_DISPATCHED`),照做會撞 `already has durable active attempt`。
    那正是 docs/gotchas-attempt.md 紅線列的「指引在它自己產生的狀態下不可執行」。
    """
    manifest_path, attempt_id = await _remotely_failed_pinned_episode(
        fake_client, tmp_path
    )
    with pytest.raises(ValueError) as caught:
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),
        )
    message = str(caught.value)
    assert attempt_id in message
    assert "podcast_attempt_retract" in message, "沒說唯一走得通的出口"
    assert "podcast_episode" in message, "沒說 retract 之後誰接得回來"
    assert "source_ids" in message, "沒提醒重生要帶回原本那組來源"
    # 這一格不能建議原樣重送(can_resend=False),照做會撞 already-has-active。
    assert "原樣重呼" not in message

    # 照訊息走真的走得出去 —— 而且**只准用公開回傳裡的值**(自足性紅線的檢驗端,
    # docs/gotchas-attempt.md)。從 fixture 硬寫 `source_ids=["src-1"]` 會讓這條測試
    # 變成空話:真實呼叫端手上沒有那組 id,省略它就靜默讀整本筆記本。
    retracted = await p.podcast_attempt_retract(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        reason="遠端生成失敗,照 series 的指引作廢重生",
    )
    for row in retracted.get("source_cleanup_obligations", []):
        await b.source_delete(row["notebook_id"], row["source_id"])
    assert retracted["safe_next_action"] == "podcast_episode"
    assert retracted["regeneration_source_ids"] == [
        "src-1"
    ], "retract 教人「帶回原本那組 source_ids」,回傳卻沒給那組"
    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=retracted["regeneration_source_ids"],
    )
    assert out["episode"] == 1
    assert _audio_call(fake_client)["source_ids"] == ["src-1"]


async def test_series_does_not_supersede_a_frozen_bundle_attempt(
    fake_client, tmp_path
):
    """歸屬不能只比 settings —— frozen bundle 的三個音訊設定可以剛好與整季相同。

    `_regeneration_entry_point()` 看的是 `input_bundle`(有 bundle 就一定是
    `podcast_episode` 的),但 settings 逐字等值那條捷徑排在它前面,於是一顆
    「不指名 source_ids、language/format/length 全用預設」的 frozen attempt 會被判成
    series 自己的,supersede 掉、改讀整本筆記本 —— 與 source_ids 那條同型的內容錯置,
    而且更難發現:兩邊的 settings **看起來一模一樣**。
    """
    fake_client.sources.seed("EP01 題目")
    workspace = tmp_path / "workspace"
    output_dir = workspace / "output"
    manifest_path = output_dir / "series_manifest.json"
    output_dir.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)

    fake_client.artifacts.fail_complete = True
    with pytest.raises(p.TerminalGenerationError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(output_dir),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )
    fake_client.artifacts.fail_complete = False

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "attempts"
    ][0]
    assert attempt["input_bundle"] is not None
    # 前提:settings 與整季逐字相同 —— 這正是捷徑會誤判的形狀。
    assert set(attempt["settings"]) == {"language", "audio_format", "audio_length"}
    before = sum(1 for c in fake_client.artifacts.calls if c[0] == "generate_audio")

    # brief 也要逐字等於 bundle 的 runtime brief,否則會先被 brief-hash 守門擋掉,
    # 測不到歸屬判斷這一層。
    with pytest.raises(ValueError, match="podcast_episode"):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "exact frozen brief\n"}],
            output_dir=str(output_dir),
        )

    assert (
        sum(1 for c in fake_client.artifacts.calls if c[0] == "generate_audio")
        == before
    ), "series 又送出了一次不帶 frozen bundle 的生成"
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(final["episodes"][0]["attempts"]) == 1


async def test_a_drifted_argument_is_not_told_to_re_call_series(fake_client, tmp_path):
    """參數漂移**不是**交棒 —— 指引不准把人送回剛剛拒絕它的那支工具。

    這一格的 attempt 形狀是 series 自己的(只有 language 對不上),所以
    `_attempt_capabilities()` 會算出 `can_resend` → `regeneration_entry` =
    `podcast_series`。若接手判準只問「settings 一不一樣」就把 caps 的話貼上來,
    訊息會變成「原樣重呼 podcast_series」——而呼叫端正是被 podcast_series 拒絕的。
    自我迴圈,又一次「指引在它自己產生的狀態下不可執行」。

    真正的出路是改回原本的參數(或 retract),那不是 attempt 狀態問題,所以
    `_series_handoff_caps()` 在這一格必須回 `None`,由守門講固定的那句。
    """
    fake_client.sources.seed("EP01 題目")
    fake_client.artifacts.fail_complete = True
    await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
        language="en",
    )
    fake_client.artifacts.fail_complete = False

    with pytest.raises(ValueError) as caught:
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),
            language="ja",
        )
    message = str(caught.value)
    # 不准只說「重呼 podcast_series」——那是剛剛拒絕它的那支工具、那組參數。
    # 要講的是「用**原本那組參數**」或「先 retract」,兩條都是真的走得通的路。
    assert "原本那組參數" in message, message
    assert "podcast_attempt_retract" in message, message
    assert "自動 supersede" not in message, f"教人原樣撞回同一道牆:{message}"


async def test_series_still_supersedes_its_own_failed_attempt(fake_client, tmp_path):
    """反向護欄:不指名來源的 attempt 仍要能自動 supersede,別把守門開太大。"""
    fake_client.sources.seed("EP01 題目")
    fake_client.artifacts.fail_complete = True
    result = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert result["complete"] is False
    fake_client.artifacts.fail_complete = False

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert out["complete"] is True
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    attempts = stored["episodes"][0]["attempts"]
    assert len(attempts) == 2, "series 自己的 attempt 沒有被 supersede"
    assert attempts[1]["supersedes_attempt_id"] == attempts[0]["attempt_id"]


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


# ---------------------------------------------------------------------------
# 不指名來源時的筆數守門。已實測的邊界(同一節目、同一 brief):11–15 筆 → 6 項教錯
# + 4 項捏造;6 筆 → 0;量到的分界是「≤9 乾淨、≥11 出事」。而錯法是**模型從 context
# 撈前面集數講過的內容填進講不出來的位置**——聽起來很順、證據全部找得到出處,
# 從任何成功訊號(音檔存在、sha 相符、時長正常)都看不出來。所以這裡 fail-closed。


def _seed_sources(fake_client, n: int) -> None:
    fake_client.sources.seed(*[f"EP{i:02d} 題目" for i in range(1, n + 1)])


async def test_episode_refuses_to_generate_when_the_notebook_holds_too_many_sources(
    fake_client, tmp_path
):
    """單集入口不指名 + 超標筆記本 = 保證產出假內容,那個組合必須呼叫不到,
    不能靠 host 讀完 skill 才避開。

    真實事故(2026-08,別的 host 跑 14 集):13 集裡 9 集內容錯置,工具全程 `ok=true`。
    只有對逐字稿提問該篇獨有的事實才驗得出來。

    `podcast_episode` **外拋**——它沒有 series 那種「回傳值表達安全停止」的契約,
    整支呼叫只做一集,拋出去就是完整的答案(對照
    `test_series_stops_structurally_instead_of_throwing_away_finished_episodes`)。
    """
    _seed_sources(fake_client, 10)

    with pytest.raises(ValueError, match="source_ids") as excinfo:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(tmp_path / "series_manifest.json"),
        )

    assert "10" in str(excinfo.value), "要說出實際筆數,否則 host 不知道差多少"
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)
    # 零副作用:連 attempt 都不該建 —— 留一筆沒 dispatch 的 attempt 會讓 host 的
    # `attempt_count` 一直長,而 skill 教它把那個當成「配額原地打轉」。
    assert not (tmp_path / "series_manifest.json").exists()


async def test_series_stops_structurally_instead_of_throwing_away_finished_episodes(
    fake_client, tmp_path
):
    """**守門在 series 裡是結構化安全停點,不是裸拋。**

    這是 F-4 修過的同一個形狀:`podcast_series` 對呼叫端的契約是「預期內的停止用
    回傳值表達」,裸拋會把**前面幾集已經跑完的 `run_results` 整份丟掉** —— 呼叫端
    看不到自己跑到哪、也拿不到 `safe_next_action`,重呼只會再拋一次。

    而且這裡的觸發時序是**常態而非邊角**:逐集加原文時 EP_n 開始前有 `2n-1` 筆
    (n 集原文 + n-1 集回錄),`2n-1 >= 10` 解出 n=6 —— 也就是 skill 說的「EP06 起
    改用單集入口」。本測試把回錄遞增壓縮成兩集來重現同一個交界。

    `safe_next_action` 必須是 `podcast_episode`:回 `podcast_series` 等於叫呼叫端
    撞回同一道牆,而 `attempt_count` 兩輪都不會變 —— 正是 `partial()` 註解說的
    「看不出自己在原地打轉」。
    """
    # 9 筆通過(實測乾淨的上緣),EP01 完成後自己的回錄讓筆記本變 10 筆 → EP02 被擋。
    _seed_sources(fake_client, 9)

    out = await p.podcast_series(
        "nb-1",
        episodes=[
            {"title": "心法篇", "brief": "第一集"},
            {"title": "實戰篇", "brief": "第二集"},
        ],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["observed_state"] == "too_many_sources"
    assert out["safe_next_action"] == "podcast_episode"
    assert len(out["episodes"]) == 1, "EP01 已經跑完,結果不可以被丟掉"
    assert out["episodes"][0]["episode"] == 1
    # 停在 EP02 之前:第二集連 attempt 都沒建,generate_audio 只發生過一次。
    assert out["attempt_id"] is None
    assert len(
        [c for c in fake_client.artifacts.calls if c[0] == "generate_audio"]
    ) == 1
    # 訊息要留在回傳值裡 —— 裸拋時指引在 exception,回 partial 就只剩這個欄位。
    assert "source_ids" in out["error"]


async def test_the_count_guard_still_wins_over_the_ownership_guard(
    fake_client, tmp_path
):
    """兩道守門的**順序**是進度保全的一部分。

    接手歸屬是純本機判斷,擺在筆數守門(要打一趟 RPC)之前看起來更划算 —— 但那會把
    這條路從結構化停點改成裸拋:外來 attempt 撞上超標筆記本時,舊行為回
    `too_many_sources` partial(EP01 仍在 `episodes` 裡),歸屬先跑則整批
    `run_results` 隨 exception 一起消失。兩道守門都不改 manifest,順序只決定「先講
    哪個理由」,那就讓保住進度的那個先講(Codex 獨立複審實跑抓到)。
    """
    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 9)
    # EP02 先留下一顆「指名來源、遠端終態」的外來 attempt。
    fake_client.artifacts.fail_complete = True
    with pytest.raises(p.TerminalGenerationError):
        await p.podcast_episode(
            "nb-1",
            episode_n=2,
            title="實戰篇",
            brief="第二集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            source_ids=["src-1"],
        )
    fake_client.artifacts.fail_complete = False

    # EP01 跑完後自己的回錄讓筆記本變 10 筆 → EP02 在歸屬之前先撞筆數守門。
    out = await p.podcast_series(
        "nb-1",
        episodes=[
            {"title": "心法篇", "brief": "第一集"},
            {"title": "實戰篇", "brief": "第二集"},
        ],
        output_dir=str(tmp_path),
    )

    assert out["observed_state"] == "too_many_sources", out["observed_state"]
    assert out["stopped_at_episode"] == 2
    assert len(out["episodes"]) == 1, "EP01 已經跑完,結果不可以被丟掉"


async def test_the_count_guard_also_covers_the_series_resend_path(
    fake_client, tmp_path
):
    """**兩條 dispatch 路徑都要守。** 全新一集走 `_run_episode`,重送/supersede 是
    `podcast_series` 自己 inline —— v0.8.0 的 failover 只補了前者,於是 pool 對「重試」
    這條最需要它的路完全無效(ADR-0010 紀律①)。同一個形狀在這裡會重演:筆記本的
    來源是**隨每集回錄遞增**的,所以「上次 dispatch 時還沒超標、這次重送時超了」
    正是那個 14 集事故的實際時序。

    這條路徑上已經有一個既有 attempt,所以停點要指認得出是**哪一個** attempt
    (`attempt_id` 非 None),而不是像全新一集那樣回 None。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 3)
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    # series 撞配額**不外拋**,回結構化安全停點——那是它對呼叫端的契約。
    blocked = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert blocked["observed_state"] == "not_accepted"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"
    existing_attempt_id = stored["episodes"][0]["active_attempt_id"]
    fake_client.artifacts.generate_audio_exc = None
    before = len([c for c in fake_client.artifacts.calls if c[0] == "generate_audio"])

    # 之後又上傳了幾集回錄,筆記本超標 —— 重送這個既有 attempt 一樣會生出假內容。
    # (`seed` 是累加,所以這裡總數是 3 + 12 = 15 筆。)
    _seed_sources(fake_client, 12)

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["observed_state"] == "too_many_sources"
    assert out["attempt_id"] == existing_attempt_id
    after = len([c for c in fake_client.artifacts.calls if c[0] == "generate_audio"])
    assert after == before, "重送路徑也不得 dispatch"

    # **擋在任何 manifest mutation 之前**:attempt 還是原本的 `not_accepted`,沒有被
    # re-arm 成 prepared,也沒有多生一顆 superseding attempt。先改狀態再拒絕送出會留下
    # 半成品,而 `attempt_count` 是呼叫端判斷「有沒有在原地打轉」的唯一依據。
    reread = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert reread["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"
    assert out["attempt_count"] == 1, "被拒絕的呼叫不該讓 attempt_count 長一格"

    # **照著 `safe_next_action` 做必須真的能離開停點。** 只斷言字串會漏掉 v0.9.1 那種
    # 「指引在它自己產生的狀態下不可執行」:這顆 attempt 的 settings 是「不指名來源」,
    # 直接改呼指名版會被 `_is_resendable_same_request` 判成不同請求而拒絕。
    assert out["safe_next_action"] == "podcast_attempt_retract"
    # FINDING-2:retract 不打 RPC,看不到筆記本已超標,它的回傳會說 `podcast_series`
    # —— 照做會再撞一次這道守門(會終止,但白跑一趟)。**擋下它的是我們,我們知道**,
    # 所以這句警告寫在這裡最便宜。
    assert "不要" in out["error"] and "podcast_series" in out["error"], out["error"]
    with pytest.raises(ValueError, match="already has durable active attempt"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            source_ids=["src-1", "src-2", "src-3"],
        )

    # **執行 `safe_next_action` 只用公開回傳裡的 `attempt_id`**(docs/gotchas-attempt.md
    # 「回傳要自足」那條紅線)——上面 433 行已經驗過 `out["attempt_id"] ==
    # existing_attempt_id`,這裡改用 `out["attempt_id"]` 驅動,不再從 manifest 私下讀。
    retracted = await p.podcast_attempt_retract(
        str(manifest_path),
        episode_n=1,
        attempt_id=out["attempt_id"],
        reason="筆記本來源已超標,改用指名版重生",
    )
    # 它從未 dispatch,所以沒有回錄 source 要清 —— 清理義務不會擋住下一步。
    assert retracted["stale_source_ids"] == []

    revived = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=["src-1", "src-2", "src-3"],
    )
    assert revived["artifact_id"], "retract 之後指名版必須跑得起來,否則停點是死路"
    # 取**最後**一次 dispatch:第一次是上面被配額拒絕的那顆(source_ids=None)。
    resent = [c[1] for c in fake_client.artifacts.calls if c[0] == "generate_audio"][-1]
    assert resent["source_ids"] == ["src-1", "src-2", "src-3"]


async def test_naming_the_sources_lifts_the_count_guard(fake_client, tmp_path):
    """指名少少幾筆就放行 —— 守門防的是「帶太多筆進 context」,不是「筆記本很大」。

    這是唯一的出路,所以它必須真的走得通:skill 教的 EP06+ 流程就是筆記本很大、
    但只挑 6 筆(本集原文 + 最近 5 集回錄)。
    """
    _seed_sources(fake_client, 15)

    out = await p.podcast_episode(
        "nb-1",
        episode_n=6,
        title="心法篇",
        brief="第六集",
        output_dir=str(tmp_path),
        manifest_path=str(tmp_path / "series_manifest.json"),
        source_ids=["src-1", "src-2", "src-3", "src-4", "src-5", "src-6"],
    )

    assert out["artifact_id"]
    assert _audio_call(fake_client)["source_ids"] == [
        "src-1", "src-2", "src-3", "src-4", "src-5", "src-6",
    ]


async def test_naming_too_many_sources_is_refused_the_same_way(fake_client, tmp_path):
    """**實測的變因是「帶進去幾筆」,不是「有沒有指名」。** 指名 10 筆與不指名 10 筆
    對模型是同一件事(實測那一列寫的就是「11–15 筆(全部前集)→ 6 教錯 + 4 捏造」),
    所以把守門掛在「有沒有指名」這個 proxy 上會漏掉真正的變因。

    現實觸發:host 讀了 skill 知道要指名,卻把「本集 + 最近 5 集」做成「本集 +
    全部前集」,或重生時多帶幾筆 —— 守門全程沉默,而事故照樣發生。
    """
    _seed_sources(fake_client, 15)

    with pytest.raises(ValueError, match="10") as excinfo:
        await p.podcast_episode(
            "nb-1",
            episode_n=6,
            title="心法篇",
            brief="第六集",
            output_dir=str(tmp_path),
            manifest_path=str(tmp_path / "series_manifest.json"),
            source_ids=[f"src-{i}" for i in range(1, 11)],
        )

    assert "最近 5 集" in str(excinfo.value), "指名版的指引要教它砍到幾筆,不是叫它去指名"
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)
    assert not (tmp_path / "series_manifest.json").exists()
    # 筆數是純本地判斷,擺在對帳之前:指名一堆 id 卻超標時,連 `assert_sources_exist`
    # 那趟 `sources.list` 都不必打就秒退。
    assert not any(c[0] == "list" for c in fake_client.sources.calls)


async def test_the_low_level_entry_point_is_guarded_too(fake_client):
    """**低階 `generate_audio` 是公開 MCP 工具,失效模式與高階一模一樣。**

    守門只掛在 podcast 家族的話,它就是繞過去的公開後門 —— host 手動組裝或救援時
    正好會用到它,而那時筆記本通常已經堆滿前面各集的回錄。
    """
    _seed_sources(fake_client, 12)

    with pytest.raises(ValueError, match="10"):
        await b.generate_audio("nb-1")

    assert not fake_client.artifacts.calls, "守門在打 RPC 之前"


async def test_a_pending_prior_upload_counts_toward_the_limit(fake_client, tmp_path):
    """`prior_mp3_path` 會在守門**之後**、生成之前再上傳一筆(standalone 續集路徑)。

    這是同一個函式內的確定性順序,不是競態:不把它算進去的話,守門看到 9 筆放行、
    上傳完以 10 筆送出 —— 剛好落在拒絕線上,守門等於沒守。
    """
    _seed_sources(fake_client, 9)

    with pytest.raises(ValueError, match="10"):
        await p.podcast_episode(
            "nb-1",
            episode_n=2,
            title="心法篇",
            brief="第二集",
            output_dir=str(tmp_path),
            prior_mp3_path=str(tmp_path / "ep01.mp3"),
        )

    assert not any(c[0] == "add_file" for c in fake_client.sources.calls), (
        "連那筆上傳都不該發生 —— 守門要在所有副作用之前"
    )
    assert not fake_client.artifacts.calls


async def test_prior_mp3_path_and_named_source_ids_are_mutually_exclusive(
    fake_client, tmp_path
):
    """P2:`prior_mp3_path` 會把上一集的回錄真的上傳進筆記本、改名,但 dispatch 只帶
    呼叫端指名的 `source_ids`——那筆新上傳的 source 沒有任何 attempt 引用它,
    `ok=true` 回傳,那筆變成無人認領的孤兒,吃掉 ≤9 上限裡的一格卻沒人記得。

    修法是 fail-closed:兩者同時非空就直接 raise,不讓这個組合有機會發生副作用。
    """
    _seed_sources(fake_client, 1)

    with pytest.raises(
        ValueError,
        match="指名 `source_ids` 時把上一集的回錄 source id 直接放進 `source_ids`",
    ):
        await p.podcast_episode(
            "nb-1",
            episode_n=2,
            title="心法篇",
            brief="第二集",
            output_dir=str(tmp_path),
            prior_mp3_path=str(tmp_path / "ep01.mp3"),
            source_ids=["src-1"],
        )

    assert not any(c[0] == "add_file" for c in fake_client.sources.calls)
    assert not fake_client.artifacts.calls


async def test_a_preflight_permission_error_still_becomes_a_structured_stop(
    fake_client, tmp_path, monkeypatch
):
    """守門的 `sources.list` 現在是整條路徑上**第一個**遠端呼叫,而權限分類原本只長在
    dispatch helper 裡(`_dispatch_audio_with_failover`)。

    不在 preflight 轉的話:pool 剛 rotate 到看不到 notebook 的帳號時,`podcast_series`
    會裸拋上游的 `ClientError` —— 前面幾集跑完的 `run_results` 一起丟掉,而且拿不到
    「去 `notebook_share_with_pool`」那條指引。v0.9.0/v0.9.1 花了兩輪才把那條路做成
    「結構化停點 + 可執行的指引」,preflight 前移不可以把它繞掉。
    """
    from notebooklm.exceptions import ClientError

    async def denied(_notebook_id):
        raise ClientError("permission denied", rpc_code=7)

    monkeypatch.setattr(fake_client.sources, "list", denied)

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["observed_state"] == "notebook_access_denied"
    assert "notebook_share_with_pool" in out["error"]
    assert not fake_client.artifacts.calls


async def test_nine_sources_still_pass_so_early_episodes_keep_working(
    fake_client, tmp_path
):
    """邊界要落在實測值上。9 筆是量到「乾淨」的上緣 —— 擋在這裡以下會把 EP01–05
    的正常整季流程一起關掉,而那條路徑實測 0 教錯 0 捏造。
    """
    _seed_sources(fake_client, 9)

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is True
    assert _audio_call(fake_client)["source_ids"] is None


async def test_the_guard_does_not_block_an_attempt_that_only_needs_finalizing(
    fake_client, tmp_path
):
    """**守門不可以擋「已 dispatch、只等 finalize」的那條。**

    那顆 attempt 不會再生成一次 —— 遠端的 artifact 是用 dispatch 當下的來源集合做的,
    此刻筆記本有幾筆與它無關。擋它不會避免任何假內容,只會把一集永久卡在半路:
    `podcast_series` 是它唯一的續跑者,而停點會叫呼叫端去 retract 一個**已經在燒配額**
    的生成。

    位置敏感度就在這裡:守門的條件是
    `dispatch_state in ("prepared", "not_accepted") or remote_state in ("failed", "removed")`,
    少寫那個條件、改成無條件擋,這條路就斷了。v0.9.3 真實驗收的情境 C 量的就是這個。
    """
    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 3)
    # dispatch 成功、finalize 前斷線 —— MCP 最常見的失敗形狀(外層 timeout 砍 request)。
    fake_client.artifacts.fail_wait_on = 1
    fake_client.artifacts.wait_exc = TimeoutError("client 被砍掉了")
    interrupted = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert interrupted["complete"] is False
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted", attempt["dispatch"]
    assert stored["episodes"][0].get("output_attempt_id") is None
    dispatched = len(
        [c for c in fake_client.artifacts.calls if c[0] == "generate_audio"]
    )

    # 中斷期間又上傳了幾集回錄,筆記本現在遠遠超標(3 + 12 = 15 筆)。
    fake_client.artifacts.wait_exc = None
    fake_client.artifacts.fail_wait_on = None
    _seed_sources(fake_client, 12)

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is True, "只等 finalize 的 attempt 不該被筆數守門攔下"
    assert out["episodes"][0]["episode"] == 1
    after = len([c for c in fake_client.artifacts.calls if c[0] == "generate_audio"])
    assert after == dispatched, "續跑只做 finalize,不得再 dispatch 一次"


async def test_the_stop_for_a_terminal_remote_attempt_is_actually_walkable(
    fake_client, tmp_path
):
    """**守門的指引在 `failed`/`removed` 那條路徑上必須真的走得通。**

    守門條件涵蓋 `remote_state in ("failed", "removed")` —— 而那時 `dispatch.status`
    是 `accepted`。v0.9.4 寫的訊息卻**無條件**說「它從未 dispatch,不需要旗標」,
    照著做 retract 會被拒:又一個「指引在它自己產生的狀態下不可執行」。

    修法是把「遠端已經給出終態」也算成 manifest 自己就知道結果 —— `failed`/`removed`
    沒有「還在飛」的可能,不存在需要外部知識的 in-flight 狀態。
    """
    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 3)
    fake_client.artifacts.fail_complete = True       # 遠端回終態失敗
    first = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "第一集"}], output_dir=str(tmp_path)
    )
    assert first["complete"] is False
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["remote"]["status"] in ("failed", "removed"), attempt["remote"]
    assert attempt["dispatch"]["status"] == "accepted", attempt["dispatch"]

    fake_client.artifacts.fail_complete = False
    _seed_sources(fake_client, 12)          # 筆記本超標

    stop = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "第一集"}], output_dir=str(tmp_path)
    )
    assert stop["observed_state"] == "too_many_sources"

    # **照著 safe_next_action 做必須走得通** —— 這是整條 finding 的重點。
    if stop["safe_next_action"] == "podcast_attempt_retract":
        await p.podcast_attempt_retract(
            str(manifest_path), 1, stop["attempt_id"], reason="來源超標,改指名版"
        )
    else:
        raise AssertionError(f"未預期的停點: {stop['safe_next_action']}")


async def test_the_permission_stop_points_at_the_tool_that_fixes_it(
    fake_client, tmp_path, monkeypatch
):
    """**`safe_next_action` 是決策樹,不能指向必然重複失敗的工具。**

    權限停點的 `error` 說要跑 `notebook_share_with_pool`,而 `safe_next_action` 回的是
    `podcast_series` —— skill 明說拿不準就直接照 `safe_next_action` 做,於是自動化只讀
    那個欄位就會原地重試同一個沒權限的帳號。

    舊註解的理由是「不想為此新增白名單字面值」,但白名單的意義是「一定是真的 MCP
    工具名」,而 `notebook_share_with_pool` 正是 —— v0.9.3 已經為同樣的理由加過
    `podcast_episode` 與 `podcast_attempt_retract`,那個理由自己被推翻了。
    """
    from notebooklm.exceptions import ClientError

    async def denied(_notebook_id):
        raise ClientError("permission denied", rpc_code=7)

    monkeypatch.setattr(fake_client.sources, "list", denied)

    out = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "第一集"}], output_dir=str(tmp_path)
    )

    assert out["observed_state"] == "notebook_access_denied"
    assert out["safe_next_action"] == "notebook_share_with_pool"
    assert "notebook_share_with_pool" in out["error"]


async def test_the_zero_candidate_reconcile_stop_carries_a_way_out(
    fake_client, tmp_path
):
    """**`acceptance_unknown` 零候選、候選窗還開著:出路要寫在回傳裡。**

    v0.9.6 驗收 FINDING-1:這條路的 `safe_next_action` 曾經指回本工具自己,連呼兩次的
    回傳**逐欄位相同** —— 照著做就是無限迴圈。而「原地打轉」偵測依據的
    `attempt_count` / `superseded_attempt_count` 在這裡恆為 1 / 0(reconcile 不建
    attempt),**偵測條件從不成立**。

    F3(主迴圈裁決,採納審查者的反駁):零候選有兩種成因,這裡測的是「候選窗還沒
    關」那一種(dispatch 剛發生,離 `wait_timeout` 還很遠)——那次生成可能還沒出現,
    晚幾分鐘再對帳就撈得到,`safe_next_action` 因此停在 `podcast_episode_reconcile`
    而不是 `podcast_attempt_retract`(那會把還在飛的因果紀錄提前寫成墓碑,ADR-0009
    禁止)。「候選窗已經關」那一種見
    `test_reconcile_with_no_candidate_past_the_window_offers_retract`
    (tests/test_audio_attempts.py)。
    """
    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 3)
    # 送出之後失去 response,而遠端**零候選**(dispatch 其實沒被受理)。
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title="心法篇", brief="第一集",
            output_dir=str(tmp_path), manifest_path=str(manifest_path),
        )
    fake_client.artifacts.generate_audio_exc = None
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown", attempt["dispatch"]

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt["attempt_id"]
    )

    assert out["candidate_artifact_ids"] == []
    assert "next_step" in out, "零候選停點沒有出路欄位 —— 呼叫端只能無限重呼"
    step = out["next_step"]
    assert "artifact_list" in step, step
    # 這顆 attempt 是全新單集、沒有 output 接手,`needs_abandon_flag` 必為 True——
    # 訊息要跟著同一顆 caps 走(`_retract_hint`),不能寫死另一個分支的措辭。
    assert "abandon_in_flight=true" in step, step
    assert "不需要" not in step, step
    # 候選窗還沒關:不准說「繼續對帳沒有用」,那句只在窗關了之後才是真的
    # (見窗外分支的鑑別測試)。
    assert "沒有用" not in step, step
    assert "窗" in step and "還沒關" in step, step
    assert out["safe_next_action"] == p.ACTION_RECONCILE, out


async def test_series_does_not_drop_the_reconcile_way_out_when_repacking_the_stop(
    fake_client, tmp_path
):
    """**P2(Codex 獨立審查抓到的):`podcast_series` 重包 partial 時漏轉 `next_step`。**

    `podcast_episode_reconcile` 零候選時回傳帶 `next_step`——那是逃離 self-loop 的
    唯一結構化指引(見上一條測試)。但 `podcast_series` 內部在它自己重呼
    `podcast_episode_reconcile` 之後重包成結構化停點時,只轉傳了
    `candidate_artifact_ids`,把 `next_step` 丟掉——`partial(**extra)` 本來就吃
    額外欄位,純漏傳。這是 AGENTS.md 點名的「series 有兩條路徑」老病的又一次現形:
    直接呼叫 reconcile 的呼叫端看得到 next_step,經由 series 重包的這條路卻看不到。
    """
    manifest_path = tmp_path / "series_manifest.json"
    _seed_sources(fake_client, 3)
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title="心法篇", brief="第一集",
            output_dir=str(tmp_path), manifest_path=str(manifest_path),
        )
    fake_client.artifacts.generate_audio_exc = None
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "acceptance_unknown"

    # 原樣重呼整季(series 的續跑動作),接手同一顆 acceptance_unknown 的 attempt。
    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
        start=1,
    )

    # 候選窗還沒關(dispatch 剛發生),`podcast_episode_reconcile` 自己回的是
    # `ACTION_RECONCILE`(見上一條測試);這裡驗的是 series 重包時**原樣帶出**
    # 那個值,不是漏傳或改寫成別的。
    assert out["safe_next_action"] == p.ACTION_RECONCILE
    assert "next_step" in out, "series 重包 partial 時把 next_step 漏傳了 —— 出路蒸發"
    assert "artifact_list" in out["next_step"], out
