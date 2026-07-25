"""共用的原子檔案落地(零依賴,供 tools_artifacts / audio_finalize 匯入)。

下載直接寫最終路徑會就地覆寫:重生中斷就留下 partial file 頂替原本完整的產物,而
manifest 仍指向同一路徑,publish 的「存在且非空」檢查也抓不到(EP36 的簡報/講義固定
下載到 ep{n:02d}-slides.pdf / -report.md,重生期間 publish 可能讀到半份)。所以一律
temp → 驗 → fsync → os.replace:並發讀者只會看到「舊的完整版」或「新的完整版」。
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Awaitable, Callable


def fsync_parent(path: str) -> None:
    """把目錄項本身刷下去——os.replace 之後才算真的換完(斷電也不會退回舊 dirent)。"""
    directory_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def download_atomically(
    final_path: str,
    download: Callable[[str], Awaitable[object]],
    validate: Callable[[str], None],
) -> None:
    """把 ``download`` 的產物原子換上 ``final_path``;任何失敗都不動既有檔案。

    ``download`` 收到 temp 路徑,``validate`` 在 replace 之前對 temp 檔跑格式檢查
    (空檔已先擋)。temp 刻意建在 ``final_path`` 同目錄——``os.replace`` 只在同一
    filesystem 上才是原子操作。"""
    directory = os.path.dirname(os.path.abspath(final_path)) or "."
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(final_path)}.", suffix=".part"
    )
    os.close(fd)
    try:
        await download(temp_path)
        if os.path.getsize(temp_path) <= 0:
            raise ValueError(f"downloaded file is empty: {final_path}")
        validate(temp_path)
        with open(temp_path, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        fsync_parent(final_path)
    except BaseException:
        # 清掉 partial;清理本身失敗不得蓋掉真正的錯誤(CancelledError 也要清,故用
        # BaseException)。
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
