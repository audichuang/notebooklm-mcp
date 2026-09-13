"""Frozen generation-input bundles consumed immediately before podcast dispatch."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 重用 v0.3.3 為簡報/講義原子換檔抽出來的那組工具,別再寫第二份:那條路上學到的三件事
# (temp 的 0600 會被帶到最終檔、commit point 之後的 fsync 不得回滾、有些 filesystem
# 不支援 directory fsync)都已經編進這裡,自己重寫就等於重踩一次。
from ._atomic import _DIR_FSYNC_UNSUPPORTED, _NEW_FILE_MODE, fsync_parent

_BUNDLE_FILES = {
    "runtime_brief": "runtime-brief.md",
    "coverage_ledger": "coverage-ledger.json",
    "evidence_manifest": "evidence-manifest.json",
}
_ALLOWED_DISPOSITIONS = {
    "spoken_required",
    "written_required",
    "intentionally_deferred",
    "out_of_scope",
}
_COMMON_COVERAGE_KEYS = {"id", "plan_locator", "disposition"}
_COVERAGE_ROW_KEYS = {
    "spoken_required": _COMMON_COVERAGE_KEYS | {"delivery_locator"},
    "written_required": _COMMON_COVERAGE_KEYS | {"delivery_locator"},
    "intentionally_deferred": _COMMON_COVERAGE_KEYS | {"destination"},
    "out_of_scope": _COMMON_COVERAGE_KEYS | {"rationale"},
}


def _read_frozen_bundle_files(
    workspace: Path, relative: Path
) -> tuple[dict[str, bytes], tuple[int, int]]:
    """Pin the bundle inode and read regular children without following symlinks.

    回傳的第二個值是釘住那一刻的 ``(st_dev, st_ino)``——呼叫端(``load_frozen_generation_input``)
    把它原樣塞進 ``prepared``,供之後的 sidecar 讀寫(``write_attempt_binding`` /
    ``read_attempt_binding``)在**自己動筆之前**重新比對一次,見那兩支函式開頭的說明。
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(workspace, directory_flags)
    try:
        for part in relative.parts:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        pinned = os.fstat(directory_fd)
        loaded: dict[str, bytes] = {}
        for filename in ["generation-request.json", *_BUNDLE_FILES.values()]:
            file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"frozen bundle member must be regular: {filename}")
                chunks: list[bytes] = []
                while chunk := os.read(file_fd, 1024 * 1024):
                    chunks.append(chunk)
                loaded[filename] = b"".join(chunks)
            finally:
                os.close(file_fd)
        current = os.lstat(workspace / relative)
        if (
            stat.S_ISLNK(current.st_mode)
            or current.st_dev != pinned.st_dev
            or current.st_ino != pinned.st_ino
        ):
            raise ValueError("input bundle changed or became a symlink while reading")
        return loaded, (pinned.st_dev, pinned.st_ino)
    except OSError as error:
        # 三個 containment 規則共用這一句,刻意不從 errno 反推是哪一條:O_NOFOLLOW 對成員
        # 檔案回 ELOOP,但加上 O_DIRECTORY 之後對 symlink 目錄回的是 ENOTDIR,而 ENOTDIR
        # 也可能只是路徑中間夾了普通檔案 —— 猜錯比不猜更糟。symlink 祖先是這三者裡最難自己
        # 看出來的(路徑字面上完全正常、`ls` 也看得到那些檔案),所以訊息要把它講出來。
        raise ValueError(
            "input bundle must be a confined real directory "
            "(no absolute path, no '..', no symlink on any segment)"
        ) from error
    finally:
        os.close(directory_fd)


