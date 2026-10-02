import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeClient

from notebooklm_mcp import runtime, tools_artifacts as a


def _manifest(tmp_path, episodes):
    p = tmp_path / "series_manifest.json"
    p.write_text(
        json.dumps({"notebook_id": "nb-1", "episodes": episodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(p)


async def test_generate_slides_downloads_and_writes_manifest(fake_client, tmp_path):
    fake_client.sources.seed("EP01 題目")
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_slides("nb-1", m, 1, source_ids=["src-1"], slide_format="detailed")

    # 呼叫了對的 SDK 方法,帶對的 enum
    from notebooklm.types import SlideDeckFormat

    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck")
    assert gen["source_ids"] == ["src-1"]
    assert gen["slide_format"] == SlideDeckFormat.DETAILED_DECK
    assert gen["language"] == "zh_Hant"  # resolve_language 預設

    # 下載到 manifest 同目錄的預期檔名
    assert res["slides_pdf_path"].endswith("ep01-slides.pdf")
    # 路徑回寫進 manifest
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]
    assert data["schema_version"] == 2
    # 一次成功生成 = **一次** manifest 寫入:路徑與 provenance 同一次 `update`。
    # (v0.9.16 中途試過「受理時先落憑據」,那讓失敗的生成留下「檔案舊、manifest 新」的
    #  錯配,已退掉 —— provenance 的語意是「現在磁碟上這份是誰生的」。)
    assert data["revision"] == 1
    assert data["episodes"][0]["slides_artifact_id"] == res["artifact_id"]
    assert data["episodes"][0]["slides_account"] == "#1"


async def test_generate_slides_unknown_episode_errors(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_slides("nb-1", m, 2)


async def test_generate_report_downloads_md_and_writes_manifest(fake_client, tmp_path):
    fake_client.sources.seed("EP01 題目")
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_report("nb-1", m, 1, report_format="study_guide", source_ids=["src-1"])

    from notebooklm.types import ReportFormat

    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_report")
    assert gen["report_format"] == ReportFormat.STUDY_GUIDE
    assert gen["language"] == "zh_Hant"

    assert res["report_md_path"].endswith("ep01-report.md")
    assert res["report_format"] == "study_guide"
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["report_md_path"] == res["report_md_path"]
    assert data["episodes"][0]["report_format"] == "study_guide"


@pytest.mark.parametrize("kind", ["slides", "report"])
async def test_generation_pins_client_through_download(fake_client, tmp_path, kind):
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    method_name = "generate_slide_deck" if kind == "slides" else "generate_report"
    original = getattr(fake_client.artifacts, method_name)

    async def generate_then_rotate(*args, **kwargs):
        status = await original(*args, **kwargs)
        runtime.rotate_client()
        return status

    setattr(fake_client.artifacts, method_name, generate_then_rotate)

    if kind == "slides":
        await a.generate_slides("nb-1", m, 1)
    else:
        await a.generate_report("nb-1", m, 1)

    assert other.artifacts.calls == []


# ---- custom report:ReportFormat.CUSTOM + custom_prompt --------------------------
# SDK 的 _report_config 對 CUSTOM 是 `custom_prompt or "Create a report based on…"`,
# 且 CUSTOM 分支**直接忽略** extra_instructions。兩種誤用都會靜默生出一份不是你要的
# 講義(燒一次配額才發現),所以三道 guard 全在本地 fail loud。


async def test_generate_report_custom_passes_prompt_to_sdk(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_report(
        "nb-1", m, 1, report_format="custom", custom_prompt="用時間軸列出每個 claim 的第一方出處"
    )

    from notebooklm.types import ReportFormat

    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_report")
    assert gen["report_format"] == ReportFormat.CUSTOM
    assert gen["custom_prompt"] == "用時間軸列出每個 claim 的第一方出處"
    assert res["report_format"] == "custom"


async def test_generate_report_custom_requires_a_prompt(fake_client, tmp_path):
    """沒有 prompt 的 custom 會靜默套用 SDK 的通用預設句,等於白燒一次配額。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    for bad in (None, "", "   "):
        with pytest.raises(ValueError, match="custom_prompt"):
            await a.generate_report("nb-1", m, 1, report_format="custom", custom_prompt=bad)
    assert not fake_client.artifacts.calls


async def test_custom_prompt_rejected_for_static_formats(fake_client, tmp_path):
    """非 CUSTOM 格式的 custom_prompt 會被 SDK 丟掉(套靜態 config),不能靜默通過。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="custom_prompt"):
        await a.generate_report("nb-1", m, 1, report_format="study_guide", custom_prompt="x")
    assert not fake_client.artifacts.calls


async def test_custom_format_rejects_extra_instructions(fake_client, tmp_path):
    """payloads.py:219 對 CUSTOM 不串接 extra_instructions —— 傳了會靜默消失。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="extra_instructions"):
        await a.generate_report(
            "nb-1", m, 1, report_format="custom", custom_prompt="p", extra_instructions="q"
        )
    assert not fake_client.artifacts.calls


# ---- 救援下載:client 端 timeout 丟掉結果時,別重生一次燒配額 ----------------------


async def test_artifact_download_slides_downloads_without_generating(fake_client, tmp_path):
    """雲端已生好、artifact_id 是呼叫端自己找回來的 → 只走下載 + 回寫,不碰生成。"""
    m = _manifest(tmp_path, [{"episode": 7, "title": "EP07"}])
    res = await a.artifact_download_slides("nb-1", m, 7, "slide-rescued")

    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck"]
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_slide_deck")
    assert dl["artifact_id"] == "slide-rescued"
    assert res["slides_pdf_path"].endswith("ep07-slides.pdf")
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]


async def test_artifact_download_report_downloads_without_generating(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 7, "title": "EP07"}])
    res = await a.artifact_download_report("nb-1", m, 7, "report-rescued")

    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_report"]
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_report")
    assert dl["artifact_id"] == "report-rescued"
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["report_md_path"] == res["report_md_path"]
    assert data["episodes"][0]["report_format"] == "study_guide"


async def test_artifact_download_rescue_requires_an_explicit_artifact_id(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 7, "title": "EP07"}])
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="artifact_id"):
            await a.artifact_download_slides("nb-1", m, 7, bad)
        with pytest.raises(ValueError, match="artifact_id"):
            await a.artifact_download_report("nb-1", m, 7, bad)
    assert not fake_client.artifacts.calls


async def test_artifact_download_slides_fails_closed_on_a_removed_artifact(fake_client, tmp_path):
    """配額下架(status="removed"、is_failed=False)不得被當成救援成功。"""
    m = _manifest(tmp_path, [{"episode": 7, "title": "EP07"}])
    fake_client.artifacts.fail_removed = True
    with pytest.raises(RuntimeError, match="removed"):
        await a.artifact_download_slides("nb-1", m, 7, "slide-rescued")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "download_slide_deck"]


# ---- 改一張投影片,不重生整份 ------------------------------------------------------


async def test_revise_slide_revises_then_redownloads_without_regenerating(fake_client, tmp_path):
    """就地改第 3 張 → 重新下載同一份 deck 並回寫 manifest,完全不碰 generate。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    res = await a.artifact_revise_slide(
        "nb-1", m, 5, "deck-1", 2, "把這頁的數字改成 2026-05 的版本"
    )

    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck"]
    rev = next(c[1] for c in fake_client.artifacts.calls if c[0] == "revise_slide")
    assert rev == {
        "notebook_id": "nb-1",
        "artifact_id": "deck-1",
        "slide_index": 2,
        "prompt": "把這頁的數字改成 2026-05 的版本",
    }
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_slide_deck")
    assert dl["artifact_id"] == "deck-1"
    assert res["slides_pdf_path"].endswith("ep05-slides.pdf")
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]


async def test_revise_slide_pins_client_from_preflight_through_download(fake_client, tmp_path):
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    original = fake_client.artifacts.get_or_none

    async def get_then_rotate(*args, **kwargs):
        artifact = await original(*args, **kwargs)
        runtime.rotate_client()
        return artifact

    fake_client.artifacts.get_or_none = get_then_rotate

    await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")

    assert other.artifacts.calls == []


async def test_revise_slide_follows_the_returned_id_not_the_input(fake_client, tmp_path):
    """REVISE_SLIDE **不是就地改版** —— v0.9.0 真實驗收實測:伺服器會 fork 出一顆
    `<原標題> (2)`,傳入那顆原封不動留在遠端。實作一律用回傳的 id,所以行為本來就對
    (當初若照舊 docstring 寫死用輸入 id,下載到的會是**沒改過的舊那份**、而且看起來
    完全成功);這條測試連同回報面一起鎖住,讓呼叫端知道 id 換了、舊的還在。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    fake_client.artifacts.revise_slide_returns_id = "deck-2"
    res = await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")

    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_slide_deck")
    assert dl["artifact_id"] == "deck-2"  # 下載改版後那份,不是原 id
    assert res["artifact_id"] == "deck-2"
    # 舊那顆仍在遠端,呼叫端要拿得到它才有辦法自己決定清不清。
    assert res["superseded_artifact_id"] == "deck-1"


async def test_revise_slide_omits_superseded_id_when_the_artifact_really_is_reused(
    fake_client, tmp_path
):
    """id 沒換的時候不要無中生有一個 `superseded_artifact_id`。

    上游哪天真的改成就地改版,這個欄位就該消失而不是指著自己 —— 呼叫端若照它去刪,
    刪掉的正是剛改好的那一顆。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    res = await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")

    assert res["artifact_id"] == "deck-1"
    assert "superseded_artifact_id" not in res


async def test_revise_slide_requires_artifact_id_and_prompt(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="artifact_id"):
            await a.artifact_revise_slide("nb-1", m, 5, bad, 0, "改這頁")
        with pytest.raises(ValueError, match="prompt"):
            await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, bad)
    assert not [c for c in fake_client.artifacts.calls if c[0] == "revise_slide"]


async def test_revise_slide_fails_closed_on_removed_deck(fake_client, tmp_path):
    """配額下架的 deck 不得被當成改版成功(改完還會覆寫本機那份完整 PDF)。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    fake_client.artifacts.fail_removed = True
    with pytest.raises(RuntimeError, match="removed"):
        await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "download_slide_deck"]


# ---- v0.2.9 token-diet:P5 episode_set_description --------------------------------


async def test_episode_set_description_writes_and_strips(tmp_path):
    """回寫走 MCP server 同 process 的讀改寫;預設清引用標記(新工具,無相容包袱)。"""
    m = _manifest(tmp_path, [{"episode": 21, "title": "EP21 標題"}])
    res = await a.episode_set_description(m, 21, "重點整理 [1],結論 [3, 4]。")
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    # 標記清掉,**標點前不留空格**(v0.9.13 驗收:show notes 是公開文案,那一格看得到)
    assert data["episodes"][0]["description"] == "重點整理,結論。"
    assert "[" not in data["episodes"][0]["description"]
    assert res["episode"] == 21 and res["description"] == data["episodes"][0]["description"]


async def test_episode_set_description_keep_citations(tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    await a.episode_set_description(m, 1, "重點 [1]。", strip_citations=False)
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["description"] == "重點 [1]。"


async def test_episode_set_description_validates(tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError):  # 空(清完標記後也算)
        await a.episode_set_description(m, 1, "  [1]  ")
    with pytest.raises(ValueError):  # 等於標題 = 假 show notes
        await a.episode_set_description(m, 1, "EP01")
    with pytest.raises(ValueError, match="episode 9 not found"):
        await a.episode_set_description(m, 9, "真 show notes")


async def test_episode_set_description_rejects_unsafe_notes_at_write_time(tmp_path):
    """夾帶外部資源的 show notes 不能安穩寫進 manifest、等到整季生完跑 publish 才爆。
    docstring 宣告「錯誤在寫入當下就爆」,就要真的跑 publish 端那顆 guard。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    for bad in (
        "本集重點\n\n![追蹤像素](https://evil.example/pixel.png)",
        "本集重點\n\n[點我](javascript:alert(1))",
    ):
        with pytest.raises(ValueError, match="自包含"):
            await a.episode_set_description(m, 1, bad)
    # 正常的中文技術散文不可誤殺(guard 誤判的代價是整季發不出去)
    ok = await a.episode_set_description(
        m, 1, "• 我們談 JavaScript:動態語言的起點\n• 設定 online=1"
    )
    assert "JavaScript" in ok["description"]


async def test_episode_set_description_rejects_xml_forbidden_chars_at_write_time(tmp_path):
    """U+000C 這類 XML 1.0 表達不了的字元,`render_episode_notes_html` 的標籤允許清單
    掃不到(它只管 HTML 標籤/屬性,不管純文字字元)——會安穩寫進 manifest,直到整季
    生完、跑 `publish_series` 組 XML 那一刻才 fail-closed。docstring 說「錯誤在寫入
    當下就爆」,這裡補上同一顆 `feed.validate_xml_text` 讓它真的在寫入當下就爆。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="XML"):
        await a.episode_set_description(m, 1, "本集重點\x0c續完")
    # 沒有半途寫入
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert "description" not in data["episodes"][0]


# ---- v0.9.18:episode_set_publication_state(deferred 的 set/clear lifecycle）------
# 這支的存在理由是 lifecycle:publish_series 讀 publication_state,但在這一版之前沒有任何
# 工具寫得動它,而 manifest 只由工具寫入的紀律不允許 host 手改 JSON —— 於是「解除」在受
# 支持的路徑上是死路。


def _revision_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8")).get("revision")


async def test_set_publication_state_writes_all_three_audit_fields(tmp_path):
    """扣下一集要連稽核脈絡一起落地 —— 只有狀態、沒有理由/時間,事後查不動。

    回傳**整份比對**:逐欄 assert 會漏掉「某個欄位在這條路徑上說謊」那一類突變
    (`episode` 回 0、`reason` 回 None 都能全綠)。"""
    m = _manifest(tmp_path, [{"episode": 46, "title": "EP46"}])
    res = await a.episode_set_publication_state(m, 46, "deferred", reason="  五次 QA 拒收  ")
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert ep["publication_state"] == "deferred"
    assert ep["publication_state_reason"] == "五次 QA 拒收"  # strip 過
    # 真的解析得動、真的是 UTC —— 只檢查 endswith("+00:00") 對 "garbage+00:00" 也會綠
    stamped = datetime.fromisoformat(ep["publication_state_at"])
    assert stamped.utcoffset() == timedelta(0)
    assert res == {
        "episode": 46,
        "publication_state": "deferred",
        "previous_state": None,
        "reason": "五次 QA 拒收",
        "changed": True,
        "withheld_from_publish": True,
    }


async def test_clearing_publication_state_removes_the_whole_audit_triple(tmp_path):
    """解除要把三個欄位一起移除。留下 reason/at 會讓下一個讀 manifest 的人以為還扣著。"""
    m = _manifest(tmp_path, [{"episode": 46, "title": "EP46"}])
    await a.episode_set_publication_state(m, 46, "deferred", reason="五次 QA 拒收")
    res = await a.episode_set_publication_state(m, 46, None)
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert "publication_state" not in ep
    assert "publication_state_reason" not in ep
    assert "publication_state_at" not in ep
    assert res == {
        "episode": 46,
        "publication_state": None,
        "previous_state": "deferred",
        "reason": None,
        "changed": True,
        "withheld_from_publish": False,
    }


async def test_replaying_the_same_call_does_not_bump_revision(tmp_path):
    """冪等重放不是錯誤,但**不准平白 +1 revision** —— 那會撞掉別的 writer 的 CAS。

    `ManifestStore.update` 的 revision 是無條件 +1,所以「沒變更就不寫盤」必須在
    mutator 內決定(先 read 再決定要不要 update,兩次呼叫之間判斷就過期了)。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    first = await a.episode_set_publication_state(m, 1, "deferred", reason="QA 拒收")
    rev = _revision_of(m)
    again = await a.episode_set_publication_state(m, 1, "deferred", reason="QA 拒收")
    assert first["changed"] is True and again["changed"] is False
    assert again["previous_state"] == "deferred"
    assert _revision_of(m) == rev  # 一次寫盤都沒發生
    # 解除一個本來就沒扣的集數同理:冪等、不寫盤
    await a.episode_set_publication_state(m, 1, None)
    rev_after_clear = _revision_of(m)
    noop = await a.episode_set_publication_state(m, 1, None)
    assert noop["changed"] is False and _revision_of(m) == rev_after_clear
    # 但**換了理由**是真的變更,要寫進去
    changed = await a.episode_set_publication_state(m, 1, "deferred", reason="改用新素材重錄")
    assert changed["changed"] is True
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert ep["publication_state_reason"] == "改用新素材重錄"


async def test_noop_path_never_reads_the_manifest_outside_the_lock(tmp_path, monkeypatch):
    """`_NoChange` 的回傳要整份從 mutator 帶出來,**不准在鎖外再讀一次**。

    鎖外重讀會拼出一份不屬於任何 snapshot 的回傳:「在 revision R 上判斷沒變更」+
    「在 R+2 上讀到的欄位」。序列跑的測試看不到這件事(沒有人插進那個縫),所以這裡直接
    鎖住不變式本身 —— 把 `ManifestStore.read` 換成會爆的版本,no-op 路徑仍必須成功。
    (`update` 走的是內部 `_load`,不受影響;真正在鎖外的那次讀取才會踩到。)"""
    from notebooklm_mcp import manifest_store

    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    await a.episode_set_publication_state(m, 1, "deferred", reason="QA 拒收")

    reads = []

    def exploding_read(self):
        reads.append(self.path)
        raise AssertionError("no-op 路徑在鎖外重讀了 manifest")

    monkeypatch.setattr(manifest_store.ManifestStore, "read", exploding_read)
    res = await a.episode_set_publication_state(m, 1, "deferred", reason="QA 拒收")
    assert res["changed"] is False and res["previous_state"] == "deferred"
    assert reads == []


async def test_replay_repairs_a_legacy_incomplete_audit_triple(tmp_path):
    """三欄**齊全**才算沒變更 —— 否則殘缺的 legacy 資料永遠補不上那一欄。

    形狀:有 `publication_state` + `reason`、沒有 `publication_state_at`(某個舊腳本或
    手改留下的)。若 no-op 只比對 state+reason,重呼會直接 `_NoChange`,而「重呼一次把它
    修好」正是呼叫端唯一能做的補救 —— 那個缺欄從此永遠是缺的。"""
    m = _manifest(
        tmp_path,
        [
            {
                "episode": 46,
                "title": "EP46",
                "publication_state": "deferred",
                "publication_state_reason": "QA 拒收",
            }
        ],
    )
    res = await a.episode_set_publication_state(m, 46, "deferred", reason="QA 拒收")
    assert res["changed"] is True  # 同一組 state+reason,但仍要寫
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert datetime.fromisoformat(ep["publication_state_at"]).utcoffset() == timedelta(0)
    # 補完之後才變成真正的 no-op
    assert (await a.episode_set_publication_state(m, 46, "deferred", reason="QA 拒收"))[
        "changed"
    ] is False


@pytest.mark.parametrize(
    "stamp",
    [None, "", "garbage+00:00", "2026-08-14T00:05:36", 1755000000],
    ids=["null", "empty", "garbage", "naive_no_tz", "epoch_int"],
)
async def test_replay_repairs_a_present_but_invalid_timestamp(tmp_path, stamp):
    """「齊全」不能只看 key 在不在 —— `publication_state_at: null` 是 key 存在的。

    這幾種值都是 key 存在但稽核脈絡為零(含 naive 的無時區字串:那筆記錄事後對不上時區)。
    若把它們當合法,no-op 會讓那個壞欄永遠補不上。"""
    m = _manifest(
        tmp_path,
        [
            {
                "episode": 46,
                "title": "EP46",
                "publication_state": "deferred",
                "publication_state_reason": "QA 拒收",
                "publication_state_at": stamp,
            }
        ],
    )
    res = await a.episode_set_publication_state(m, 46, "deferred", reason="QA 拒收")
    assert res["changed"] is True
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert datetime.fromisoformat(ep["publication_state_at"]).utcoffset() == timedelta(0)


async def test_set_publication_state_validates(tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    # 只認 publish 端白名單裡的狀態 —— 設得進去卻不被發布認,比擋下來更糟
    with pytest.raises(ValueError, match="unknown publication_state"):
        await a.episode_set_publication_state(m, 1, "defered", reason="拼錯")
    with pytest.raises(ValueError, match="unknown publication_state"):
        await a.episode_set_publication_state(m, 1, "", reason="空字串")
    # 稽核脈絡是這個狀態的重點
    with pytest.raises(ValueError, match="reason is required"):
        await a.episode_set_publication_state(m, 1, "deferred")
    with pytest.raises(ValueError, match="reason is required"):
        await a.episode_set_publication_state(m, 1, "deferred", reason="   ")
    # 解除時傳 reason = 呼叫端搞錯語意,靜默丟掉會讓它以為那句話留在 manifest 裡
    with pytest.raises(ValueError, match="only meaningful when setting"):
        await a.episode_set_publication_state(m, 1, None, reason="因為修好了")
    with pytest.raises(ValueError, match="episode 9 not found"):
        await a.episode_set_publication_state(m, 9, "deferred", reason="不存在的集")
    # 全程一個字都不准落地
    assert "publication_state" not in Path(m).read_text(encoding="utf-8")


async def test_set_publication_state_never_touches_attempts_or_output(tmp_path):
    """它只管發布層:不動 attempt、artifact、本機檔案 —— deferred 不代表禁止重生。"""
    m = _manifest(
        tmp_path,
        [
            {
                "episode": 46,
                "title": "EP46",
                "attempts": [{"attempt_id": "att-1", "remote": {"artifact_id": "art-1"}}],
                "artifact_id": "art-1",
                "mp3_path": "/tmp/ep46.mp3",
                "retracted_attempt_ids": ["att-0"],
            }
        ],
    )
    await a.episode_set_publication_state(m, 46, "deferred", reason="QA 拒收")
    ep = json.loads(Path(m).read_text(encoding="utf-8"))["episodes"][0]
    assert ep["attempts"] == [{"attempt_id": "att-1", "remote": {"artifact_id": "art-1"}}]
    assert ep["artifact_id"] == "art-1" and ep["mp3_path"] == "/tmp/ep46.mp3"
    assert ep["retracted_attempt_ids"] == ["att-0"]


def test_importing_tools_publish_first_does_not_deadlock_on_the_whitelist():
    """把白名單搬回 `tools_publish` 會炸循環 import —— 而且只在特定順序下。

    `tools_publish` → `app` → 註冊 `tools_artifacts` → 回頭 import `tools_publish`,
    那時它只初始化到 `from .app import mcp` 那一行,常數還不存在。全套測試跑起來看不到
    (別的 test module 先把 `app` import 進來了),所以這條開**乾淨的 interpreter**
    直接以 `tools_publish` 為第一個 import。正本因此放在 `publish/state.py`。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import notebooklm_mcp.tools_publish as t; "
                "assert t.state_mod.WITHHELD_PUBLICATION_STATES"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


async def test_state_whitelist_is_shared_with_publish_not_duplicated(tmp_path):
    """可以設的狀態 = 會被扣下的狀態,同一份白名單。

    各寫一份的話兩邊會漂:設得進去、發布卻不認(那一集照樣公開),或反過來。

    v0.9.19 起 publisher **不再持有自己的參照**:「哪些集進 feed」的判準(含未知值
    fail-loud)收成 `state.is_withheld`,兩支修 `published_at` 的腳本也照同一份 —— 所以
    這裡驗的是 writer 用同一個 frozenset、而 publisher 用同一個 state 模組。"""
    from notebooklm_mcp import tools_artifacts, tools_publish
    from notebooklm_mcp.publish import state

    assert tools_artifacts.WITHHELD_PUBLICATION_STATES is state.WITHHELD_PUBLICATION_STATES
    assert tools_publish.state_mod is state


# ---- v0.3.3:簡報/講義原子換檔(torn write regression)---------------------------
# 固定檔名 ep{n:02d}-slides.pdf / -report.md 的就地覆寫:重生中斷會讓 partial file 頂替
# 原本完整的產物,而 manifest 仍指向同一路徑,publish 的「存在且非空」檢查抓不到。


def _episode_with_existing(tmp_path, name, content):
    """manifest 同目錄先放一份「上一版完整產物」,模擬重生前的現狀。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    existing = tmp_path / name
    existing.write_bytes(content)
    return m, existing


def _part_files(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")]


async def test_slides_download_failure_leaves_previous_pdf_intact(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 half"  # partial 先落地
    fake_client.artifacts.download_slides_exc = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await a.generate_slides("nb-1", m, 1)

    assert existing.read_bytes() == b"%PDF-1.4 GOOD OLD"  # 舊那份毫髮無傷
    assert _part_files(tmp_path) == []  # temp 清乾淨
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert "slides_pdf_path" not in data["episodes"][0]  # 失敗不回寫 manifest


async def test_report_download_failure_leaves_previous_markdown_intact(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-report.md", "# 舊講義\n完整\n".encode())
    fake_client.artifacts.download_report_bytes = "# 半份".encode()
    fake_client.artifacts.download_report_exc = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await a.generate_report("nb-1", m, 1)

    assert existing.read_bytes() == "# 舊講義\n完整\n".encode()
    assert _part_files(tmp_path) == []
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert "report_md_path" not in data["episodes"][0]


async def test_slides_replaced_atomically_on_success(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 BRAND NEW"
    res = await a.generate_slides("nb-1", m, 1)

    assert existing.read_bytes() == b"%PDF-1.4 BRAND NEW"  # 一次性換上
    assert res["slides_pdf_path"] == str(existing)
    assert _part_files(tmp_path) == []
    data = json.loads(Path(m).read_text(encoding="utf-8"))
    assert data["episodes"][0]["slides_pdf_path"] == str(existing)


async def test_empty_download_is_rejected_and_old_file_kept(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b""  # 零位元組
    with pytest.raises(ValueError, match="empty"):
        await a.generate_slides("nb-1", m, 1)
    assert existing.read_bytes() == b"%PDF-1.4 GOOD OLD"
    assert _part_files(tmp_path) == []


async def test_non_pdf_download_is_rejected_and_old_file_kept(fake_client, tmp_path):
    """非空但不成形:magic 檢查是 torn write 的第二層,非空檢查放行的那種。"""
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b"<html>error page</html>"
    with pytest.raises(ValueError, match="not a PDF"):
        await a.generate_slides("nb-1", m, 1)
    assert existing.read_bytes() == b"%PDF-1.4 GOOD OLD"
    assert _part_files(tmp_path) == []


async def test_invalid_utf8_report_is_rejected_and_old_file_kept(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-report.md", "# 舊講義\n".encode())
    fake_client.artifacts.download_report_bytes = b"\xff\xfe truncated multibyte"
    with pytest.raises(ValueError, match="not valid UTF-8"):
        await a.generate_report("nb-1", m, 1)
    assert existing.read_bytes() == "# 舊講義\n".encode()
    assert _part_files(tmp_path) == []


# ---- v0.3.3 review fixes:原子換檔的權限與 commit point ---------------------------


async def test_atomic_replace_preserves_existing_file_mode(fake_client, tmp_path):
    """mkstemp 建的 temp 是 0600,os.replace 會把它帶到最終檔——既有 0644 的講義被重生後
    別人就讀不到了(下一次 publish_series 拿到 PermissionError)。換檔要保留原 mode。"""
    import os
    import stat

    m, existing = _episode_with_existing(tmp_path, "ep01-report.md", "# 舊\n".encode())
    os.chmod(existing, 0o644)
    await a.generate_report("nb-1", m, 1)
    assert stat.S_IMODE(os.stat(existing).st_mode) == 0o644


async def test_atomic_replace_new_file_is_not_private(fake_client, tmp_path):
    """首次生成沒有舊檔可繼承 mode,也不該落成 mkstemp 的 0600。"""
    import os
    import stat

    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_slides("nb-1", m, 1)
    assert stat.S_IMODE(os.stat(res["slides_pdf_path"]).st_mode) == 0o644


async def test_unsupported_directory_fsync_does_not_fail_the_download(
    fake_client, tmp_path, monkeypatch
):
    """os.replace 之後就是 commit point。有些 filesystem 不支援 directory fsync
    (EINVAL/ENOTSUP)——那不是失敗,不該讓已經成功的換檔回報成錯誤。"""
    import errno

    from notebooklm_mcp import _atomic

    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 NEW"

    def unsupported(path):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(_atomic, "fsync_parent", unsupported)
    res = await a.generate_slides("nb-1", m, 1)  # 不該 raise
    assert existing.read_bytes() == b"%PDF-1.4 NEW"
    assert res["slides_pdf_path"] == str(existing)


async def test_post_commit_failure_says_the_file_was_already_replaced(
    fake_client, tmp_path, monkeypatch
):
    """真正的 IO 錯誤仍要 raise,但訊息必須講明「檔案已經換掉了」——否則呼叫端會照
    docstring 以為舊檔還在,做出錯誤的復原決定。"""
    import errno

    from notebooklm_mcp import _atomic

    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 NEW"

    def io_error(path):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(_atomic, "fsync_parent", io_error)
    with pytest.raises(OSError, match="already replaced"):
        await a.generate_slides("nb-1", m, 1)
    assert existing.read_bytes() == b"%PDF-1.4 NEW"  # commit 已發生,誠實反映


# ---- v0.4.1:遠端 mutation 前的便宜 preflight ------------------------------------
# 這些不是 durable attempt(外層斷線重跑仍會重複 mutation,那是整個 slides/report 家族
# 共有的架構債)。它們只擋「可預判、不必打 RPC 就知道會錯」的那些,免得燒配額才發現。


async def test_revise_slide_rejects_an_artifact_from_another_notebook(fake_client, tmp_path):
    """REVISE_SLIDE 只靠 artifact_id 定位(notebook_id 是 routing header),
    伺服器不會擋錯配的 ID —— 而這支改的是遠端狀態,不像 download 只寫本機檔。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    with pytest.raises(ValueError, match="不在 notebook nb-1"):
        await a.artifact_revise_slide("nb-1", m, 5, "deck-elsewhere", 0, "改這頁")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "revise_slide"]


async def test_revise_slide_rejects_a_non_slide_artifact(fake_client, tmp_path):
    from notebooklm.types import ArtifactType

    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("rep-1", kind=ArtifactType.REPORT)
    with pytest.raises(ValueError, match="不是 slide_deck"):
        await a.artifact_revise_slide("nb-1", m, 5, "rep-1", 0, "改這頁")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "revise_slide"]


async def test_revise_slide_rejects_an_unfinished_deck(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1", completed=False)
    with pytest.raises(ValueError, match="尚未完成"):
        await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "revise_slide"]


async def test_revise_slide_rejects_an_unknown_episode_before_any_rpc(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    with pytest.raises(ValueError, match="episode 9 not found"):
        await a.artifact_revise_slide("nb-1", m, 9, "deck-1", 0, "改這頁")
    assert not fake_client.artifacts.calls  # 連 get_or_none 都還沒打


async def test_generate_slides_and_report_check_the_episode_before_generating(
    fake_client, tmp_path
):
    """打錯 episode_n 舊行為是「生成 → 下載 → 回寫時才 raise」= 白燒一次配額。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_slides("nb-1", m, 2)
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_report("nb-1", m, 2)
    assert not fake_client.artifacts.calls


# ---- M2:source_ids preflight(現成 guard 補進 generate_slides / generate_report) --
# 打錯/已刪的 source_id 伺服器不擋,燒完一次生成配額才發現拿到聚焦錯誤的產物。
# generate_audio(tools_basic.py)與 podcast_episode(tools_podcast.py)已在用同一道
# _sources.to_source_ids + assert_sources_exist——這裡補上這兩支缺的那份(見
# test_source_selection.py 的對照組)。


async def test_generate_slides_rejects_an_absent_source_before_generating(fake_client, tmp_path):
    fake_client.sources.seed("EP01 題目")
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="src-9"):
        await a.generate_slides("nb-1", m, 1, source_ids=["src-1", "src-9"])
    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck"]


async def test_generate_report_rejects_an_absent_source_before_generating(fake_client, tmp_path):
    fake_client.sources.seed("EP01 題目")
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="src-9"):
        await a.generate_report("nb-1", m, 1, source_ids=["src-1", "src-9"])
    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_report"]


@pytest.mark.parametrize("bad", [[], ["src-1", "src-1"], ["src-1", ""], ["src-1", 2], "src-1"])
async def test_generate_slides_rejects_a_malformed_selection(fake_client, tmp_path, bad):
    """空清單/重複/非字串在打 RPC 之前就退,與 to_source_ids 的既定語意一致。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError):
        await a.generate_slides("nb-1", m, 1, source_ids=bad)
    assert not fake_client.artifacts.calls


@pytest.mark.parametrize("bad", [[], ["src-1", "src-1"], ["src-1", ""], ["src-1", 2], "src-1"])
async def test_generate_report_rejects_a_malformed_selection(fake_client, tmp_path, bad):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError):
        await a.generate_report("nb-1", m, 1, source_ids=bad)
    assert not fake_client.artifacts.calls


async def test_generate_slides_without_selection_keeps_sdk_fallback(fake_client, tmp_path):
    """不傳 source_ids = 現行行為(SDK 自己抓全部來源),不多打 sources.list。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    await a.generate_slides("nb-1", m, 1)
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck")
    assert gen["source_ids"] is None
    assert not [c for c in fake_client.sources.calls if c[0] == "list"]


async def test_generate_report_without_selection_keeps_sdk_fallback(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    await a.generate_report("nb-1", m, 1)
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_report")
    assert gen["source_ids"] is None
    assert not [c for c in fake_client.sources.calls if c[0] == "list"]


async def test_backend_undispatchable_report_format_is_refused_before_any_dispatch(
    fake_client, tmp_path
):
    """web 生不出來的格式要在**進 closure 之前**被擋掉,不是在裡面炸。

    v0.9.25-rc 驗收 5.1 的事故形狀:`concept_explanation` 一度被加進
    `enums._REPORT_FORMAT`(當時的理由是「SDK enum 有,白名單漏了」),於是
    `to_report_format()` 放行,失敗點移進 closure 裡的 SDK param builder ——
    而 `_dispatch_attachment` 的泛用 except 把那個**純本地** `ValidationError`
    記成 `attachment_acceptance_unknown`。實測 `CREATE_ARTIFACT` 打了**零次**,
    呼叫端卻被叫去跑一次註定撈不到東西的對帳。

    `tools_artifacts` 早就有正確的紀律(「純本地轉換擋在 closure 外」),壞的是
    白名單讓本地錯誤穿過去。所以這條測試釘的是**零 dispatch**,不是錯誤訊息長相 ——
    後者會隨上游措辭漂,前者才是不變式。能不能開放某個格式由
    `tests/test_enums.py::test_report_format_whitelist_matches_what_the_web_backend_can_dispatch`
    守著。
    """
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="report format"):
        await a.generate_report("nb-1", m, 1, report_format="concept_explanation")
    assert not fake_client.artifacts.calls, (
        "本地就該拒絕的格式跑進了 dispatch —— 它會被記成假的 acceptance_unknown"
    )
