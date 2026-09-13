import errno
import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from notebooklm_mcp import tools_podcast as p
from notebooklm_mcp.generation_input import load_frozen_generation_input


def _write_bundle(workspace, *, brief="exact frozen brief\n", request_id="request-1"):
    bundle = workspace / f"seasons/s01/qa/attempt-inputs/{request_id}"
    bundle.mkdir(parents=True)
    files = {
        "runtime_brief": ("runtime-brief.md", brief.encode()),
        "coverage_ledger": (
            "coverage-ledger.json",
            b'{"schema_version":1,"qa_kind":"delivery_coverage_ledger","episode_id":"ep01","rows":[{"id":"x","plan_locator":"plan#x","disposition":"spoken_required","delivery_locator":"brief#x"}]}\n',
        ),
        "evidence_manifest": (
            "evidence-manifest.json",
            b'{"schema_version":1,"qa_kind":"generation_evidence_manifest","episode_id":"ep01","artifacts":[{"id":"packet","path":"seasons/s01/sources/ep01.md","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","bytes":1}]}\n',
        ),
    }
    records = {}
    for key, (name, data) in files.items():
        (bundle / name).write_bytes(data)
        records[key] = {
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    request = {
        "schema_version": 1,
        "qa_kind": "generation_attempt_input",
        "status": "frozen",
        "episode_id": "ep01",
        "generation_request_id": request_id,
        "frozen_at": "2026-07-28T00:00:00+00:00",
        "files": records,
        "dispatch_contract": "provider_must_load_runtime_brief_from_this_bundle",
    }
    (bundle / "generation-request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return bundle, brief


def _replace_bundle_file(bundle: Path, key: str, data: bytes) -> None:
    request_path = bundle / "generation-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    filename = request["files"][key]["path"]
    (bundle / filename).write_bytes(data)
    request["files"][key]["sha256"] = hashlib.sha256(data).hexdigest()
    request["files"][key]["bytes"] = len(data)
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")


async def test_manifest_episode_dispatches_exact_frozen_brief_and_binds_before_rpc(
    fake_client, tmp_path
):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    output_dir = workspace / "output"
    bundle, brief = _write_bundle(workspace)

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )

    generate = next(call for call in fake_client.artifacts.calls if call[0] == "generate_audio")
    assert generate[1]["instructions"] == brief
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = manifest["episodes"][0]["attempts"][0]
    assert attempt["brief_sha256"] == hashlib.sha256(brief.encode()).hexdigest()
    assert attempt["input_bundle"]["generation_request_id"] == "request-1"
    binding = json.loads((bundle / "attempt-binding.json").read_text(encoding="utf-8"))
    assert binding["attempt_id"] == attempt["attempt_id"]
    assert attempt["input_bundle"]["attempt_binding_sha256"] == hashlib.sha256(
        (bundle / "attempt-binding.json").read_bytes()
    ).hexdigest()
    assert list(bundle.glob(".attempt-binding.*.tmp")) == []


async def test_frozen_bundle_rejects_competing_inline_brief_before_rpc(fake_client, tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)

    with pytest.raises(ValueError, match="brief must be None"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="competing inline text",
            output_dir=str(workspace / "output"),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )
    assert fake_client.artifacts.calls == []


async def test_frozen_bundle_hash_drift_fails_before_manifest_or_rpc(fake_client, tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    original = (bundle / "runtime-brief.md").read_bytes()
    (bundle / "runtime-brief.md").write_bytes(b"X" + original[1:])

    with pytest.raises(ValueError, match="SHA-256"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(workspace / "output"),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )
    assert not manifest_path.exists()
    assert fake_client.artifacts.calls == []


@pytest.mark.parametrize(
    "target",
    ["generation_request", "coverage_ledger", "evidence_manifest"],
)
def test_schema_version_boolean_is_rejected(target, tmp_path):
    manifest = tmp_path / "manifest" / "series_manifest.json"
    bundle, _ = _write_bundle(tmp_path)
    if target == "generation_request":
        request_path = bundle / "generation-request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["schema_version"] = True
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        request = json.loads(
            (bundle / "generation-request.json").read_text(encoding="utf-8")
        )
        filename = request["files"][target]["path"]
        payload = json.loads((bundle / filename).read_text(encoding="utf-8"))
        payload["schema_version"] = True
        _replace_bundle_file(
            bundle,
            target,
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    with pytest.raises(ValueError, match="metadata|coverage ledger|evidence manifest"):
        load_frozen_generation_input(
            manifest_path=manifest,
            input_bundle_path=bundle.relative_to(tmp_path),
            episode_n=1,
        )


def test_bundle_directory_swap_cannot_redirect_frozen_reads(tmp_path):
    manifest = tmp_path / "manifest" / "series_manifest.json"
    bundle, trusted_brief = _write_bundle(tmp_path, brief="trusted inside\n")
    outside_root = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_root.mkdir()
    outside_bundle, _ = _write_bundle(outside_root, brief="attacker outside\n")
    held = tmp_path / "held-original"
    original_is_dir = Path.is_dir
    swapped = False

    def swap_after_check(path):
        nonlocal swapped
        result = original_is_dir(path)
        if path == bundle and not swapped:
            swapped = True
            bundle.rename(held)
            bundle.symlink_to(outside_bundle, target_is_directory=True)
        return result

    try:
        with patch.object(Path, "is_dir", swap_after_check):
            try:
                prepared = load_frozen_generation_input(
                    manifest_path=manifest,
                    input_bundle_path=bundle.relative_to(tmp_path),
                    episode_n=1,
                )
            except ValueError:
                return
        assert prepared["brief"] == trusted_brief
    finally:
        shutil.rmtree(outside_root, ignore_errors=True)


@pytest.mark.parametrize(
    ("key", "data", "message"),
    [
        ("coverage_ledger", b"{}\n", "coverage ledger"),
        ("evidence_manifest", b'{"episode_id":"ep01"}\n', "evidence manifest"),
    ],
)
async def test_self_consistent_malformed_bundle_schema_fails_before_auth_manifest_or_rpc(
    fake_client, tmp_path, key, data, message
):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    _replace_bundle_file(bundle, key, data)

    with pytest.raises(ValueError, match=message):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(workspace / "output"),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )
    assert not manifest_path.exists()
    assert fake_client.artifacts.calls == []


async def test_future_frozen_request_is_rejected_before_binding_auth_or_rpc(
    fake_client, tmp_path, monkeypatch
):
    manifest = tmp_path / "manifest" / "series_manifest.json"
    bundle, _ = _write_bundle(tmp_path)
    request_path = bundle / "generation-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["frozen_at"] = "2999-01-01T00:00:00+00:00"
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    auth_calls = 0

    async def counted_probe(_client):
        nonlocal auth_calls
        auth_calls += 1

    monkeypatch.setattr(p, "probe_auth", counted_probe)
    with pytest.raises(ValueError, match="timestamp order"):
        await p.podcast_episode(
            "notebook-1",
            episode_n=1,
            title="EP01",
            brief=None,
            output_dir=str(tmp_path / "output"),
            manifest_path=str(manifest),
            input_bundle_path=str(bundle.relative_to(tmp_path)),
        )
    assert not (bundle / "attempt-binding.json").exists()
    assert auth_calls == 0
    assert fake_client.artifacts.calls == []


async def test_attempt_binding_must_not_precede_frozen_request(
    fake_client, tmp_path, monkeypatch
):
    manifest = tmp_path / "manifest" / "series_manifest.json"
    bundle, _ = _write_bundle(tmp_path)
    bundle_relative = bundle.relative_to(tmp_path)
    prepared = load_frozen_generation_input(
        manifest_path=manifest, input_bundle_path=bundle_relative, episode_n=1
    )
    binding = {
        "schema_version": 1,
        "qa_kind": "generation_attempt_input_binding",
        "episode_id": "ep01",
        "generation_request_id": prepared["record_base"]["generation_request_id"],
        "attempt_id": "attempt-backdated",
        "generation_request_sha256": prepared["record_base"][
            "generation_request_sha256"
        ],
        "manifest_workspace_sha256": prepared["record_base"][
            "manifest_workspace_sha256"
        ],
        "bound_at": "2026-07-27T23:59:59+00:00",
    }
    (bundle / "attempt-binding.json").write_text(
        json.dumps(binding, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    auth_calls = 0

    async def counted_probe(_client):
        nonlocal auth_calls
        auth_calls += 1

    monkeypatch.setattr(p, "probe_auth", counted_probe)
    with pytest.raises(ValueError, match="binding timestamp order"):
        await p.podcast_episode(
            "notebook-1",
            episode_n=1,
            title="EP01",
            brief=None,
            output_dir=str(tmp_path / "output"),
            manifest_path=str(manifest),
            input_bundle_path=str(bundle_relative),
        )
    assert auth_calls == 0
    assert fake_client.artifacts.calls == []


async def test_bound_bundle_copied_to_another_manifest_workspace_cannot_replay(
    fake_client, tmp_path, monkeypatch
):
    auth_calls = 0

    async def counted_probe(_client):
        nonlocal auth_calls
        auth_calls += 1

    monkeypatch.setattr(p, "probe_auth", counted_probe)
    first = tmp_path / "first"
    first_manifest = first / "manifest/series_manifest.json"
    first_manifest.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(first)
    relative_bundle = bundle.relative_to(first)
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(first / "output"),
        manifest_path=str(first_manifest),
        input_bundle_path=str(relative_bundle),
    )
    calls_before = len(
        [call for call in fake_client.artifacts.calls if call[0] == "generate_audio"]
    )

    second = tmp_path / "second"
    second_manifest = second / "manifest/series_manifest.json"
    second_manifest.parent.mkdir(parents=True)
    shutil.copytree(bundle, second / relative_bundle)
    with pytest.raises(ValueError, match="manifest workspace"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(second / "output"),
            manifest_path=str(second_manifest),
            input_bundle_path=str(relative_bundle),
        )
    assert not second_manifest.exists()
    assert auth_calls == 1
    assert len(
        [call for call in fake_client.artifacts.calls if call[0] == "generate_audio"]
    ) == calls_before


async def test_pre_dispatch_baseline_failure_keeps_frozen_attempt_prepared_and_retryable(
    fake_client, tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    real_list = fake_client.artifacts.list
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionError("baseline unavailable")
        return await real_list(*args, **kwargs)

    monkeypatch.setattr(fake_client.artifacts, "list", fail_once)
    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(workspace / "output"),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )
    with pytest.raises(ConnectionError, match="baseline unavailable"):
        await p.podcast_episode("nb-1", **args)
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = first["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "prepared"
    assert not any(call[0] == "generate_audio" for call in fake_client.artifacts.calls)

    await p.podcast_episode("nb-1", **args)
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final["episodes"][0]["attempts"][0]["attempt_id"] == attempt["attempt_id"]


async def test_manifest_postcommit_fsync_error_keeps_binding_for_public_retry(
    fake_client, tmp_path, monkeypatch
):
    """manifest 的 replace 已提交後即使 directory fsync 回 EIO，呼叫端也不能把
    frozen binding 當成未提交而刪掉；相同 public request 必須可沿用同一 attempt。"""
    from notebooklm_mcp import manifest_store

    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    real_fsync_parent = manifest_store._fsync_parent
    calls = 0

    def fail_first_manifest_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "simulated post-commit directory fsync failure")
        return real_fsync_parent(path)

    monkeypatch.setattr(manifest_store, "_fsync_parent", fail_first_manifest_fsync)
    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(workspace / "output"),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )

    with pytest.raises(OSError, match="committed"):
        await p.podcast_episode("nb-1", **args)

    binding_path = bundle / "attempt-binding.json"
    assert binding_path.exists()
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    committed = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_attempt = committed["episodes"][0]["attempts"][0]
    assert first_attempt["attempt_id"] == binding["attempt_id"]
    assert not any(call[0] == "generate_audio" for call in fake_client.artifacts.calls)

    result = await p.podcast_episode("nb-1", **args)
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["attempt_id"] == first_attempt["attempt_id"]
    assert len(final["episodes"][0]["attempts"]) == 1
    assert sum(call[0] == "generate_audio" for call in fake_client.artifacts.calls) == 1


async def test_binding_sidecar_rolls_back_when_manifest_rejects_attempt(fake_client, tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    output_dir = workspace / "output"
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="existing output",
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
    )
    bundle, _ = _write_bundle(workspace)
    calls_before = len(fake_client.artifacts.calls)

    with pytest.raises(ValueError, match="refusing to silently overwrite"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(output_dir),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )

    assert not (bundle / "attempt-binding.json").exists()
    assert len(fake_client.artifacts.calls) == calls_before


async def test_same_frozen_bundle_reuses_not_accepted_attempt(fake_client, tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    output_dir = workspace / "output"
    bundle, brief = _write_bundle(workspace)
    fake_client.artifacts.fail_generate = True

    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(output_dir),
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = first["episodes"][0]["active_attempt_id"]

    fake_client.artifacts.fail_generate = False
    result = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )

    assert result["artifact_id"]
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(final["episodes"][0]["attempts"]) == 1
    assert final["episodes"][0]["output_attempt_id"] == attempt_id
    assert [
        call[1]["instructions"]
        for call in fake_client.artifacts.calls
        if call[0] == "generate_audio"
    ] == [brief, brief]


async def test_reuse_frozen_input_attempt_points_at_a_way_out_that_actually_works(
    fake_client, tmp_path
):
    """**F2:`_reuse_frozen_input_attempt` 是單一事實來源唯一漏收的出口。**

    可達狀態之一:frozen bundle 送出成功、遠端回終態失敗(`remote.status` in
    failed/removed),而 `dispatch.status` 停在 `accepted`(從未回到 `not_accepted`/
    `prepared`)。呼叫端照冪等契約原樣重呼同一個 frozen bundle 撞進這條路——舊訊息
    手寫死「reconcile or resume it」,但兩條建議都走不通:reconcile 被
    `dispatch_status not in (...)` 的狀態檢查擋(它只收 acceptance_unknown /
    reconciliation_ambiguous),resume 因為終態已定直接拋 `TerminalGenerationError`。
    而同一顆 attempt 餵進 `_attempt_capabilities` 的答案是 `authorization_basis=
    "settled"`——免旗標 retract 才是唯一走得通的出口。
    """
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    output_dir = workspace / "output"
    bundle, _ = _write_bundle(workspace)

    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )
    fake_client.artifacts.fail_complete = True  # wait 回終態失敗 → TerminalGenerationError
    with pytest.raises(RuntimeError):
        await p.podcast_episode("nb-1", **args)
    fake_client.artifacts.fail_complete = False

    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = first["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted", attempt["dispatch"]
    assert attempt["remote"]["status"] in ("failed", "removed"), attempt["remote"]

    # 冪等契約:呼叫端原樣重呼同一個 frozen bundle,撞進 `_reuse_frozen_input_attempt`
    # 的 fallback 分支。
    with pytest.raises(ValueError) as excinfo:
        await p.podcast_episode("nb-1", **args)
    message = str(excinfo.value)
    assert "podcast_attempt_retract" in message, message
    assert "不需要" in message and "abandon_in_flight" in message, message
    # 舊訊息的死路建議不該再出現。
    assert "reconcile or resume it instead" not in message, message


async def test_settled_frozen_attempt_retract_and_regenerate_with_a_new_bundle_actually_works(
    fake_client, tmp_path
):
    """**P2(item 4)完整序列**:settled 分支附上 `regeneration_hint` 之後,照著做真的
    走得通,不是只有字面上出現而已。

    只斷言訊息包含「retract」字樣測不出「retract 之後呢?」——`_reuse_frozen_input_
    attempt` 這條路上 `regeneration_entry` 恆為 `podcast_episode`,而沿用同一個
    frozen bundle 目錄重生會被 tombstone 擋下來(bundle 裡的 attempt-binding.json
    還綁著剛作廢的那顆)。這裡把整條路走一次:settled attempt → 免旗標 retract →
    用**新的、尚未綁定**的 frozen bundle 重生成功。
    """
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    output_dir = workspace / "output"
    bundle_1, _ = _write_bundle(workspace, request_id="request-1")

    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle_1.relative_to(workspace)),
    )
    fake_client.artifacts.fail_complete = True  # wait 回終態失敗 → TerminalGenerationError
    with pytest.raises(RuntimeError):
        await p.podcast_episode("nb-1", **args)
    fake_client.artifacts.fail_complete = False

    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = first["episodes"][0]["active_attempt_id"]
    attempt = first["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted", attempt["dispatch"]
    assert attempt["remote"]["status"] in ("failed", "removed"), attempt["remote"]

    # 撞進 settled 分支,確認 `regeneration_hint` 真的附上了(P2 修復點)。
    with pytest.raises(ValueError) as excinfo:
        await p.podcast_episode("nb-1", **args)
    message = str(excinfo.value)
    assert "尚未綁定的 frozen bundle" in message, message

    # 照 hint 做:免旗標(authorization_basis="settled")retract。
    retraction = await p.podcast_attempt_retract(
        str(manifest_path), 1, attempt_id, reason="settled,換新 bundle 重生"
    )
    assert retraction["authorization_basis"] == "settled"
    assert retraction["stale_source_ids"] == []  # 從沒 finalize 過,沒有回錄 source 要清

    # 用新的、尚未綁定的 frozen bundle 重生——這才是 hint 真正要驗的事:沿用
    # bundle_1 會被它自己的 attempt-binding.json 擋下來,必須是全新的 bundle。
    bundle_2, _ = _write_bundle(workspace, request_id="request-2")
    result = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(output_dir),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle_2.relative_to(workspace)),
    )
    assert result["artifact_id"]