class _DuplicateKey(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(f"duplicate key: {key}")
        value[key] = item
    return value


def _strict_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as error:
        raise ValueError(f"invalid strict JSON in {label}: {error}") from error


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_coverage_ledger(value: object, *, episode_id: str) -> None:
    expected_root = {"schema_version", "qa_kind", "episode_id", "rows"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_root
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("qa_kind") != "delivery_coverage_ledger"
        or value.get("episode_id") != episode_id
        or not isinstance(value.get("rows"), list)
        or not value["rows"]
    ):
        raise ValueError("coverage ledger schema is invalid")
    seen: set[str] = set()
    for row in value["rows"]:
        disposition = row.get("disposition") if isinstance(row, dict) else None
        if (
            disposition not in _ALLOWED_DISPOSITIONS
            or set(row) != _COVERAGE_ROW_KEYS[disposition]
            or not _nonempty(row.get("id"))
            or row["id"] in seen
            or not _nonempty(row.get("plan_locator"))
        ):
            raise ValueError("coverage ledger row schema is invalid")
        seen.add(row["id"])
        locator = {
            "spoken_required": "delivery_locator",
            "written_required": "delivery_locator",
            "intentionally_deferred": "destination",
            "out_of_scope": "rationale",
        }[disposition]
        if not _nonempty(row.get(locator)):
            raise ValueError("coverage ledger row locator is invalid")


def _validate_evidence_manifest(value: object, *, episode_id: str) -> None:
    expected_root = {"schema_version", "qa_kind", "episode_id", "artifacts"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_root
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("qa_kind") != "generation_evidence_manifest"
        or value.get("episode_id") != episode_id
        or not isinstance(value.get("artifacts"), list)
        or not value["artifacts"]
    ):
        raise ValueError("evidence manifest schema is invalid")
    ids: set[str] = set()
    paths: set[str] = set()
    for artifact in value["artifacts"]:
        path_value = artifact.get("path") if isinstance(artifact, dict) else None
        digest = artifact.get("sha256") if isinstance(artifact, dict) else None
        byte_count = artifact.get("bytes") if isinstance(artifact, dict) else None
        artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
        path = Path(path_value) if isinstance(path_value, str) else None
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"id", "path", "sha256", "bytes"}
            or not _nonempty(artifact_id)
            or artifact_id in ids
            or path is None
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path_value in paths
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 1
        ):
            raise ValueError("evidence manifest artifact schema is invalid")
        ids.add(str(artifact_id))
        paths.add(str(path_value))


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    offset = parsed.utcoffset()
    return offset is not None and abs(offset.total_seconds()) <= 14 * 60 * 60


