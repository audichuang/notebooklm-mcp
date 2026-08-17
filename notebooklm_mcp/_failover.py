"""配額 failover 的共用迴圈:被伺服器拒絕就換帳號、**原地重送同一次 dispatch**(ADR-0010)。

原本整條只長在 `tools_podcast._dispatch_audio_with_failover` 裡、音檔專用。v0.9.16 把附件
生成(簡報 / 講義 / 改版單頁)也接上時抽到這裡 —— **不是為了漂亮,是因為這個迴圈的每一個
分支都是一次真實事故的疤**,各寫一份等於下一次修正只有一處被改到,正是 AGENTS.md 反覆
點名的「補一半」。五條紅線都住在這個檔案裡,只有這裡:

  ① `REFUSED_WITHOUT_DISPATCH` **刻意不長大** —— 只有契約講死「沒建出 task」的那兩種
     才准重送。其餘失敗都可能發生在伺服器已受理之後,重送 = 重複 artifact + 重燒配額。
  ② 「有 `task_id` 卻 `is_failed`」**不是**零副作用拒絕,不准 rotate:遠端已經建出 task,
     換帳號重送會產生第二顆而 caller 只綁得到後面那顆(ADR-0010 紀律②)。
  ③ 權限被拒不 rotate —— 設定問題,一個個帳號試過去只會埋掉根因還每次多燒一輪 RPC
     (v0.8.0 驗收 F-2)。
  ④ 終止性靠呼叫端自己的 `tried`,**不靠 `rotate_client()` 的冷卻時鐘**:冷卻會過期,
     每一腿夠慢就會繞回第一格、`while True` 失去終止性(v0.9.8 F7)。
  ⑤ `tried` 這道門要擋在**稽核寫入之前**。`rotate_for_quota` 不是純查詢:它在回傳前
     已經推進冷卻與全域游標、並寫過一筆稽核。等呼叫端事後才丟棄回傳值的話,manifest
     會留下一次**從未發生**的換帳號,還覆寫掉上一輪真正生效的 account(v0.9.8 P1)。

**家族差異只有一個軸:稽核往哪寫。** 用**單一** `audit(phase, reason, **fields)` 表達,
不是三個各自可為 `None` 的 callback —— v0.9.16 的第一版就是三個,而三個之中只有一個
(當時的 `record_failover`)在把關「准不准 rotate」,於是「rotation 的稽核接了、終態的沒接」
是**簽名允許的合法狀態**,而第一版正好落在那裡:帳號耗盡時最後一腿一筆紀錄都不留,
單帳號 pool 則完全沒有紀錄。獨立複審花了一整輪抓那個缺陷,而它是參數形狀**天生允許**的。
收成一個之後,`audit is None` 就是唯一的開關(= 沒有稽核面 = 不准換帳號 = 也不寫終態),
半接線在結構上不可能。

音檔與附件的稽核**內容**仍然不同(音檔寫進 durable attempt 的 `errors[]` 並標
`not_accepted` / `acceptance_unknown`,附件寫進 episode 級的 append-only 清單),那個差異由
各家自己的 audit 函式吸收 —— 統一紀錄形狀而不統一狀態模型只是化妝,消不掉任何一個
條件式;真正的統一是 ADR-0011 延後的 attachment attempt。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from notebooklm.exceptions import ArtifactFeatureUnavailableError, RateLimitError

from . import runtime
from ._errors import access_denied_error, is_permission_denied
from ._status import ensure_started
from .manifest_store import ManifestPostCommitError

# 生成 kickoff 的例外裡,**契約上保證「伺服器沒有建出任何 task」**的那幾種。
# notebooklm-py 0.8.0(ADR-0019 / #1342)把同步拒絕從「回傳 status='failed'」改成
# raise:`RateLimitError` 是伺服器的 USER_DISPLAYABLE_ERROR 拒絕(配額/限流),
# `ArtifactFeatureUnavailableError` 來自 `_parse_generation_result` 的
# 「a missing id means no task was created」。兩者都等同 0.7.x 的 not_accepted。
#
# **刻意不收 `RPCError` / `DecodingError` / 網路錯誤 / CancelledError**:那些都可能
# 發生在伺服器已經受理之後,歸成 not_accepted 會讓呼叫端直接重生 → 重複 artifact +
# 重燒配額。兩種誤判的代價不對稱——把拒絕誤判成 unknown 只是多跑一次撈不到東西的
# 對帳(便宜),把已受理誤判成拒絕是真的損失,所以這個集合只放契約講死的那兩種。
REFUSED_WITHOUT_DISPATCH = (RateLimitError, ArtifactFeatureUnavailableError)

#: `audit()` 的 phase。**三個都會被呼叫到**,呼叫端不准只認其中幾個。
PHASE_FAILOVER = "dispatch_failover"             #: A 拒絕 → 換 B 重送(帶 from/to_account)
PHASE_REFUSED = "dispatch_refused"               #: 沒帳號可換了,乾淨終態(帶 account)
PHASE_ACCEPTANCE_UNKNOWN = "acceptance_unknown"  #: 受理不明,遠端可能已建出東西(帶 account)

#: `audit(phase, reason, **fields)` —— 把一次 dispatch 事件寫進呼叫端自己的稽核面。
#: **傳 `None` = 這個呼叫端沒有稽核面 → 一律不換帳號、也不寫終態**:ADR-0010
#: §Transparency 說 manifest 是唯一的稽核憑據,沒地方記錄就靜默換帳號等於自廢那條紀律
#: (低階 `tools_basic.generate_audio` 就是這一種,它連 `manifest_path` 都沒有)。
Audit = Callable[..., None]


def refusal_fields(reason: object) -> tuple[str | None, str | None]:
    """讀出上游拒絕的 `(error_code, error)` **原值**,不套任何預設。

    **這是整個 repo 唯一讀 `.error` / `.error_code` 這兩個上游欄位名的地方**,那正是抽出來
    的理由:上游改欄位名時只該有一處要改。`describe_refusal` 與
    `tools_podcast._mark_not_accepted` 都建在它上面 —— 後者刻意把原值(含 `None`)寫進
    `remote`,所以預設值不能塞進這一層。

    **兩種形狀**:0.8.0 起的同步拒絕是例外(沒有 `.error`/`.error_code`,用型別名與
    `str()`);0.7.x 是「不 raise,回 `task_id=""` + `is_failed`」的 status 物件。
    """
    if isinstance(reason, BaseException):
        return type(reason).__name__, (str(reason) or type(reason).__name__)
    return getattr(reason, "error_code", None), getattr(reason, "error", None)


def describe_refusal(reason: object) -> tuple[str, str]:
    """把拒絕原因翻成稽核紀錄要寫的 `(type, message)`,**套上預設值**。

    非例外的形狀可能兩個欄位都是 `None`(舊 SDK 的空 status)—— 不套預設的話 `type` 會是
    字面上的 `None`、`message` 是空的,那筆紀錄就等於沒寫。

    預設**無條件**套上,不必再分一次例外/非例外:例外那條路的兩個值都保證非空
    (`type(...).__name__`、`str(...) or type(...).__name__`),所以 `or` 永遠不會觸發。
    """
    code, message = refusal_fields(reason)
    return code or "not_accepted", message or getattr(reason, "status", "failed")


def _write_audit(audit: Audit | None, phase: str, reason: object, **fields) -> None:
    """寫一筆稽核,但**不讓稽核自己的 post-commit 失敗遮蔽主要例外**。

    `ManifestPostCommitError` 的語意是「`os.replace` 已經是 commit point,紀錄**是**
    durable 的,只有額外的 parent-directory fsync 失敗(NAS/overlay 回 EIO/ENOTSUP)」。
    往外拋會造成兩種後果,兩種都比吞掉更糟:
      - rotation 那一筆:紀錄說「換到 B」而主迴圈從未 dispatch B —— 正是 v0.9.8 P1
        修掉的幽靈紀錄形狀。
      - 終態那一筆:單帳號撞配額時,呼叫端收到的從 `RateLimitError` 變成寫檔錯誤,
        直接違反本迴圈「原樣重拋」的承諾(實測)。

    **其餘例外照樣往外拋**:那代表紀錄真的沒寫進去,而「沒有稽核就不准換帳號」是
    ADR-0010 的紅線 —— 這時候安靜繼續比噴一個寫檔錯誤更糟。

    ⚠️ **這個容忍只准有一份。** v0.9.16 第一版只補了 rotation 那半、漏了終態那半
    (在修「補一半」的那一輪裡又補一半);之後的修正又留下兩份逐字相同的 try/except。
    現在 rotation 與三個終態全部走這一支,`audit is None` 的短路也只有這裡判。
    """
    if audit is None:
        return
    try:
        audit(phase, reason, **fields)
    except ManifestPostCommitError:
        pass


def rotate_for_quota(
    audit: Audit | None,
    reason: object,
    from_account: str | None,
    tried: set[str],
) -> tuple[str | None, Any] | None:
    """還有沒試過的帳號就換過去,回**換過去那一個**的 `(label, client)`;沒有就回 None。

    `audit is None`(沒有稽核面)一律不換,理由見 `Audit`。

    **回 `(label, client)` 而不是 bool**:呼叫端要拿著這一份往下送,不能回頭再讀一次
    全域 —— `rotate_client()` 與下一次 `get_client()` 之間有 await,並行的另一次 rotate
    會落在那個縫裡。`from_account` 也由呼叫端傳進來、不在這裡重讀,理由同上:稽核記的
    「從哪個帳號換到哪個」必須是**這次 dispatch 實際用過的**那一個,不是「此刻剛好輪到誰」。
    同一個值原樣轉給 `runtime.rotate_client(refused=...)` —— 冷卻要進的是真正被拒的那個
    槽位,不是游標此刻剛好指到的那個(並行 dispatch 下兩者可能不同,v0.9.7 P1)。

    **`tried` 同時是「排除清單」也是「事後防線」。** 排除主動傳給
    `runtime.rotate_client(skip=...)`,讓掃描本身跳過已試過的槽位、繼續往後找 —— 而不是
    只回「游標後方第一個不在冷卻中的槽位」、回傳之後才發現它已經 tried 過就整批放棄
    (游標後面可能還有完全沒試過、也沒在冷卻中的帳號)。下面的 `to_account in tried`
    保留成第二道防線:`runtime.rotate_client` 不保證每個呼叫端(含測試的 monkeypatch
    假件)都真的遵守 `skip`,這裡仍是稽核寫入前唯一擋得住假紀錄的地方(紅線⑤)。
    """
    if audit is None:
        return None
    # 冷卻副作用永遠要做(帳號真的被拒過),所以這一步不能被 `tried` 擋掉。
    to_account = runtime.rotate_client(refused=from_account, skip=frozenset(tried))
    if to_account is None or to_account in tried:
        return None
    _write_audit(
        audit, PHASE_FAILOVER, reason,
        from_account=from_account, to_account=to_account,
    )
    # rotate_client() 與 snapshot() 之間沒有 await,所以取到的必然是剛換過去那個槽位。
    return runtime.snapshot()


async def dispatch_with_failover(
    dispatch: Callable[[Any], Awaitable[Any]],
    *,
    account: str | None,
    client: Any,
    audit: Audit | None,
    notebook_id: str | None = None,
) -> tuple[str, str | None, Any]:
    """送出一次生成;配額拒絕就換帳號原地重送。回 `(artifact_id, account, client)`。

    **後兩個回傳值呼叫端必須接住往下用。** 只回 artifact_id 的話,呼叫端要做「等完成 →
    下載」只能回頭 `runtime.get_client()` —— 又變成「此刻游標指到誰」而不是「這次是誰
    送的」,而且**風險視窗幾乎全落在那一半**:dispatch 只有幾秒,後半段是數十分鐘。
    分享不完整時症狀是十幾分鐘後在下載爆 401、根因在遠處;分享完整時退化成稽核失真
    (紀錄說 A 生成、實際下載的是 B,而下載身分不寫進任何欄位,事後查不出來)。

    `account` / `client` 由呼叫端用 `runtime.snapshot()` **一次取好**傳進來,函式內不再
    自己讀全域:MCP 是並行的(每則 message 一個 task),記帳點與真正的 `dispatch(client)`
    之間隔著至少一次 await —— 分兩次讀全域時,另一個工具呼叫在那個縫裡撞到配額並 rotate,
    紀錄就會記下 A、實際卻由 B 送出。failover 換帳號時兩者一起換,永遠同源。

    Args:
        dispatch: `async (client) -> status`。**只包 kickoff 那一個 RPC**,不要把純本地
            的轉換(`resolve_language` / enum)、下載或回寫包進來 —— ①重送的冪等性只在
            「伺服器還沒建出 task」這個前提下成立;②本地的 `ValueError` 落進下面的泛用
            except 會被寫成一筆「遠端受理不明」,而遠端一次都沒被碰到(實測 SDK 呼叫
            次數 0),那筆假紀錄還進 append-only 的表、清不掉。
        audit: 見 `Audit`。`None` = 沒有稽核面 = 不換帳號也不寫終態。
        notebook_id: 只用來讓權限錯誤的指引訊息指名是哪一本(紅線③)。
    """
    tried: set[str] = {account} if account else set()
    while True:
        try:
            status = await dispatch(client)
        except REFUSED_WITHOUT_DISPATCH as exc:
            # 伺服器明確拒絕、沒有建出 task(0.8.0 起改成 raise;0.7.x 走下面的
            # ensure_started 分支)。這是**乾淨的終態**,不是「結果不明」。
            rotated = rotate_for_quota(audit, exc, account, tried)
            if rotated is not None:
                account, client = rotated
                if account:
                    tried.add(account)
                continue
            _write_audit(audit, PHASE_REFUSED, exc, account=account)
            raise
        except (Exception, asyncio.CancelledError) as exc:
            if is_permission_denied(exc):
                # 這個帳號看不到那個 notebook —— 伺服器沒建出任何 task,所以是**乾淨的
                # 終態**而不是「受理不明」:走泛用分支會把呼叫端叫去跑一次註定撈不到東西
                # 的 reconcile。**不 rotate**(紅線③)。
                # 用這次 dispatch 實際持有的 `account`,不重讀全域:並行 rotate 會讓訊息
                # 指認錯的帳號,而這條訊息會被寫進稽核紀錄。
                denied = access_denied_error(
                    exc, notebook_id=notebook_id, account=account
                )
                # 先建好帶指引的例外、再寫終態 —— 紀錄下來的要是**這份帶著下一步動作**的
                # 訊息,不是上游原始的 "permission denied"。呼叫端事後只看得到紀錄時
                # (例如 podcast_series 的結構化 partial 只回訊息不回原始例外),
                # 指引才不會整條蒸發。
                _write_audit(audit, PHASE_REFUSED, denied, account=account)
                raise denied from exc
            _write_audit(audit, PHASE_ACCEPTANCE_UNKNOWN, exc, account=account)
            raise

        # task_id IS the artifact_id — notebooklm-py _types/artifacts.py:421 states
        # "task_id and artifact_id are the same identifier"; GenerationStatus has NO
        # artifact_id field, so we must use task_id for the download/rename targeting
        # (otherwise download falls back to "latest" and rename targets None).
        # ensure_started guards the failed/empty-task_id case (rate limit / quota / refusal).
        try:
            return ensure_started(status), account, client
        except RuntimeError:
            if getattr(status, "task_id", None):
                # 有 id 卻 is_failed=True:這**不是**零副作用拒絕(紅線②)。SDK
                # `_artifact/generation.py:586-590` 明寫「有 artifact_id 就回
                # GenerationStatus(task_id=artifact_id, status=...)」,而
                # `_ARTIFACT_STATUS_MAP` 含 FAILED → "failed",所以「有 id + failed」
                # 這個形狀在上游可達。rotate 重送在這個形狀下會產生第二顆 artifact。
                _write_audit(audit, PHASE_ACCEPTANCE_UNKNOWN, status, account=account)
                raise
            rotated = rotate_for_quota(audit, status, account, tried)
            if rotated is not None:
                account, client = rotated
                if account:
                    tried.add(account)
                continue
            _write_audit(audit, PHASE_REFUSED, status, account=account)
            raise
