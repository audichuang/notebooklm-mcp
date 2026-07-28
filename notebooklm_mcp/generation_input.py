"""Frozen generation-input bundles consumed immediately before podcast dispatch."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BUNDLE_FILES = {
    "runtime_brief": "runtime-brief.md",
    "coverage_ledger": "coverage-ledger.json",
    "evidence_manifest": "evidence-manifest.json",
}


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


def load_frozen_generation_input(
    *, manifest_path: str, input_bundle_path: str, episode_n: int
) -> dict[str, Any]:
    """Read each frozen byte sequence once and return the exact provider brief.

    **Workspace** = the manifest's grandparent (`<workspace>/<any>/series_manifest.json`),
    and it is the only containment boundary: the bundle must resolve inside it, with no
    symlink on any segment. The manifest's parent directory name is deliberately NOT
    constrained — a directory *name* is not a security boundary, and the three podcast
    workspaces in use disagree on it (`manifest/`, `output/`, `season-01/output/`).
    Requiring one of them made the feature unreachable everywhere but one project,
    while adding nothing that ``relative_to(workspace)`` did not already guarantee.
    """
    manifest = Path(manifest_path).expanduser().resolve(strict=False)
    if manifest.name != "series_manifest.json":
        raise ValueError("frozen input requires a series_manifest.json path")
    workspace = manifest.parent.parent.resolve()
    raw_bundle = Path(input_bundle_path).expanduser()
    if raw_bundle.is_absolute() or ".." in raw_bundle.parts:
        raise ValueError("input_bundle_path must be a confined relative path")
    candidate_bundle = workspace / raw_bundle
    current = workspace
    for part in raw_bundle.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("input bundle path must not contain symlinks")
    bundle = candidate_bundle.resolve(strict=False)
    try:
        relative = bundle.relative_to(workspace)
    except ValueError as error:
        raise ValueError("input bundle must stay inside manifest workspace") from error
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("input bundle must not traverse symlinks")
    if not bundle.is_dir():
        raise ValueError("input bundle must be a real directory")

    request_path = bundle / "generation-request.json"
    if not request_path.is_file() or request_path.is_symlink():
        raise ValueError("generation-request.json is missing")
    request_bytes = request_path.read_bytes()
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
    ):
        raise ValueError("generation request metadata is invalid")
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
        path = bundle / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"frozen {key} must be a regular file")
        data = path.read_bytes()
        if len(data) != record["bytes"]:
            raise ValueError(f"{key} byte count mismatch")
        if _sha(data) != record["sha256"]:
            raise ValueError(f"{key} SHA-256 mismatch")
        loaded[key] = data

    try:
        brief = loaded["runtime_brief"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("runtime brief must be UTF-8") from error
    return {
        "brief": brief,
        "bundle": bundle,
        "episode_id": request["episode_id"],
        "record_base": {
            "generation_request_id": request["generation_request_id"],
            "path": relative.as_posix(),
            "generation_request_sha256": _sha(request_bytes),
            "runtime_brief_sha256": files["runtime_brief"]["sha256"],
            "runtime_brief_bytes": files["runtime_brief"]["bytes"],
        },
    }


def write_attempt_binding(
    prepared: dict[str, Any], *, attempt_id: str
) -> tuple[dict[str, Any], bytes]:
    bundle = prepared["bundle"]
    record_base = prepared["record_base"]
    binding = {
        "schema_version": 1,
        "qa_kind": "generation_attempt_input_binding",
        "episode_id": prepared["episode_id"],
        "generation_request_id": record_base["generation_request_id"],
        "attempt_id": attempt_id,
        "generation_request_sha256": record_base["generation_request_sha256"],
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = (json.dumps(binding, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = bundle / "attempt-binding.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as error:
        # 呼叫端一律先 read_attempt_binding;走到這裡代表**併發**——sidecar 在那次讀取
        # 之後才出現。裸的 errno 17 在這條路上讀不出原因,所以講清楚並給下一步。
        raise ValueError(
            f"attempt-binding.json 在讀取後才出現({path});"
            "同一個 bundle 有另一個 dispatch 併發搶先綁定。重跑一次即可——"
            "重跑會讀回既有綁定並沿用同一個 attempt,不會重複燒配額"
        ) from error
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    record = dict(record_base)
    record["attempt_binding_sha256"] = _sha(encoded)
    return record, encoded


def read_attempt_binding(
    prepared: dict[str, Any],
) -> tuple[str, dict[str, Any], bytes] | None:
    """Read back a previously persisted binding for safe local retry."""
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
        or not isinstance(attempt_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", attempt_id) is None
        or not isinstance(binding.get("bound_at"), str)
    ):
        raise ValueError("attempt binding does not match frozen generation request")
    record = dict(record_base)
    record["attempt_binding_sha256"] = _sha(encoded)
    return attempt_id, record, encoded


def rollback_attempt_binding(prepared: dict[str, Any], encoded: bytes) -> str | None:
    """Remove only the exact sidecar created by this failed local transaction.

    Returns a note when the sidecar could NOT be removed, so the caller can attach it
    to the original exception instead of losing it. Never raises: this runs inside an
    ``except`` handler, and a permission error here must not replace the real failure
    (that error is the one worth reading).
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
    return None
