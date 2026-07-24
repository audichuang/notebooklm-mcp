"""cover_cli(notebooklm-cover)固化後的測試:設計在 HTML template,工具做
填佔位符 → Chrome 光柵化 → JPEG。需要系統有 headless Chrome 的測試會自動 skip。"""
import json
import os
import sys

import pytest

from notebooklm_mcp import cover_cli


def _has_chrome() -> bool:
    try:
        cover_cli._find_chrome()
        return True
    except SystemExit:
        return False


chrome_required = pytest.mark.skipif(not _has_chrome(), reason="無 headless Chrome")


def test_episode_hue_deterministic():
    # 決定性鐵律:集號 → 固定色相(同集永遠同色,發布端 content-hash 才穩)
    assert cover_cli._episode_hue(1) == 77
    assert cover_cli._episode_hue(2) == 154
    assert cover_cli._episode_hue(3) == 231
    assert cover_cli._episode_hue(5) == (5 * 77) % 360


def test_find_chrome_missing(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_COVER_CHROME", raising=False)
    monkeypatch.setattr(cover_cli.shutil, "which", lambda c: None)
    with pytest.raises(SystemExit):
        cover_cli._find_chrome()


def test_find_chrome_explicit(tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("x")
    assert cover_cli._find_chrome(str(fake)) == str(fake)


def test_templates_present():
    # 固化的設計檔必須隨套件在
    assert cover_cli._load_template("cover_episode.html").lstrip().startswith("<!doctype")


@chrome_required
def test_render_episode_smoke(tmp_path):
    tpl = cover_cli._load_template("cover_episode.html")
    out = tmp_path / "EP07.jpg"
    info = cover_cli._render(
        tpl,
        {"__SHOW__": "Audicast", "__EPNUM__": "07",
         "__TITLE__": "測試標題 test <&>", "__BYLINE__": "tester", "__HUE__": 120},
        str(out), cover_cli._find_chrome())
    assert info == {"width": 3000, "height": 3000, "format": "JPEG"}
    # 擋「相對路徑 → Chrome ERR_INVALID_URL 白頁」回歸:深色設計平均亮度應偏低
    from PIL import Image, ImageStat
    luma = ImageStat.Stat(Image.open(out).convert("L")).mean[0]
    assert luma < 120, f"疑似白色錯誤頁而非封面 (luma={luma})"


@chrome_required
def test_batch_writes_cover_path(tmp_path, monkeypatch):
    man = tmp_path / "m.json"
    man.write_text(json.dumps(
        {"episodes": [{"episode": 1, "title": "甲集"}, {"episode": 2, "title": "乙集"}]}),
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover", "--manifest", str(man), "--show-name", "Audicast",
        "--byline", "x", "--output-dir", str(tmp_path)])
    cover_cli.main()
    m = json.loads(man.read_text(encoding="utf-8"))
    for ep in m["episodes"]:
        assert ep["cover_path"].endswith(f"EP{ep['episode']:02d}.jpg")
        assert os.path.getsize(ep["cover_path"]) > 0

def test_batch_manifest_update_uses_manifest_store(tmp_path, monkeypatch):
    man = tmp_path / "m.json"
    man.write_text(
        json.dumps({
            "unknown": {"keep": True},
            "episodes": [{"episode": 1, "title": "甲集"}],
        }),
        encoding="utf-8",
    )

    def fake_render(_template, _subs, output, _chrome):
        with open(output, "wb") as image_file:
            image_file.write(b"fake-cover")
        return {"width": 3000, "height": 3000, "format": "JPEG"}

    monkeypatch.setattr(cover_cli, "_find_chrome", lambda _explicit=None: "fake-chrome")
    monkeypatch.setattr(cover_cli, "_render", fake_render)
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover",
        "--manifest", str(man),
        "--show-name", "Audicast",
        "--output-dir", str(tmp_path),
    ])

    cover_cli.main()

    stored = json.loads(man.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 2
    assert stored["revision"] == 1
