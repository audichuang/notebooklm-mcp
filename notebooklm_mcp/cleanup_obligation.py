"""清理義務(ADR-0009)的單一模組:記錄 / 排隊 / 結案 / 「現在能不能生成」。

義務的形狀(`episode["pending_source_cleanup"]`、tombstone 的 `source_cleanup_unresolved`)
與四階段 gate 的順序(驗身分 → 一次 `sources.list` → revision-CAS 寫入 → 寫完才 raise)
都只活在這裡;`tools_podcast` 的 retract / adopt / resume / series 只呼叫本模組。

唯一的遠端依賴是 `list_sources`(`async (notebook_id) -> sources`,通常是
`client.sources.list`)。候選判準仍只有一份:`audio_finalize.unresolved_upload_candidates`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .audio_finalize import (
    UPLOAD_DISPATCH_WINDOW,
    claimed_source_ids,
    unresolved_upload_candidates,
    unresolved_upload_descriptor,
    upload_dispatch_window_closed,
)
from .manifest_store import ManifestConflictError, ManifestStore


def list_obligations(episode: dict, *, fallback_notebook: object = None) -> list[dict]:
    """讀出這一集未結案的清理義務,**每一筆都帶自己的 notebook 身分**。

    身分要跟著義務走,不能拿 episode 當下的 notebook 回推(盲審 P1):`manifest_store`
    明文允許 retract 的 tombstone 保留建立時綁的舊 notebook(否則 retract 之後想換
    notebook 重生就寫不進去),於是同一集換本重生之後,舊義務的 source 其實躺在**別本**
    筆記本裡 —— 拿新本去查會「查無此 source」而把義務誤判成已結案,舊來源從此沒人記得,
    卻仍在原本那本污染後續每一集的 context。

    v0.9.11 之前寫進去的是純字串(沒有身分)。那些只能補上 `fallback_notebook`
    (通常是 episode 當下的 canonical)—— best effort,補不出來(`None`)就是身分不明,
    由呼叫端 fail-closed,**不准當成已結案**。
    """
    obligations: list[dict] = []
    for row in episode.get("pending_source_cleanup") or []:
        if isinstance(row, str) and row:
            obligations.append({"source_id": row, "notebook_id": fallback_notebook})
        elif isinstance(row, dict) and isinstance(row.get("source_id"), str):  # noqa: SIM102 —— 巢狀 if 刻意分開,與上一個分支的語意並列
            if row["source_id"]:
                obligations.append(
                    {
                        "source_id": row["source_id"],
                        "notebook_id": row.get("notebook_id"),
                    }
                )
    return obligations


def record_cleanup_obligation(episode: dict, source_id: str, notebook_id: object) -> None:
    """把一筆帶身分的義務排進這一集(同一個 source_id 已在就不重複加)。

    只在真的有東西要排時才建 key,維持「沒有義務就沒有這個欄位」的既有形狀。
    """
    pending = episode.setdefault("pending_source_cleanup", [])
    for row in pending:
        existing = row if isinstance(row, str) else (row or {}).get("source_id")
        if existing == source_id:
            return
    pending.append({"source_id": source_id, "notebook_id": notebook_id})


def drop_cleanup_obligations(episode: dict, source_ids: set[str]) -> None:
    """清掉「這次真的查過、確認不在」的那幾筆(逐筆比對,不整欄 pop)。"""
    left = [
        row
        for row in episode.get("pending_source_cleanup") or []
        if (row if isinstance(row, str) else (row or {}).get("source_id")) not in source_ids
    ]
    if left:
        episode["pending_source_cleanup"] = left
    else:
        episode.pop("pending_source_cleanup", None)


def queue_pending_source_cleanup(
    episode: dict, source_ids: list[str], *, exclude: str, notebook_id: object
) -> list[str]:
    """把 adopt 換掉的舊 source(s)排進這一集的 ``pending_source_cleanup``(去重、
    排除這次選中的那筆),讓 `assert_generation_allowed` 在下一次生成前逼刪——
    否則同名重複 source 從此沒人記得,後續每集生成都讀到它。

    回傳的是**這一集尚未結案的全部義務**,不是只有這次新增的那幾筆:adopt 回應遺失時
    host 會冪等重呼,若第二次回傳空清單、`safe_next_action` 就會從 source_delete 翻回
    series,host 照著走卻被 gate 硬擋成 ValueError(狀態冪等、指引卻不冪等,等於把
    自動化 host 導進死路)。"""
    for source_id in dict.fromkeys(source_ids):  # 去重、保留順序
        if isinstance(source_id, str) and source_id and source_id != exclude:
            record_cleanup_obligation(episode, source_id, notebook_id)
    return [row["source_id"] for row in list_obligations(episode)]


#: 對帳結果寫回時撞上並行寫入的重試上限。撞這麼多次代表有別的東西在狂寫 manifest,
#: 那時候硬拚不會比讓呼叫端重跑更好。
_CLEANUP_SETTLE_RETRIES = 5


def settle_cleanup_state(
    store: ManifestStore,
    settle,
    *,
    expected_revision: object,
    discovered: dict,
    settled_attempt_ids: set,
    checked_absent_by_episode: dict,
    notebook_id: str,
) -> None:
    """把這一輪算出來的清理狀態寫回去,**永遠帶 revision CAS**。

    **為什麼清除那一側也要 CAS**(盲審 P1,靜默遺失義務):上一版只在「有新發現」時才 CAS,
    理由是「沒有新發現時 settle 只做對並行天生安全的事」—— 那個論證只看了 manifest 側的
    競態,漏了**兩個 process 對遠端的觀測不同**這件事:

        A、B 同讀 revision R
        A 的 sources.list 回零候選(orphan 還沒出現在清單裡)、窗已關
        B 的 sources.list 撈到 orphan
        A 無條件寫入 → 清掉 source_cleanup_unresolved,成為 R+1
        B ── 下一輪連這筆記錄都不會收集(旗標沒了)→ 不算候選、不寫任何東西、**成功返回**

    結果是 orphan 從 manifest 完全消失,而且全程零錯誤、零告警,下一次生成直接放行。
    清旗標是破壞性方向,卻是唯一沒有保護的那條路。

    **只加 CAS 還不夠**:誰先 commit 誰贏,零候選那方先贏的話,正候選那方重跑也看不到旗標。
    所以衝突時的處置是不對稱的 ——

    - **保留「加義務」**(fail-closed 方向):重讀最新 manifest、**重驗 ownership**(await
      期間別集可能剛把某個 source 認領成它的合法 continuity source,那筆就不再是孤兒),
      再條件寫入。已觀測到的證據不因為衝突而消失。
    - **丟掉「結案 unresolved 旗標」**(`settled_attempt_ids`):那是拿可能過期的零候選觀測
      算出來的,而清錯就是永久遺失。下一輪 gate 會用新的 manifest 重算,代價只是多跑一次。
    - **保留 `checked_absent_by_episode`**:它逐筆比對「這次真的去 notebook 查過、確認不在」
      的那幾個 id,別的 writer 併發追加的是**別的** id,不會被它碰到 —— 這一個清除方向對
      並行是真的安全(「await 期間又一次 retract 追加新義務不得被吞掉」有既有測試鎖著)。
    """
    try:
        store.update(settle, expected_revision=expected_revision)
        return
    except ManifestConflictError:
        pass

    settled_attempt_ids.clear()
    if not discovered and not checked_absent_by_episode:
        return

    for _ in range(_CLEANUP_SETTLE_RETRIES):
        fresh = store.read()
        claimed = claimed_source_ids(fresh)
        for ep_n in list(discovered):
            still_orphan = [source_id for source_id in discovered[ep_n] if source_id not in claimed]
            if still_orphan:
                discovered[ep_n] = still_orphan
            else:
                del discovered[ep_n]
        if not discovered and not checked_absent_by_episode:
            return
        try:
            store.update(settle, expected_revision=fresh.get("revision"))
            return
        except ManifestConflictError:
            continue
    raise ValueError(
        f"notebook {notebook_id!r} 的回錄 source 清理義務對帳期間 manifest 一直被改動,"
        f"連續 {_CLEANUP_SETTLE_RETRIES} 次寫不進去。已經撈到的候選還沒落盤,"
        "**不要當成沒事**:等並行的工作停下來後重呼同一支工具重新對帳。"
    )


async def assert_generation_allowed(
    list_sources, store: ManifestStore, notebook_id: str, episode_n: int
) -> None:
    """`podcast_attempt_retract` 留下的清理義務,在下一次生成前強制結案。

    retract 不打 RPC(刻意),所以刪舊回錄 source 是呼叫端的動作;但「靠文件提醒」等於
    沒有防護——finalize 是按 source_id 驗的,漏刪會靜默留下兩筆同名 media,之後每次
    生成都把兩份逐字稿餵進 context。故把它變成生成的 precondition:真的去 notebook
    查,還在就 fail-closed;已經不在才清掉義務、放行。

    聚合範圍是**整個 canonical notebook**,不是只有 `episode_n` 這一列:retract EP_a
    後漏刪其 stale source,若只驗當前這一集,回頭跳過 dirty 的 EP_a、改生成同 notebook
    的 EP_b(例如 start 跳過較早集)會直接放行——EP_b 未指名 source_ids 時就把 EP_a
    的拒收逐字稿讀進 context。canonical 不同的集互不影響(不同 notebook 的義務不該
    互相卡住)。

    **兩種義務都在這裡結案**(F2):
      (1) ``pending_source_cleanup``——身分已確定的 id,驗「已經不在 notebook」。
      (2) tombstone 上的 ``source_cleanup_unresolved``——retract 作廢了一顆 upload 還沒
          落盤的 attempt,遠端可能多出一筆沒人記得的 media。用
          ``unresolved_upload_candidates()``(與 finalize 對帳同一份判準)去撈。

    **執行順序本身就是安全性質**(盲審 P1×3 + P2×2 之後重寫):

    ① **身分先於一切。** 清理義務綁的是「當初上傳的那本 notebook」,而 (2) 的權威來源是
       **attempt 自己的 `notebook_id`** —— `manifest_store._validate` 明文允許 tombstone
       保留建立時綁的舊 notebook(否則 retract 之後想換 notebook 重生就寫不進去),所以
       episode 當下的 notebook 只是 fallback,不是判準。身分沒對上就**不篩選、不持久化、
       不回傳任何候選**:拿呼叫端隨手傳的 notebook 去撈,撈到的是**別本筆記本裡碰巧同名
       同時間窗**的 source,而我們會把它寫進清理義務、再叫呼叫端 `source_delete` —— 那是
       對無關 notebook 下破壞性指令,比漏掉義務更糟。
    ② **一次 list,純計算,不中途拋。** 掃描過程只累積結果(candidates / settled /
       waiting / checkpoint 壞掉的),**不在迴圈裡 raise** —— 前面幾顆已經撈到的候選還沒
       落盤,一拋就永遠回不來(那些 id 從此沒人記得)。
    ③ **單次 revision-CAS mutation,而衝突時「加義務」不准跟著陪葬。**
       `_claimed_source_ids` 是在 `await` **之前**的 snapshot 上算的,await 期間另一個
       finalizer 可能剛好把某個 source claim 成它那一集的合法 continuity source —— 拿舊
       ownership 寫新 manifest 就是把合法來源排進待刪清單。所以寫入一律帶
       `expected_revision`。**但衝突不能一律放棄**:那會讓「零候選那方先 commit、正候選
       那方撞衝突」變成孤兒永久失憶(見 `settle_cleanup_state`)。處置是不對稱的 ——
       重驗 ownership 後把候選寫進去,清旗標那一側則丟掉、留給下一輪重算。
    ④ **寫完才 raise;完全沒變更就不寫。** 「已確認不在」的清除、撈到的新義務、結案的
       unresolved 旗標,全部在同一次 mutation 裡結算 —— 否則已經刪掉的 id 會卡在
       `pending_source_cleanup` 裡(因為這一輪先在 waiting 那裡拋了),而
       `podcast_attempt_retract` 的冪等回傳會繼續說 `source_delete`,指向一個早就不存在的
       東西。反過來,blocked/waiting 這種什麼都沒改的路徑**一次 `store.update` 都不准發**:
       `ManifestStore` 的 revision 是無條件 +1,無效寫盤還會平白撞掉別人的 discovery CAS。

    `sources.list` / 認證失敗一律往上拋 —— 義務不會被誤清。"""
    snapshot = store.read()
    snapshot_revision = snapshot.get("revision")
    episodes = snapshot.get("episodes", [])
    manifest_notebook_id = snapshot.get("notebook_id")

    def canonical_notebook(row: dict) -> object:
        return row.get("notebook_id") or manifest_notebook_id

    def cleanup_notebook(row: dict, attempt: dict) -> object:
        """清理義務綁的那本 notebook:**attempt 自己的優先**(見 docstring ①)。"""
        return attempt.get("notebook_id") or canonical_notebook(row)

    # (episode_n, attempt_id, attempt, upload)。attempt 整顆帶著走:候選判準要讀它的
    # finalize checkpoint(baseline／expected_title／dispatched_at),而那份資料刻意
    # 只有一份、留在 attempt 上,沒有複製進 retraction。
    unresolved_here: list[tuple[object, str, dict, dict]] = []
    unresolved_elsewhere: list[tuple[object, str, object]] = []
    for row in episodes:
        for candidate_attempt in row.get("attempts", []):
            retraction = candidate_attempt.get("retraction") or {}
            if not retraction.get("source_cleanup_unresolved"):
                continue
            upload = unresolved_upload_descriptor(candidate_attempt)
            if upload is None:
                # source_id 後來落盤了(adopt/對帳補回身分):義務改由既有的
                # stale_source_ids／pending 路徑處理,這裡不再管它。
                continue
            owner = cleanup_notebook(row, candidate_attempt)
            record = (row.get("episode"), candidate_attempt.get("attempt_id"))
            if owner == notebook_id:
                unresolved_here.append((*record, candidate_attempt, upload))
            else:
                # **別本 notebook 的義務不擋這一本的生成**(canonical 不同的集互不影響),
                # 但身分不明(`None`)的擋 —— 那種列無從證明它不是這一本的。
                if owner is None:
                    unresolved_elsewhere.append((*record, owner))

    # **每筆義務用它自己的 notebook 身分分流**(盲審 P1):
    #   == 這一本 → 這次要驗;
    #   != 這一本 → 別本的問題,不參與(目標這一集自己的另外擋,見下);
    #   None(legacy 純字串且該集連 canonical 都沒有)→ 身分不明,fail-closed。
    pending_here: list[tuple[object, str]] = []
    pending_identity_unknown: list[tuple[object, str]] = []
    for row in episodes:
        for obligation in list_obligations(row, fallback_notebook=canonical_notebook(row)):
            owner = obligation["notebook_id"]
            if owner == notebook_id:
                pending_here.append((row.get("episode"), obligation["source_id"]))
            elif owner is None:
                pending_identity_unknown.append((row.get("episode"), obligation["source_id"]))

    # 目標這一集自己的義務若屬於**別本** notebook,不該在這裡「順便放行」:那筆 source
    # 仍在原本那本污染它的 context,而這次生成不會去碰它。明確擋下來並說出是哪一本,
    # 比靜默跳過誠實(判準是義務自己的身分,不再是 episode 當下的 canonical)。
    target_elsewhere = sorted(
        {
            row["notebook_id"]
            for episode_row in episodes
            if episode_row.get("episode") == episode_n
            for row in list_obligations(
                episode_row, fallback_notebook=canonical_notebook(episode_row)
            )
            if row["notebook_id"] not in (None, notebook_id)
        },
        key=str,
    )
    if target_elsewhere:
        raise ValueError(
            f"episode {episode_n} 的回錄 source 清理義務屬於 notebook {target_elsewhere!r},"
            f"不是 {notebook_id!r};不能從別本筆記本結案(那幾筆仍在原本那本污染 context)。"
        )

    # **身分不明的義務在打 RPC 之前就擋掉**(docstring ①):不篩選、不持久化、
    # 不回傳任何候選 —— 這一條必須排在 `sources.list` 之前,否則「撈到的候選」已經被算出來
    # 並寫進義務了,擋在後面沒有意義。
    if unresolved_elsewhere or pending_identity_unknown:
        details = ", ".join(
            [f"episode {ep_n}: attempt {att!r}" for ep_n, att, _ in unresolved_elsewhere]
            + [f"episode {ep_n}: source {sid!r}" for ep_n, sid in pending_identity_unknown]
        )
        raise ValueError(
            f"這幾筆回錄 source 清理義務沒有可核對的 notebook 身分:{details}。"
            "拿別本筆記本查到「沒有」不能當成義務結案,而拿它查到的候選更不能當成要刪的東西。"
            # **不要教「去 manifest 補 notebook_id」** —— `series_manifest.json` 只由工具
            # 寫入(手寫的紀錄繞過 artifact claim 唯一性與 advisory lock),而且沒有任何
            # 工具設得了那個欄位:那句話會把呼叫端導進一條它做不到、也不准做的路。
            # 這種狀態只出現在 v0.9.11 之前留下的純字串義務 + 該集連 canonical notebook
            # 都沒有的 legacy manifest 上,出路是人工判斷後用 source_delete 收掉。
            "這是 v0.9.11 之前的 legacy 義務(當時沒有記身分)且該集沒有 canonical "
            "notebook_id 才會出現。**不要手改 manifest**(它只由工具寫入):用 "
            "source_list 在候選的 notebook 裡找到這幾筆,確認之後 source_delete 掉;"
            "刪乾淨後這道 gate 就會放行。"
        )

    if not pending_here and not unresolved_here:
        return

    sources = await list_sources(notebook_id)
    live = {getattr(source, "id", None) for source in sources}
    # T5(P3):這一份是「已確定身分的 pending 義務」違規,不受下面 CAS 修剪影響
    # (那道修剪只管 `discovered`——見下方 `settle_cleanup_state` 呼叫後的重算)。
    pending_violations = [
        (ep_n, source_id) for ep_n, source_id in pending_here if source_id in live
    ]

    # 純計算:全部累積,迴圈裡一律不 raise(docstring ②)。
    now = datetime.now(UTC)
    discovered: dict[object, list[str]] = {}
    waiting: list[tuple[object, str]] = []
    unreconcilable: list[str] = []
    settled_attempt_ids: set[str] = set()
    for ep_n, unresolved_attempt_id, unresolved_attempt, upload in unresolved_here:
        try:
            candidates = unresolved_upload_candidates(
                snapshot, unresolved_attempt, unresolved_attempt_id, sources
            )
            window_closed = upload_dispatch_window_closed(upload, now=now)
        except ValueError as exc:
            # checkpoint 缺 expected_title／dispatched_at(手改過或跨版本的 manifest):
            # 對不出來就不能放行,但錯誤訊息要說得出是哪一顆,否則生成前突然冒出一句
            # 「dispatch time is missing」沒人查得動。**先記下來,不在這裡拋** ——
            # 前面幾顆撈到的候選還沒落盤。
            unreconcilable.append(f"episode {ep_n} attempt {unresolved_attempt_id!r}({exc})")
            continue
        if candidates:
            discovered.setdefault(ep_n, []).extend(candidates)
            # T5(P3):**不在這裡就併進最終 violations** —— `settle_cleanup_state`
            # 撞 CAS 衝突時會重驗 ownership、就地修剪 `discovered`(await 期間別的
            # finalizer 可能剛把某個候選 claim 成它自己的合法 continuity source,
            # 那筆就不再是孤兒)。若在這裡先併進一份定案的 `violations`,修剪只影響
            # `discovered`、不影響這份副本,回傳的錯誤訊息會繼續點名一筆已經被
            # 合法認領的 source,叫呼叫端去 `source_delete` 它。violations 的最終
            # 內容改到 `settle_cleanup_state` 之後、單一位置,從(可能已修剪的)
            # `discovered` 重算。
        elif window_closed:
            settled_attempt_ids.add(unresolved_attempt_id)
        else:
            waiting.append((ep_n, unresolved_attempt_id))

    # 「這次真的查過、確認不在」的那幾筆。**只涵蓋身分等於這一本的義務** —— 別本的
    # 根本沒查過,不可能算「確認不在」(逐筆比對、不整欄 pop:await 期間可能又有一次
    # retract 追加新義務)。
    checked_absent_by_episode: dict[object, set[str]] = {}
    for ep_n, source_id in pending_here:
        if source_id not in live:
            checked_absent_by_episode.setdefault(ep_n, set()).add(source_id)

    def settle(manifest: dict) -> None:
        for row in manifest["episodes"]:
            for candidate_attempt in row.get("attempts", []):
                if candidate_attempt.get("attempt_id") in settled_attempt_ids:
                    # 窗已關 + 零候選 = 遠端真的沒有多出東西。義務結案,重生放行。
                    (candidate_attempt.get("retraction") or {}).pop(
                        "source_cleanup_unresolved", None
                    )
            found = discovered.get(row.get("episode"))
            if found:
                history = row.setdefault("previous_feedback_source_ids", [])
                for source_id in found:
                    if source_id not in history:
                        history.append(source_id)
                    # 候選是從**這一本**的 sources.list 撈出來的,身分就是它。
                    record_cleanup_obligation(row, source_id, notebook_id)
            checked_absent = checked_absent_by_episode.get(row.get("episode"))
            if checked_absent:
                drop_cleanup_obligations(row, checked_absent)

    # **完全沒有變更就不要寫**(盲審 P2):`ManifestStore.update` 的 revision 是無條件 +1,
    # 所以 blocked/waiting 這種「只是來查一下」的路徑照樣會 bump —— 除了無效寫盤,還會
    # 平白撞掉另一個 process 正在做的 discovery CAS。
    if discovered or settled_attempt_ids or checked_absent_by_episode:
        settle_cleanup_state(
            store,
            settle,
            expected_revision=snapshot_revision,
            discovered=discovered,
            settled_attempt_ids=settled_attempt_ids,
            checked_absent_by_episode=checked_absent_by_episode,
            notebook_id=notebook_id,
        )

    # T5(P3):**單一位置**,用 CAS 衝突處置之後(可能已修剪)的 `discovered` 重算
    # 最終要回報的 violations——`settle_cleanup_state` 撞衝突時會就地修剪
    # `discovered`(見上方呼叫點),這裡讀到的一定是修剪後的結果,不會把已被別的
    # finalizer 合法認領的 source 也點名進錯誤訊息。
    violations = list(pending_violations)
    for ep_n, source_ids in discovered.items():
        violations.extend((ep_n, source_id) for source_id in source_ids)

    if violations:
        details = ", ".join(f"episode {ep_n}: {sid}" for ep_n, sid in violations)
        raise ValueError(
            f"notebook {notebook_id!r} has retracted feedback sources still in the "
            f"notebook: {details}. Delete them first — "
            f"source_delete(notebook_id={notebook_id!r}, source_id=...) — then retry; "
            "leaving them creates two identically named sources."
        )
    if unreconcilable:
        raise ValueError(
            "這幾顆 retracted attempt 留下未結案的回錄 source 清理義務,但它們的 upload "
            f"checkpoint 對不出候選:{', '.join(unreconcilable)}。"
        )
    if waiting:
        details = ", ".join(f"episode {ep_n}: {att!r}" for ep_n, att in waiting)
        window_seconds = int(UPLOAD_DISPATCH_WINDOW.total_seconds())
        raise ValueError(
            f"notebook {notebook_id!r} has retracted attempts whose feedback source "
            f"upload was never resolved: {details}. 現在查到零候選,但候選窗還沒關"
            f"(dispatch 起算 {window_seconds} 秒),此刻的「沒有」不等於「不會出現」。"
            "等窗關上後重呼同一支工具,它會自己對帳:撈到就會要求 source_delete,"
            "確認零候選才放行。"
        )
