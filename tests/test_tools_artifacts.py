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