def test_frozen_input_rejects_absolute_bundle_path(tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)

    with pytest.raises(ValueError, match="relative"):
        load_frozen_generation_input(
            manifest_path=str(manifest_path),
            input_bundle_path=str(bundle),
            episode_n=1,
        )


def test_frozen_input_rejects_symlink_ancestor(tmp_path):
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    alias = workspace / "bundle-parent-alias"
    alias.symlink_to(bundle.parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        load_frozen_generation_input(
            manifest_path=str(manifest_path),
            input_bundle_path=f"{alias.name}/{bundle.name}",
            episode_n=1,
        )


# ---- v0.4.1:manifest 佈局不該被目錄名綁死 ----------------------------------------
# 原本要求 `<workspace>/manifest/series_manifest.json`,那正好等於上面 fixture 的形狀
# —— 所以四個既有測試全綠,卻在真實 workspace 一定失敗。三個 podcast 專案的佈局:
#   network-podcast/manifest/…  podcast-lab/output/…  data-structure-podcast/season-01/output/…
# 只有第一個過得了。目錄名不是安全邊界,containment 由 relative_to(workspace) 保證。


async def test_output_layout_workspace_is_accepted(fake_client, tmp_path):
    """podcast-lab(Audicast)的真實佈局:manifest 在 output/ 而不是 manifest/。"""
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "output/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, brief = _write_bundle(workspace)

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(workspace / "output"),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )
    generate = next(c for c in fake_client.artifacts.calls if c[0] == "generate_audio")
    assert generate[1]["instructions"] == brief


