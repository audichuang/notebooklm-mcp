"""提供 ``series_manifest.json`` 的崩潰安全儲存。

對外只提供讀取與加鎖的 read-modify-write；schema、CAS、鎖與原子寫入細節皆封裝於此。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ._atomic import _DIR_FSYNC_UNSUPPORTED, _NEW_FILE_MODE
from ._atomic import fsync_parent as _fsync_parent


class ManifestConflictError(RuntimeError):
    """caller 讀取預期 revision 後，manifest 已被其他 writer 更新。"""


class ManifestStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")

    def read(self) -> dict[str, Any]:
        """在持有同路徑檔案鎖期間，回傳通過驗證的 snapshot。"""
        with self._locked():
            return _migrate(self._load())

    def update(
        self,
        mutator: Callable[[dict[str, Any]], Any],
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """原子修改最新 snapshot，並回傳 snapshot 與 mutator 結果。"""
        with self._locked():
            current = self._load()
            revision = _revision(current)
            if expected_revision is not None and expected_revision != revision:
                raise ManifestConflictError(
                    f"manifest revision conflict: expected {expected_revision}, found {revision}"
                )

            candidate = _migrate(current)
            result = mutator(candidate)
            candidate["revision"] = revision + 1
            _validate(candidate, self.path, on_write=True)
            self._write(candidate)
            return copy.deepcopy(candidate), result

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 2, "revision": 0, "episodes": []}
        try:
            with self.path.open(encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"manifest is corrupt and was not modified: {self.path}") from exc
        _validate(manifest, self.path)
        return manifest

    def _write(self, manifest: dict[str, Any]) -> None:
        encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        fd, temporary_path = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        replaced = False
        try:
            with os.fdopen(fd, "wb") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.flush()
                # mkstemp 給 0600,os.replace 會把 temp 的 mode 一起帶到最終檔——沿用既有
                # manifest 的 mode(手動 chmod 644 後不會被下一次 update 打回 0600),
                # 首次寫入借用 `_NEW_FILE_MODE` 這個常數,但維持 0644 的理由是我們自己的:
                # manifest 雖是內部狀態,`publish_series` 附件路徑查詢與人工 QA 本來就要用
                # 另一個 process／帳號讀它,0600 只會讓那些讀取直接撞 PermissionError——
                # 不是 `_atomic._NEW_FILE_MODE` docstring 講的「產物跨帳號可讀」那個理由,
                # 這裡只是借用同一個數值常數,不借它的敘事。
                try:
                    mode = stat.S_IMODE(os.stat(self.path).st_mode)
                except FileNotFoundError:
                    mode = _NEW_FILE_MODE
                os.fchmod(temporary_file.fileno(), mode)
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            replaced = True
        finally:
            if not replaced:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

        # os.replace 是 commit point:上面成功後 manifest 已是新 revision。directory fsync
        # 只保證「這個新目錄項在斷電後也還在」,是額外的 crash-durability,不是提交本身。
        # 把它留在上面的 try 內會讓「檔案已換好、只是目錄 fsync 不支援(NAS/overlay mount
        # 回 EINVAL/ENOTSUP)」被當成整個 update 失敗往外拋——呼叫端會據此 rollback(例如
        # generation_input 的 binding),造成「manifest 有新 attempt、binding 卻被刪掉」的
        # 分裂。故移出 try 並容忍那組 errno(對齊 _atomic.download_atomically / audio_finalize
        # 的 post-commit 處理);其他 errno 才往外拋。
        try:
            _fsync_parent(str(self.path))
        except OSError as exc:
            if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
                raise


def _revision(manifest: dict[str, Any]) -> int:
    revision = manifest.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("manifest revision must be a non-negative integer")
    return revision


def _migrate(manifest: dict[str, Any]) -> dict[str, Any]:
    migrated = copy.deepcopy(manifest)
    migrated["schema_version"] = 2
    migrated.setdefault("revision", 0)
    return migrated


def _validate(manifest: Any, path: Path, *, on_write: bool = False) -> None:
    """``on_write=True`` 才跑「只擋新寫入」的規則。

    notebook 一致性就是這種規則:v0.5.0 之前 `_create_audio_attempt` 完全沒擋,線上很可能
    已經存在 episode/attempt 身分分裂的 manifest。若讀取端也驗,那些 manifest 每一次
    `read()` 都會 raise「manifest is corrupt」——連 `podcast_attempt_retract`(本該是修復
    正門)與 `scripts/backfill_published_at.py` 都打不開,唯一出路變成 ADR-0009 明令禁止的
    手改 JSON。所以讀取保持寬鬆(壞資料讀得進來、才修得掉),寫入端 fail-closed 擋住新的
    分裂。"""
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest is corrupt: top level must be an object: {path}")

    version = manifest.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2):
        raise ValueError(f"manifest schema_version must be 1 or 2: {path}")
    if version == 2 and "revision" not in manifest:
        raise ValueError(f"schema v2 manifest requires revision: {path}")
    _revision(manifest)

    episodes = manifest.get("episodes")
    if not isinstance(episodes, list):
        raise ValueError(f"manifest is corrupt: episodes must be a list: {path}")

    episode_numbers: set[int] = set()
    attempt_ids: set[str] = set()
    for episode in episodes:
        if not isinstance(episode, dict):
            raise ValueError(f"manifest is corrupt: every episode must be an object: {path}")
        episode_number = episode.get("episode")
        if (
            not isinstance(episode_number, int)
            or isinstance(episode_number, bool)
            or episode_number < 1
        ):
            raise ValueError(f"manifest is corrupt: episode must be an integer >= 1: {path}")
        if episode_number in episode_numbers:
            raise ValueError(f"manifest is corrupt: duplicate episode {episode_number}: {path}")
        episode_numbers.add(episode_number)

        attempts = episode.get("attempts", [])
        if not isinstance(attempts, list):
            raise ValueError(f"manifest is corrupt: attempts must be a list: {path}")
        # 只驗 episode ↔ attempts 的 notebook 是否互相一致——**不要**拿 manifest 級
        # notebook_id 來比,這份 manifest 支援每集不同 notebook,manifest 級只是預設值
        # (見 tools_podcast._create_audio_attempt 的 setdefault)。
        episode_notebook_id = episode.get("notebook_id")
        local_attempt_ids: set[str] = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError(f"manifest is corrupt: every attempt must be an object: {path}")
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise ValueError(f"manifest is corrupt: attempt_id must be non-empty: {path}")
            if attempt_id in attempt_ids:
                raise ValueError(f"manifest is corrupt: duplicate attempt_id {attempt_id}: {path}")
            attempt_episode = attempt.get("episode", episode_number)
            if (
                not isinstance(attempt_episode, int)
                or isinstance(attempt_episode, bool)
                or attempt_episode != episode_number
            ):
                raise ValueError(
                    f"manifest is corrupt: attempt {attempt_id} belongs to another episode: {path}"
                )
            # retract 的 tombstone 是歷史紀錄,允許保留它建立時綁的舊 notebook_id,
            # 不參與這道一致性檢查(否則 retract 之後想切換 notebook 重生就寫不進去)。
            attempt_notebook_id = attempt.get("notebook_id")
            if (
                on_write
                and not attempt.get("retraction")
                and episode_notebook_id is not None
                and attempt_notebook_id is not None
                and episode_notebook_id != attempt_notebook_id
            ):
                raise ValueError(
                    f"manifest is corrupt: episode {episode_number} notebook_id "
                    f"does not match attempt {attempt_id} notebook_id: {path}"
                )
            attempt_ids.add(attempt_id)
            local_attempt_ids.add(attempt_id)

        retracted_ids = {
            attempt["attempt_id"]
            for attempt in attempts
            if isinstance(attempt, dict) and attempt.get("retraction")
        }
        for pointer in ("active_attempt_id", "output_attempt_id"):
            value = episode.get(pointer)
            if value is not None and (
                not isinstance(value, str) or value not in local_attempt_ids
            ):
                raise ValueError(
                    f"manifest is corrupt: {pointer} does not reference this episode: {path}"
                )
            # 寫入時的最後一道背壩:任何 writer(含手改)把指標指回已作廢的 attempt
            # 就是把被拒收的輸出復活,寧可讓那次寫入失敗。
            if value in retracted_ids:
                raise ValueError(
                    f"manifest is corrupt: {pointer} points at retracted attempt {value}: {path}"
                )
