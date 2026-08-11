"""`_atomic.prepared_replacement` 是本 package **唯一一份**原子換檔實作。

`docs/gotchas-files.md` 的紅線就是「任何新的原子寫入一律重用它」——自己再寫一份的下場
記過三次,每次漏的都是同樣那幾件事(mkstemp 的 0600 被帶到最終檔、commit point 之後的
fsync 放在 try 裡、沒容忍 `_DIR_FSYNC_UNSUPPORTED`)。這個檔把那幾件事逐條釘住,新的
caller 接上來就自動有保障。
"""
import errno
import os
import stat

import pytest

from notebooklm_mcp._atomic import _NEW_FILE_MODE, prepared_replacement


def _write(path, payload=b"new"):
    with open(path, "wb") as handle:
        handle.write(payload)


def test_replacement_is_atomic_and_leaves_no_temp(tmp_path):
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    with prepared_replacement(str(target)) as temp_path:
        _write(temp_path)
        assert target.read_bytes() == b"old", "commit point 之前不得動到既有檔案"

    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


def test_failure_inside_the_body_preserves_the_existing_file(tmp_path):
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    with pytest.raises(RuntimeError, match="boom"):
        with prepared_replacement(str(target)) as temp_path:
            _write(temp_path)
            raise RuntimeError("boom")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_cancellation_also_cleans_the_temp_file(tmp_path):
    """`CancelledError` 是 `BaseException` —— 清理用 `except BaseException` 才收得到。"""
    import asyncio

    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    with pytest.raises(asyncio.CancelledError):
        with prepared_replacement(str(target)) as temp_path:
            _write(temp_path)
            raise asyncio.CancelledError()

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_empty_output_is_refused(tmp_path):
    """空檔不准頂替既有產物(半途中斷最常見的形狀)。"""
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    with pytest.raises(ValueError, match="empty file"):
        with prepared_replacement(str(target)):
            pass

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_existing_mode_is_inherited_not_the_mkstemp_0600(tmp_path):
    """**mkstemp 給 0600,而 `os.replace` 會把 temp 的 mode 一起換過去。**

    漏掉這一步的下場:重生一次就把 0644 的產物變成 0600,之後 publish 讀不到。
    """
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")
    target.chmod(0o644)

    with prepared_replacement(str(target)) as temp_path:
        _write(temp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_first_write_uses_the_shared_default_mode(tmp_path):
    target = tmp_path / "artifact.bin"

    with prepared_replacement(str(target)) as temp_path:
        _write(temp_path)

    assert stat.S_IMODE(target.stat().st_mode) == _NEW_FILE_MODE


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_explicit_mode_wins_over_the_existing_file(tmp_path):
    """憑證用的是這條:0600 不能因為舊檔剛好是 0644 就被放寬。"""
    target = tmp_path / "storage_state.json"
    target.write_bytes(b"old")
    target.chmod(0o644)

    with prepared_replacement(str(target), mode=0o600) as temp_path:
        _write(temp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_unsupported_directory_fsync_is_not_an_error(tmp_path, monkeypatch):
    """部分 network/overlay mount 不支援 directory fsync —— 那不是失敗。"""
    target = tmp_path / "artifact.bin"
    real_fsync = os.fsync
    calls = {"n": 0}

    def picky_fsync(fd):
        calls["n"] += 1
        if calls["n"] > 1:                      # 第一次是檔案本身,第二次才是目錄
            raise OSError(errno.EINVAL, "not supported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", picky_fsync)

    with prepared_replacement(str(target)) as temp_path:
        _write(temp_path)

    assert target.read_bytes() == b"new"


def test_directory_fsync_failure_says_the_file_was_already_replaced(tmp_path, monkeypatch):
    """**commit point 之後的失敗不得回報成「沒換成」。**

    呼叫端會照著「舊檔還在」做復原決定,而舊內容其實已經不可回復了。
    """
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")
    real_fsync = os.fsync
    calls = {"n": 0}

    def failing_dir_fsync(fd):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(errno.EIO, "disk is angry")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_dir_fsync)

    with pytest.raises(OSError, match="already replaced"):
        with prepared_replacement(str(target)) as temp_path:
            _write(temp_path)

    # 換檔本身已經成功 —— 錯誤訊息必須說得出這件事,而檔案就是新的。
    assert target.read_bytes() == b"new"
