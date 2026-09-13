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

**家族差異用 callback 表達,不用 if/else 分家。** 音檔有 durable attempt 可以標終態
(`not_accepted` / `acceptance_unknown`,好讓呼叫端知道要不要先對帳);附件沒有 attempt,
它的終態就是原樣拋給呼叫端 —— 遠端什麼都沒建出來,沒有東西要對帳。差別只在傳不傳
`on_clean_refusal` / `on_acceptance_unknown`,迴圈本身一個字都不分岔。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from notebooklm.exceptions import ArtifactFeatureUnavailableError, RateLimitError

from . import runtime
from ._errors import access_denied_error, is_permission_denied
from .manifest_store import ManifestPostCommitError
from ._status import ensure_started

# 生成 kickoff 的例外裡,**契約上保證「伺服器沒有建出任何 task」**的那幾種。
# notebooklm-py 0.8.0(ADR-0019 / #1342)把同步拒絕從「回傳 status='failed'」改成
# raise:`ArtifactFeatureUnavailableError` 來自 `_parse_generation_result` 的
# 「a missing id means no task was created」,契約保證乾淨。
#
# `RateLimitError` **不是只有一個生產者**(round2 獨立複審 V-B 修正過的認知,
# 原本這裡誤把它寫成單一形狀):
#   - decoder 把伺服器回應裡的 `USER_DISPLAYABLE_ERROR` 解碼成例外時,附帶
#     `rpc_code="USER_DISPLAYABLE_ERROR"`(`_web/wire/decoder.py::extract_rpc_result`)
#     ——這是契約講死「沒建出 task」的那一種,`test_contracts.py` 有 `getsource` 鎖住;
#   - transport 層的 HTTP 429(`_web/transport/executor.py`)`rpc_code=None`——請求
#     **已經送到伺服器**才被限流打回來,上游自己的
#     `notebooklm.artifacts.with_rate_limit_retry` 對這種形狀也是原地重送
#     (docstring 明寫「retrying on a raised RateLimitError」),不特別區分來源。
# 這個 tuple 目前**兩種都當成乾淨拒絕、rotate 帳號重送**——這是**已知的取捨,不是
# 誤判修好了**:唯一可靠的判別特徵是 `rpc_code`(`retry_after`/`__cause__` 都不可靠,
# 沒有 `Retry-After` header 時 `retry_after` 也是 `None`);429 那條理論上換帳號可能
# 是錯的解藥(限流通常綁 IP/host,不綁帳號,換帳號等於讓 N 個帳號輪流撞同一個限流
# 器),但要動這條分類需要先跑一輪真帳號 pool 驗收,本輪刻意只把認知寫對、不改行為。
#
# **刻意不收 `RPCError` / `DecodingError` / 網路錯誤 / CancelledError**:那些都可能
# 發生在伺服器已經受理之後,歸成 not_accepted 會讓呼叫端直接重生 → 重複 artifact +
# 重燒配額。兩種誤判的代價不對稱——把拒絕誤判成 unknown 只是多跑一次撈不到東西的
# 對帳(便宜),把已受理誤判成拒絕是真的損失,所以這個集合只放契約講死的那兩種
# (而 `RateLimitError` 目前寬鬆到含 429 那個次要來源,見上)。
REFUSED_WITHOUT_DISPATCH = (RateLimitError, ArtifactFeatureUnavailableError)

#: 寫一筆「A 拒絕 → 改用 B 重送」的稽核紀錄。`(reason, from_account, to_account)`。
#: **傳 `None` = 這個呼叫端沒有稽核面 → 一律不換帳號**:ADR-0010 §Transparency 說
#: manifest 是唯一的稽核憑據,沒地方記錄就靜默換帳號等於自廢那條紀律(低階
#: `tools_basic.generate_audio` 就是這一種,它連 manifest 參數都沒有)。
RecordFailover = Callable[[object, "str | None", str], None]


