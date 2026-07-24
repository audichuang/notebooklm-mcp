"""PR5：整季工具從 durable attempt state 安全續跑。"""

import json

import pytest

from notebooklm_mcp import tools_podcast as p


EPS3 = [
    {"title": "心法篇", "brief": "1"},
    {"title": "實戰篇", "brief": "2"},
    {"title": "收尾篇", "brief": "3"},
]


def _generate_briefs(fake_client, start: int = 0) -> list[str]:
    return [
        call[1]["instructions"]
        for call in fake_client.artifacts.calls[start:]
        if call[0] == "generate_audio"
    ]


async def _complete_episode(fake_client, tmp_path, episode_n: int) -> None:
    episode = EPS3[episode_n - 1]
    await p.podcast_episode(
        "nb-1",
        episode_n=episode_n,
        title=episode["title"],
        brief=episode["brief"],
        output_dir=str(tmp_path),
        manifest_path=str(tmp_path / "series_manifest.json"),
    )


async def test_default_start_skips_completed_attempts_and_generates_only_ep3(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    await _complete_episode(fake_client, tmp_path, 2)
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=1
    )

    assert _generate_briefs(fake_client, call_boundary) == ["3"]
    assert [episode["episode"] for episode in out["episodes"]] == [3]


async def test_start_two_skips_completed_ep2_and_generates_only_ep3(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 2)
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )

    assert _generate_briefs(fake_client, call_boundary) == ["3"]
    assert [episode["episode"] for episode in out["episodes"]] == [3]


async def test_completed_episode_with_missing_feedback_source_stops_series(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    fake_client.sources.sources.clear()
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "continuity_unverified"
    assert out["safe_next_action"] == "restore_feedback_source_explicitly"
    assert _generate_briefs(fake_client, call_boundary) == []


async def test_start_three_does_not_validate_earlier_plan_entries(
    fake_client, tmp_path
):
    out = await p.podcast_series(
        "nb-1",
        episodes=[None, {"ignored": True}, EPS3[2]],
        output_dir=str(tmp_path),
        start=3,
    )

    assert out["complete"] is True
    assert _generate_briefs(fake_client) == ["3"]


async def test_series_treats_flat_v1_artifact_as_legacy_completed_output(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    legacy_source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(tmp_path / "legacy-ep01.mp3"),
                        "feedback_source_id": legacy_source_id,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "legacy-ep01.mp3").write_bytes(b"legacy audio")

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert _generate_briefs(fake_client) == ["2"]
    assert [episode["episode"] for episode in out["episodes"]] == [2]
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = stored["episodes"][0]
    assert legacy["artifact_id"] == "legacy-artifact"
    assert legacy.get("attempts", []) == []
    assert "output_attempt_id" not in legacy


async def test_unverified_legacy_stub_stops_before_next_episode(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(tmp_path / "unlinked-ep01.mp3"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "unlinked-ep01.mp3").write_bytes(b"legacy audio")
    fake_client.sources._add("EP01 心法篇", kind="media")

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["attempt_id"] is None
    assert out["observed_state"] == "legacy_output_unverified"
    assert out["safe_next_action"] == "resume_legacy_artifact_explicitly"
    assert out["artifact_id"] == "legacy-artifact"
    assert _generate_briefs(fake_client) == []


async def test_acceptance_unknown_stops_series_without_generating_ep2_or_ep3(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=2,
            title=EPS3[1]["title"],
            brief=EPS3[1]["brief"],
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    fake_client.artifacts.generate_audio_exc = None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = manifest["episodes"][0]["active_attempt_id"]
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )

    assert _generate_briefs(fake_client, call_boundary) == []
    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["attempt_id"] == attempt_id
    assert out["observed_state"] == "acceptance_unknown"
    assert out["safe_next_action"] == "wait_and_reconcile"


async def test_first_call_not_accepted_returns_structured_safe_stop(
    fake_client, tmp_path
):
    fake_client.artifacts.fail_generate = True

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "not_accepted"
    assert out["safe_next_action"] == "create_new_attempt_explicitly"
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"


async def test_series_retries_same_prepared_attempt_after_pre_dispatch_crash(
    fake_client, tmp_path, monkeypatch
):
    original_claim = p._claim_prepared_dispatch
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise ConnectionError("crash before dispatch")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(p, "_claim_prepared_dispatch", crash_once)
    first = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    manifest_path = tmp_path / "series_manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    assert first["complete"] is False
    assert first["observed_state"] == "prepared"
    assert _generate_briefs(fake_client) == []

    resumed = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = final["episodes"][0]
    assert resumed["complete"] is True
    assert len(episode["attempts"]) == 1
    assert episode["output_attempt_id"] == attempt_id
    assert episode["attempts"][0]["dispatch"]["status"] == "accepted"
    assert _generate_briefs(fake_client) == ["1"]


async def test_series_rejects_manifest_from_another_notebook(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    call_boundary = len(fake_client.artifacts.calls)

    with pytest.raises(ValueError, match="different notebook|另一個 notebook"):
        await p.podcast_series(
            "nb-2",
            episodes=EPS3,
            output_dir=str(tmp_path),
            start=1,
        )

    assert _generate_briefs(fake_client, call_boundary) == []
    manifest = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["notebook_id"] == "nb-1"
