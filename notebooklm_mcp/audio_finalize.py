"""Manifest-backed、可重入的 audio finalize。

每個遠端／本機 side effect 都先 checkpoint，再以可觀察 postcondition 決定
是否需要補做。這個 module 不負責 generation submit，也不改 compatibility
projection；caller 只在本函式完整成功後才 promotion。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Callable

from notebooklm.types import ArtifactType

from ._status import TerminalGenerationError, ensure_completed
from .manifest_store import ManifestStore

_SOURCE_CLOCK_SKEW = timedelta(minutes=1)
_SOURCE_DISPATCH_WINDOW = timedelta(minutes=11)
_TZ = timezone(timedelta(hours=8))


def new_finalize_state() -> dict:
    """回傳每個 attempt 各自擁有的 finalize 初始狀態。"""
    return {
        "artifact_rename": {"status": "not_started"},
        "download": {
            "status": "not_started",
            "path": None,
            "bytes": None,
            "sha256": None,
            "temp_path": None,
        },
        "feedback_source_upload": {
            "status": "not_started",
            "source_ids_before": [],
            "source_id": None,
            "dispatched_at": None,
            "expected_title": None,
            "candidate_source_ids": [],
        },
        "feedback_source_rename": {
            "status": "not_started",
            "source_name": None,
            "verified_at": None,
        },
    }


def _record(manifest: dict, episode_n: int, attempt_id: str) -> tuple[dict, dict]:
    episode = next(
        (row for row in manifest["episodes"] if row.get("episode") == episode_n),
        None,
    )
    if episode is None:
        raise ValueError(f"episode {episode_n} is missing from the manifest")
    attempt = next(
        (
            row
            for row in episode.get("attempts", [])
            if row.get("attempt_id") == attempt_id
        ),
        None,
    )
    if attempt is None:
        raise ValueError(f"attempt {attempt_id!r} is missing from episode {episode_n}")
    return episode, attempt


def _ensure_finalize(store: ManifestStore, episode_n: int, attempt_id: str) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _record(manifest, episode_n, attempt_id)
        attempt.setdefault("finalize", new_finalize_state())

    store.update(mutate)


def _mutate(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    callback: Callable[[dict, dict], None],
) -> None:
    def mutate(manifest: dict) -> None:
        episode, attempt = _record(manifest, episode_n, attempt_id)
        attempt.setdefault("finalize", new_finalize_state())
        callback(episode, attempt)

    store.update(mutate)


def _subject(
    store: ManifestStore, episode_n: int, attempt_id: str
) -> tuple[dict, dict]:
    snapshot = store.read()
    return _record(snapshot, episode_n, attempt_id)


def _claimed_source_ids(manifest: dict, excluding_attempt_id: str) -> set[str]:
    claimed: set[str] = set()
    for episode in manifest["episodes"]:
        for attempt in episode.get("attempts", []):
            if attempt.get("attempt_id") == excluding_attempt_id:
                continue
            finalize = attempt.get("finalize", {})
            if not isinstance(finalize, dict):
                continue
            upload = finalize.get("feedback_source_upload", {})
            if not isinstance(upload, dict):
                continue
            source_id = upload.get("source_id")
            if isinstance(source_id, str) and source_id:
                claimed.add(source_id)
    return claimed


def _kind_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _source_ready(source: object) -> bool:
    ready = getattr(source, "is_ready", None)
    if isinstance(ready, bool):
        return ready
    status = getattr(source, "status", None)
    status_value = _kind_value(status)
    return status_value in ("ready", "completed") if status_value else False


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: str, expected_size: int, expected_sha256: str) -> bool:
    try:
        if os.path.getsize(path) != expected_size:
            return False
        digest = _sha256_file(path)
    except OSError:
        return False
    return digest == expected_sha256


def _fsync_parent(path: str) -> None:
    directory_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _completed_output(
    episode: dict, attempt: dict, episode_n: int, attempt_id: str
) -> dict | None:
    finalize = attempt["finalize"]
    statuses = (
        finalize["artifact_rename"]["status"],
        finalize["download"]["status"],
        finalize["feedback_source_upload"]["status"],
        finalize["feedback_source_rename"]["status"],
    )
    if statuses != ("completed", "completed", "completed", "completed"):
        return None
    artifact_id = attempt["remote"].get("artifact_id")
    path = finalize["download"].get("path")
    if not artifact_id or not path:
        raise ValueError("completed finalize is missing artifact_id or download path")
    return {
        "episode": episode_n,
        "title": attempt["title"],
        "label": f"EP{episode_n:02d} {attempt['title'].strip()}",
        "task_id": artifact_id,
        "artifact_id": artifact_id,
        "mp3_path": path,
        "published_at": episode.get("published_at")
        or format_datetime(datetime.now(_TZ)),
        "attempt_id": attempt_id,
        "feedback_source_id": finalize["feedback_source_upload"]["source_id"],
    }


async def _reconcile_source_upload(
    client: object,
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    notebook_id: str,
) -> str:
    snapshot = store.read()
    _, attempt = _record(snapshot, episode_n, attempt_id)
    upload = attempt["finalize"]["feedback_source_upload"]
    baseline = set(upload.get("source_ids_before") or [])
    expected_title = upload.get("expected_title")
    dispatched_raw = upload.get("dispatched_at")
    if not isinstance(expected_title, str) or not expected_title:
        raise ValueError("feedback source expected title is missing")
    if not isinstance(dispatched_raw, str):
        raise ValueError("feedback source dispatch time is missing")
    dispatched_at = datetime.fromisoformat(dispatched_raw)
    if dispatched_at.tzinfo is None:
        raise ValueError("feedback source dispatch time must include timezone")
    dispatched_at = dispatched_at.astimezone(timezone.utc)
    claimed = _claimed_source_ids(snapshot, attempt_id)
    sources = await client.sources.list(notebook_id)
    candidates: list[str] = []
    for source in sources:
        source_id = getattr(source, "id", None)
        created_at = getattr(source, "created_at", None)
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in baseline
            or source_id in claimed
            or _kind_value(getattr(source, "kind", None)) != "media"
            or getattr(source, "title", None) != expected_title
            or not isinstance(created_at, datetime)
            or created_at.tzinfo is None
        ):
            continue
        created_at = created_at.astimezone(timezone.utc)
        if (
            dispatched_at - _SOURCE_CLOCK_SKEW
            <= created_at
            <= dispatched_at + _SOURCE_DISPATCH_WINDOW
        ):
            candidates.append(source_id)
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        source_id = candidates[0]

        # CAS 式重新檢查由 ManifestStore.update 的鎖定 read-modify-write 完成。
        def mutate(manifest: dict) -> None:
            _, current = _record(manifest, episode_n, attempt_id)
            if source_id in _claimed_source_ids(manifest, attempt_id):
                raise ValueError(
                    f"feedback source {source_id!r} was claimed during reconciliation"
                )
            current_upload = current["finalize"]["feedback_source_upload"]
            if current_upload.get("source_id") not in (None, source_id):
                raise ValueError("attempt already maps to another feedback source")
            current_upload.update(
                {
                    "status": "accepted",
                    "source_id": source_id,
                    "candidate_source_ids": [],
                }
            )

        store.update(mutate)
        return source_id

    def unresolved(_episode: dict, current: dict) -> None:
        current_upload = current["finalize"]["feedback_source_upload"]
        current_upload["candidate_source_ids"] = candidates
        current_upload["status"] = (
            "reconciliation_ambiguous" if candidates else "acceptance_unknown"
        )

    _mutate(store, episode_n, attempt_id, unresolved)
    if candidates:
        raise RuntimeError(
            "feedback source reconciliation is ambiguous: "
            + ", ".join(candidates)
        )
    raise RuntimeError(
        "feedback source acceptance remains unknown; wait and resume the same attempt"
    )


async def _feedback_source_verified(
    client: object,
    notebook_id: str,
    source_id: str,
    label: str,
) -> bool:
    sources = await client.sources.list(notebook_id)
    matches = [
        source
        for source in sources
        if getattr(source, "id", None) == source_id
        and getattr(source, "title", None) == label
        and _kind_value(getattr(source, "kind", None)) == "media"
        and _source_ready(source)
    ]
    return len(matches) == 1


async def finalize_attempt(
    client: object,
    store: ManifestStore,
    *,
    episode_n: int,
    attempt_id: str,
    output_dir: str,
    wait_timeout: float,
) -> dict:
    """從 durable checkpoint 補完一個已 accepted 的 audio attempt。"""
    _ensure_finalize(store, episode_n, attempt_id)
    episode, attempt = _subject(store, episode_n, attempt_id)
    notebook_id = attempt.get("notebook_id")
    artifact_id = attempt.get("remote", {}).get("artifact_id")
    if not isinstance(notebook_id, str) or not notebook_id:
        raise ValueError("attempt notebook_id is missing")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("attempt has no accepted artifact_id")
    title = attempt.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("attempt title is missing")
    label = f"EP{episode_n:02d} {title.strip()}"

    completed = _completed_output(episode, attempt, episode_n, attempt_id)
    if completed is not None:
        download = attempt["finalize"]["download"]
        if (
            isinstance(download.get("path"), str)
            and isinstance(download.get("bytes"), int)
            and isinstance(download.get("sha256"), str)
            and _file_matches(
                download["path"], download["bytes"], download["sha256"]
            )
        ):
            source_id = attempt["finalize"]["feedback_source_upload"].get(
                "source_id"
            )
            if (
                isinstance(source_id, str)
                and await _feedback_source_verified(
                    client, notebook_id, source_id, label
                )
            ):
                return completed

    os.makedirs(output_dir, exist_ok=True)
    stored_download_path = attempt["finalize"]["download"].get("path")
    if isinstance(stored_download_path, str) and stored_download_path:
        mp3_path = stored_download_path
    else:
        prior_output_attempt_id = episode.get("output_attempt_id")
        has_prior_output = prior_output_attempt_id not in (None, attempt_id) or (
            prior_output_attempt_id is None
            and isinstance(episode.get("artifact_id"), str)
            and isinstance(episode.get("mp3_path"), str)
        )
        if has_prior_output:
            if os.path.basename(attempt_id) != attempt_id or attempt_id in (".", ".."):
                raise ValueError("attempt_id cannot be used as a path component")
            attempt_dir = os.path.join(output_dir, "attempts", attempt_id)
            os.makedirs(attempt_dir, exist_ok=True)
            mp3_path = os.path.abspath(
                os.path.join(attempt_dir, f"ep{episode_n:02d}.mp3")
            )
        else:
            mp3_path = os.path.abspath(
                os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
            )

    if attempt["remote"].get("status") != "completed":
        final = await client.artifacts.wait_for_completion(
            notebook_id, artifact_id, timeout=wait_timeout
        )
        try:
            ensure_completed(final)
        except TerminalGenerationError as error:
            terminal_status = (
                "removed" if getattr(final, "is_removed", False) else "failed"
            )

            def remote_terminal(_episode: dict, current: dict) -> None:
                current["remote"].update(
                    {
                        "status": terminal_status,
                        "status_origin": "remote",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "error": str(error),
                    }
                )

            _mutate(store, episode_n, attempt_id, remote_terminal)
            raise

        def remote_completed(_episode: dict, current: dict) -> None:
            current["remote"].update(
                {
                    "status": "completed",
                    "status_origin": "remote",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        _mutate(store, episode_n, attempt_id, remote_completed)

    _, attempt = _subject(store, episode_n, attempt_id)
    rename = attempt["finalize"]["artifact_rename"]
    if rename["status"] in ("dispatching", "outcome_unknown"):
        artifacts = await client.artifacts.list(
            notebook_id, artifact_type=ArtifactType.AUDIO
        )
        landed = any(
            getattr(artifact, "id", None) == artifact_id
            and getattr(artifact, "title", None) == label
            for artifact in artifacts
        )
        if not landed:
            raise RuntimeError(
                f"artifact rename outcome is unknown for {artifact_id!r}; "
                "remote title postcondition is not satisfied"
            )

        def adopt_rename(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "completed"

        _mutate(store, episode_n, attempt_id, adopt_rename)
        _, attempt = _subject(store, episode_n, attempt_id)
        rename = attempt["finalize"]["artifact_rename"]

    if rename["status"] != "completed":
        def rename_dispatching(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "dispatching"

        _mutate(store, episode_n, attempt_id, rename_dispatching)
        try:
            await client.artifacts.rename(
                notebook_id, artifact_id, label, return_object=False
            )
        except Exception:
            def rename_unknown(_episode: dict, current: dict) -> None:
                current["finalize"]["artifact_rename"]["status"] = "outcome_unknown"

            _mutate(store, episode_n, attempt_id, rename_unknown)
            raise

        def rename_completed(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "completed"

        _mutate(store, episode_n, attempt_id, rename_completed)

    _, attempt = _subject(store, episode_n, attempt_id)
    download = attempt["finalize"]["download"]
    download_complete = (
        download["status"] == "completed"
        and isinstance(download.get("bytes"), int)
        and isinstance(download.get("sha256"), str)
        and isinstance(download.get("path"), str)
        and _file_matches(
            download["path"], download["bytes"], download["sha256"]
        )
    )
    if not download_complete:
        fd, temp_path = tempfile.mkstemp(
            dir=output_dir, prefix=f".ep{episode_n:02d}.", suffix=".part"
        )
        os.close(fd)

        def download_dispatching(_episode: dict, current: dict) -> None:
            current["finalize"]["download"].update(
                {"status": "dispatching", "temp_path": temp_path}
            )

        _mutate(store, episode_n, attempt_id, download_dispatching)
        try:
            await client.artifacts.download_audio(
                notebook_id, temp_path, artifact_id
            )
            size = os.path.getsize(temp_path)
            if size <= 0:
                raise ValueError("downloaded audio is empty")
            digest = _sha256_file(temp_path)
            with open(temp_path, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_path, mp3_path)
            _fsync_parent(mp3_path)
        except Exception:
            def download_failed(_episode: dict, current: dict) -> None:
                current["finalize"]["download"].update(
                    {"status": "failed", "temp_path": temp_path}
                )

            _mutate(store, episode_n, attempt_id, download_failed)
            raise

        def download_completed(_episode: dict, current: dict) -> None:
            current["finalize"]["download"].update(
                {
                    "status": "completed",
                    "path": mp3_path,
                    "bytes": size,
                    "sha256": digest,
                    "temp_path": None,
                }
            )

        _mutate(store, episode_n, attempt_id, download_completed)

    _, attempt = _subject(store, episode_n, attempt_id)
    upload = attempt["finalize"]["feedback_source_upload"]
    source_id = upload.get("source_id")
    if isinstance(source_id, str) and source_id:
        sources = await client.sources.list(notebook_id)
        if not any(getattr(source, "id", None) == source_id for source in sources):
            raise RuntimeError(
                f"saved feedback source {source_id!r} no longer exists"
            )
    elif upload["status"] in (
        "dispatching",
        "acceptance_unknown",
        "reconciliation_ambiguous",
    ):
        source_id = await _reconcile_source_upload(
            client, store, episode_n, attempt_id, notebook_id
        )
    else:
        sources = await client.sources.list(notebook_id)
        baseline = [
            source.id
            for source in sources
            if isinstance(getattr(source, "id", None), str)
        ]
        dispatched_at = datetime.now(timezone.utc).isoformat()
        expected_title = os.path.basename(mp3_path)

        def claim_upload(manifest: dict) -> bool:
            _, current = _record(manifest, episode_n, attempt_id)
            current_upload = current["finalize"]["feedback_source_upload"]
            if current_upload["status"] != "not_started":
                return False
            current_upload.update(
                {
                    "status": "dispatching",
                    "source_ids_before": baseline,
                    "dispatched_at": dispatched_at,
                    "expected_title": expected_title,
                    "candidate_source_ids": [],
                }
            )
            return True

        _, owns_upload = store.update(claim_upload)
        if not owns_upload:
            source_id = await _reconcile_source_upload(
                client, store, episode_n, attempt_id, notebook_id
            )
        else:
            try:
                source = await client.sources.add_file(
                    notebook_id,
                    mp3_path,
                    mime_type="audio/mpeg",
                    wait=True,
                    wait_timeout=600.0,
                )
            except Exception:
                def upload_unknown(_episode: dict, current: dict) -> None:
                    current["finalize"]["feedback_source_upload"][
                        "status"
                    ] = "acceptance_unknown"

                _mutate(store, episode_n, attempt_id, upload_unknown)
                raise
            source_id = getattr(source, "id", None)
            if not isinstance(source_id, str) or not source_id:
                raise RuntimeError("feedback source upload returned no source id")

            def upload_accepted(_episode: dict, current: dict) -> None:
                current["finalize"]["feedback_source_upload"].update(
                    {"status": "accepted", "source_id": source_id}
                )

            _mutate(store, episode_n, attempt_id, upload_accepted)

    _, attempt = _subject(store, episode_n, attempt_id)
    source_rename = attempt["finalize"]["feedback_source_rename"]
    sources = await client.sources.list(notebook_id)
    source = next(
        (row for row in sources if getattr(row, "id", None) == source_id), None
    )
    if source is None:
        raise RuntimeError(f"feedback source {source_id!r} cannot be verified")
    already_named = getattr(source, "title", None) == label and _source_ready(source)
    if source_rename["status"] != "completed" and not already_named:
        def source_rename_dispatching(_episode: dict, current: dict) -> None:
            current["finalize"]["feedback_source_rename"][
                "status"
            ] = "dispatching"

        _mutate(store, episode_n, attempt_id, source_rename_dispatching)
        try:
            await client.sources.rename(
                notebook_id, source_id, label, return_object=False
            )
        except Exception:
            check = await client.sources.list(notebook_id)
            landed = next(
                (
                    row
                    for row in check
                    if getattr(row, "id", None) == source_id
                    and getattr(row, "title", None) == label
                    and _source_ready(row)
                ),
                None,
            )
            if landed is None:
                def source_rename_unknown(_episode: dict, current: dict) -> None:
                    current["finalize"]["feedback_source_rename"][
                        "status"
                    ] = "outcome_unknown"

                _mutate(store, episode_n, attempt_id, source_rename_unknown)
                raise
    check = await client.sources.list(notebook_id)
    verified = next(
        (
            row
            for row in check
            if getattr(row, "id", None) == source_id
            and getattr(row, "title", None) == label
            and _source_ready(row)
        ),
        None,
    )
    if verified is None:
        raise RuntimeError(
            f"feedback source {source_id!r} postcondition is not satisfied"
        )

    def source_completed(_episode: dict, current: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        current["finalize"]["feedback_source_upload"]["status"] = "completed"
        current["finalize"]["feedback_source_rename"].update(
            {
                "status": "completed",
                "source_name": label,
                "verified_at": now,
            }
        )

    _mutate(store, episode_n, attempt_id, source_completed)
    episode, attempt = _subject(store, episode_n, attempt_id)
    completed = _completed_output(episode, attempt, episode_n, attempt_id)
    if completed is None:
        raise RuntimeError("finalize postconditions are incomplete")
    return completed
