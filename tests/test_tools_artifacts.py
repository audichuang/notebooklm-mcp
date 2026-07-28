import json

import pytest

from notebooklm_mcp import tools_artifacts as a


def _manifest(tmp_path, episodes):
    p = tmp_path / "series_manifest.json"
    p.write_text(json.dumps({"notebook_id": "nb-1", "episodes": episodes}, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


async def test_generate_slides_downloads_and_writes_manifest(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_slides("nb-1", m, 1, source_ids=["src-1"], slide_format="detailed")

    # 呼叫了對的 SDK 方法,帶對的 enum
    from notebooklm.types import SlideDeckFormat
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck")
    assert gen["source_ids"] == ["src-1"]
    assert gen["slide_format"] == SlideDeckFormat.DETAILED_DECK
    assert gen["language"] == "zh_Hant"                       # resolve_language 預設

    # 下載到 manifest 同目錄的預期檔名
    assert res["slides_pdf_path"].endswith("ep01-slides.pdf")
    # 路徑回寫進 manifest
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]
    assert data["schema_version"] == 2
    assert data["revision"] == 1


async def test_generate_slides_unknown_episode_errors(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_slides("nb-1", m, 2)


async def test_generate_report_downloads_md_and_writes_manifest(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_report("nb-1", m, 1, report_format="study_guide", source_ids=["src-1"])

    from notebooklm.types import ReportFormat
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_report")
    assert gen["report_format"] == ReportFormat.STUDY_GUIDE
    assert gen["language"] == "zh_Hant"

    assert res["report_md_path"].endswith("ep01-report.md")
    assert res["report_format"] == "study_guide"
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["report_md_path"] == res["report_md_path"]
    assert data["episodes"][0]["report_format"] == "study_guide"


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
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]


async def test_artifact_download_report_downloads_without_generating(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 7, "title": "EP07"}])
    res = await a.artifact_download_report("nb-1", m, 7, "report-rescued")

    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_report"]
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_report")
    assert dl["artifact_id"] == "report-rescued"
    data = json.loads(open(m, encoding="utf-8").read())
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
    res = await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 2, "把這頁的數字改成 2026-05 的版本")

    assert not [c for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck"]
    rev = next(c[1] for c in fake_client.artifacts.calls if c[0] == "revise_slide")
    assert rev == {"notebook_id": "nb-1", "artifact_id": "deck-1", "slide_index": 2,
                   "prompt": "把這頁的數字改成 2026-05 的版本"}
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_slide_deck")
    assert dl["artifact_id"] == "deck-1"
    assert res["slides_pdf_path"].endswith("ep05-slides.pdf")
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]


async def test_revise_slide_follows_the_returned_id_not_the_input(fake_client, tmp_path):
    """SDK **沒有保證** REVISE_SLIDE 回傳的 task_id 等於傳入的 artifact_id(它只是 parse
    RPC 回來的那個 id)。實作因此一律用回傳值——餵一個不同的 id 證明沒有依賴那個假設。"""
    m = _manifest(tmp_path, [{"episode": 5, "title": "EP05"}])
    fake_client.artifacts.seed_artifact("deck-1")
    fake_client.artifacts.revise_slide_returns_id = "deck-2"
    res = await a.artifact_revise_slide("nb-1", m, 5, "deck-1", 0, "改這頁")

    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download_slide_deck")
    assert dl["artifact_id"] == "deck-2"          # 下載改版後那份,不是原 id
    assert res["artifact_id"] == "deck-2"


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
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["description"] == "重點整理 ,結論 。"   # 標記清掉
    assert "[" not in data["episodes"][0]["description"]
    assert res["episode"] == 21 and res["description"] == data["episodes"][0]["description"]