def describe_refusal(reason: object) -> tuple[str, str]:
    """把拒絕的原因翻成稽核紀錄要寫的 `(type, message)`。

    **兩種形狀**:0.8.0 起的同步拒絕是例外;0.7.x 是「不 raise,回 `task_id=""` +
    `is_failed`」的 status 物件。稽核紀錄兩種都要讀得懂,否則 0.7.x 那條路留下的
    `type` 會是 `"NoneType"` 之類的無用字串。

    抽到這裡是因為音檔的 `_record_dispatch_failover` 與附件的 recorder 都要同一份 ——
    各寫一份就是「上游改了 status 的欄位名、只有一處被改到」。
    """
    if isinstance(reason, BaseException):
        return type(reason).__name__, str(reason) or type(reason).__name__
    message = getattr(reason, "error", None) or getattr(reason, "status", "failed")
    return getattr(reason, "error_code", None) or "not_accepted", message


def rotate_for_quota(
    record: RecordFailover | None,
    reason: object,
    from_account: str | None,
    tried: set[str],
) -> tuple[str | None, Any] | None:
    """還有沒試過的帳號就換過去,回**換過去那一個**的 `(label, client)`;沒有就回 None。

    `record is None`(沒有稽核面)一律不換,理由見 `RecordFailover`。

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
    假件)都真的遵守 `skip`,這裡仍是 `record` 之前唯一擋得住假稽核紀錄的地方(紅線⑤)。
    """
    if record is None:
        return None
    # 冷卻副作用永遠要做(帳號真的被拒過),所以這一步不能被 `tried` 擋掉。
    to_account = runtime.rotate_client(refused=from_account, skip=frozenset(tried))
    if to_account is None or to_account in tried:
        return None
    try:
        record(reason, from_account, to_account)
    except ManifestPostCommitError:
        # `os.replace` 已經是 commit point —— 這筆稽核**是** durable 的,只有「額外的」
        # parent-directory fsync 失敗(NAS/overlay 回 EIO/ENOTSUP)。往外拋會讓紀錄說
        # 「換到 B」而主迴圈從未 dispatch B,正是 v0.9.8 P1 修掉的幽靈紀錄形狀
        # (codex 獨立複審 Review B #4)。放行才能讓紀錄與行為一致。
        # **其餘例外照樣往外拋**:那代表紀錄沒 commit,而「沒有稽核就不准換帳號」
        # 是 ADR-0010 的紅線 —— 這時候安靜換帳號比噴一個寫檔錯誤更糟。
        pass
    # rotate_client() 與 snapshot() 之間沒有 await,所以取到的必然是剛換過去那個槽位。
    return runtime.snapshot()


