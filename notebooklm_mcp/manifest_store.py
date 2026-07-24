"""提供 ``series_manifest.json`` 的崩潰安全儲存。

對外只提供讀取與加鎖的 read-modify-write；schema、CAS、鎖與原子寫入細節皆封裝於此。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


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
            _validate(candidate, self.path)
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
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            replaced = True
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if not replaced:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass


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


def _validate(manifest: Any, path: Path) -> None:
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
            attempt_ids.add(attempt_id)
            local_attempt_ids.add(attempt_id)

        for pointer in ("active_attempt_id", "output_attempt_id"):
            value = episode.get(pointer)
            if value is not None and (
                not isinstance(value, str) or value not in local_attempt_ids
            ):
                raise ValueError(
                    f"manifest is corrupt: {pointer} does not reference this episode: {path}"
                )
