"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from notebooklm.types import ArtifactType

from . import runtime
from ._status import TerminalGenerationError, ensure_completed, ensure_started
from .audio_finalize import finalize_attempt, new_finalize_state
from .app import mcp
from .auth_probe import probe_auth
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .manifest_store import ManifestStore

_TZ = timezone(timedelta(hours=8))
_RECONCILIATION_CLOCK_SKEW = timedelta(minutes=1)


def _episode_label(episode_n: int, title: str) -> str:
    """Unified name for BOTH the Studio artifact and the self-uploaded source:
    ``EP{n:02d} {title}`` (e.g. ``EP01 心法篇``). The EP prefix keeps ordering /
    resume / reject-then-delete addressable; the title makes it human-legible.
    Both sides use this identical string (the unified-naming iron rule)."""
    return f"EP{episode_n:02d} {title.strip()}"


def _attempt_record(manifest: dict, episode_n: int, attempt_id: str) -> tuple[dict, dict]:
    episode = next(
        (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
        None,
    )
    if episode is None:
        raise ValueError(f"episode {episode_n} is missing from the manifest")
    attempt = next(
        (row for row in episode.get("attempts", []) if row.get("attempt_id") == attempt_id),
        None,
    )
    if attempt is None:
        raise ValueError(f"attempt {attempt_id!r} is missing from episode {episode_n}")
    return episode, attempt


def _create_audio_attempt(
    store: ManifestStore,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    language: str,
    audio_format: str | None,
    audio_length: str | None,
) -> str:
    attempt_id = str(uuid.uuid4())
    attempt = {
        "attempt_id": attempt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notebook_id": notebook_id,
        "episode": episode_n,
        "title": title.strip(),
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "settings": {
            "language": language,
            "audio_format": audio_format,
            "audio_length": audio_length,
        },
        "dispatch": {
            "status": "prepared",
            "artifact_ids_before": [],
            "dispatched_at": None,
            "accepted_at": None,
        },
        "remote": {
            "artifact_id": None,
            "status": "unknown",
            "status_origin": None,
            "observed_at": None,
            "error": None,
            "error_code": None,
        },
        "finalize": new_finalize_state(),
        "errors": [],
    }

    def mutate(manifest: dict) -> None:
        manifest.setdefault("notebook_id", notebook_id)
        episode = next(
            (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
            None,
        )
        if episode is not None:
            active_attempt_id = episode.get("active_attempt_id")
            if active_attempt_id:
                raise ValueError(
                    f"episode {episode_n} already has durable active attempt "
                    f"{active_attempt_id!r}; reconcile or resume it before creating "
                    "another attempt"
                )
            if episode.get("output_attempt_id") or (
                episode.get("artifact_id") and episode.get("mp3_path")
            ):
                raise ValueError(
                    f"episode {episode_n} already has a durable output; "
                    "P0 does not support implicit regeneration"
                )
        if episode is None:
            episode = {"episode": episode_n, "attempts": []}
            manifest["episodes"].append(episode)
        episode.setdefault("title", title.strip())
        episode.setdefault("label", _episode_label(episode_n, title))
        episode.setdefault("notebook_id", notebook_id)
        episode.setdefault("attempts", []).append(attempt)
        episode["active_attempt_id"] = attempt_id

    store.update(mutate)
    return attempt_id


def _ensure_resume_attempt(
    store: ManifestStore,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
) -> str:
    """以 caller 已知的 artifact identity 找回或建立 durable resume attempt。"""

    def mutate(manifest: dict) -> str:
        manifest.setdefault("notebook_id", notebook_id)
        claimed = [
            (ep, attempt)
            for ep in manifest["episodes"]
            for attempt in ep.get("attempts", [])
            if attempt.get("remote", {}).get("artifact_id") == artifact_id
        ]
        if len(claimed) > 1:
            raise ValueError(
                f"artifact {artifact_id!r} is claimed by multiple attempts"
            )
        if claimed:
            episode, attempt = claimed[0]
            if episode.get("episode") != episode_n:
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another episode"
                )
            if attempt.get("notebook_id") != notebook_id:
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another notebook"
                )
            if attempt.get("title") != title.strip():
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another title"
                )
            current_active_id = episode.get("active_attempt_id")
            if (
                current_active_id
                and current_active_id != attempt["attempt_id"]
                and current_active_id != episode.get("output_attempt_id")
            ):
                raise ValueError(
                    f"episode {episode_n} has another active attempt "
                    f"{current_active_id!r}; resume it before switching artifacts"
                )
            attempt.setdefault("finalize", new_finalize_state())
            episode["active_attempt_id"] = attempt["attempt_id"]
            return attempt["attempt_id"]

        episode = next(
            (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
            None,
        )
        if episode is None:
            episode = {"episode": episode_n, "attempts": []}
            manifest["episodes"].append(episode)
        active_attempt_id = episode.get("active_attempt_id")
        if active_attempt_id and active_attempt_id != episode.get(
            "output_attempt_id"
        ):
            raise ValueError(
                f"episode {episode_n} has active attempt {active_attempt_id!r}; "
                "reconcile or resume that attempt before supplying another artifact"
            )
        existing_notebook = episode.get("notebook_id")
        if existing_notebook not in (None, notebook_id):
            raise ValueError(
                f"episode {episode_n} belongs to another notebook"
            )
        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        attempt = {
            "attempt_id": attempt_id,
            "created_at": now,
            "notebook_id": notebook_id,
            "episode": episode_n,
            "title": title.strip(),
            "brief_sha256": None,
            "settings": {"origin": "explicit_resume"},
            "dispatch": {
                "status": "accepted",
                "artifact_ids_before": [],
                "dispatched_at": None,
                "accepted_at": now,
            },
            "remote": {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "caller_verified",
                "observed_at": now,
                "error": None,
                "error_code": None,
            },
            "finalize": new_finalize_state(),
            "errors": [],
        }
        episode.setdefault("title", title.strip())
        episode.setdefault("label", _episode_label(episode_n, title))
        episode["notebook_id"] = notebook_id
        episode.setdefault("attempts", []).append(attempt)
        episode["active_attempt_id"] = attempt_id
        return attempt_id

    _, attempt_id = store.update(mutate)
    return attempt_id


def _claim_prepared_dispatch(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_ids: list[str],
) -> bool:
    """原子保存 baseline 並取得 prepared attempt 的 dispatch ownership。"""

    def mutate(manifest: dict) -> bool:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        dispatch = attempt["dispatch"]
        if dispatch["status"] != "prepared":
            return False
        dispatch.update(
            {
                "status": "dispatching",
                "artifact_ids_before": artifact_ids,
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return True

    _, claimed = store.update(mutate)
    return claimed


def _mark_acceptance_unknown(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    error: BaseException,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if attempt["dispatch"]["status"] != "dispatching":
            return
        attempt["dispatch"]["status"] = "acceptance_unknown"
        attempt["errors"].append(
            {
                "phase": "dispatch",
                "type": type(error).__name__,
                "message": str(error),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    store.update(mutate)


def _mark_not_accepted(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    status: object,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if attempt["dispatch"]["status"] != "dispatching":
            return
        now = datetime.now(timezone.utc).isoformat()
        error = getattr(status, "error", None)
        error_code = getattr(status, "error_code", None)
        attempt["dispatch"]["status"] = "not_accepted"
        attempt["remote"].update(
            {
                "status": "failed",
                "observed_at": now,
                "error": error,
                "error_code": error_code,
            }
        )
        attempt["errors"].append(
            {
                "phase": "dispatch",
                "type": "not_accepted",
                "message": error or error_code or getattr(status, "status", "failed"),
                "recorded_at": now,
            }
        )

    store.update(mutate)


def _claimed_artifact_ids(manifest: dict, excluding_attempt_id: str) -> set[str]:
    claimed: set[str] = set()
    for episode in manifest["episodes"]:
        for attempt in episode.get("attempts", []):
            if attempt.get("attempt_id") == excluding_attempt_id:
                continue
            remote = attempt.get("remote", {})
            if not isinstance(remote, dict):
                raise ValueError("attempt remote state must be an object")
            artifact_id = remote.get("artifact_id")
            if artifact_id is None:
                continue
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("claimed remote artifact_id must be a non-empty string")
            claimed.add(artifact_id)
    return claimed


def _reconciliation_subject(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
) -> tuple[dict, str, set[str], datetime]:
    episode, attempt = _attempt_record(manifest, episode_n, attempt_id)
    notebook_id = attempt.get("notebook_id")
    if not isinstance(notebook_id, str) or not notebook_id:
        raise ValueError("attempt notebook_id must be a non-empty string")
    if episode.get("notebook_id") not in (None, notebook_id):
        raise ValueError("episode notebook_id does not match the attempt")

    dispatch = attempt.get("dispatch")
    if not isinstance(dispatch, dict):
        raise ValueError("attempt dispatch state must be an object")
    baseline = dispatch.get("artifact_ids_before")
    if not isinstance(baseline, list) or any(
        not isinstance(value, str) or not value for value in baseline
    ):
        raise ValueError("attempt artifact baseline must be a list of ids")
    dispatched_at = dispatch.get("dispatched_at")
    if not isinstance(dispatched_at, str):
        raise ValueError("attempt dispatched_at is required for reconciliation")
    try:
        dispatched = datetime.fromisoformat(dispatched_at)
    except ValueError:
        raise ValueError("attempt dispatched_at is invalid") from None
    if dispatched.tzinfo is None:
        raise ValueError("attempt dispatched_at must include a timezone")
    return attempt, notebook_id, set(baseline), dispatched.astimezone(timezone.utc)


def _bind_reconciled_artifact(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_id: str,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        remote = attempt.get("remote")
        if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
            raise ValueError("attempt already has a remote artifact mapping")
        if artifact_id in _claimed_artifact_ids(manifest, attempt_id):
            raise ValueError(f"artifact {artifact_id!r} was claimed during reconciliation")
        now = datetime.now(timezone.utc).isoformat()
        attempt["dispatch"]["status"] = "accepted"
        attempt["dispatch"]["accepted_at"] = now
        remote.update(
            {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "remote",
                "observed_at": now,
            }
        )

    store.update(mutate)


def _mark_reconciliation_ambiguous(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    candidate_ids: list[str],
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        remote = attempt.get("remote")
        if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
            raise ValueError("attempt already has a remote artifact mapping")
        if _claimed_artifact_ids(manifest, attempt_id).intersection(candidate_ids):
            raise ValueError("candidate claim changed during reconciliation")
        attempt["dispatch"]["status"] = "reconciliation_ambiguous"
        attempt["dispatch"]["candidate_artifact_ids"] = candidate_ids

    store.update(mutate)


def _bind_accepted_artifact(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_id: str,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        dispatch = attempt["dispatch"]
        if dispatch["status"] not in (
            "dispatching",
            "acceptance_unknown",
            "reconciliation_ambiguous",
            "accepted",
        ):
            raise ValueError(
                f"attempt {attempt_id!r} cannot accept an artifact from "
                f"dispatch state {dispatch['status']!r}"
            )
        remote = attempt["remote"]
        existing_artifact_id = remote.get("artifact_id")
        if existing_artifact_id not in (None, artifact_id):
            raise ValueError(
                f"attempt {attempt_id!r} is already mapped to another artifact "
                f"{existing_artifact_id!r}"
            )
        if existing_artifact_id == artifact_id:
            return
        if existing_artifact_id is None and artifact_id in _claimed_artifact_ids(
            manifest, attempt_id
        ):
            raise ValueError(f"artifact {artifact_id!r} is already claimed")
        now = datetime.now(timezone.utc).isoformat()
        attempt["dispatch"]["status"] = "accepted"
        attempt["dispatch"]["accepted_at"] = now
        attempt["remote"].update(
            {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "sdk_heuristic",
                "observed_at": now,
            }
        )

    store.update(mutate)


def _promote_attempt_output(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    output: dict,
) -> None:
    def mutate(manifest: dict) -> None:
        episode, attempt = _attempt_record(manifest, episode_n, attempt_id)
        attempt["remote"]["status"] = "completed"
        attempt["remote"]["observed_at"] = datetime.now(timezone.utc).isoformat()
        episode["artifact_id"] = output["artifact_id"]
        episode["mp3_path"] = os.path.abspath(output["mp3_path"])
        episode["output_attempt_id"] = attempt_id
        for key in ("title", "label", "published_at"):
            episode.setdefault(key, output[key])

    store.update(mutate)


def _validate_episode_args(episode_n: int, title: str, prior_mp3_path: str | None) -> None:
    """單集參數的純本地驗證(不打網路)。壞參數 ValueError 秒退——必須在
    auth 預檢之前跑,認證錯誤不得蓋掉參數錯誤。"""
    # episode_n 必須是 >= 1 的整數(bool 是 int 子類,明確擋掉):否則會生出 EP00/
    # 負集號、還燒掉一次生成 quota。podcast_series 有 start>=1 守衛,單集入口也要有。
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError(f"episode_n must be an int >= 1, got: {episode_n!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} requires a non-empty 'title'")
    if prior_mp3_path and episode_n <= 1:
        raise ValueError("prior_mp3_path requires episode_n >= 2 (there is no prior to episode 1)")


async def _finalize_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float,
) -> dict:
    """單集後半段(生成之後):等完成 → 命名 → 下載 → 自上傳回錄 → 回傳 manifest 列。

    抽成獨立函式,讓 `podcast_episode_resume` 能拿一個「已在雲端啟動」的 artifact_id
    直接續完,不重生、不燒 quota。artifact_id 就是 generate_audio 的 task_id
    (task_id ≡ artifact id;GenerationStatus 無 artifact_id 欄位)。"""
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)
    label = _episode_label(episode_n, title)

    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    # Rename the Studio artifact BEFORE downloading: name it in NotebookLM first so
    # the notebook stays legible regardless of the download outcome, then pull the mp3.
    # fire-and-forget:0.7.3 預設 return_object=True 會再抓全量清單驗證且可能
    # raise not-found;顯式 False 保留 0.4.1 語意(RPC 層錯誤仍會 raise)。
    await client.artifacts.rename(notebook_id, artifact_id, label, return_object=False)

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

    # Re-upload THIS episode's own mp3 as a source named IDENTICALLY to its Studio
    # artifact ("EP02" artifact <-> "EP02" source — same string, no suffix). This is
    # the heart of the sequential-feedback method AND keeps a complete record:
    #  - EVERY episode (including the last) ends up in Sources, name-matched to Studio.
    #  - the NEXT episode's generation automatically sees this source for continuity,
    #    so podcast_series needs no separate prior-upload step.
    own_src = await client.sources.add_file(
        notebook_id, mp3_path, mime_type="audio/mpeg", wait=True, wait_timeout=600.0
    )
    await client.sources.rename(notebook_id, own_src.id, label, return_object=False)

    return {
        "episode": episode_n,
        "title": title.strip(),
        "label": label,
        "task_id": artifact_id,
        "artifact_id": artifact_id,
        "mp3_path": mp3_path,
        "published_at": format_datetime(datetime.now(_TZ)),  # 產出時間 → 進 manifest
    }


async def _run_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None,
    language: str | None,
    audio_format: str | None,
    audio_length: str | None,
    wait_timeout: float,
    manifest_path: str | None = None,
) -> dict:
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)

    _validate_episode_args(episode_n, title, prior_mp3_path)
    resolved_language = resolve_language(language)
    resolved_audio_format = to_audio_format(audio_format)
    resolved_audio_length = to_audio_length(audio_length)

    store = ManifestStore(manifest_path) if manifest_path else None
    attempt_id = None
    if store is not None:
        attempt_id = _create_audio_attempt(
            store,
            notebook_id=notebook_id,
            episode_n=episode_n,
            title=title,
            brief=brief,
            language=resolved_language,
            audio_format=audio_format,
            audio_length=audio_length,
        )

    # Standalone continuity: if the caller hands us a prior episode's mp3 that is
    # NOT yet in the notebook (one-off podcast_episode use), upload + name it so
    # this episode can recap it. In a full podcast_series this is unnecessary —
    # each episode self-uploads at the end (below), so the prior is already there.
    if prior_mp3_path:
        prior_src = await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )
        await client.sources.rename(
            notebook_id, prior_src.id, f"EP{episode_n - 1:02d}", return_object=False
        )

    if store is not None:
        baseline = await client.artifacts.list(
            notebook_id, artifact_type=ArtifactType.AUDIO
        )
        claimed = _claim_prepared_dispatch(
            store,
            episode_n,
            attempt_id,
            [artifact.id for artifact in baseline],
        )
        if not claimed:
            raise RuntimeError(
                f"attempt {attempt_id!r} is no longer prepared; retry by its durable state"
            )

    try:
        status = await client.artifacts.generate_audio(
            notebook_id,
            language=resolved_language,
            instructions=brief,
            audio_format=resolved_audio_format,
            audio_length=resolved_audio_length,
        )
    except (Exception, asyncio.CancelledError) as exc:
        if store is not None:
            _mark_acceptance_unknown(store, episode_n, attempt_id, exc)
            exc.args = (
                f"{exc}\n生成受理結果不明(attempt_id={attempt_id!r})；"
                "先對帳，禁止直接重生："
                f"podcast_episode_reconcile(manifest_path={manifest_path!r}, "
                f"episode_n={episode_n}, attempt_id={attempt_id!r})",
            )
        raise

    # task_id IS the artifact_id — notebooklm-py _types/artifacts.py:421 states
    # "task_id and artifact_id are the same identifier"; GenerationStatus has NO
    # artifact_id field, so we must use task_id for the download/rename targeting
    # (otherwise download falls back to "latest" and rename targets None).
    # ensure_started guards the failed/empty-task_id case (rate limit / quota / refusal).
    try:
        artifact_id = ensure_started(status)
    except RuntimeError:
        if store is not None:
            _mark_not_accepted(store, episode_n, attempt_id, status)
        raise
    if store is not None:
        _bind_accepted_artifact(store, episode_n, attempt_id, artifact_id)

    # 生成一旦送出,artifact 就在 NotebookLM 雲端建立並跑到完成,不靠本地連線活著。
    try:
        if store is not None:
            output = await finalize_attempt(
                client,
                store,
                episode_n=episode_n,
                attempt_id=attempt_id,
                output_dir=output_dir,
                wait_timeout=wait_timeout,
            )
            _promote_attempt_output(store, episode_n, attempt_id, output)
        else:
            output = await _finalize_episode(
                notebook_id, episode_n, title, artifact_id, output_dir, wait_timeout
            )
        return output
    except TerminalGenerationError:
        # 伺服器端終態(failed / removed,如每日配額耗盡):artifact 已被下架,resume
        # 也救不回——原樣往上拋,不誤導成「可續跑」。用專屬型別而非 except RuntimeError,
        # 才不會把下載/命名步驟意外的 RuntimeError 也當成不可續跑。
        raise
    except Exception as exc:
        # 其餘失敗(本地 wait 超時、下載中斷、網路斷)發生在生成之後,artifact 仍在雲端
        # 完好。就地改寫 exc.args 附上 artifact_id + 現成的 podcast_episode_resume 呼叫,
        # 再原樣 re-raise —— 保留原例外「型別與結構化欄位」(SDK 的 ArtifactTimeoutError
        # 等 constructor 需 notebook_id/task_id/timeout 多個必填參數,type(exc)(str) 會
        # 反而 TypeError 吞掉真錯;改寫 args 對內建與 SDK 例外都能把 hint 帶進 str(exc))。
        # 呼叫端據此續完(不重生、不燒 quota),不必再 artifact_list 撈 id。
        durable_argument = (
            f", manifest_path={manifest_path!r}" if manifest_path else ""
        )
        exc.args = (
            f"{exc}\n音檔已在雲端生成(artifact_id={artifact_id!r})但後續步驟失敗。"
            f"既有 attempt_id={attempt_id!r}；用 podcast_episode_resume "
            "續完(不會重新生成):"
            f"podcast_episode_resume(notebook_id={notebook_id!r}, episode_n={episode_n}, "
            f"title={title.strip()!r}, artifact_id={artifact_id!r}, "
            f"output_dir={output_dir!r}{durable_argument})",
        )
        raise


@mcp.tool()
async def podcast_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None = None,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
) -> dict:
    """生成、命名、下載並回錄一集 podcast。

    Studio artifact 與回錄 source 都命名為 ``EP{n:02d} {title}``。

    傳 ``manifest_path`` 時，attempt 會在任何遠端 generation 副作用前持久化；
    timeout／斷線後須依錯誤中的 ``attempt_id`` 呼叫 ``podcast_episode_reconcile``，
    不可重送本工具來重生。finalize 各步驟皆 checkpoint，可用
    ``podcast_episode_resume`` 接續。不傳 manifest 則保留 standalone best-effort 行為。
    """
    # 本地驗證先行(壞參數 ValueError 秒退,不浪費 RPC),再做認證預檢:
    # 單集也要等最多 20 分鐘,cookie 死了先秒退(見 auth_probe docstring)。
    _validate_episode_args(episode_n, title, prior_mp3_path)
    if manifest_path and prior_mp3_path:
        raise ValueError(
            "prior_mp3_path with manifest_path is not checkpointed in P0; "
            "use an already verified notebook source or standalone best-effort"
        )
    await probe_auth(runtime.get_client())
    return await _run_episode(
        notebook_id,
        episode_n,
        title,
        brief,
        output_dir,
        prior_mp3_path,
        language,
        audio_format,
        audio_length,
        wait_timeout,
        manifest_path=manifest_path,
    )


@mcp.tool()
async def podcast_episode_reconcile(
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    wait_timeout: float = 1200.0,
) -> dict:
    """在不重新生成的前提下，找回遺失的 ``generate_audio`` response。

    只收養一筆建立時間符合持久化 dispatch window、且尚未被認領的 audio artifact；
    零筆或多筆候選都安全停止，不做猜測。
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path must be a non-empty string")
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError("episode_n must be an int >= 1")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    if (
        not isinstance(wait_timeout, (int, float))
        or isinstance(wait_timeout, bool)
        or wait_timeout <= 0
    ):
        raise ValueError("wait_timeout must be greater than zero")

    store = ManifestStore(manifest_path)
    snapshot = store.read()
    attempt, notebook_id, baseline, dispatched_at = _reconciliation_subject(
        snapshot, episode_n, attempt_id
    )
    dispatch_status = attempt["dispatch"].get("status")
    remote_artifact_id = attempt["remote"].get("artifact_id")
    if remote_artifact_id is not None:
        if dispatch_status != "accepted":
            raise ValueError("attempt has an inconsistent remote artifact mapping")
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": remote_artifact_id,
            "safe_next_action": "resume",
        }
    if dispatch_status == "dispatching":
        def mark_unknown(manifest: dict) -> None:
            _, current = _attempt_record(manifest, episode_n, attempt_id)
            if current["dispatch"].get("status") == "dispatching":
                current["dispatch"]["status"] = "acceptance_unknown"

        snapshot, _ = store.update(mark_unknown)
        attempt, notebook_id, baseline, dispatched_at = _reconciliation_subject(
            snapshot, episode_n, attempt_id
        )
        dispatch_status = attempt["dispatch"].get("status")
    if dispatch_status not in ("acceptance_unknown", "reconciliation_ambiguous"):
        raise ValueError(
            f"attempt state {dispatch_status!r} cannot be reconciled"
        )

    client = runtime.get_client()
    await probe_auth(client)
    artifacts = await client.artifacts.list(
        notebook_id, artifact_type=ArtifactType.AUDIO
    )
    claimed = _claimed_artifact_ids(snapshot, attempt_id)
    window_start = dispatched_at - _RECONCILIATION_CLOCK_SKEW
    window_end = (
        dispatched_at
        + timedelta(seconds=float(wait_timeout))
        + _RECONCILIATION_CLOCK_SKEW
    )
    candidates: set[str] = set()
    for artifact in artifacts:
        artifact_id = getattr(artifact, "id", None)
        kind = getattr(getattr(artifact, "kind", None), "value", None)
        created_at = getattr(artifact, "created_at", None)
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or artifact_id in baseline
            or artifact_id in claimed
            or kind != ArtifactType.AUDIO.value
            or not isinstance(created_at, datetime)
            or created_at.tzinfo is None
        ):
            continue
        created_at = created_at.astimezone(timezone.utc)
        if window_start <= created_at <= window_end:
            candidates.add(artifact_id)

    candidate_ids = sorted(candidates)
    if len(candidate_ids) == 1:
        artifact_id = candidate_ids[0]
        _bind_reconciled_artifact(
            store, episode_n, attempt_id, artifact_id
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": artifact_id,
            "safe_next_action": "resume",
        }
    if len(candidate_ids) > 1:
        _mark_reconciliation_ambiguous(
            store, episode_n, attempt_id, candidate_ids
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "reconciliation_ambiguous",
            "candidate_artifact_ids": candidate_ids,
            "safe_next_action": "resolve_reconciliation_ambiguity",
        }

    latest = store.read()
    latest_attempt, _, _, _ = _reconciliation_subject(
        latest, episode_n, attempt_id
    )
    latest_artifact_id = latest_attempt["remote"].get("artifact_id")
    if latest_artifact_id is not None:
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": latest_artifact_id,
            "safe_next_action": "resume",
        }
    return {
        "complete": False,
        "episode_n": episode_n,
        "attempt_id": attempt_id,
        "observed_state": latest_attempt["dispatch"]["status"],
        "candidate_artifact_ids": [],
        "safe_next_action": "wait_and_reconcile",
    }


@mcp.tool()
async def podcast_episode_resume(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
) -> dict:
    """接續一個「已在 NotebookLM 雲端啟動」的音檔生成,續完後半段而**不重新生成**。

    ``artifact_id`` 應來自原呼叫的 durable attempt 或
    ``podcast_episode_reconcile`` 唯一收養結果，不可自行猜「最新」artifact。

    傳 ``manifest_path`` 時，各 finalize 步驟皆依 checkpoint 與 postcondition
    冪等接續，重複呼叫不會新增第二筆 source。不傳時僅 best-effort。
    """
    # 本地驗證先行(壞參數 ValueError 秒退),再認證預檢——與 podcast_episode 同一
    # fail-fast 順序:認證錯誤不得蓋掉參數錯誤。
    _validate_episode_args(episode_n, title, None)
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id 必填(從 durable attempt 或 reconcile 結果取得)")
    await probe_auth(runtime.get_client())
    if manifest_path:
        store = ManifestStore(manifest_path)
        attempt_id = _ensure_resume_attempt(
            store,
            notebook_id=notebook_id,
            episode_n=episode_n,
            title=title,
            artifact_id=artifact_id.strip(),
        )
        output = await finalize_attempt(
            runtime.get_client(),
            store,
            episode_n=episode_n,
            attempt_id=attempt_id,
            output_dir=output_dir,
            wait_timeout=wait_timeout,
        )
        _promote_attempt_output(store, episode_n, attempt_id, output)
        return output

    output = await _finalize_episode(
        notebook_id, episode_n, title, artifact_id.strip(), output_dir, wait_timeout
    )
    output["durability_warning"] = (
        "manifest_path 未提供；這次 standalone resume 只有 best-effort，"
        "重呼可能重做 download 或 feedback source upload。"
    )
    return output


def _source_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _source_is_ready(source: object) -> bool:
    ready = getattr(source, "is_ready", None)
    if isinstance(ready, bool):
        return ready
    return _source_value(getattr(source, "status", None)) in (
        "ready",
        "completed",
    )


async def _continuity_source_verified(
    client: object,
    notebook_id: str,
    label: str,
    *,
    source_id: str | None,
) -> bool:
    sources = await client.sources.list(notebook_id)
    matches = [
        source
        for source in sources
        if (source_id is None or getattr(source, "id", None) == source_id)
        and getattr(source, "title", None) == label
        and _source_value(getattr(source, "kind", None)) == "media"
        and _source_is_ready(source)
    ]
    return len(matches) == 1


@mcp.tool()
async def podcast_series(
    notebook_id: str,
    episodes: list[dict],
    output_dir: str,
    start: int = 1,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """依 manifest 的安全續點，確定性地生成整季 podcast。"""
    # start 是執行下界，不是重生旗標；N 以前的 plan 是 caller 明示的 trust
    # boundary，不讀、不驗證。範圍錯誤仍須在任何遠端副作用前失敗。
    if start < 1:
        raise ValueError("start must be >= 1")
    if start > len(episodes):
        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")

    # 只驗證 candidate range，避免執行中途才因壞 plan 消耗 generation 額度。
    for i, ep in enumerate(episodes[start - 1 :], start=start):
        if not isinstance(ep, dict) or "brief" not in ep:
            raise ValueError(
                f"episode {i} must be a dict with a 'brief' key, got: {ep!r}"
            )
        title = ep.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"episode {i} must have a non-empty 'title' (got: {title!r})"
            )

    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")

    store = ManifestStore(manifest_path)
    initial_snapshot = store.read()
    manifest_notebook_id = initial_snapshot.get("notebook_id")
    if manifest_notebook_id not in (None, notebook_id):
        raise ValueError(
            "series manifest belongs to a different notebook"
        )
    client = runtime.get_client()
    run_results: list[dict] = []

    def partial(
        episode_n: int,
        attempt_id: str | None,
        observed_state: str,
        safe_next_action: str,
        **extra: object,
    ) -> dict:
        return {
            "notebook_id": notebook_id,
            "episodes": run_results,
            "manifest": manifest_path,
            "complete": False,
            "stopped_at_episode": episode_n,
            "attempt_id": attempt_id,
            "observed_state": observed_state,
            "safe_next_action": safe_next_action,
            **extra,
        }

    # Resume/finalize 也需要有效認證；每個新 generation 前會再 probe 一次，
    # 避免數小時 series 中途 cookie 失效後仍燒 submit。
    await probe_auth(client)

    for episode_n in range(start, len(episodes) + 1):
        plan = episodes[episode_n - 1]
        expected_title = plan["title"].strip()
        expected_brief_hash = hashlib.sha256(
            plan["brief"].encode("utf-8")
        ).hexdigest()
        snapshot = store.read()
        episode = next(
            (
                row
                for row in snapshot["episodes"]
                if row.get("episode") == episode_n
            ),
            None,
        )

        if episode is not None:
            output_attempt_id = episode.get("output_attempt_id")
            if output_attempt_id is not None:
                _, output_attempt = _attempt_record(
                    snapshot, episode_n, output_attempt_id
                )
                if output_attempt.get("title") != expected_title:
                    raise ValueError(
                        f"episode {episode_n} title differs from completed attempt"
                    )
                stored_hash = output_attempt.get("brief_sha256")
                if stored_hash not in (None, expected_brief_hash):
                    raise ValueError(
                        f"episode {episode_n} brief differs from completed attempt"
                    )
                upload = output_attempt.get("finalize", {}).get(
                    "feedback_source_upload", {}
                )
                source_id = upload.get("source_id")
                continuity_ok = (
                    upload.get("status") == "completed"
                    and isinstance(source_id, str)
                    and await _continuity_source_verified(
                        client,
                        notebook_id,
                        _episode_label(episode_n, expected_title),
                        source_id=source_id,
                    )
                )
                if not continuity_ok:
                    return partial(
                        episode_n,
                        output_attempt_id,
                        "continuity_unverified",
                        "restore_feedback_source_explicitly",
                    )
                continue

            is_legacy_output = (
                not episode.get("attempts")
                and isinstance(episode.get("artifact_id"), str)
                and isinstance(episode.get("mp3_path"), str)
            )
            if is_legacy_output:
                if episode.get("title") not in (None, expected_title):
                    raise ValueError(
                        f"episode {episode_n} title differs from legacy output"
                    )
                legacy_path = episode["mp3_path"]
                try:
                    local_audio_ok = (
                        os.path.isfile(legacy_path)
                        and os.path.getsize(legacy_path) > 0
                    )
                except OSError:
                    local_audio_ok = False
                legacy_source_id = episode.get("feedback_source_id")
                continuity_ok = (
                    local_audio_ok
                    and isinstance(legacy_source_id, str)
                    and await _continuity_source_verified(
                        client,
                        notebook_id,
                        _episode_label(episode_n, expected_title),
                        source_id=legacy_source_id,
                    )
                )
                if not continuity_ok:
                    return partial(
                        episode_n,
                        None,
                        "legacy_output_unverified",
                        "resume_legacy_artifact_explicitly",
                        artifact_id=episode["artifact_id"],
                    )
                continue

            active_attempt_id = episode.get("active_attempt_id")
            if active_attempt_id is not None:
                _, attempt = _attempt_record(
                    snapshot, episode_n, active_attempt_id
                )
                if attempt.get("title") != expected_title:
                    raise ValueError(
                        f"episode {episode_n} title changed during an active attempt"
                    )
                stored_hash = attempt.get("brief_sha256")
                if stored_hash not in (None, expected_brief_hash):
                    raise ValueError(
                        f"episode {episode_n} brief changed during an active attempt"
                    )

                dispatch_state = attempt.get("dispatch", {}).get("status")
                remote = attempt.get("remote", {})
                remote_state = remote.get("status")
                artifact_id = remote.get("artifact_id")
                if dispatch_state == "not_accepted":
                    return partial(
                        episode_n,
                        active_attempt_id,
                        "not_accepted",
                        "create_new_attempt_explicitly",
                    )
                if remote_state in ("failed", "removed"):
                    return partial(
                        episode_n,
                        active_attempt_id,
                        remote_state,
                        "create_new_attempt_explicitly",
                    )

                if dispatch_state == "prepared":
                    expected_settings = {
                        "language": resolve_language(language),
                        "audio_format": audio_format,
                        "audio_length": audio_length,
                    }
                    if attempt.get("settings") != expected_settings:
                        raise ValueError(
                            f"episode {episode_n} settings changed during a "
                            "prepared attempt"
                        )
                    baseline = await client.artifacts.list(
                        notebook_id, artifact_type=ArtifactType.AUDIO
                    )
                    claimed = _claim_prepared_dispatch(
                        store,
                        episode_n,
                        active_attempt_id,
                        [row.id for row in baseline],
                    )
                    if not claimed:
                        latest = store.read()
                        _, latest_attempt = _attempt_record(
                            latest, episode_n, active_attempt_id
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            latest_attempt["dispatch"]["status"],
                            "retry_series",
                        )
                    try:
                        started = await client.artifacts.generate_audio(
                            notebook_id,
                            language=expected_settings["language"],
                            instructions=plan["brief"],
                            audio_format=to_audio_format(audio_format),
                            audio_length=to_audio_length(audio_length),
                        )
                    except asyncio.CancelledError as exc:
                        _mark_acceptance_unknown(
                            store, episode_n, active_attempt_id, exc
                        )
                        raise
                    except Exception as exc:
                        _mark_acceptance_unknown(
                            store, episode_n, active_attempt_id, exc
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            "acceptance_unknown",
                            "wait_and_reconcile",
                        )
                    try:
                        artifact_id = ensure_started(started)
                    except RuntimeError:
                        _mark_not_accepted(
                            store, episode_n, active_attempt_id, started
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            "not_accepted",
                            "create_new_attempt_explicitly",
                        )
                    _bind_accepted_artifact(
                        store, episode_n, active_attempt_id, artifact_id
                    )

                if not artifact_id:
                    if dispatch_state not in (
                        "dispatching",
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        raise ValueError(
                            f"episode {episode_n} attempt has no remote artifact "
                            f"in state {dispatch_state!r}"
                        )
                    reconciled = await podcast_episode_reconcile(
                        manifest_path,
                        episode_n=episode_n,
                        attempt_id=active_attempt_id,
                        wait_timeout=wait_timeout,
                    )
                    if reconciled["observed_state"] != "accepted":
                        return partial(
                            episode_n,
                            active_attempt_id,
                            reconciled["observed_state"],
                            reconciled["safe_next_action"],
                            candidate_artifact_ids=reconciled.get(
                                "candidate_artifact_ids", []
                            ),
                        )

                try:
                    result = await finalize_attempt(
                        client,
                        store,
                        episode_n=episode_n,
                        attempt_id=active_attempt_id,
                        output_dir=output_dir,
                        wait_timeout=wait_timeout,
                    )
                except TerminalGenerationError:
                    current = store.read()
                    _, failed_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    return partial(
                        episode_n,
                        active_attempt_id,
                        failed_attempt["remote"]["status"],
                        "create_new_attempt_explicitly",
                    )
                except TimeoutError:
                    return partial(
                        episode_n,
                        active_attempt_id,
                        "pending",
                        "retry_series",
                    )
                except RuntimeError:
                    current = store.read()
                    _, stopped_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    upload_state = stopped_attempt.get("finalize", {}).get(
                        "feedback_source_upload", {}
                    ).get("status")
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        action = (
                            "resolve_reconciliation_ambiguity"
                            if upload_state == "reconciliation_ambiguous"
                            else "retry_series"
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            upload_state,
                            action,
                        )
                    raise
                _promote_attempt_output(
                    store, episode_n, active_attempt_id, result
                )
                run_results.append(result)
                continue

        # candidate range 內完全沒有 attempt 才能產生新的遠端副作用。
        await probe_auth(client)
        try:
            result = await _run_episode(
                notebook_id,
                episode_n,
                plan["title"],
                plan["brief"],
                output_dir,
                None,
                language,
                audio_format,
                audio_length,
                wait_timeout,
                manifest_path=manifest_path,
            )
        except TerminalGenerationError:
            current = store.read()
            row = next(
                item
                for item in current["episodes"]
                if item.get("episode") == episode_n
            )
            attempt_id = row["active_attempt_id"]
            _, failed_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            return partial(
                episode_n,
                attempt_id,
                failed_attempt["remote"]["status"],
                "create_new_attempt_explicitly",
            )
        except RuntimeError:
            current = store.read()
            row = next(
                item
                for item in current["episodes"]
                if item.get("episode") == episode_n
            )
            attempt_id = row["active_attempt_id"]
            _, stopped_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            if stopped_attempt["dispatch"]["status"] != "not_accepted":
                raise
            return partial(
                episode_n,
                attempt_id,
                "not_accepted",
                "create_new_attempt_explicitly",
            )
        except (TimeoutError, ConnectionError):
            current = store.read()
            row = next(
                item
                for item in current["episodes"]
                if item.get("episode") == episode_n
            )
            attempt_id = row["active_attempt_id"]
            _, stopped_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            if stopped_attempt["remote"].get("artifact_id") is None:
                observed = stopped_attempt["dispatch"]["status"]
            else:
                upload_state = stopped_attempt.get("finalize", {}).get(
                    "feedback_source_upload", {}
                ).get("status")
                observed = (
                    upload_state
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    )
                    else stopped_attempt["remote"]["status"]
                )
            action = (
                "wait_and_reconcile"
                if observed == "acceptance_unknown"
                else "retry_series"
            )
            return partial(
                episode_n,
                attempt_id,
                observed,
                action,
            )
        run_results.append(result)

    return {
        "notebook_id": notebook_id,
        "episodes": run_results,
        "manifest": manifest_path,
        "complete": True,
    }
