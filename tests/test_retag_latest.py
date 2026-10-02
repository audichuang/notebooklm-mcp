"""CI 驗過哪顆 release commit，latest 就只能指向那顆。"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retag-latest.sh"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def test_retag_uses_the_commit_that_passed_ci_even_if_a_newer_tag_exists(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-q", str(work), cwd=tmp_path)
    _git("config", "user.name", "test", cwd=work)
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)

    commits = []
    for version in ("v0.9.28", "v0.9.29"):
        _git("commit", "-q", "--allow-empty", "-m", version, cwd=work)
        _git("tag", "-am", version, version, cwd=work)
        commits.append(_git("rev-parse", "HEAD", cwd=work))
    _git("push", "-q", "origin", "--tags", cwd=work)

    unchecked = subprocess.run(
        ["bash", str(SCRIPT), "--push"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert unchecked.returncode != 0, "直接 --push 不得跳過 CI 驗過的 commit"

    checked = subprocess.run(
        ["bash", str(SCRIPT), "--push", "v0.9.28", commits[0]],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
    assert (
        _git("--git-dir", str(remote), "rev-list", "-n", "1", "latest", cwd=tmp_path) == commits[0]
    )

    moved = subprocess.run(
        ["bash", str(SCRIPT), "--push", "v0.9.28", commits[1]],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert moved.returncode != 0
    assert (
        _git("--git-dir", str(remote), "rev-list", "-n", "1", "latest", cwd=tmp_path) == commits[0]
    )
