"""notebooklm-cover --manifest 的 preflight 契約(跑真 CLI,只測錯誤路徑:壞資料在
任何算圖/寫回前就 fail,且不覆寫 manifest)。錯誤路徑不觸發字型/算圖,所以快且不依賴字型。

斷言鎖到 argparse error 的 exit code 2 + 特定錯誤訊息 + stderr 無 Traceback——
只看 `returncode != 0` 會假綠(import 錯、語法錯、任何 traceback 都是非零)。"""
import json
import subprocess
import sys
from pathlib import Path


def _run(manifest_path: Path):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "notebooklm_mcp.cover_cli",
            "--manifest",
            str(manifest_path),
            "--tag",
            "~/.claude/",
            "--byline",
            "x",
        ],
        capture_output=True,
        text=True,
    )


def _write(tmp_path, episodes, title="節目名"):
    p = tmp_path / "m.json"
    p.write_text(
        json.dumps({"title": title, "episodes": episodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def _assert_preflight_rejected(r, msg_substr):
    # argparse error -> exit 2;不是 traceback(exit 1)也不是任何其他非零。
    assert r.returncode == 2, f"expected argparse exit 2, got {r.returncode}: {r.stderr}"
    assert "Traceback" not in r.stderr, f"crashed instead of clean reject: {r.stderr}"
    assert msg_substr in r.stderr, f"missing preflight msg {msg_substr!r} in: {r.stderr}"


def test_string_episode_rejected_and_manifest_untouched(tmp_path):
    # "1"(字串)以前會撞上 f"EP{n:02d}" 崩潰或被靜默處理;現在 preflight 直接擋。
    p = _write(tmp_path, [{"episode": "1", "title": "心法篇"}])
    before = p.read_text(encoding="utf-8")
    _assert_preflight_rejected(_run(p), "整數")
    assert p.read_text(encoding="utf-8") == before   # 壞資料不半途覆寫(原子)


def test_missing_title_rejected(tmp_path):
    _assert_preflight_rejected(_run(_write(tmp_path, [{"episode": 1}])), "title")


def test_empty_episodes_rejected(tmp_path):
    _assert_preflight_rejected(_run(_write(tmp_path, [])), "episodes")


def test_duplicate_episode_rejected(tmp_path):
    r = _run(_write(tmp_path, [{"episode": 1, "title": "a"}, {"episode": 1, "title": "b"}]))
    _assert_preflight_rejected(r, "重複")
