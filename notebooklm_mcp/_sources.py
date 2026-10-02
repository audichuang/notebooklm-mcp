"""Caller-supplied source selection: shape check + cheap existence preflight.

不傳 ``source_ids`` 時 SDK 自己抓筆記本全部來源(``_artifacts.py`` 的 fallback);
要「只聽這幾筆」就得指名。**選哪幾筆是 host 的政策**(ADR-0007)——例如重生某集時
排除集號更大的來源——這裡只負責別讓壞的選取燒掉一次生成。
"""

from __future__ import annotations

from ._errors import raise_if_access_denied


async def _list_sources(client, notebook_id: str):
    """唯讀 `sources.list`,但把「這個帳號看不到 notebook」翻成 `NotebookAccessDenied`。

    **這兩個 preflight 現在是整條路徑上第一個遠端呼叫**,而權限分類原本只長在 dispatch
    helper 裡(`_dispatch_audio_with_failover`)。不在這裡轉的話,pool 剛 rotate 到看不到
    notebook 的帳號時,`podcast_series` 會裸拋上游的 `ClientError` —— 前面幾集跑完的
    `run_results` 一起丟掉,而且拿不到「去 `notebook_share_with_pool`」那條指引。
    v0.9.0/v0.9.1 花了兩輪才把那條路做成「結構化停點 + 可執行的指引」,preflight 前移
    不可以把它繞掉。

    只轉權限那一種:網路錯誤、認證過期一路吞下去只會把根因埋掉(同 `notebook_share_with_pool`
    的紀律)。`except Exception` 不含 `CancelledError`,取消照樣往上傳。
    """
    try:
        return await client.sources.list(notebook_id)
    except Exception as exc:
        # 訊息本體住在 `_errors.raise_if_access_denied`,與 `notebook_get` 共用一份
        # (v0.9.14 FINDING-F:原本只有這裡有,那支裸拋上游訊息)。
        raise_if_access_denied(exc, notebook_id)
        raise


