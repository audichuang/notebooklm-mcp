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


def test_render_failure_preserves_existing_output(tmp_path, monkeypatch):
    from PIL import Image

    output = tmp_path / "show.jpg"
    output.write_bytes(b"previous-cover")

    def fake_chrome(command, **_kwargs):
        screenshot = next(
            value.split("=", 1)[1] for value in command if value.startswith("--screenshot=")
        )
        Image.new("RGB", (3000, 3000)).save(screenshot, "PNG")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    def reject_artwork(_path):
        raise ValueError("invalid rendered artwork")

    monkeypatch.setattr(cover_cli, "_find_chrome", lambda _explicit=None: "fake-chrome")
    monkeypatch.setattr(cover_cli.subprocess, "run", fake_chrome)
    monkeypatch.setattr(cover_cli, "validate_artwork", reject_artwork)
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover", "--show", "--output", str(output),
    ])

    with pytest.raises(ValueError, match="invalid rendered artwork"):
        cover_cli.main()

    assert output.read_bytes() == b"previous-cover"


def test_skip_existing_rebuilds_a_corrupt_cover_instead_of_trusting_it(
    tmp_path, monkeypatch, capsys
):
    """`--skip-existing` 的語意是「已經做完的跳過」,而壞檔就是沒做完。

    只看 `os.path.exists` 會把截斷的 JPEG 當成產出,一路帶到 publish 才被 Apple 端擋
    ——但也不該因為一個壞檔就讓整批 abort:那要人先手動刪檔才跑得動。驗不過就重畫。
    """
    from PIL import Image

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"episodes": [{"episode": 1, "title": "甲集"}]}),
        encoding="utf-8",
    )
    corrupt = tmp_path / "EP01.jpg"
    corrupt.write_bytes(b"not-an-image")

    def fake_chrome(command, **_kwargs):
        screenshot = next(
            value.split("=", 1)[1] for value in command if value.startswith("--screenshot=")
        )
        Image.new("RGB", (3000, 3000)).save(screenshot, "PNG")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(cover_cli, "_find_chrome", lambda _explicit=None: "fake-chrome")
    monkeypatch.setattr(cover_cli.subprocess, "run", fake_chrome)
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover", "--manifest", str(manifest),
        "--output-dir", str(tmp_path), "--skip-existing",
    ])

    cover_cli.main()

    assert "REBUILD" in capsys.readouterr().out
    assert cover_cli.validate_artwork(str(corrupt))["format"] == "JPEG"
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["cover_path"] == str(corrupt)


def test_skip_existing_keeps_a_valid_cover_untouched(tmp_path, monkeypatch, capsys):
    """驗得過就真的跳過 —— 不重畫、不改檔案內容(這才是 `--skip-existing` 的本業)。"""
    from PIL import Image

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"episodes": [{"episode": 1, "title": "甲集"}]}),
        encoding="utf-8",
    )
    good = tmp_path / "EP01.jpg"
    Image.new("RGB", (1400, 1400)).save(good, "JPEG", quality=92)
    before = good.read_bytes()

    def explode(*_args, **_kwargs):
        raise AssertionError("驗得過的封面不該重畫")

    monkeypatch.setattr(cover_cli, "_find_chrome", lambda _explicit=None: "fake-chrome")
    monkeypatch.setattr(cover_cli.subprocess, "run", explode)
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover", "--manifest", str(manifest),
        "--output-dir", str(tmp_path), "--skip-existing",
    ])

    cover_cli.main()

    assert "SKIP" in capsys.readouterr().out
    assert good.read_bytes() == before


def test_skip_existing_does_not_need_chrome_when_every_cover_is_valid(
    tmp_path, monkeypatch
):
    """全部封面都驗得過時,這一輪根本不需要 Chrome —— 不該因為沒裝而失敗。

    Chrome lookup 原本在 main 開頭無條件跑,於是「只是要把既有封面寫回 manifest」也得
    先有 Chrome。
    """
    from PIL import Image

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"episodes": [{"episode": 1, "title": "甲集"}]}),
        encoding="utf-8",
    )
    Image.new("RGB", (1400, 1400)).save(tmp_path / "EP01.jpg", "JPEG", quality=92)

    def no_chrome_on_this_machine(_explicit=None):
        raise SystemExit("找不到 headless Chrome")

    monkeypatch.setattr(cover_cli, "_find_chrome", no_chrome_on_this_machine)
    monkeypatch.setattr(sys, "argv", [
        "notebooklm-cover", "--manifest", str(manifest),
        "--output-dir", str(tmp_path), "--skip-existing",
    ])

    cover_cli.main()

    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["cover_path"].endswith("EP01.jpg")