async def test_bundle_outside_the_workspace_is_still_rejected(fake_client, tmp_path):
    """放鬆目錄名之後,containment 仍然要擋得住 —— 這才是真正的邊界。"""
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "output/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    outsider = tmp_path / "elsewhere"
    bundle, _ = _write_bundle(outsider)

    with pytest.raises(ValueError, match="confined relative"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(workspace / "output"),
            manifest_path=str(manifest_path),
            input_bundle_path=(
                "../elsewhere/" + bundle.relative_to(outsider).as_posix()
            ),
        )
    assert fake_client.artifacts.calls == []


async def test_a_non_series_manifest_path_is_rejected(fake_client, tmp_path):
    workspace = tmp_path / "workspace"
    other = workspace / "output/other.json"
    other.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)

    with pytest.raises(ValueError, match="series_manifest.json"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief=None,
            output_dir=str(workspace / "output"),
            manifest_path=str(other),
            input_bundle_path=str(bundle.relative_to(workspace)),
        )


async def test_rebinding_the_same_bundle_reuses_the_attempt(fake_client, tmp_path):
    """retract 之後用同一個 bundle 重跑:讀回既有綁定、沿用同一個 attempt_id,
    不會拿到裸的 FileExistsError,也不會重複建 attempt。"""
    workspace = tmp_path / "workspace"
    manifest_path = workspace / "output/series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    args = dict(episode_n=1, title="心法篇", brief=None,
                output_dir=str(workspace / "output"),
                manifest_path=str(manifest_path), input_bundle_path=str(bundle.relative_to(workspace)))

    await p.podcast_episode("nb-1", **args)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first = manifest["episodes"][0]["attempts"][0]["attempt_id"]
    binding = json.loads((bundle / "attempt-binding.json").read_text(encoding="utf-8"))
    assert binding["attempt_id"] == first


