"""發布端讀「某集的 canonical 音檔」的唯一唯讀查詢(ADR-0002:發布不寫 attempt)。

過去 `tools_publish` 自己伸手進 attempt 內部(`output_attempt_id`、
`attempts[].finalize.download.sha256`)比對 sha;欄位形狀由 `audio_finalize` 寫、
卻由發布端各自解讀,兩邊容易漂移。現在形狀的解讀收在這裡(與寫入端同層),
發布端只拿 `CanonicalAudio`。

本模組**不 raise**:歧義與驗不動都以 `unverifiable` 的型別化原因回報,由呼叫端決定
擋或標註(`tools_publish` 維持原本「先 resolve 檔案、再驗出身」的順序與訊息)。
retired 閘留在 `publish/state.py`(ADR-0013),不在這裡。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._atomic import sha256_file


class Unverifiable(Enum):
    """驗不出來的原因。LEGACY / NO_SHA = 放行但要標註;其餘 = fail-closed。"""

    LEGACY = "legacy"  # 無 attempts 記錄,無從比對
    NO_OUTPUT_POINTER = "no_output_pointer"  # 有 attempts 但沒指明哪一顆是 canonical
    DANGLING_POINTER = "dangling_pointer"  # output_attempt_id 指向不存在的 attempt
    NO_SHA = "no_sha"  # 舊版 finalize 形狀,沒記 sha256

    @property
    def blocks_publish(self) -> bool:
        return self in (Unverifiable.NO_OUTPUT_POINTER, Unverifiable.DANGLING_POINTER)


@dataclass(frozen=True)
class CanonicalAudio:
    episode: int | None
    attempt_id: str | None
    mp3_path: str | None
    artifact_id: str | None
    notebook_id: str | None
    account: str | None  # 受理該 attempt 的帳號(dispatch.account);僅供對帳,重抓不用它
    sha256: str | None
    unverifiable: Unverifiable | None
    reason: str  # unverifiable 時的人讀說明;驗得動時為 ""


def canonical_audio(ep: dict) -> CanonicalAudio:
    n = ep.get("episode")
    base = {
        "episode": n,
        "mp3_path": ep.get("mp3_path"),
        "artifact_id": ep.get("artifact_id"),
        "notebook_id": ep.get("notebook_id"),
    }

    def gap(kind, reason, *, attempt_id=None, account=None, sha=None):
        return CanonicalAudio(
            **base,
            attempt_id=attempt_id,
            account=account,
            sha256=sha,
            unverifiable=kind,
            reason=reason,
        )

    attempts = ep.get("attempts") or []
    promoted = ep.get("output_attempt_id")
    if not attempts and not promoted:
        return gap(Unverifiable.LEGACY, "legacy manifest(無 attempts 記錄,無從比對)")
    if not promoted:
        return gap(
            Unverifiable.NO_OUTPUT_POINTER,
            f"episode {n}: 有 attempts 但沒有 output_attempt_id,分不出哪一顆是 canonical"
            " —— 不靜默挑一顆(那正是本閘要消滅的路徑);用工具把輸出 promote 回"
            " episode 層再發布",
        )
    attempt = next((a for a in attempts if a.get("attempt_id") == promoted), None)
    if attempt is None:
        return gap(
            Unverifiable.DANGLING_POINTER,
            f"episode {n}: output_attempt_id {promoted} 指向不存在的 attempt"
            " —— manifest 不自洽,不發布",
        )
    account = (attempt.get("dispatch") or {}).get("account")
    expected = ((attempt.get("finalize") or {}).get("download") or {}).get("sha256")
    if not expected:
        return gap(
            Unverifiable.NO_SHA,
            f"promoted attempt {promoted} 沒記 finalize.download.sha256(舊版 finalize 形狀)",
            attempt_id=promoted,
            account=account,
        )
    return CanonicalAudio(
        **base,
        attempt_id=promoted,
        account=account,
        sha256=expected,
        unverifiable=None,
        reason="",
    )


def verify_file(canon: CanonicalAudio, local_path: str) -> str | None:
    """None = 驗過相符;字串 = 驗不動的原因(呼叫端標註);不符或歧義 → raise。"""
    if canon.unverifiable is not None:
        if canon.unverifiable.blocks_publish:
            raise ValueError(canon.reason)
        return canon.reason
    actual = sha256_file(local_path)
    if actual != canon.sha256:
        raise ValueError(
            f"episode {canon.episode}: mp3 檔案的 sha256 與 promoted attempt 不符"
            f"(檔案 {actual[:12]}…,attempt {canon.attempt_id} 記錄 {canon.sha256[:12]}…)"
            " —— 檔案被換過或 mp3_path 指錯位置;重新對帳 manifest,別讓"
            "「檔案存在」冒充 canonical"
        )
    return None
