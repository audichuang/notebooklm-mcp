"""Manifest-backed、可重入的 audio finalize。

每個遠端／本機 side effect 都先 checkpoint，再以可觀察 postcondition 決定
是否需要補做。這個 module 不負責 generation submit，也不改 compatibility
projection；caller 只在本函式完整成功後才 promotion。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Callable, Iterable

from notebooklm.types import ArtifactType

from ._atomic import _DIR_FSYNC_UNSUPPORTED, _NEW_FILE_MODE
from ._atomic import fsync_parent as _fsync_parent
from ._status import TerminalGenerationError, ensure_completed
from .manifest_store import ManifestStore
from .naming import episode_label

_SOURCE_CLOCK_SKEW = timedelta(minutes=1)
_SOURCE_DISPATCH_WINDOW = timedelta(minutes=11)
# 「已 dispatch,但 source_id 還沒落盤」的三種狀態。三種的共同後果都是「遠端可能多出
# 一筆 media、而 manifest 記不住它是誰」,所以清理義務必須一起涵蓋(見
# `unresolved_upload_descriptor`)。
_UNRESOLVED_UPLOAD_STATUSES = (
    "dispatching",
    "acceptance_unknown",
    "reconciliation_ambiguous",
)
# 候選窗全長(含 clock skew)。清理義務的指引要講「等多久」,而那個數字必須跟
# `upload_dispatch_window_closed()` 用的是同一個,不能在別的 module 再加一次。
UPLOAD_DISPATCH_WINDOW = _SOURCE_DISPATCH_WINDOW + _SOURCE_CLOCK_SKEW
_TZ = timezone(timedelta(hours=8))


def has_hard_output_evidence(episode: dict) -> bool:
    """已生成或發布的 legacy 證據；即使後來出現 attempt 也不可隱式覆寫。"""
    hard_fields = ("artifact_id", "task_id", "mp3_path", "published_at")
    return any(
        isinstance(episode.get(field), str) and episode[field].strip()
        for field in hard_fields
    )


def has_durable_output_evidence(episode: dict) -> bool:
    """既有輸出留下任一耐久證據時，不得覆寫相容路徑。"""
    if has_hard_output_evidence(episode):
        return True
    # 被 retract 的那版本機 mp3 就是拒收證據,不能讓取代版蓋掉它——否則同一集有沒有
    # 保留證據會取決於「那時剛好生過封面沒有」,不可預測。取代版一律走
    # attempts/<attempt_id>/ 子目錄(見下方 finalize 的 mp3_path 選擇)。
    if episode.get("retracted_attempt_ids"):
        return True
    cover = episode.get("cover_path")
    if isinstance(cover, str) and cover.strip():
        return True
    label = episode.get("label")
    return (
        not episode.get("attempts")
        and isinstance(label, str)
        and bool(label.strip())
    )


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
    # Tombstone。`podcast_attempt_retract` 作廢的 attempt 不得再被 finalize:所有 finalize
    # checkpoint 的讀寫都走這裡,所以一個 in-flight finalizer 在 retract 之後的第一次
    # manifest 觸碰就會停住,不會繼續 upload／rename／promote 把被拒收的那版復活。
    if attempt.get("retraction"):
        raise ValueError(
            f"attempt {attempt_id!r} was retracted (episode {episode_n}); "
            "generate a replacement instead of finalizing it"
        )
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


def claimed_source_ids(manifest: dict) -> set[str]:
    """所有已被某顆 attempt 認領成回錄 source 的 id。

    「這筆 source 有人認領」= 它不是孤兒,清理義務不該把它排進待刪清單 —— 對帳結果撞上
    並行寫入而要重算時,這是唯一要重驗的那件事(見 `tools_podcast._settle_cleanup_state`)。
    """
    return _claimed_source_ids(manifest, "")


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
        "label": episode_label(episode_n, attempt["title"]),
        "task_id": artifact_id,
        "artifact_id": artifact_id,
        "mp3_path": path,
        "published_at": episode.get("published_at")
        or format_datetime(datetime.now(_TZ)),
        "attempt_id": attempt_id,
        "feedback_source_id": finalize["feedback_source_upload"]["source_id"],
    }


def _created_at_utc(value: object) -> datetime | None:
    """把 Source.created_at 正規化成 aware UTC。

    **notebooklm-py 0.7.x 的 `Source.created_at` 是 host-local naive**
    (`_datetime_from_timestamp` 走 `datetime.fromtimestamp(value)`,不帶 tz)——舊版
    reconciliation 直接用 `tzinfo is None` 排除,等於把**實裝 SDK 回來的每一筆** source
    都濾掉,response-loss 後永遠對不到、卡在 acceptance_unknown。naive 值視為本地時間
    (`astimezone()` 的預設)轉 UTC:unix timestamp 是絕對時刻,round-trip 回正確 instant。
    0.8 起改回 aware,這裡對 aware/naive 都正確。"""
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc)


def unresolved_upload_descriptor(attempt: dict) -> dict | None:
    """回傳「已經 dispatch、但回錄 source id 還沒落盤」的那份 upload checkpoint。

    **三種狀態都算 unresolved,不只 `dispatching`**:`acceptance_unknown` 與
    `reconciliation_ambiguous` 同樣是「遠端可能已經多出一筆 media,而 manifest 記不住
    它是誰」。只修 `dispatching` 是補一半——漏掉的那兩種留下的孤兒 source 一樣從此
    沒人記得,而 finalize 是按 source_id 驗的,之後每次生成都把那份逐字稿讀進 context。

    `source_id` 一落盤就不再 unresolved(那時候清理義務走既有的 `stale_source_ids`
    路徑,身分是確定的,不需要對帳)。
    """
    finalize = attempt.get("finalize")
    upload = (finalize or {}).get("feedback_source_upload")
    if not isinstance(upload, dict) or upload.get("source_id"):
        return None
    if upload.get("status") not in _UNRESOLVED_UPLOAD_STATUSES:
        return None
    return upload


def _dispatched_at_utc(upload: dict) -> datetime:
    dispatched_raw = upload.get("dispatched_at")
    if not isinstance(dispatched_raw, str):
        raise ValueError("feedback source dispatch time is missing")
    dispatched_at = datetime.fromisoformat(dispatched_raw)
    if dispatched_at.tzinfo is None:
        raise ValueError("feedback source dispatch time must include timezone")
    return dispatched_at.astimezone(timezone.utc)


def upload_dispatch_window_closed(upload: dict, *, now: datetime | None = None) -> bool:
    """候選窗(含 clock skew)是否已經關上。

    **只有關上之後,「零候選」才等於「遠端真的沒有多出東西」**;窗還開著時零候選
    可能只是 source 還沒出現在 list 裡,那時候清掉清理義務就是把孤兒放生。
    """
    moment = now or datetime.now(timezone.utc)
    return moment > _dispatched_at_utc(upload) + UPLOAD_DISPATCH_WINDOW


def _upload_kind_matches(source: object) -> bool:
    """回錄 upload 的候選 kind:已分類成 `media`,**或還在 ingest**(未分類且 not ready)。

    v0.9.13 真實驗收:上傳成功但 NotebookLM 端 ingest 卡死,那筆 source 逾 13 小時
    停在 `kind=unknown` / `ready=false`。只認 `media` 的話它對兩個呼叫端同時隱形——
    對 finalize 對帳是「零候選 → acceptance_unknown」(續不下去),對清理義務對帳是
    「零候選 → 義務結案放行」,而孤兒還躺在 notebook 裡被之後每一集讀進生成 context。

    放寬只針對「尚未分類」,不含任何已分類成非 media 的來源;而且 kind 只是五個條件
    之一,還要 title 逐字等於 `expected_title`(= 上傳當下的檔名)、落在 dispatch 窗內、
    不在 baseline、未被別顆認領。

    對 finalize 這一側的效果是**把身分綁回來**,不是放行:認回來之後
    `finalize_attempt` 的 postcondition 仍然要 `_source_ready`,沒 ingest 完照樣停在
    「postcondition is not satisfied」,而 `_completed_output` 要四個 checkpoint 都
    completed 才產出。差別在於 retract 這時走的是身分確定的 `stale_source_ids` 路徑。
    """
    kind = _kind_value(getattr(source, "kind", None))
    if kind == "media":
        return True
    return kind in (None, "unknown") and not _source_ready(source)


def unresolved_upload_candidates(
    manifest: dict, attempt: dict, attempt_id: str, sources: Iterable[object]
) -> list[str]:
    """從 notebook 現況篩出「可能是這次 upload 建出來的」source id。

    **這是唯一一份候選判準**:finalize 對帳(`_reconcile_source_upload`)與 retract
    之後的清理義務對帳(`tools_podcast._assert_source_cleanup_done`)都走這裡。
    baseline／title／kind／時間窗／已被別顆認領這五個條件,漏任何一個都會把別集的
    回錄誤判成這次的孤兒(或反過來漏掉真的孤兒),而兩邊各寫一份就等於保證有一天
    只有一邊被修到——本 repo 對「各寫一份」記過的帳已經夠多了。

    刻意收 `attempt` 而不是走 `_record()`:tombstone 之後的對帳是這支的主要用途,
    而 `_record()` 對已 retract 的 attempt 一律 raise(default-deny)。
    """
    upload = attempt["finalize"]["feedback_source_upload"]
    baseline = set(upload.get("source_ids_before") or [])
    expected_title = upload.get("expected_title")
    if not isinstance(expected_title, str) or not expected_title:
        raise ValueError("feedback source expected title is missing")
    dispatched_at = _dispatched_at_utc(upload)
    claimed = _claimed_source_ids(manifest, attempt_id)
    candidates: set[str] = set()
    for source in sources:
        source_id = getattr(source, "id", None)
        created_at = _created_at_utc(getattr(source, "created_at", None))
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in baseline
            or source_id in claimed
            or not _upload_kind_matches(source)
            or getattr(source, "title", None) != expected_title
            or created_at is None
        ):
            continue
        if (
            dispatched_at - _SOURCE_CLOCK_SKEW
            <= created_at
            <= dispatched_at + _SOURCE_DISPATCH_WINDOW
        ):
            candidates.add(source_id)
    return sorted(candidates)


async def _reconcile_source_upload(
    client: object,
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    notebook_id: str,
) -> str:
    snapshot = store.read()
    _, attempt = _record(snapshot, episode_n, attempt_id)
    sources = await client.sources.list(notebook_id)
    candidates = unresolved_upload_candidates(snapshot, attempt, attempt_id, sources)
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


async def _artifact_title_state(
    client: object,
    notebook_id: str,
    artifact_id: str,
    label: str,
) -> bool | None:
    """回傳精確 postcondition；list 暫時看不到 ID 時回 None。

    呼叫端將 None 視為無法證實 remote identity，並 fail-closed。
    """
    artifacts = await client.artifacts.list(notebook_id)
    matches = [
        artifact
        for artifact in artifacts
        if getattr(artifact, "id", None) == artifact_id
    ]
    if not matches:
        return None
    return len(matches) == 1 and (
        _kind_value(getattr(matches[0], "kind", None)) == ArtifactType.AUDIO.value
        and getattr(matches[0], "title", None) == label
    )


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
    label = episode_label(episode_n, title)

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
            artifact_title_state = await _artifact_title_state(
                client, notebook_id, artifact_id, label
            )
            if (
                artifact_title_state is True
                and isinstance(source_id, str)
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
            prior_output_attempt_id is None and has_durable_output_evidence(episode)
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
            # except block 結束時 Python 會 `del error`,所以文字先取出來:閉包只在這個
            # block 內被 _mutate 同步呼叫過一次,但只要有人把那次呼叫搬出去就會 NameError。
            error_text = str(error)

            def remote_terminal(_episode: dict, current: dict) -> None:
                current["remote"].update(
                    {
                        "status": terminal_status,
                        "status_origin": "remote",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "error": error_text,
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
    artifact_title_state = await _artifact_title_state(
        client, notebook_id, artifact_id, label
    )
    if artifact_title_state is None:
        raise RuntimeError(
            f"artifact {artifact_id!r} cannot be verified in the remote list"
        )

    if artifact_title_state is True and rename["status"] != "completed":
        def adopt_rename(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "completed"

        _mutate(store, episode_n, attempt_id, adopt_rename)
        _, attempt = _subject(store, episode_n, attempt_id)
        rename = attempt["finalize"]["artifact_rename"]

    if rename["status"] != "completed" or artifact_title_state is False:
        def rename_dispatching(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "dispatching"

        _mutate(store, episode_n, attempt_id, rename_dispatching)

        def rename_unknown(_episode: dict, current: dict) -> None:
            current["finalize"]["artifact_rename"]["status"] = "outcome_unknown"

        try:
            await client.artifacts.rename(
                notebook_id, artifact_id, label, return_object=False
            )
        except Exception:
            landed = await _artifact_title_state(
                client, notebook_id, artifact_id, label
            )
            if landed is not True:
                _mutate(store, episode_n, attempt_id, rename_unknown)
                raise
        else:
            landed = await _artifact_title_state(
                client, notebook_id, artifact_id, label
            )
            if landed is not True:
                _mutate(store, episode_n, attempt_id, rename_unknown)
                raise RuntimeError(
                    f"artifact {artifact_id!r} rename postcondition failed"
                )

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
        replaced = False
        try:
            await client.artifacts.download_audio(
                notebook_id, temp_path, artifact_id
            )
            size = os.path.getsize(temp_path)
            if size <= 0:
                raise ValueError("downloaded audio is empty")
            digest = _sha256_file(temp_path)
            # mkstemp 給 0600,os.replace 會把 temp 的 mode 一起帶到最終 mp3——沿用既有
            # 檔案的 mode,首次下載才用 _NEW_FILE_MODE(對齊 _atomic.download_atomically
            # 同一做法,fsync 前就要設好)。
            try:
                mode = stat.S_IMODE(os.stat(mp3_path).st_mode)
            except FileNotFoundError:
                mode = _NEW_FILE_MODE
            os.chmod(temp_path, mode)
            with open(temp_path, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_path, mp3_path)
            replaced = True
        except Exception:
            def download_failed(_episode: dict, current: dict) -> None:
                # 只准從自己 claim 的 "dispatching" 降級:併發 finalizer 若已經把這顆
                # checkpoint 寫成 "completed"(例如更快完成的另一個 process),這裡
                # 遲到的失敗不能盲寫倒退——不比對就寫會把已驗證過的 completed 蓋回
                # failed。temp_path 這裡就要清掉的檔案,寫回 None 才跟
                # download_completed 一致——留舊路徑會讓稽核欄位說謊(它已經被下面
                # 的 finally unlink 掉了)。
                download_state = current["finalize"]["download"]
                if download_state.get("status") != "dispatching":
                    return
                download_state.update({"status": "failed", "temp_path": None})

            _mutate(store, episode_n, attempt_id, download_failed)
            raise
        finally:
            # CancelledError 是 BaseException 子類,不會落進上面的 except Exception——
            # 而 client cancellation(mcporter 預設 60s vs 單集動輒 20 分)正是這條路
            # 最常見的中斷來源。清理不能只掛在 except Exception 底下,否則每次取消都
            # 留一份 .part。用涵蓋整段的 finally + replaced flag(manifest_store.py 的
            # _write 用過的 pattern):os.replace 成功就不用清(temp_path 已經不存在),失敗或
            # 取消都清掉沒換成功的 partial。cancelled 不寫 "failed" checkpoint——durable
            # 語意不變:resume 判定只看 status=="completed"(:516-524),"dispatching"
            # 原地留著,下次會重新從 mkstemp 開始。
            if not replaced:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        # directory fsync 是 commit(os.replace)**之後**的事,且在不支援目錄 fsync 的
        # mount(network/overlay)上會回 EINVAL/ENOTSUP——放進上面的 try 會讓「檔案已換好、
        # 只是 fsync 不支援」被 except 寫成 status="failed",而 resume 只認 "completed",
        # 於是每次重下載都在同一行失敗、永遠 finalize 不了。故移到 try 外並容忍那組 errno
        # (對齊 _atomic.download_atomically 的 post-commit 處理)。
        try:
            _fsync_parent(mp3_path)
        except OSError as exc:
            if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
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
            # **`CancelledError` 要顯式收**:它是 `BaseException`,`except Exception`
            # 收不到,而 MCP 的長 request 被 client 取消是常態不是意外。漏收的下場是
            # checkpoint 永久停在 `dispatching`——遠端可能已經建出 source,而狀態機
            # 卡在「還在飛」,retract 的清理義務也就永遠算不出要對帳什麼(F2 的根因)。
            # 刻意不寫 `except BaseException`:KeyboardInterrupt／SystemExit 不該在這裡
            # 被當成「上傳結果不明」處理。
            except (Exception, asyncio.CancelledError) as exc:
                def upload_unknown(_episode: dict, current: dict) -> None:
                    # 只准從自己 claim 的 "dispatching" 降級:併發 finalizer(或
                    # `podcast_attempt_adopt`)若已經把這顆 checkpoint 寫成
                    # "completed",這裡遲到的失敗不能盲寫倒退——不比對就寫會把
                    # 已驗證過的 completed 蓋回 acceptance_unknown。
                    upload_state = current["finalize"]["feedback_source_upload"]
                    if upload_state.get("status") != "dispatching":
                        return
                    upload_state["status"] = "acceptance_unknown"

                try:
                    _mutate(store, episode_n, attempt_id, upload_unknown)
                except Exception as checkpoint_error:
                    # 並行 retract 已經 tombstone 掉這顆(`_record` default-deny),或
                    # manifest 根本寫不進去。兩種都不該蓋掉呼叫端真正要讀的那個例外
                    # ——retract 那條路自己會留下 unresolved 清理義務。
                    exc.add_note(
                        f"upload checkpoint 未能改寫成 acceptance_unknown:{checkpoint_error}"
                    )
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
    sources = await client.sources.list(notebook_id)
    source = next(
        (row for row in sources if getattr(row, "id", None) == source_id), None
    )
    if source is None:
        raise RuntimeError(f"feedback source {source_id!r} cannot be verified")
    already_named = getattr(source, "title", None) == label and _source_ready(source)
    if not already_named:
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
