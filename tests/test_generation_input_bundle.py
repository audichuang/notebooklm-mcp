import hashlib
import json

import pytest

from notebooklm_mcp import tools_podcast as p
from notebooklm_mcp.generation_input import load_frozen_generation_input


def _write_bundle(workspace, *, brief="exact frozen brief\n"):
    bundle = workspace / "seasons/s01/qa/attempt-inputs/request-1"
    bundle.mkdir(parents=True)
    files = {
        "runtime_brief": ("runtime-brief.md", brief.encode()),
        "coverage_ledger": (
            "coverage-ledger.json",
            b'{"schema_version":1,"qa_kind":"delivery_coverage_ledger","episode_id":"ep01","rows":[{"id":"x","plan_locator":"plan#x","disposition":"spoken_required","delivery_locator":"brief#x"}]}\n',
        ),
        "evidence_manifest": (
            "evidence-manifest.json",
            b'{"episode_id":"ep01","sources":[]}\n',
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
        "generation_request_id": "request-1",
        "frozen_at": "2026-07-28T00:00:00+00:00",
        "files": records,
        "dispatch_contract": "provider_must_load_runtime_brief_from_this_bundle",
    }
    (bundle / "generation-request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return bundle, brief


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

    with pytest.raises(ValueError, match="durable output"):
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
