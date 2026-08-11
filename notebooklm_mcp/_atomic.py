"""共用的原子檔案落地(零依賴,供 tools_artifacts / audio_finalize 匯入)。

下載直接寫最終路徑會就地覆寫:重生中斷就留下 partial file 頂替原本完整的產物,而
manifest 仍指向同一路徑,publish 的「存在且非空」檢查也抓不到(EP36 的簡報/講義固定
下載到 ep{n:02d}-slides.pdf / -report.md,重生期間 publish 可能讀到半份)。所以一律
temp → 驗 → fsync → os.replace:並發讀者只會看到「舊的完整版」或「新的完整版」。
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable, Iterator

#: directory fsync 在某些 filesystem(部分 network/overlay mount)本來就不支援。那不是
#: 失敗,只是拿不到額外的持久性保證 —— 不該讓它把一次成功的換檔回報成錯誤。
_DIR_FSYNC_UNSUPPORTED = frozenset(
    getattr(errno, name) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "ENOTTY")
    if hasattr(errno, name)
)
#: 新檔沒有舊 mode 可繼承時用這個。**不能**留 mkstemp 的 0600:產物要讓其他帳號
#: (QA / 發布)讀得到,否則後續 publish_series 會拿到 PermissionError。
_NEW_FILE_MODE = 0o644


def fsync_parent(path: str) -> None:
    """把目錄項本身刷下去——os.replace 之後才算真的換完(斷電也不會退回舊 dirent)。"""
    directory_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def prepared_replacement(
    final_path: str, *, suffix: str = ".part", mode: int | None = None
) -> Iterator[str]:
    """在 ``final_path`` 同目錄開一個 temp 檔,離開 with 區塊時**原子換上去**。

    這是本 package 唯一一份「原子換檔」實作(見 `docs/gotchas-files.md` 的紅線:任何新的
    原子寫入一律重用這裡)。自己再寫一份的下場已經記過三次,每次漏的都是同樣那幾件:

    - **mkstemp 的 0600 被帶到最終檔**:``os.replace`` 會把 temp 的 mode 一起換過去,
      於是重生一次就把 0644 的產物變成 0600,之後 publish 讀不到。這裡沿用既有檔案的
      mode(首次建立用 `_NEW_FILE_MODE`),或由 ``mode`` 顯式指定(憑證用 0600)。
    - **commit point 之後的 fsync 被放進 try 裡**:一拋就把剛換上去的東西刪掉。所以
      directory fsync 刻意留在 try 之外。
    - **沒容忍 `_DIR_FSYNC_UNSUPPORTED`**:部分 network/overlay mount 本來就不支援
      directory fsync,那不是失敗,只是拿不到額外的持久性保證。

    契約:**``os.replace`` 是 commit point。** 它之前的任何失敗都不動既有檔案(temp 會被
    清掉);它之後舊內容即不可回復,所以之後唯一可能的失敗(directory fsync)會回報成
    明講「檔案已經換掉了」的錯誤,呼叫端才不會照著「舊檔還在」做復原決定。

    temp 刻意建在同目錄:``os.replace`` 只在同一 filesystem 上才是原子操作。
    """
    directory = os.path.dirname(os.path.abspath(final_path)) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(final_path)}.", suffix=suffix
    )
    os.close(fd)
    try:
        yield temp_path
        if os.path.getsize(temp_path) <= 0:
            raise ValueError(f"refusing to install an empty file: {final_path}")
        resolved_mode = mode
        if resolved_mode is None:
            try:
                resolved_mode = stat.S_IMODE(os.stat(final_path).st_mode)
            except FileNotFoundError:
                resolved_mode = _NEW_FILE_MODE
        os.chmod(temp_path, resolved_mode)
        with open(temp_path, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)          # ← commit point
    except BaseException:
        # 清掉 partial;清理本身失敗不得蓋掉真正的錯誤(CancelledError 也要清,故用
        # BaseException)。
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    try:
        fsync_parent(final_path)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return
        raise OSError(
            exc.errno,
            f"{final_path} was already replaced with the new content; only the "
            f"directory fsync failed ({exc.strerror}) — the previous version is gone, "
            f"do not treat this as 'download did not happen'",
        ) from exc


async def download_atomically(
    final_path: str,
    download: Callable[[str], Awaitable[object]],
    validate: Callable[[str], None],
) -> None:
    """把 ``download`` 的產物原子換上 ``final_path``。

    ``download`` 收到 temp 路徑,``validate`` 在 replace 之前對 temp 檔跑格式檢查
    (空檔已先擋)。換檔契約見 `prepared_replacement`。"""
    with prepared_replacement(final_path) as temp_path:
        await download(temp_path)
        if os.path.getsize(temp_path) <= 0:
            raise ValueError(f"downloaded file is empty: {final_path}")
        validate(temp_path)