async def dispatch_with_failover(
    dispatch: Callable[[Any], Awaitable[Any]],
    *,
    account: str | None,
    client: Any,
    record_failover: RecordFailover | None,
    notebook_id: str | None = None,
    on_clean_refusal: Callable[[object, "str | None"], None] | None = None,
    on_acceptance_unknown: Callable[[object, "str | None"], None] | None = None,
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
        dispatch: `async (client) -> status`。**只包 kickoff 那一個 RPC**,不要把下載或
            回寫包進來 —— 重送的冪等性只在「伺服器還沒建出 task」這個前提下成立。
        record_failover: 見 `RecordFailover`。`None` = 不准換帳號。
        notebook_id: 只用來讓權限錯誤的指引訊息指名是哪一本(紅線③)。
        on_clean_refusal: `(reason, account)` —— 「伺服器明確拒絕、沒建出 task」的終態。
            `account` 是**這一腿實際被拒的**帳號。有 durable attempt 的呼叫端拿它標
            `not_accepted`(讓呼叫端可以直接重試、不必先跑一次註定撈不到東西的對帳);
            沒有 attempt 的呼叫端拿它補上「最後那一腿」的稽核 —— `record_failover` 只在
            **找得到下一個帳號**時才寫,所以耗盡時最後一個(單帳號 pool 則是唯一一個)
            被拒的帳號**一筆紀錄都不會留**,ADR-0010 要的「哪個帳號被拒過」就答不出來
            (codex 獨立複審 Review B #5)。
        on_acceptance_unknown: `(reason, account)` —— 「受理結果不明」的終態(同上)。
            附件家族也要記:遠端可能已經有一顆沒人綁得到的 artifact,留一筆才看得出來。
    """
    def _mark(callback, reason: object, used: str | None) -> None:
        """呼叫終態 callback,但**不讓稽核的 post-commit 失敗遮蔽主要例外**。

        `rotate_for_quota` 早就對 rotation 的 recorder 做了這件事,終態這半卻漏了 ——
        典型的「補一半」,而且後果直接違反這個函式對呼叫端的承諾:單帳號撞配額時,
        `attachment_dispatch_refused` 的 `os.replace` 已經成功、只有 parent-dir fsync 回
        EIO,呼叫端拿到的就從 `RateLimitError` 變成 `ManifestPostCommitError`
        (實測;獨立複審第二輪 F5)。紀錄**是** durable 的,所以吞掉它、原例外照樣往外拋。
        其餘例外不吞:那代表紀錄真的沒寫進去,比配額錯誤更值得知道。
        """
        if callback is None:
            return
        try:
            callback(reason, used)
        except ManifestPostCommitError:
            pass

    tried: set[str] = {account} if account else set()
    while True:
        try:
            status = await dispatch(client)
        except REFUSED_WITHOUT_DISPATCH as exc:
            # 伺服器明確拒絕、沒有建出 task(0.8.0 起改成 raise;0.7.x 走下面的
            # ensure_started 分支)。這是**乾淨的終態**,不是「結果不明」。
            try:
                rotated = rotate_for_quota(record_failover, exc, account, tried)
            except Exception:
                # record() 本身寫入失敗(非 ManifestPostCommitError,例如磁碟滿)——
                # `runtime.rotate_client()` 在 `rotate_for_quota` 裡已經先跑,冷卻與
                # 游標都已生效,但「換帳號」這件事從未真正稽核成功。若讓這個新例外
                # 直接穿出去,下面的 `_mark(on_clean_refusal, …)` 就會被跳過,attempt
                # 停在 `dispatching` 沒有任何終態(下一次 reconcile 才會發現,而不是
                # 這裡就講清楚)。終態要標成**原本的拒絕原因**(`exc`)——比照 `_mark`
                # 自己的取捨,寫檔真的沒成功比配額被拒更值得讓呼叫端知道,所以原樣
                # 往外拋這個寫入失敗,不是原本的 `exc`。
                _mark(on_clean_refusal, exc, account)
                raise
            if rotated is not None:
                account, client = rotated
                if account:
                    tried.add(account)
                continue
            _mark(on_clean_refusal, exc, account)
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
                # 先建好帶指引的例外、再標終態 —— 紀錄下來的要是**這份帶著下一步動作**的
                # 訊息,不是上游原始的 "permission denied"。呼叫端事後只看得到紀錄時
                # (例如 podcast_series 的結構化 partial 只回訊息不回原始例外),
                # 指引才不會整條蒸發。
                _mark(on_clean_refusal, denied, account)
                raise denied from exc
            _mark(on_acceptance_unknown, exc, account)
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
                _mark(on_acceptance_unknown, status, account)
                raise
            try:
                rotated = rotate_for_quota(record_failover, status, account, tried)
            except Exception:
                # 0.7.x status 分支的孿生保護——理由與上面 REFUSED_WITHOUT_DISPATCH
                # 那個 except 完全相同,只有一處只補一個分支就是「補一半」。
                _mark(on_clean_refusal, status, account)
                raise
            if rotated is not None:
                account, client = rotated
                if account:
                    tried.add(account)
                continue
            _mark(on_clean_refusal, status, account)
            raise
