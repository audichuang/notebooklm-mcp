"""attempt_admission 的決策表:episode 狀態 × 請求來源 → Admit / Refusal。

純 dict,不需要 FakeClient / ManifestStore。訊息逐字等於搬進來之前各入口拋的那句。
"""

from __future__ import annotations

import pytest

from notebooklm_mcp import attempt_admission as adm
from notebooklm_mcp.attempt_admission import (
    Admit,
    Origin,
    Refusal,
    Request,
    check_series_identity,
    decide,
)

NB = "nb-1"
TITLE = "T"
BRIEF = "brief"
SHA = adm.brief_sha256(BRIEF)
SETTINGS = {"language": "zh", "audio_format": None, "audio_length": None}


def stub_next_step(episode, attempt, attempt_id):
    return f"NEXT[{attempt_id}]"


def attempt(aid="a1", *, dispatch="prepared", remote="unknown", artifact=None, **extra):
    row = {
        "attempt_id": aid,
        "notebook_id": NB,
        "title": TITLE,
        "brief_sha256": SHA,
        "settings": dict(SETTINGS),
        "dispatch": {"status": dispatch},
        "remote": {"status": remote, "artifact_id": artifact},
    }
    row.update(extra)
    return row


def manifest(*episodes):
    return {"notebook_id": NB, "episodes": list(episodes)}


def episode(*attempts, **fields):
    row = {"episode": 1, "attempts": list(attempts)}
    row.update(fields)
    return row


def gen(**kw):
    base = {"brief_sha256": SHA, "settings": dict(SETTINGS)}
    base.update(kw)
    return Request(Origin.GENERATE, NB, 1, TITLE, **base)


def resume(artifact="art-1", **kw):
    return Request(Origin.RESUME, NB, 1, kw.pop("title", TITLE), artifact_id=artifact, **kw)


def frozen(**kw):
    base = {
        "brief_sha256": SHA,
        "settings": dict(SETTINGS),
        "input_bundle": {"b": 1},
        "attempt_id": "a1",
    }
    base.update(kw)
    return Request(Origin.FROZEN_REUSE, NB, 1, TITLE, **base)


def refused(decision, fragment):
    assert isinstance(decision, Refusal), decision
    assert decision.exc_type is ValueError
    assert fragment in decision.message, decision.message
    return decision


GENERATE_CASES = [
    ("no episode -> create", manifest(), gen(), Admit(adm.CREATE)),
    (
        "no episode + supersede",
        manifest(),
        gen(supersedes_attempt_id="a1"),
        "cannot supersede an attempt from a missing episode",
    ),
    (
        "other notebook",
        manifest(episode(notebook_id="nb-2")),
        gen(),
        "episode 1 belongs to another notebook",
    ),
    (
        "output pointer",
        manifest(episode(attempt(), output_attempt_id="a1")),
        gen(),
        "already has a completed output",
    ),
    (
        "hard evidence",
        manifest(episode(mp3_path="/x.mp3")),
        gen(),
        "already has a completed output",
    ),
    (
        "legacy preparatory (label, no attempts)",
        manifest(episode(label="EP01 T")),
        gen(),
        "already has a completed output",
    ),
    (
        "notebook beats output",
        manifest(episode(notebook_id="nb-2", output_attempt_id="a1")),
        gen(),
        "belongs to another notebook",
    ),
    (
        "same request on prepared active -> resend",
        manifest(episode(attempt(), active_attempt_id="a1")),
        gen(),
        Admit(adm.RESEND, "a1"),
    ),
    (
        "different brief on active -> refuse with caps guidance",
        manifest(episode(attempt(), active_attempt_id="a1")),
        gen(brief_sha256="other"),
        "already has durable active attempt 'a1' (dispatch='prepared'); NEXT[a1]",
    ),
    (
        "accepted active is never resendable",
        manifest(episode(attempt(dispatch="accepted", artifact="art"), active_attempt_id="a1")),
        gen(),
        "already has durable active attempt 'a1' (dispatch='accepted'); NEXT[a1]",
    ),
    (
        "supersede terminal failed -> create",
        manifest(
            episode(
                attempt(dispatch="accepted", remote="failed", artifact="art"),
                active_attempt_id="a1",
            )
        ),
        gen(supersedes_attempt_id="a1"),
        Admit(adm.CREATE),
    ),
    (
        "supersede non-terminal",
        manifest(
            episode(
                attempt(dispatch="accepted", remote="in_progress", artifact="art"),
                active_attempt_id="a1",
            )
        ),
        gen(supersedes_attempt_id="a1"),
        "attempt 'a1' is not terminal and cannot be superseded",
    ),
    (
        "supersede without active",
        manifest(episode(attempt())),
        gen(supersedes_attempt_id="a1"),
        "superseded attempt is no longer active",
    ),
    (
        "retracted + changed title",
        manifest(
            episode(
                attempt("x", retraction={"at": "t"}),
                retracted_attempt_ids=["x"],
                title="Old",
                notebook_id=NB,
            )
        ),
        gen(),
        "title cannot change in a retract replacement (manifest has 'Old')",
    ),
    (
        "retracted + same title -> create",
        manifest(
            episode(
                attempt("x", retraction={"at": "t"}),
                retracted_attempt_ids=["x"],
                title=TITLE,
                notebook_id=NB,
            )
        ),
        gen(),
        Admit(adm.CREATE),
    ),
    (
        # 刻意保留的差異:generate 的標題不可變只在 retract 之後才擋(resume 永遠擋)。
        "changed title without retraction -> create (documented divergence)",
        manifest(episode(attempt(remote="failed"), title="Old")),
        gen(),
        Admit(adm.CREATE),
    ),
]


