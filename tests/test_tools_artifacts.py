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
