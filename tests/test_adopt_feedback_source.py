"""`audio_finalize.adopt_feedback_source`:adopt 對 finalize checkpoint 的轉換。

checkpoint 的形狀與轉換都歸 `audio_finalize`;`podcast_attempt_adopt` 只在
`ManifestStore.update` 的 mutate 裡呼叫它。
"""

import pytest

from notebooklm_mcp.audio_finalize import adopt_feedback_source, new_finalize_state

NOW = "2026-10-12T00:00:00+00:00"


def _attempt(**upload) -> dict:
    attempt = {"finalize": new_finalize_state()}
    attempt["finalize"]["feedback_source_upload"].update(upload)
    return attempt


def test_adopt_without_rename_completes_both_checkpoints_and_records_previous():
    attempt = _attempt(status="completed", source_id="src-old")

    previous, stale = adopt_feedback_source(
        attempt, "src-new", needs_rename=False, expected_title="EP01 心法篇", now=NOW
    )

    assert (previous, stale) == ("src-old", [])
    upload = attempt["finalize"]["feedback_source_upload"]
    assert upload["status"] == "completed"
    assert upload["source_id"] == "src-new"
    assert upload["adopted_at"] == NOW
    assert upload["candidate_source_ids"] == []
    assert upload["previous_source_ids"] == ["src-old"]
    rename = attempt["finalize"]["feedback_source_rename"]
    assert rename["status"] == "completed"
    assert rename["adopted_at"] == NOW


def test_adopt_ambiguity_candidate_resets_rename_and_returns_unchosen_candidates():
    attempt = _attempt(
        status="reconciliation_ambiguous",
        candidate_source_ids=["src-a", "src-b"],
        expected_title="ep01.mp3",
    )

    previous, stale = adopt_feedback_source(
        attempt, "src-a", needs_rename=True, expected_title="ep01.mp3", now=NOW
    )

    assert previous is None
    assert stale == ["src-b"]
    upload = attempt["finalize"]["feedback_source_upload"]
    assert upload["status"] == "accepted"
    assert upload["candidate_source_ids"] == []
    assert "previous_source_ids" not in upload
    assert attempt["finalize"]["feedback_source_rename"]["status"] == "not_started"


@pytest.mark.parametrize(
    "upload",
    [
        {"status": "completed", "candidate_source_ids": ["src-a"], "expected_title": "ep01.mp3"},
        {
            "status": "reconciliation_ambiguous",
            "candidate_source_ids": ["src-b"],
            "expected_title": "ep01.mp3",
        },
        {
            "status": "reconciliation_ambiguous",
            "candidate_source_ids": ["src-a"],
            "expected_title": "other.mp3",
        },
    ],
)
def test_rename_adoption_requires_a_live_ambiguity_candidate(upload):
    attempt = _attempt(**upload)
    before = {key: dict(value) for key, value in attempt["finalize"].items()}

    with pytest.raises(ValueError, match="no longer an ambiguity candidate"):
        adopt_feedback_source(
            attempt, "src-a", needs_rename=True, expected_title="ep01.mp3", now=NOW
        )

    assert attempt["finalize"] == before