@pytest.mark.parametrize("name,mf,req,expected", GENERATE_CASES, ids=[c[0] for c in GENERATE_CASES])
def test_generate_admission(name, mf, req, expected):
    decision = decide(mf, req, stub_next_step)
    if isinstance(expected, str):
        refused(decision, expected)
    else:
        assert decision == expected


RESUME_CASES = [
    ("new episode -> resume_new", manifest(), resume(), Admit(adm.RESUME_NEW)),
    (
        "claimed by multiple",
        manifest(
            episode(attempt("a1", artifact="art-1")),
            {"episode": 2, "attempts": [attempt("a2", artifact="art-1")]},
        ),
        resume(),
        "artifact 'art-1' is claimed by multiple attempts",
    ),
    (
        "claimed by another episode",
        manifest({"episode": 2, "attempts": [attempt("a2", artifact="art-1")]}),
        resume(),
        "artifact 'art-1' belongs to another episode",
    ),
    (
        "claimed: attempt in other notebook",
        manifest(episode({**attempt("a1", artifact="art-1"), "notebook_id": "nb-2"})),
        resume(),
        "artifact 'art-1' belongs to another notebook",
    ),
    (
        "claimed: attempt with other title",
        manifest(episode({**attempt("a1", artifact="art-1"), "title": "Z"})),
        resume(),
        "artifact 'art-1' belongs to another title",
    ),
    (
        "claimed: output is another attempt",
        manifest(episode(attempt("a1", artifact="art-1"), attempt("a0"), output_attempt_id="a0")),
        resume(),
        "already has durable output 'a0'",
    ),
    (
        "claimed: episode title differs",
        manifest(episode(attempt("a1", artifact="art-1"), title="Old")),
        resume(),
        "title does not match the manifest ('Old'); resume cannot rename an episode",
    ),
    (
        "claimed: other active attempt gets caps guidance",
        manifest(episode(attempt("a1", artifact="art-1"), attempt("a2"), active_attempt_id="a2")),
        resume(),
        "has another active attempt 'a2'; NEXT[a2]",
    ),
    (
        "claimed: self is output and active -> claim",
        manifest(
            episode(
                attempt("a1", artifact="art-1"),
                output_attempt_id="a1",
                active_attempt_id="a1",
                title=TITLE,
            )
        ),
        resume(),
        Admit(adm.CLAIM, "a1"),
    ),
    (
        "new: any output pointer blocks",
        manifest(episode(attempt("a0"), output_attempt_id="a0")),
        resume(),
        "already has durable output 'a0'",
    ),
    (
        "new: legacy evidence on a different artifact",
        manifest(episode(artifact_id="old-art", mp3_path="/x.mp3")),
        resume(),
        "legacy output evidence bound to artifact 'old-art'",
    ),
    (
        "new: legacy evidence on the SAME artifact is allowed",
        manifest(episode(artifact_id="art-1", mp3_path="/x.mp3")),
        resume(),
        Admit(adm.RESUME_NEW),
    ),
    (
        "new: active attempt gets caps guidance",
        manifest(episode(attempt("a2"), active_attempt_id="a2")),
        resume(),
        "has active attempt 'a2' (dispatch='prepared'); NEXT[a2]",
    ),
    (
        "new: other notebook",
        manifest(episode(notebook_id="nb-2")),
        resume(),
        "episode 1 belongs to another notebook",
    ),
    (
        "new: title differs (retract then rename is refused)",
        manifest(episode(title="Old", retracted_attempt_ids=["x"])),
        resume(),
        "resume cannot rename an episode",
    ),
    (
        "new: after retract with same title",
        manifest(
            episode(attempt("x", retraction={"at": "t"}), retracted_attempt_ids=["x"], title=TITLE)
        ),
        resume(),
        Admit(adm.RESUME_NEW),
    ),
]