def _discard(path: Path) -> None:
    """Best-effort temp removal.

    Cleanup runs inside ``except`` handlers; a permission error while deleting a temp
    file must never replace the exception the caller actually needs to read (same rule
    as ``_atomic.download_atomically``'s cleanup). After the publish point it must not
    raise either — a stray hidden temp is harmless, undoing a committed binding is not.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _other_series_manifests(workspace: Path, manifest: Path) -> list[Path]:
    """Every ``series_manifest.json`` under ``workspace`` except ``manifest`` itself.

    不限深度、不跟 symlink。寫死 glob 深度就是下一個「寫死 ``manifest/``」——對現在的
    佈局成立、對下一季的佈局不成立。**不跳過隱藏目錄**:曾經跳過(理由是排除
    ``.venv``/``.git`` 這類雜訊),但這條圍籬的職責是「找出所有 series_manifest.json」,
    不是「找出乾淨的目錄樹」——跳過隱藏目錄等於幫攻擊面開一個後門:
    ``base/showA/…`` 與 ``base/.archive/showB/series_manifest.json`` 並存時,舊寫法回傳
    ``others=[]``,showA 的 workspace 推導會靜默吃下 showB 的 bundle(ADR-0012 那次事故
    的同形狀)。podcast-lab 的 ``shows/`` 底下唯一的隱藏目錄是 ``.venv*``,不會有套件在
    裡面 ship ``series_manifest.json``,所以拿掉這個排除不會誤傷真實佈局。"""
    others: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        if "series_manifest.json" in filenames:
            found = Path(dirpath, "series_manifest.json")
            if found.resolve(strict=False) != manifest:
                others.append(found)
    return others


def load_frozen_generation_input(
    *,
    manifest_path: str,
    input_bundle_path: str,
    episode_n: int,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Read each frozen byte sequence once and return the exact provider brief.

    **Workspace** is the only containment boundary: the bundle must resolve inside it,
    with no symlink on any segment. The host declares it with ``workspace_root`` (the
    show's directory; the manifest must live inside it). When omitted, it is inferred as
    the manifest's grandparent (`<workspace>/<any>/series_manifest.json`) — the manifest's
    parent directory name is deliberately NOT constrained, because a directory *name* is
    not a security boundary and the podcast workspaces in use disagree on it
    (`manifest/`, `output/`, `season-01/output/`).

    Inference fails closed when the grandparent also holds **another**
    ``series_manifest.json``: that means the manifest sits at a show root inside a
    multi-show container, so "grandparent" is the whole container and a bundle from a
    *different* show would pass the fence (observed 2026-08-30: a show-root manifest
    accepted another show's bundle). Pass ``workspace_root`` in that layout (ADR-0012).
    """
    manifest = Path(manifest_path).expanduser().resolve(strict=False)
    if manifest.name != "series_manifest.json":
        raise ValueError("frozen input requires a series_manifest.json path")
    if workspace_root is not None:
        workspace = Path(workspace_root).expanduser().resolve(strict=False)
        try:
            manifest.relative_to(workspace)
        except ValueError as error:
            raise ValueError(
                "manifest_path must live inside workspace_root "
                f"(manifest {manifest}, workspace_root {workspace})"
            ) from error
    else:
        workspace = manifest.parent.parent.resolve()
        others = _other_series_manifests(workspace, manifest)
        if others:
            raise ValueError(
                f"inferred workspace {workspace} (the manifest's grandparent) also contains "
                f"{len(others)} other series_manifest.json — this is a multi-show container, "
                "and a bundle from another show would pass the fence. Pass "
                "workspace_root=<this show's directory> (the manifest must be inside it) "
                "and give input_bundle_path relative to that directory."
            )
    raw_bundle = Path(input_bundle_path).expanduser()
    if raw_bundle.is_absolute() or ".." in raw_bundle.parts:
        raise ValueError("input_bundle_path must be a confined relative path")
    bundle = workspace / raw_bundle
    relative = raw_bundle
    frozen_files, bundle_inode = _read_frozen_bundle_files(workspace, relative)
    request_bytes = frozen_files["generation-request.json"]
    request = _strict_json(request_bytes, "generation-request.json")
    expected_keys = {
        "schema_version",
        "qa_kind",
        "status",
        "episode_id",
        "generation_request_id",
        "frozen_at",
        "files",
        "dispatch_contract",
    }
    expected_episode = f"ep{episode_n:02d}"
    if (
        not isinstance(request, dict)
        or set(request) != expected_keys
        or type(request.get("schema_version")) is not int
        or request.get("schema_version") != 1
        or request.get("qa_kind") != "generation_attempt_input"
        or request.get("status") != "frozen"
        or request.get("episode_id") != expected_episode
        or request.get("dispatch_contract")
        != "provider_must_load_runtime_brief_from_this_bundle"
        or not isinstance(request.get("generation_request_id"), str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request["generation_request_id"]
        )
        is None
        or not _valid_timestamp(request.get("frozen_at"))
    ):
        raise ValueError("generation request metadata is invalid")
    frozen_at = datetime.fromisoformat(
        request["frozen_at"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if frozen_at > datetime.now(timezone.utc):
        raise ValueError("generation request timestamp order is invalid")
    files = request.get("files")
    if not isinstance(files, dict) or set(files) != set(_BUNDLE_FILES):
        raise ValueError("generation request files schema is invalid")

    loaded: dict[str, bytes] = {}
    for key, filename in _BUNDLE_FILES.items():
        record = files.get(key)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "bytes"}
            or record.get("path") != filename
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
        ):
            raise ValueError(f"generation request {key} record is invalid")
        data = frozen_files[filename]
        if len(data) != record["bytes"]:
            raise ValueError(f"{key} byte count mismatch")
        if _sha(data) != record["sha256"]:
            raise ValueError(f"{key} SHA-256 mismatch")
        loaded[key] = data

    _validate_coverage_ledger(
        _strict_json(loaded["coverage_ledger"], "coverage-ledger.json"),
        episode_id=request["episode_id"],
    )
    _validate_evidence_manifest(
        _strict_json(loaded["evidence_manifest"], "evidence-manifest.json"),
        episode_id=request["episode_id"],
    )

    try:
        brief = loaded["runtime_brief"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("runtime brief must be UTF-8") from error
    return {
        "brief": brief,
        "bundle": bundle,
        # sidecar 讀寫前的 containment 重驗用(見 write_attempt_binding /
        # read_attempt_binding 開頭的說明);不進 record_base,不落地到任何 JSON——
        # 純本機 (st_dev, st_ino),跨機器無意義。
        "bundle_inode": bundle_inode,
        "episode_id": request["episode_id"],
        "frozen_at": request["frozen_at"],
        "record_base": {
            "generation_request_id": request["generation_request_id"],
            "path": relative.as_posix(),
            "generation_request_sha256": _sha(request_bytes),
            "manifest_workspace_sha256": _sha(str(manifest).encode("utf-8")),
            "runtime_brief_sha256": files["runtime_brief"]["sha256"],
            "runtime_brief_bytes": files["runtime_brief"]["bytes"],
        },
    }


def _reverify_bundle_containment(prepared: dict[str, Any]) -> None:
    """Re-check the bundle directory against the inode pinned at load time.

    `load_frozen_generation_input` → sidecar 讀寫之間隔著 `probe_auth` 加三趟遠端 RPC,
    是秒級的視窗;`_read_frozen_bundle_files` 當時釘住的 `(st_dev, st_ino)` 到這裡還沒
    被重新確認過。窗口內攻擊面邊際價值趨近於零(能在窗內把 bundle 換掉的人,在 load
    之前就能換掉整顆自洽的 bundle),但重驗幾乎零成本,而不驗的殘留失敗形狀是
    「binding 綁錯目錄、稽核紀錄說謊」——即使不會讓錯的 brief 被送出(brief 已經從
    釘住的舊 inode 讀出),也值得補上這一道。
    """
    current = os.lstat(prepared["bundle"])
    if (current.st_dev, current.st_ino) != prepared["bundle_inode"]:
        raise ValueError(
            "input bundle changed or became a symlink since the frozen input was read"
        )


def write_attempt_binding(
    prepared: dict[str, Any], *, attempt_id: str
) -> tuple[dict[str, Any], bytes]:
    _reverify_bundle_containment(prepared)
    bundle = prepared["bundle"]
    record_base = prepared["record_base"]
    bound_at = datetime.now(timezone.utc)
    frozen_at = datetime.fromisoformat(
        prepared["frozen_at"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if bound_at < frozen_at:
        raise ValueError("attempt binding timestamp order is invalid")
    binding = {
        "schema_version": 1,
        "qa_kind": "generation_attempt_input_binding",
        "episode_id": prepared["episode_id"],
        "generation_request_id": record_base["generation_request_id"],
        "attempt_id": attempt_id,
        "generation_request_sha256": record_base["generation_request_sha256"],
        "manifest_workspace_sha256": record_base["manifest_workspace_sha256"],
        "bound_at": bound_at.isoformat(),
    }
    encoded = (json.dumps(binding, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = bundle / "attempt-binding.json"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".attempt-binding.", suffix=".tmp", dir=bundle
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            # **chmod 要在 fsync 之前**:mkstemp 給 0600,而 os.link 會把 temp 的 mode
            # 帶到最終檔。順序反過來的話,斷電可能持久化「0600 的 inode + 新 dirent」,
            # 只有 mode 沒落地——sidecar 是給人稽核的證據檔,不該只有本人讀得到。
            # 用 fchmod 而非 chmod(path):對已開啟的 fd 動作,沒有 TOCTOU。
            os.fchmod(handle.fileno(), _NEW_FILE_MODE)
            os.fsync(handle.fileno())
        # ← 這行是本函式的原子發布點:link 只在 path 不存在時成功,保留 O_EXCL 的
        #   「一個 bundle 只綁一次」語義(os.replace 會靜默蓋掉,不能用)。
        #   注意它**不是整筆業務交易的不可逆 commit**——呼叫端在 manifest 寫入失敗時
        #   仍會走 rollback_attempt_binding 做補償,那發生在遠端生成之前,符合 ADR-0001。
        os.link(temporary, path)
    except FileExistsError as error:
        _discard(temporary)
        # 呼叫端一律先 read_attempt_binding;走到這裡代表**併發**——sidecar 在那次讀取
        # 之後才出現。裸的 errno 17 在這條路上讀不出原因,所以講清楚並給下一步。
        raise ValueError(
            f"attempt-binding.json 在讀取後才出現({path});"
            "同一個 bundle 有另一個 dispatch 併發搶先綁定。重跑一次即可——"
            "重跑會讀回既有綁定並沿用同一個 attempt,不會重複燒配額"
        ) from error
    except BaseException:
        _discard(temporary)
        raise

    # ---- 發布之後 --------------------------------------------------------------
    # 綁定已經在磁碟上了。這之後的清理失敗都**不得回滾** —— 把成功的綁定刪掉,會讓
    # 下一次重跑誤以為沒綁過而重建 attempt。
    _discard(temporary)             # 殘留一個隱藏 temp 檔無害,不值得炸掉已成功的綁定
    try:
        fsync_parent(str(path))
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            pass                    # 有些 filesystem 不支援 directory fsync,那不是失敗
        else:
            raise OSError(
                exc.errno,
                f"attempt-binding.json 已經建立在 {path};只有 directory fsync 失敗"
                f"({exc.strerror})——綁定是有效的,別當成「沒綁到」而重試",
            ) from exc
    record = dict(record_base)
    record["attempt_binding_sha256"] = _sha(encoded)
    return record, encoded


def read_attempt_binding(
    prepared: dict[str, Any],
) -> tuple[str, dict[str, Any], bytes] | None:
    """Read back a previously persisted binding for safe local retry."""
    _reverify_bundle_containment(prepared)
    path = prepared["bundle"] / "attempt-binding.json"
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError("attempt-binding.json must be a regular file")
    encoded = path.read_bytes()
    binding = _strict_json(encoded, "attempt-binding.json")
    record_base = prepared["record_base"]
    expected_keys = {
        "schema_version",
        "qa_kind",
        "episode_id",
        "generation_request_id",
        "attempt_id",
        "generation_request_sha256",
        "manifest_workspace_sha256",
        "bound_at",
    }
    attempt_id = binding.get("attempt_id") if isinstance(binding, dict) else None
    if (
        not isinstance(binding, dict)
        or set(binding) != expected_keys
        or binding.get("schema_version") != 1
        or binding.get("qa_kind") != "generation_attempt_input_binding"
        or binding.get("episode_id") != prepared["episode_id"]
        or binding.get("generation_request_id")
        != record_base["generation_request_id"]
        or binding.get("generation_request_sha256")
        != record_base["generation_request_sha256"]
        or binding.get("manifest_workspace_sha256")
        != record_base["manifest_workspace_sha256"]
        or not isinstance(attempt_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", attempt_id) is None
        or not _valid_timestamp(binding.get("bound_at"))
    ):
        raise ValueError(
            "attempt binding does not match frozen generation request or manifest workspace"
        )
    bound_timestamp = datetime.fromisoformat(
        binding["bound_at"].replace("Z", "+00:00")
    )
    frozen_timestamp = datetime.fromisoformat(
        prepared["frozen_at"].replace("Z", "+00:00")
    )
    if bound_timestamp.astimezone(timezone.utc) < frozen_timestamp.astimezone(
        timezone.utc
    ):
        raise ValueError("attempt binding timestamp order is invalid")
    record = dict(record_base)
    record["attempt_binding_sha256"] = _sha(encoded)
    return attempt_id, record, encoded


def rollback_attempt_binding(prepared: dict[str, Any], encoded: bytes) -> str | None:
    """Remove only the exact sidecar created by this failed local transaction.

    Returns a note when the sidecar could NOT be removed, so the caller can attach it
    to the original exception instead of losing it.

    **Does not raise on filesystem errors** — it runs inside an ``except`` handler, and
    a permission error here must not replace the real failure (that error is the one
    worth reading). It is deliberately *not* a blanket never-raises: a malformed
    ``prepared`` (KeyError) is a programming bug that should surface, and
    ``KeyboardInterrupt`` must stay interruptible.
    """
    path = prepared["bundle"] / "attempt-binding.json"
    try:
        if path.read_bytes() != encoded:
            return None            # 已被別人換掉,不是我們這次寫的,不碰
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as error:
        return (
            f"attempt-binding.json 殘留在 {path}(清理失敗:{error})——"
            "下次用同一個 bundle 重跑會讀回這份綁定,先確認它指的 attempt 是否真的沒建立"
        )
    # 刪除本身已經成功;目錄項沒刷下去不影響正確性(重跑會重新讀,讀不到就重建),
    # 所以這裡不回 note、也不 raise。
    try:
        fsync_parent(str(path))
    except OSError:
        pass
    return None
