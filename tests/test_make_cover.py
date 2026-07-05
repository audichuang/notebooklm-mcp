"""make_cover.py --manifest 的 preflight 契約(跑真 CLI,只測錯誤路徑:壞資料在
任何算圖/寫回前就 fail,且不覆寫 manifest)。錯誤路徑不觸發字型/算圖,所以快且不依賴字型。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_cover.py"


def _run(manifest_path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest_path),
         "--tag", "~/.claude/", "--byline", "x"],
        capture_output=True, text=True,
    )


def _write(tmp_path, episodes, title="節目名"):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"title": title, "episodes": episodes}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def test_string_episode_rejected_and_manifest_untouched(tmp_path):
    # "1"(字串)以前會撞上 f"EP{n:02d}" 崩潰或被靜默處理;現在 preflight 直接擋。
    p = _write(tmp_path, [{"episode": "1", "title": "心法篇"}])
    before = p.read_text(encoding="utf-8")
    r = _run(p)
    assert r.returncode != 0
    assert p.read_text(encoding="utf-8") == before   # 壞資料不半途覆寫(原子)


def test_missing_title_rejected(tmp_path):
    r = _run(_write(tmp_path, [{"episode": 1}]))
    assert r.returncode != 0


def test_empty_episodes_rejected(tmp_path):
    r = _run(_write(tmp_path, []))
    assert r.returncode != 0


def test_duplicate_episode_rejected(tmp_path):
    r = _run(_write(tmp_path, [{"episode": 1, "title": "a"}, {"episode": 1, "title": "b"}]))
    assert r.returncode != 0
