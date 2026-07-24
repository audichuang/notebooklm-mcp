import json
import multiprocessing
from pathlib import Path

import pytest

from notebooklm_mcp.manifest_store import ManifestConflictError, ManifestStore


def _increment_episode(path: str, episode_number: int, iterations: int) -> None:
    store = ManifestStore(path)
    for _ in range(iterations):
        def increment(manifest):
            episode = next(
                item
                for item in manifest["episodes"]
                if item["episode"] == episode_number
            )
            episode["updates"] = episode.get("updates", 0) + 1

        store.update(increment)


def test_update_migrates_legacy_manifest_without_losing_unknown_fields(tmp_path):
    path = tmp_path / "series_manifest.json"
    legacy = {
        "notebook_id": "nb-1",
        "show": {"show_id": "audicast", "extra": {"theme": "amber"}},
        "unknown_top_level": ["keep", {"nested": True}],
        "episodes": [
            {
                "episode": 1,
                "title": "心法篇",
                "description": "人工簡介",
                "cover_path": "/covers/ep01.jpg",
                "attachments": {"slides": "/slides/ep01.pdf"},
                "unknown_episode_field": {"keep": 1},
            }
        ],
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    snapshot, result = ManifestStore(path).update(
        lambda manifest: manifest["episodes"][0].update({"mp3_path": "/audio/ep01.mp3"})
    )

    assert result is None
    assert snapshot["schema_version"] == 2
    assert snapshot["revision"] == 1
    assert snapshot["unknown_top_level"] == legacy["unknown_top_level"]
    assert snapshot["show"] == legacy["show"]
    assert snapshot["episodes"][0] == {
        **legacy["episodes"][0],
        "mp3_path": "/audio/ep01.mp3",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot


def test_legacy_read_projects_revision_for_cas_without_rewriting_disk(tmp_path):
    path = tmp_path / "series_manifest.json"
    legacy = {
        "notebook_id": "nb-1",
        "episodes": [{"episode": 1, "title": "心法篇"}],
        "unknown": {"keep": True},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    snapshot = ManifestStore(path).read()

    assert snapshot == {**legacy, "schema_version": 2, "revision": 0}
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


def test_missing_manifest_reads_as_empty_schema_without_creating_data_file(tmp_path):
    path = tmp_path / "series_manifest.json"

    assert ManifestStore(path).read() == {
        "schema_version": 2,
        "revision": 0,
        "episodes": [],
    }
    assert not path.exists()


@pytest.mark.parametrize(
    "contents",
    [
        "{not valid json",
        "[1, 2, 3]",
        json.dumps({"episodes": {}}),
    ],
)
def test_corrupt_manifest_fails_closed_for_read_and_update(tmp_path, contents):
    path = tmp_path / "series_manifest.json"
    path.write_text(contents, encoding="utf-8")
    original = path.read_bytes()
    store = ManifestStore(path)

    with pytest.raises(ValueError, match="manifest"):
        store.read()
    with pytest.raises(ValueError, match="manifest"):
        store.update(lambda manifest: manifest.update({"should_not_land": True}))

    assert path.read_bytes() == original


def test_attempt_pointers_must_reference_an_attempt_in_the_same_episode(tmp_path):
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 4,
                "episodes": [
                    {
                        "episode": 1,
                        "attempts": [{"attempt_id": "attempt-1", "episode": 1}],
                        "active_attempt_id": "missing-attempt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="active_attempt_id"):
        ManifestStore(path).read()


def test_revision_conflict_does_not_call_mutator_or_change_file(tmp_path):
    path = tmp_path / "series_manifest.json"
    original = {
        "schema_version": 2,
        "revision": 3,
        "episodes": [{"episode": 1, "title": "心法篇"}],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    mutator_called = False

    def mutate(manifest):
        nonlocal mutator_called
        mutator_called = True
        manifest["episodes"][0]["title"] = "不應寫入"

    with pytest.raises(ManifestConflictError, match="expected 2, found 3"):
        ManifestStore(path).update(mutate, expected_revision=2)

    assert mutator_called is False
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_successful_update_increments_revision_and_returns_mutator_result(tmp_path):
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 7,
                "episodes": [{"episode": 1, "title": "心法篇"}],
            }
        ),
        encoding="utf-8",
    )

    snapshot, result = ManifestStore(path).update(
        lambda manifest: manifest["episodes"][0].setdefault("description", "重點")
    )

    assert result == "重點"
    assert snapshot["revision"] == 8
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot


def test_validation_failure_after_mutation_preserves_old_manifest(tmp_path):
    path = tmp_path / "series_manifest.json"
    original = {"episodes": [{"episode": 1, "title": "心法篇"}]}
    path.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(ValueError, match="episodes"):
        ManifestStore(path).update(lambda manifest: manifest.update({"episodes": "broken"}))

    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_file_fsync_failure_preserves_old_manifest(tmp_path, monkeypatch):
    path = tmp_path / "series_manifest.json"
    original = {"episodes": [{"episode": 1, "title": "心法篇"}]}
    path.write_text(json.dumps(original), encoding="utf-8")

    def fail_fsync(_fd):
        raise OSError("simulated file fsync failure")

    monkeypatch.setattr("notebooklm_mcp.manifest_store.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="file fsync"):
        ManifestStore(path).update(
            lambda manifest: manifest["episodes"][0].update({"title": "不應寫入"})
        )

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob(".series_manifest.json.*.tmp"))


def test_replace_failure_preserves_old_manifest(tmp_path, monkeypatch):
    path = tmp_path / "series_manifest.json"
    original = {"episodes": [{"episode": 1, "title": "心法篇"}]}
    path.write_text(json.dumps(original), encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("notebooklm_mcp.manifest_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        ManifestStore(path).update(
            lambda manifest: manifest["episodes"][0].update({"title": "不應寫入"})
        )

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob(".series_manifest.json.*.tmp"))


def test_concurrent_process_updates_do_not_lose_either_episode(tmp_path):
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "episodes": [
                    {"episode": 1, "updates": 0},
                    {"episode": 2, "updates": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_increment_episode, args=(str(path), episode_number, 50))
        for episode_number in (1, 2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert [process.exitcode for process in processes] == [0, 0]
    snapshot = ManifestStore(path).read()
    assert snapshot["revision"] == 100
    assert {episode["episode"]: episode["updates"] for episode in snapshot["episodes"]} == {
        1: 50,
        2: 50,
    }
    assert json.loads(Path(path).read_text(encoding="utf-8")) == snapshot


@pytest.mark.parametrize(
    ("attempt", "active_attempt_id"),
    [
        ({"attempt_id": "attempt-1", "episode": True}, "attempt-1"),
        ({"attempt_id": "attempt-1", "episode": 1}, []),
    ],
)
def test_malformed_attempt_references_raise_value_error(
    tmp_path,
    attempt,
    active_attempt_id,
):
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "episodes": [
                    {
                        "episode": 1,
                        "attempts": [attempt],
                        "active_attempt_id": active_attempt_id,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest is corrupt"):
        ManifestStore(path).read()