# ---- v0.4.2:sidecar 沿用 _atomic 的 commit-point 紀律 -----------------------------
# 這兩條 repo 早就為簡報/講義學過(test_atomic_replace_preserves_existing_file_mode /
# test_unsupported_directory_fsync_does_not_fail_the_download),但 sidecar 這條路自己
# 重寫了一份 fsync,於是又漏了一次。改成重用 _atomic.fsync_parent 並在此鎖住。


def _prepared(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    return {
        "bundle": bundle,
        "episode_id": "ep01",
        # binding 的 bound_at 不得早於 request 的 frozen_at,所以 helper 得給一個過去的
        # 凍結時刻(load_frozen_generation_input 也是這樣把它塞進 prepared 的)。
        "frozen_at": "2026-07-28T00:00:00+00:00",
        "record_base": {
            "generation_request_id": "request-1", "path": "bundle",
            "generation_request_sha256": "x",
            "manifest_workspace_sha256": "z",
            "runtime_brief_sha256": "y", "runtime_brief_bytes": 1,
        },
    }


def test_binding_sidecar_is_group_readable(tmp_path):
    """mkstemp 給 0600,而 os.link 會把 temp 的 mode 帶到最終檔——證據檔不該悄悄
    變成只有本人讀得到(同 _atomic 的教訓,那邊用 chmod 修掉)。"""
    import stat
    from notebooklm_mcp.generation_input import write_attempt_binding

    prepared = _prepared(tmp_path)
    write_attempt_binding(prepared, attempt_id="a1")
    mode = stat.S_IMODE((prepared["bundle"] / "attempt-binding.json").stat().st_mode)
    assert mode == 0o644


def test_unsupported_directory_fsync_does_not_undo_the_binding(tmp_path, monkeypatch):
    """有些 filesystem 不支援 directory fsync(EINVAL/ENOTSUP)。那不是失敗——
    舊版把 fsync 放在 try 裡,一拋就把剛建立的綁定刪掉,整個 dispatch 陪葬。"""
    import errno
    from notebooklm_mcp import generation_input as gi

    prepared = _prepared(tmp_path)
    monkeypatch.setattr(
        gi, "fsync_parent",
        lambda path: (_ for _ in ()).throw(OSError(errno.EINVAL, "Invalid argument")),
    )
    record, encoded = gi.write_attempt_binding(prepared, attempt_id="a1")
    binding_path = prepared["bundle"] / "attempt-binding.json"
    assert binding_path.read_bytes() == encoded          # 綁定還在
    assert record["attempt_binding_sha256"]
    assert list(prepared["bundle"].glob(".attempt-binding.*.tmp")) == []


def test_real_directory_fsync_error_says_the_binding_already_exists(tmp_path, monkeypatch):
    """真 IO 錯誤仍要 raise,但訊息必須講明綁定已經建立——否則呼叫端會當成
    「沒綁到」而重試,結果撞上 FileExistsError。"""
    import errno
    from notebooklm_mcp import generation_input as gi

    prepared = _prepared(tmp_path)
    monkeypatch.setattr(
        gi, "fsync_parent",
        lambda path: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")),
    )
    with pytest.raises(OSError, match="已經建立"):
        gi.write_attempt_binding(prepared, attempt_id="a1")
    assert (prepared["bundle"] / "attempt-binding.json").exists()   # 誠實反映:沒回滾
    assert list(prepared["bundle"].glob(".attempt-binding.*.tmp")) == []


def test_mode_is_persisted_before_the_file_fsync(tmp_path, monkeypatch):
    """chmod 必須在 fsync **之前**(_atomic 的順序)。順序反過來時,斷電可能持久化
    「0600 的 inode + 新 dirent」而 mode 沒落地——只驗最終 mode 抓不到這個。"""
    from notebooklm_mcp import generation_input as gi

    order = []
    real_fchmod, real_fsync, real_link = os.fchmod, os.fsync, os.link
    monkeypatch.setattr(os, "fchmod", lambda fd, m: (order.append("chmod"), real_fchmod(fd, m))[1])
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "link", lambda s, d: (order.append("link"), real_link(s, d))[1])

    gi.write_attempt_binding(_prepared(tmp_path), attempt_id="a1")
    # 完整序列:chmod → 檔案 fsync → link(發布點)→ 目錄 fsync。
    assert order == ["chmod", "fsync", "link", "fsync"]