async def test_episode_set_description_keep_citations(tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    await a.episode_set_description(m, 1, "重點 [1]。", strip_citations=False)
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["description"] == "重點 [1]。"


async def test_episode_set_description_validates(tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError):                      # 空(清完標記後也算)
        await a.episode_set_description(m, 1, "  [1]  ")
    with pytest.raises(ValueError):                      # 等於標題 = 假 show notes
        await a.episode_set_description(m, 1, "EP01")
    with pytest.raises(ValueError, match="episode 9 not found"):
        await a.episode_set_description(m, 9, "真 show notes")


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
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 half"     # partial 先落地
    fake_client.artifacts.download_slides_exc = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await a.generate_slides("nb-1", m, 1)

    assert existing.read_bytes() == b"%PDF-1.4 GOOD OLD"               # 舊那份毫髮無傷
    assert _part_files(tmp_path) == []                                 # temp 清乾淨
    data = json.loads(open(m, encoding="utf-8").read())
    assert "slides_pdf_path" not in data["episodes"][0]                # 失敗不回寫 manifest


async def test_report_download_failure_leaves_previous_markdown_intact(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-report.md", "# 舊講義\n完整\n".encode())
    fake_client.artifacts.download_report_bytes = "# 半份".encode()
    fake_client.artifacts.download_report_exc = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await a.generate_report("nb-1", m, 1)

    assert existing.read_bytes() == "# 舊講義\n完整\n".encode()
    assert _part_files(tmp_path) == []
    data = json.loads(open(m, encoding="utf-8").read())
    assert "report_md_path" not in data["episodes"][0]


async def test_slides_replaced_atomically_on_success(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 BRAND NEW"
    res = await a.generate_slides("nb-1", m, 1)

    assert existing.read_bytes() == b"%PDF-1.4 BRAND NEW"              # 一次性換上
    assert res["slides_pdf_path"] == str(existing)
    assert _part_files(tmp_path) == []
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["slides_pdf_path"] == str(existing)


async def test_empty_download_is_rejected_and_old_file_kept(fake_client, tmp_path):
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 GOOD OLD")
    fake_client.artifacts.download_slides_bytes = b""                  # 零位元組
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
    import os, stat
    m, existing = _episode_with_existing(tmp_path, "ep01-report.md", "# 舊\n".encode())
    os.chmod(existing, 0o644)
    await a.generate_report("nb-1", m, 1)
    assert stat.S_IMODE(os.stat(existing).st_mode) == 0o644


async def test_atomic_replace_new_file_is_not_private(fake_client, tmp_path):
    """首次生成沒有舊檔可繼承 mode,也不該落成 mkstemp 的 0600。"""
    import os, stat
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_slides("nb-1", m, 1)
    assert stat.S_IMODE(os.stat(res["slides_pdf_path"]).st_mode) == 0o644


async def test_unsupported_directory_fsync_does_not_fail_the_download(fake_client, tmp_path, monkeypatch):
    """os.replace 之後就是 commit point。有些 filesystem 不支援 directory fsync
    (EINVAL/ENOTSUP)——那不是失敗,不該讓已經成功的換檔回報成錯誤。"""
    import errno
    from notebooklm_mcp import _atomic
    m, existing = _episode_with_existing(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 OLD")
    fake_client.artifacts.download_slides_bytes = b"%PDF-1.4 NEW"

    def unsupported(path):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(_atomic, "fsync_parent", unsupported)
    res = await a.generate_slides("nb-1", m, 1)          # 不該 raise
    assert existing.read_bytes() == b"%PDF-1.4 NEW"
    assert res["slides_pdf_path"] == str(existing)


async def test_post_commit_failure_says_the_file_was_already_replaced(fake_client, tmp_path, monkeypatch):
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
    assert existing.read_bytes() == b"%PDF-1.4 NEW"      # commit 已發生,誠實反映


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
    assert not fake_client.artifacts.calls          # 連 get_or_none 都還沒打


async def test_generate_slides_and_report_check_the_episode_before_generating(fake_client, tmp_path):
    """打錯 episode_n 舊行為是「生成 → 下載 → 回寫時才 raise」= 白燒一次配額。"""
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_slides("nb-1", m, 2)
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_report("nb-1", m, 2)
    assert not fake_client.artifacts.calls
