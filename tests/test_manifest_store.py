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
                item for item in manifest["episodes"] if item["episode"] == episode_number
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


def test_new_manifest_file_is_not_left_private(tmp_path):
    """mkstemp 給 0600;首次寫入沒有舊檔可繼承 mode,不該落成 0600(對齊
    _atomic.download_atomically 同一顧慮——QA／發布之類的其他帳號會讀不到)。"""
    import os
    import stat

    path = tmp_path / "series_manifest.json"
    ManifestStore(path).update(lambda manifest: manifest["episodes"].append({"episode": 1}))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_update_preserves_existing_manifest_file_mode(tmp_path):
    """os.replace 會把 mkstemp 的 0600 帶到最終檔——手動 chmod 之後,任一次 update
    不能把它打回別的值。改成 chmod 0o600(不是首寫本來就會落地的 0o644)才真的鎖住
    「os.stat 讀到既有檔案就沿用」那個分支;chmod 644 是 no-op,測不出東西。"""
    import os
    import stat

    path = tmp_path / "series_manifest.json"
    ManifestStore(path).update(lambda manifest: manifest["episodes"].append({"episode": 1}))
    os.chmod(path, 0o600)

    ManifestStore(path).update(lambda manifest: manifest["episodes"][0].update({"title": "心法篇"}))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


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


def test_existing_notebook_split_manifest_stays_readable(tmp_path):
    """notebook 一致性只在寫入端擋。v0.5.0 之前 _create_audio_attempt 沒有這道 guard,
    線上可能已經有 episode/attempt 身分分裂的 manifest;若讀取端也驗,那些 manifest 每次
    read() 都 raise,連 podcast_attempt_retract(修復正門)與回填腳本都打不開,唯一出路
    變成 ADR-0009 禁止的手改 JSON。讀得進來、才修得掉。"""
    path = tmp_path / "series_manifest.json"
    split = {
        "schema_version": 2,
        "revision": 3,
        "episodes": [
            {
                "episode": 1,
                "notebook_id": "nb-A",
                "attempts": [{"attempt_id": "att-1", "episode": 1, "notebook_id": "nb-B"}],
            }
        ],
    }
    path.write_text(json.dumps(split), encoding="utf-8")

    snapshot = ManifestStore(path).read()  # 不得 raise
    assert snapshot["episodes"][0]["notebook_id"] == "nb-A"

    # 但新的寫入仍 fail-closed(不讓分裂繼續長)
    with pytest.raises(ValueError, match="notebook_id"):
        ManifestStore(path).update(
            lambda manifest: manifest["episodes"][0].update({"title": "心法篇"})
        )


def test_directory_fsync_unsupported_is_tolerated_after_commit(tmp_path, monkeypatch):
    """os.replace 是 commit point。目錄 fsync 在不支援的 mount(NAS/overlay,回
    EINVAL/ENOTSUP)上失敗**不得**讓 update 看起來整個失敗——否則呼叫端會據此
    rollback(如 generation_input 的 binding),造成「manifest 有新 revision、binding 卻
    被刪」的分裂。這條鎖住 commit 後的 errno 容忍。"""
    import errno

    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps({"schema_version": 2, "revision": 0, "episodes": []}),
        encoding="utf-8",
    )

    def unsupported(_path):
        raise OSError(errno.EINVAL, "directory fsync unsupported on this mount")

    monkeypatch.setattr("notebooklm_mcp.manifest_store._fsync_parent", unsupported)

    # 不得 raise
    ManifestStore(path).update(lambda manifest: manifest.update({"notebook_id": "nb-1"}))

    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed["notebook_id"] == "nb-1"  # 已提交
    assert committed["revision"] == 1  # revision 有遞增
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


def test_episode_attempt_notebook_split_raises(tmp_path):
    """episode.notebook_id 與其（非 retracted）attempt.notebook_id 必須一致——只驗
    episode ↔ attempts,不拿 manifest 級 notebook_id 來比(這份 manifest 支援每集不同
    notebook,manifest 級只是預設值)。這正是 EP35 事故的手改破壞形狀。

    **只擋寫入、不擋讀取**:v0.5.0 之前建立 attempt 完全沒有這道 guard,線上可能已經
    有分裂的 manifest;讀取端也驗會讓它們每次 read() 都 raise,連修復正門
    (podcast_attempt_retract)與回填腳本都打不開(見
    test_existing_notebook_split_manifest_stays_readable)。"""
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "episodes": [
                    {
                        "episode": 1,
                        "notebook_id": "nb-A",
                        "attempts": [
                            {
                                "attempt_id": "attempt-1",
                                "episode": 1,
                                "notebook_id": "nb-B",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="notebook_id"):
        ManifestStore(path).update(
            lambda manifest: manifest["episodes"][0].update({"title": "心法篇"})
        )


def test_retracted_attempt_notebook_split_is_exempt(tmp_path):
    """retract 的 tombstone 是歷史紀錄,不參與這道一致性檢查——否則 retract 之後想
    切換 notebook 重生就寫不進去(取代版的新 attempt 才要跟 episode 一致)。"""
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "episodes": [
                    {
                        "episode": 1,
                        "notebook_id": "nb-B",
                        "attempts": [
                            {
                                "attempt_id": "attempt-1",
                                "episode": 1,
                                "notebook_id": "nb-A",
                                "retraction": {"reason": "QA"},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # 走**寫入**路徑才鎖得住豁免:讀取端本來就不驗這條,用 read() 會空過。
    ManifestStore(path).update(lambda manifest: manifest["episodes"][0].update({"title": "心法篇"}))


def test_multi_notebook_manifest_with_consistent_attempts_passes(tmp_path):
    """合法用法:兩個 episode 各自不同 notebook,各自的 attempt 都跟自己的 episode
    一致——manifest 支援每集不同 notebook,寫入必須照常通過。"""
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "episodes": [
                    {
                        "episode": 1,
                        "notebook_id": "nb-A",
                        "attempts": [
                            {"attempt_id": "attempt-1", "episode": 1, "notebook_id": "nb-A"}
                        ],
                    },
                    {
                        "episode": 2,
                        "notebook_id": "nb-B",
                        "attempts": [
                            {"attempt_id": "attempt-2", "episode": 2, "notebook_id": "nb-B"}
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # 同上:走寫入路徑,否則這條「合法多 notebook 不被誤擋」的斷言測不到東西。
    snapshot, _ = ManifestStore(path).update(
        lambda manifest: manifest["episodes"][0].update({"title": "心法篇"})
    )
    assert snapshot["episodes"][0]["notebook_id"] == "nb-A"
    assert snapshot["episodes"][1]["notebook_id"] == "nb-B"