def test_cleanup_failure_does_not_mask_the_real_error(tmp_path, monkeypatch):
    """pre-commit 清 temp 失敗時,呼叫端要看到的是原本那個有操作指引的錯誤,
    不是 unlink 的 PermissionError。"""
    import errno
    from notebooklm_mcp import generation_input as gi

    prepared = _prepared(tmp_path)
    gi.write_attempt_binding(prepared, attempt_id="a1")      # 先佔住 → 第二次會撞 link

    def refuse(self, missing_ok=False):
        raise PermissionError(errno.EACCES, "read-only filesystem")

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(ValueError, match="併發搶先綁定"):     # 不是 PermissionError
        gi.write_attempt_binding(prepared, attempt_id="a2")


def test_second_write_never_overwrites_an_existing_binding(tmp_path):
    """O_EXCL 語義:一個 bundle 只綁一次,既有綁定的 bytes 不得被換掉。"""
    from notebooklm_mcp import generation_input as gi

    prepared = _prepared(tmp_path)
    _, first = gi.write_attempt_binding(prepared, attempt_id="a1")
    with pytest.raises(ValueError):
        gi.write_attempt_binding(prepared, attempt_id="a2")
    assert (prepared["bundle"] / "attempt-binding.json").read_bytes() == first


def test_post_publish_temp_cleanup_failure_keeps_the_binding(tmp_path, monkeypatch):
    """發布之後清 temp 失敗不得回滾 —— 刪掉成功的綁定會讓重跑誤以為沒綁過而重建 attempt。"""
    import errno
    from notebooklm_mcp import generation_input as gi

    prepared = _prepared(tmp_path)
    real_unlink = Path.unlink

    def refuse_temp(self, missing_ok=False):
        if self.name.startswith(".attempt-binding."):
            raise PermissionError(errno.EACCES, "read-only filesystem")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_temp)
    _, encoded = gi.write_attempt_binding(prepared, attempt_id="a1")
    assert (prepared["bundle"] / "attempt-binding.json").read_bytes() == encoded