@pytest.mark.parametrize("name,mf,req,expected", RESUME_CASES, ids=[c[0] for c in RESUME_CASES])
def test_resume_admission(name, mf, req, expected):
    decision = decide(mf, req, stub_next_step)
    if isinstance(expected, str):
        refused(decision, expected)
    else:
        assert decision == expected


FROZEN_CASES = [
    ("no episode -> create", manifest(), frozen(), Admit(adm.CREATE)),
    (
        "episode without the attempt -> create",
        manifest(episode(attempt("zz"))),
        frozen(),
        Admit(adm.CREATE),
    ),
    (
        "duplicated identity",
        manifest(
            episode(
                attempt(input_bundle={"b": 1}),
                attempt(input_bundle={"b": 1}),
                active_attempt_id="a1",
            )
        ),
        frozen(),
        "frozen generation attempt identity is duplicated",
    ),
    (
        "prepared -> reuse",
        manifest(episode(attempt(input_bundle={"b": 1}), active_attempt_id="a1")),
        frozen(),
        Admit(adm.REUSE, "a1"),
    ),
    (
        "not_accepted -> rearm",
        manifest(
            episode(attempt(dispatch="not_accepted", input_bundle={"b": 1}), active_attempt_id="a1")
        ),
        frozen(),
        Admit(adm.REARM, "a1"),
    ),
    (
        "accepted -> caps guidance, not hand-written",
        manifest(
            episode(
                attempt(dispatch="accepted", artifact="art", input_bundle={"b": 1}),
                active_attempt_id="a1",
            )
        ),
        frozen(),
        "frozen attempt is 'accepted'; NEXT[a1]",
    ),
    (
        "not active",
        manifest(episode(attempt(input_bundle={"b": 1}))),
        frozen(),
        "existing attempt does not match frozen generation input",
    ),
    (
        "episode has output",
        manifest(
            episode(attempt(input_bundle={"b": 1}), active_attempt_id="a1", output_attempt_id="a0")
        ),
        frozen(),
        "existing attempt does not match frozen generation input",
    ),
    (
        "different settings (source_ids changed)",
        manifest(episode(attempt(input_bundle={"b": 1}), active_attempt_id="a1")),
        frozen(settings={**SETTINGS, "source_ids": ["s"]}),
        "existing attempt does not match frozen generation input",
    ),
    (
        "different bundle",
        manifest(episode(attempt(input_bundle={"b": 2}), active_attempt_id="a1")),
        frozen(),
        "existing attempt does not match frozen generation input",
    ),
]


@pytest.mark.parametrize("name,mf,req,expected", FROZEN_CASES, ids=[c[0] for c in FROZEN_CASES])
def test_frozen_reuse_admission(name, mf, req, expected):
    decision = decide(mf, req, stub_next_step)
    if isinstance(expected, str):
        refused(decision, expected)
    else:
        assert decision == expected