def to_source_ids(value: object) -> list[str] | None:
    """Normalize a caller's selection; ``None`` keeps the SDK's use-everything fallback."""
    if value is None:
        return None
    # 字串是最容易誤傳的形狀(單一 id 忘了包 list),而它剛好是 iterable of str,
    # 放行會靜默變成「每個字元一筆來源」。
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"source_ids must be a list of source id strings, got {value!r}")
    ids = list(value)
    if not ids:
        raise ValueError("source_ids must not be empty; omit it to use every source")
    if any(not isinstance(sid, str) or not sid.strip() for sid in ids):
        raise ValueError(f"source_ids entries must be non-empty strings, got {ids!r}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"source_ids must not repeat a source id, got {ids!r}")
    return ids


# 帶進一次生成的來源筆數上限(**含**指名的情況)。
# **實測到的**(同一節目、同一 brief):11–15 筆 → 6 項教錯 + 4 項捏造;6 筆 → 0;1 筆 → 0。
# 所以量到的是「≤9 乾淨、≥11 出事」——**10 這一格從來沒量過**。從 10 起拒絕是
# fail-closed 的政策選擇(未知的那一格算它會出事),不是實驗結論;寫清楚是因為
# 下一個要調這個數字的人,會把政策選擇誤當成量測結果。放寬要有新的實測。
_REFUSE_AT_SOURCE_COUNT = 10


class TooManySourcesError(ValueError):
    """帶進生成的來源筆數超過實測安全上限。

    **專屬型別是為了讓 `podcast_series` 認得出它**:series 對呼叫端的契約是「預期內
    的停止用回傳值表達」,而它的 except 分支收的是 `RuntimeError` /
    `_REFUSED_WITHOUT_DISPATCH` / `_TRANSIENT_TRANSPORT_ERRORS` —— 一個裸的
    `ValueError` 會直接冒泡出去,把**前面幾集已經跑完的 `run_results` 整份丟掉**。
    F-4 修過同一個形狀(見 `tools_podcast` inline dispatch 分支的註解),這裡用型別
    把它擋在再犯之前。`podcast_episode` 沒有那個契約,照樣外拋。
    """


async def assert_source_count_is_safe(
    client,
    notebook_id: str,
    source_ids: list[str] | None = None,
    *,
    pending_uploads: int = 0,
) -> None:
    """帶進這次生成的來源超過實測安全上限就拒絕,而且在任何遠端副作用之前。

    為什麼這條是 fail-closed 而不是文件警語:錯法是**模型從 context 撈前面集數講過的
    內容,填進它講不出來的位置**。聽起來很順、證據全部找得到出處(某集的四項捏造分別
    來自 EP01/EP02/EP12 的考點,一字不差),而**任何成功訊號都看不出來** —— 音檔存在、
    sha 相符、時長正常、`ok=true`。只有對逐字稿提問該篇獨有的事實才驗得出來。
    2026-08 有 host 跑 14 集,13 集裡 9 集內容錯置,全程回報成功。

    **變因是「帶進去幾筆」,不是「有沒有指名」。** 指名 12 筆與不指名 12 筆對模型是
    同一件事(實測那一列寫的就是「11–15 筆(全部前集)」),所以 `source_ids is not
    None` 只是 proxy,不能拿來當判準:host 讀了 skill 知道要指名、卻把「本集 + 最近
    5 集」做成「本集 + 全部前集」時,掛在 proxy 上的守門會全程沉默而事故照樣發生。
    指名時算 `len(source_ids)`(純本地,不打 RPC);沒指名才去問筆記本現有幾筆。

    `podcast_series` 在結構上生不出帶 `source_ids` 的 settings(兩條 dispatch 路徑都
    沒有那個參數),所以對它而言這道門等於「超過 9 筆就換工具」——那正是 skill §Episodic
    要求的行為,只是從「host 要讀到才知道」變成「呼叫不到」。

    **呼叫點有三個 —— 每個公開的音檔生成入口一個**,各自擺在該路徑最早的零副作用位置:

    - `tools_basic.generate_audio`:低階救援入口,但失效模式與高階一模一樣。守門只掛在
      podcast 家族的話,它就是繞過去的公開後門。
    - `_run_episode`:建 attempt 之前(`podcast_episode` 直呼、以及 series 的全新一集)。
    - `podcast_series` 的 `refuse_if_too_many_sources`:re-arm / supersede **之前**,
      也就是任何 manifest mutation 之前。那條的 attempt 是上一次呼叫建的,而筆記本來源
      **隨每集回錄遞增**,「上次還沒超標、這次超了」是真實時序。

    **仍未守的一處**:`artifact_retry_failed` 對 failed AUDIO 的原地重跑。RETRY_ARTIFACT
    只送 artifact_id,伺服器**應該**沿用原本的來源集合——但那是推測、沒有實測前提,所以
    既不加守門也不宣稱安全(見該工具 docstring)。
    """
    if source_ids is not None:
        count = len(source_ids)
        cause = f"指名了 {count} 筆來源"
        remedy = (
            "把清單砍到本集自己的來源 + 最近 5 集的音檔回錄(共約 6 筆),集號比本集大的回錄一律排除。"
        )
    else:
        # `pending_uploads` 是**這次呼叫自己稍後會加進筆記本**的來源筆數(目前只有
        # `prior_mp3_path` 那條 standalone 續集路徑)。不算進來的話守門會看到 9 筆放行、
        # 上傳完變 10 筆才送出 —— **同一個函式內的確定性順序**,不是競態,而守門等於沒守。
        count = len(await _list_sources(client, notebook_id)) + pending_uploads
        cause = f"notebook {notebook_id} 生成時會有 {count} 筆來源,不指名 source_ids 會全部帶進生成"
        remedy = (
            "改用 podcast_episode(..., source_ids=[...]) 指名這一集要聽的來源:"
            "本集自己的來源 + 最近 5 集的音檔回錄(共約 6 筆),集號比本集大的回錄一律排除。"
        )
    if count < _REFUSE_AT_SOURCE_COUNT:
        return
    raise TooManySourcesError(
        f"{cause} —— 實測 >= 11 筆就會讓模型拿前面集數的內容填空"
        "(聽起來很順、但整項是假的,而音檔/sha/時長全部正常,事後只有逐字稿提問驗得出來);"
        f"{_REFUSE_AT_SOURCE_COUNT} 這一格沒量過,所以 fail-closed 從它開始拒絕。"
        f"{remedy}"
        "用 source_list 拿真實 id,manifest 每集的 feedback_source_id 是回錄來源的正本。"
    )


async def assert_sources_exist(client, notebook_id: str, source_ids: list[str]) -> None:
    """打錯或已被刪掉的 source_id 伺服器不會擋——它只會靜默生出一集「看不到那幾筆
    來源」的音檔,燒完配額與二十分鐘才發現(同 ``_validate_report_prompt`` 的情境)。
    一次唯讀 list RPC 換掉那個代價。"""
    live = {getattr(source, "id", None) for source in await _list_sources(client, notebook_id)}
    missing = [sid for sid in source_ids if sid not in live]
    if missing:
        raise ValueError(
            f"source_ids not in notebook {notebook_id}: {', '.join(missing)}; "
            "call source_list and select from its real ids"
        )