# ---- workspace_root:圍籬由 host 宣告,不從目錄深度推 ------------------------------------
# podcast-lab 的真實形狀:`shows/` 底下十幾個節目,其中五個把 manifest 直接放 show root。
# 「workspace = manifest 的祖父目錄」在那些節目上會變成**整個 shows/**——2026-08-30 實測
# graphify 的 manifest 吃進了 audicast 的 bundle(回 binding None),下一步就是拿別節目的
# brief 燒配額。目錄深度不是安全邊界,所以圍籬改由 host 宣告;推不出唯一節目時 fail-closed。


def _multi_show_container(tmp_path, *, hidden=False):
    shows = tmp_path / "shows"
    (shows / "graphify").mkdir(parents=True)
    # hidden=True:sibling manifest 搬進隱藏目錄(退役節目常見形狀是「搬進看不見的
    # 資料夾」,不是刪掉——一次 `mv shows/old shows/.old` 就是這裡)。
    sibling_show = ".archive/audicast" if hidden else "audicast"
    (shows / sibling_show / "output").mkdir(parents=True)
    (shows / sibling_show / "output" / "series_manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    return shows


@pytest.mark.parametrize("hidden", [False, True])
def test_multi_show_container_is_refused_without_workspace_root(tmp_path, hidden):
    """祖父目錄裡還有別的 series_manifest.json = 多節目容器 → 沒宣告 workspace_root 就拒,
    而且要在讀任何 bundle bytes 之前拒(別節目的 bundle 路徑合法、檔案都在,靠圍籬才擋得住)。
    隱藏目錄(如 `.archive/`)裡的 sibling manifest 一樣要被抓到——曾經被跳過,等於幫
    `base/showA/…` + `base/.archive/showB/series_manifest.json` 這種佈局開一個後門。"""
    shows = _multi_show_container(tmp_path, hidden=hidden)
    foreign, _ = _write_bundle(shows / "audicast")

    with pytest.raises(ValueError, match="workspace_root"):
        load_frozen_generation_input(
            manifest_path=str(shows / "graphify" / "series_manifest.json"),
            input_bundle_path=foreign.relative_to(shows).as_posix(),
            episode_n=1,
        )


def test_workspace_root_confines_bundle_to_the_declared_show(tmp_path):
    shows = _multi_show_container(tmp_path)
    own, brief = _write_bundle(shows / "graphify")
    foreign, _ = _write_bundle(shows / "audicast")
    manifest = shows / "graphify" / "series_manifest.json"
    root = str(shows / "graphify")

    prepared = load_frozen_generation_input(
        manifest_path=str(manifest),
        input_bundle_path=own.relative_to(shows / "graphify").as_posix(),
        episode_n=1,
        workspace_root=root,
    )
    assert prepared["brief"] == brief
    # record 的 path 相對於宣告的 workspace,不再帶節目前綴
    assert prepared["record_base"]["path"] == own.relative_to(shows / "graphify").as_posix()

    # 別節目的 bundle:只能用 `..` 才指得到,圍籬擋下
    with pytest.raises(ValueError, match="confined"):
        load_frozen_generation_input(
            manifest_path=str(manifest),
            input_bundle_path="../audicast/" + foreign.relative_to(shows / "audicast").as_posix(),
            episode_n=1,
            workspace_root=root,
        )
    # manifest 自己不在宣告的 workspace 裡:宣告與 manifest 各說各話,拒
    with pytest.raises(ValueError, match="workspace_root"):
        load_frozen_generation_input(
            manifest_path=str(shows / "audicast" / "output" / "series_manifest.json"),
            input_bundle_path=own.relative_to(shows / "graphify").as_posix(),
            episode_n=1,
            workspace_root=root,
        )


async def test_podcast_episode_passes_workspace_root_through(fake_client, tmp_path):
    """工具層把 workspace_root 傳下去,show-root manifest 的節目就能在多節目容器裡用凍結輸入。"""
    shows = _multi_show_container(tmp_path)
    own, brief = _write_bundle(shows / "graphify")

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(shows / "graphify"),
        manifest_path=str(shows / "graphify" / "series_manifest.json"),
        input_bundle_path=own.relative_to(shows / "graphify").as_posix(),
        workspace_root=str(shows / "graphify"),
    )
    generate = next(c for c in fake_client.artifacts.calls if c[0] == "generate_audio")
    assert generate[1]["instructions"] == brief
    binding = json.loads((own / "attempt-binding.json").read_text(encoding="utf-8"))
    assert binding["manifest_workspace_sha256"] == hashlib.sha256(
        str((shows / "graphify" / "series_manifest.json").resolve()).encode("utf-8")
    ).hexdigest()