def test_tombstoned_active_attempt_is_still_default_denied():
    """admission 不繞過 ADR-0009:active 指到 tombstone 時仍由 find_attempt 拒絕。"""
    mf = manifest(episode(attempt(retraction={"at": "t"}), active_attempt_id="a1"))
    with pytest.raises(ValueError):
        decide(mf, gen(), stub_next_step)


SERIES_CASES = [
    ("active", attempt(), None),
    ("active", {**attempt(), "title": "X"}, "episode 1 title changed during an active attempt"),
    (
        "active",
        {**attempt(), "brief_sha256": "zz"},
        "episode 1 brief changed during an active attempt",
    ),
    ("active", {**attempt(), "brief_sha256": None}, None),
    ("output", {**attempt(), "title": "X"}, "episode 1 title differs from completed attempt"),
    (
        "output",
        {**attempt(), "brief_sha256": "zz"},
        "episode 1 brief differs from completed attempt",
    ),
    ("legacy", {"title": None}, None),
    ("legacy", {"title": "X"}, "episode 1 title differs from legacy output"),
]


@pytest.mark.parametrize("subject,holder,message", SERIES_CASES)
def test_series_identity(subject, holder, message):
    result = check_series_identity(
        subject, episode_n=1, holder=holder, expected_title=TITLE, expected_brief_hash=SHA
    )
    if message is None:
        assert result is None
    else:
        assert result == Refusal(ValueError, message)
        with pytest.raises(ValueError, match=message):
            result.raise_()


def test_output_predicates_keep_their_documented_differences():
    legacy_prep = {"episode": 1, "attempts": [], "label": "EP01 T"}
    assert adm.output_blocks_generate(legacy_prep)
    assert not adm.has_output_pointer_or_hard(legacy_prep)
    assert adm.has_output_pointer_or_hard({"output_attempt_id": "a"})
    assert adm.has_output_pointer_or_hard({"mp3_path": "/x"})
    # generate 用真值、series 用 is-not-None:空字串只在 series 算有 output。
    assert not adm.output_blocks_generate({"output_attempt_id": "", "attempts": [{}]})
    assert adm.has_output_pointer_or_hard({"output_attempt_id": ""})


def test_record_builders_have_single_shape():
    prepared = adm.new_prepared_attempt(
        attempt_id="a1",
        created_at="t",
        notebook_id=NB,
        episode_n=1,
        title=" T ",
        brief=BRIEF,
        settings=dict(SETTINGS),
        supersedes_attempt_id="a0",
        input_bundle={"b": 1},
    )
    assert prepared["title"] == "T"
    assert prepared["brief_sha256"] == SHA
    assert prepared["dispatch"]["status"] == "prepared"
    assert prepared["supersedes_attempt_id"] == "a0"
    assert prepared["input_bundle"] == {"b": 1}

    ep = {"artifact_id": "art-1", "feedback_source_id": "src", "feedback_source_adopted_at": "t0"}
    seeded = adm.new_resume_attempt(
        attempt_id="a2",
        now="t1",
        notebook_id=NB,
        episode_n=1,
        title=TITLE,
        artifact_id="art-1",
        episode=ep,
    )
    assert seeded["settings"] == {"origin": "explicit_resume"}
    assert seeded["remote"]["artifact_id"] == "art-1"
    assert seeded["finalize"]["feedback_source_upload"]["source_id"] == "src"
    # 不同 artifact 不接手 legacy source。
    other = adm.new_resume_attempt(
        attempt_id="a3",
        now="t1",
        notebook_id=NB,
        episode_n=1,
        title=TITLE,
        artifact_id="art-2",
        episode=ep,
    )
    assert other["finalize"]["feedback_source_upload"]["source_id"] is None


def test_attach_new_attempt_creates_episode_and_sets_active():
    mf = manifest()
    row = attempt("a9")
    adm.attach_new_attempt(mf, 3, row, title=" T ", notebook_id=NB, overwrite_notebook=False)
    ep = mf["episodes"][0]
    assert ep["episode"] == 3
    assert ep["active_attempt_id"] == "a9"
    assert ep["title"] == "T" and ep["notebook_id"] == NB
    assert ep["attempts"] == [row]
