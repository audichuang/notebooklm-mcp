"""canonical_audio 唯讀查詢與 EmptyDownloadError 的介面測試。"""

import hashlib

import pytest

from notebooklm_mcp._atomic import EmptyDownloadError, download_atomically
from notebooklm_mcp.canonical_audio import Unverifiable, canonical_audio, verify_file


def _ep(**kw):
    return {"episode": 3, "mp3_path": "/x.mp3", "artifact_id": "a", "notebook_id": "nb", **kw}


def _att(aid, sha=None, account=None):
    return {
        "attempt_id": aid,
        "dispatch": {"account": account} if account else {},
        "finalize": {"download": {"sha256": sha}},
    }


@pytest.mark.parametrize(
    "ep,kind",
    [
        (_ep(), Unverifiable.LEGACY),
        (_ep(attempts=[_att("a1", "s")]), Unverifiable.NO_OUTPUT_POINTER),
        (_ep(attempts=[_att("a1", "s")], output_attempt_id="zz"), Unverifiable.DANGLING_POINTER),
        (_ep(attempts=[_att("a1")], output_attempt_id="a1"), Unverifiable.NO_SHA),
        (_ep(attempts=[_att("a1", "s")], output_attempt_id="a1"), None),
    ],
)
def test_classification(ep, kind):
    assert canonical_audio(ep).unverifiable is kind


def test_fields_and_recorded_account():
    c = canonical_audio(_ep(attempts=[_att("a1", "s", "pool-account-2")], output_attempt_id="a1"))
    assert (c.mp3_path, c.artifact_id, c.notebook_id) == ("/x.mp3", "a", "nb")
    assert (c.attempt_id, c.sha256, c.account) == ("a1", "s", "pool-account-2")


def test_verify_file(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"abc")
    sha = hashlib.sha256(b"abc").hexdigest()
    ok = canonical_audio(_ep(attempts=[_att("a1", sha)], output_attempt_id="a1"))
    assert verify_file(ok, str(f)) is None
    bad = canonical_audio(_ep(attempts=[_att("a1", "0" * 64)], output_attempt_id="a1"))
    with pytest.raises(ValueError, match="sha256 與 promoted attempt 不符"):
        verify_file(bad, str(f))
    assert "legacy" in verify_file(canonical_audio(_ep()), str(f))
    with pytest.raises(ValueError, match="沒有 output_attempt_id"):
        verify_file(canonical_audio(_ep(attempts=[_att("a1")])), str(f))


async def test_empty_download_is_typed(tmp_path):
    async def noop(_dest):
        return None

    with pytest.raises(EmptyDownloadError, match="downloaded file is empty"):
        await download_atomically(str(tmp_path / "o.mp3"), noop, lambda _d: None)
    assert issubclass(EmptyDownloadError, ValueError)
