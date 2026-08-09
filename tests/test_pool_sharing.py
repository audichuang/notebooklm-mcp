"""pool 模式下 `notebook_create` 自動把 notebook 分享給其餘帳號(v0.8.0 驗收 F-2)。

failover 換帳號後是拿新帳號對**同一個 notebook_id** 送出 —— 新帳號看不到那個 notebook
的話,整條 pool 是空談。而 MCP 自己建的 notebook 預設只屬於建立它的帳號,**沒有任何
機制建立那個前置狀態**:驗收時是人工用 SDK 補上才走得動,真實使用者不會知道要做這件事。
"""
import pytest
from conftest import FakeClient
from notebooklm.exceptions import ClientError
from notebooklm_mcp._errors import NotebookAccessDenied

from notebooklm.rpc.types import SharePermission

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_basic as basic


async def test_notebook_create_shares_with_the_rest_of_the_pool(fake_client):
    runtime.set_clients(
        [("a@x.com", fake_client), ("b@x.com", fake_client), ("c@x.com", fake_client)]
    )

    out = await basic.notebook_create("驗收用")

    assert out["shared_with"] == ["b@x.com", "c@x.com"], "作用中的帳號自己不用分享"
    # notify=False:這是同一個人的帳號,不需要寄通知信。
    assert fake_client.sharing.calls == [
        (out["notebook_id"], "b@x.com", SharePermission.EDITOR, False),
        (out["notebook_id"], "c@x.com", SharePermission.EDITOR, False),
    ]


async def test_share_is_dispatched_by_the_client_that_created_the_notebook(fake_client):
    """P1:並行的另一個工具呼叫在 `notebooks.create()` 的 await 裡把游標 rotate
    A→B 時,分享仍必須由**建立 notebook 的那個 client**(A)發出。

    `notebook_create` 裡 `peers = _pool_peers(...)` 與 `notebooks.create()` 之間
    沒有 await(那半是乾淨的),裂縫在下一步:`create` 是 await,`_share_each`
    若在那之後才回頭讀 `runtime.get_client()`,讀到的就是此刻剛好輪到的 B——
    B 對一個自己還看不到的 notebook 送分享會失敗,雲端留下只有 A 看得到的孤兒
    notebook。而且 `rotate_client()` 刻意不回頭,事後在同一個 process 跑
    `notebook_share_with_pool` 一樣是 B 在跑,只能重啟 server 或人工補分享。

    兩個槽位必須是**不同的 fake 物件**才測得到「這通 RPC 實際發給誰」這一維
    (同型手法見
    `test_pool_gaps.test_download_after_failover_runs_on_the_new_accounts_client`)。
    """
    account_a, account_b = fake_client, FakeClient()
    runtime.set_clients([("a@x.com", account_a), ("b@x.com", account_b)])

    async def create_and_rotate_away(title):
        # 模擬「另一個工具呼叫在這趟 create() RPC 期間撞到配額並換了帳號」:
        # 全域游標被推到 b@x.com,而**這次建立**用的自始至終是 a@x.com 的 client。
        runtime.rotate_client()
        return type("NB", (), {"id": "nb-123", "title": title})()

    account_a.notebooks.create = create_and_rotate_away

    out = await basic.notebook_create("並行 rotate")

    assert out["shared_with"] == ["b@x.com"]
    assert account_a.sharing.calls == [
        (out["notebook_id"], "b@x.com", SharePermission.EDITOR, False)
    ], "分享必須由建立 notebook 的 a@x.com 發出,不是 create() 之後遊標停的位置"
    assert account_b.sharing.calls == [], (
        "b@x.com 只是被並行呼叫推到的游標位置,它自己還看不到剛建的 notebook,"
        "不該參與這次分享"
    )


async def test_single_account_creates_without_sharing(fake_client):
    """單帳號(現行所有機器的樣子):完全照舊,不多打任何 RPC。"""
    runtime.set_client(fake_client)

    out = await basic.notebook_create("單帳號")

    assert out["shared_with"] == []
    assert fake_client.sharing.calls == []


async def test_share_with_pool_backfills_an_existing_notebook(fake_client):
    """既有 notebook(v0.8.1 之前建的、或手動建的)的補救入口。

    自動分享只對 `notebook_create` 生效。既有 notebook 沒有這個前置狀態,配額耗盡
    failover 換帳號時會 NotebookAccessDenied —— 而在有這支工具之前,唯一的補法是
    自己寫 SDK 腳本。
    """
    runtime.set_clients(
        [("a@x.com", fake_client), ("b@x.com", fake_client), ("c@x.com", fake_client)]
    )
    fake_client.sharing.existing = ["a@x.com", "b@x.com"]  # b 已經分享過了

    out = await basic.notebook_share_with_pool("nb-old")

    assert out["shared_with"] == ["c@x.com"], "只補缺的那些"
    assert out["already_shared"] == ["b@x.com"]
    assert fake_client.sharing.calls == [
        ("nb-old", "c@x.com", SharePermission.EDITOR, False)
    ]


async def test_share_with_pool_is_idempotent(fake_client):
    """全部都分享過了就完全不打 add_user —— 重跑安全。"""
    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.existing = ["a@x.com", "b@x.com"]

    out = await basic.notebook_share_with_pool("nb-old")

    assert out["shared_with"] == []
    assert out["already_shared"] == ["b@x.com"]
    assert fake_client.sharing.calls == []


async def test_share_with_pool_on_single_account_is_a_noop(fake_client):
    runtime.set_client(fake_client)
    out = await basic.notebook_share_with_pool("nb-old")
    assert out["shared_with"] == [] and out["already_shared"] == []
    assert fake_client.sharing.calls == []


async def test_unresolvable_account_label_fails_loud(fake_client):
    """label 退回 `#N`(啟動時拿不到 email)就分享不了,必須當場爆掉。

    靜默略過會讓那個帳號永遠沒有權限,而症狀要等到 failover 換過去才出現 ——
    中間隔著整段生成時間。**驗證必須先於變更**:這是純本機、決定性的失敗
    (重啟前不會變),放在 notebooks.create() 之後會讓呼叫端每重試一次就多一個
    雲端孤兒 notebook —— 所以還要斷言完全沒建出 notebook。
    """
    runtime.set_clients([("a@x.com", fake_client), ("#2", fake_client)])

    with pytest.raises(RuntimeError, match="#2"):
        await basic.notebook_create("壞 label")

    assert fake_client.notebooks.created == [], "驗證失敗時不可留下孤兒 notebook"


async def test_share_failure_names_the_notebook_it_left_behind(fake_client):
    """分享失敗要說出 notebook 已經建出來了、id 是什麼。

    否則呼叫端只看到一個錯誤,不知道雲端多了一個孤兒 notebook,也無從手動補分享。
    """
    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.add_user_exc = RuntimeError("boom")

    with pytest.raises(RuntimeError) as excinfo:
        await basic.notebook_create("分享會失敗")

    message = str(excinfo.value)
    assert "已建立" in message
    assert fake_client.notebooks.created[-1] in message


async def test_existing_viewer_is_not_counted_as_already_shared(fake_client):
    """P0:VIEWER 不算「已分享」——`add_user` 的預設權限就是 VIEWER,使用者在
    NotebookLM 網頁手動分享過的既有 notebook 極可能就是這個權限,failover 換過去
    一樣 permission denied。只有 EDITOR/OWNER 才算數,VIEWER 要被升權重送。
    """
    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.existing = [("b@x.com", SharePermission.VIEWER)]

    out = await basic.notebook_share_with_pool("nb-old")

    assert out["already_shared"] == [], "VIEWER 不夠,不能算已分享"
    assert out["shared_with"] == ["b@x.com"]
    assert fake_client.sharing.calls == [
        ("nb-old", "b@x.com", SharePermission.EDITOR, False)
    ], "升級成 EDITOR 不需要新路徑——add_user 本身就是 upsert"


async def test_owner_peer_is_not_re_shared(fake_client):
    """failover 之後 pool 裡的 peer 可能就是那個 notebook 的 owner——SDK 只擋
    `permission==OWNER` 這個參數,不擋「對象就是 owner」,對 owner 呼叫
    add_user(EDITOR) 等於把他背後降權。OWNER 必須跟 EDITOR 一樣算「已足夠」。
    """
    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.existing = [("b@x.com", SharePermission.OWNER)]

    out = await basic.notebook_share_with_pool("nb-old")

    assert out["already_shared"] == ["b@x.com"]
    assert out["shared_with"] == []
    assert fake_client.sharing.calls == [], "不能對 owner 打 add_user(EDITOR)"


async def test_permission_check_is_case_insensitive(fake_client):
    """P2:email 比對大小寫不同不該恆為 False。

    pool label 的 email 是啟動時真 RPC(`get_account_email()`)拿到的,伺服器端
    的大小寫正規化不受我們控制;`shared_users` 回傳的 email 也一樣。兩邊只要有一
    邊大小寫跟另一邊不同,精確字串比對就永遠 False → `add_user` 明明成功、後檢卻
    判定「未生效」而 raise,留下孤兒分享(fail-closed,不是安全問題,但每次都會
    誤報)。
    """
    runtime.set_clients([("a@x.com", fake_client), ("B@X.com", fake_client)])
    # 模擬伺服器端回的既有共享者大小寫跟 pool label 不同(伺服器自己正規化過)。
    fake_client.sharing.existing = [("b@x.com", SharePermission.EDITOR)]

    out = await basic.notebook_share_with_pool("nb-old")

    assert out["already_shared"] == ["B@X.com"], "大小寫不同但其實是同一個帳號,要算已分享"
    assert out["shared_with"] == []
    assert fake_client.sharing.calls == [], "已經足夠,不該再打 add_user"


async def test_add_user_ineffective_share_raises(fake_client):
    """P1:`add_user` 沒 raise 不代表真的生效——workspace 網域政策擋外部分享、
    email 打錯字、或伺服器靜默忽略,RPC 都回 null 而不 raise(`allow_null=True`)。
    `add_user` 回傳的 ShareStatus 是零成本的後檢(那趟 RPC 本來就打了),沒接住
    就會把「根因在建立當下」的事故回報成功,症狀留給十幾分鐘後的 failover。
    """
    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.silently_ignore = {"b@x.com"}

    with pytest.raises(RuntimeError, match="b@x.com"):
        await basic.notebook_share_with_pool("nb-old")


async def test_duplicate_pool_slots_are_deduped(fake_client):
    """`_pool_peers` 的去重是 **defense-in-depth,不是生產可達路徑**:`app.py` 的
    `_reject_duplicate_accounts` 在 `runtime.set_clients()` 之前就擋掉重複帳號的
    pool,production 永遠到不了這裡帶著重複 label(那條路徑上 server 直接啟動
    失敗)。這支測試繞過 lifespan 直接呼叫 `runtime.set_clients()` 才能餵出這個
    狀態——鎖住的是「萬一有呼叫端繞過 lifespan guard 塞進重複帳號(branch config
    繼承、或 `stg` 的付費兜底槽位與 `prd` 共用帳號那種同型事故,AGENTS.md 記過),
    去重邏輯仍不會對同一個 email 打兩次 add_user、回報值也不會失真成
    `["b@x.com","b@x.com"]`」,不是在證明這個狀態會在真實 server 上出現。
    """
    runtime.set_clients([
        ("a@x.com", fake_client), ("b@x.com", fake_client), ("b@x.com", fake_client),
    ])

    out = await basic.notebook_create("重複槽位")

    assert out["shared_with"] == ["b@x.com"]
    assert fake_client.sharing.calls == [
        (out["notebook_id"], "b@x.com", SharePermission.EDITOR, False)
    ]


async def test_share_failure_message_reports_progress_so_far(fake_client):
    """部分成功的進度回報要說出「已經完成到哪裡」——docstring 自稱的行為從沒被
    驗證過(既有的唯一失敗測試讓*第一次*呼叫就失敗,shared 因此恆為 [])。
    這裡讓第二個 peer 失敗,第一個必須已經分享成功,訊息要看得到它。
    """
    runtime.set_clients([
        ("a@x.com", fake_client), ("b@x.com", fake_client), ("c@x.com", fake_client),
    ])
    fake_client.sharing.add_user_exc = RuntimeError("boom")
    fake_client.sharing.fail_on_email = "c@x.com"

    with pytest.raises(RuntimeError) as excinfo:
        await basic.notebook_share_with_pool("nb-old")

    message = str(excinfo.value)
    assert "b@x.com" in message, "第一個已經分享成功,訊息要說出來"
    assert "c@x.com" in message, "這個是失敗的那一個"
    assert fake_client.sharing.calls == [
        ("nb-old", "b@x.com", SharePermission.EDITOR, False)
    ], "第二個 add_user 沒真的打成,只有第一個記進 calls"


async def test_cancelled_during_share_still_carries_notebook_id(fake_client):
    """P1:`except Exception` 不含 `CancelledError`(它是 BaseException 的子類)。
    外層 client timeout 砍掉 8 趟 RPC 的分享迴圈時,呼叫端原本會拿到裸
    CancelledError——訊息裡沒有 notebook id,雲端留下部分共享的孤兒卻找不回來。
    修法不是吞掉 cancellation(繼續往外拋才對),而是讓 notebook id 帶得出來。
    """
    import asyncio

    runtime.set_clients([("a@x.com", fake_client), ("b@x.com", fake_client)])
    fake_client.sharing.add_user_exc = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await basic.notebook_create("砍線")

    message = str(excinfo.value)
    assert "已建立" in message
    assert fake_client.notebooks.created[-1] in message


def _denied() -> ClientError:
    """SDK 對「這個帳號看不到那個 notebook」回的形狀(gRPC PERMISSION_DENIED)。"""
    return ClientError("The server rejected this request (permission denied).", rpc_code=7)


async def test_share_runs_on_whichever_account_can_actually_see_the_notebook(fake_client):
    """作用中帳號看不到這個 notebook 時,要自己找到看得到的那個來分享。

    這是 v0.9.0 真實驗收 Phase 9-1 抓到的死路:`podcast_series` 因權限被拒停下,
    停點的 `error` 指引呼叫端跑 `notebook_share_with_pool` —— 而那個當下作用中帳號
    **正是看不到 notebook 的那一個**(配額 failover 換過去了),於是指引自己也
    permission denied,呼叫端只是從一個死路換到另一個。

    典型情境正是文件描述的那種:既有 notebook 的 owner 通常是 slot 1,而 pool 會
    rotate 走 —— 所以「照著指引做」必須真的解得開。
    """
    owner = fake_client                       # slot 1,notebook 的實際擁有者
    rotated_to = FakeClient()                 # slot 2,配額 failover 換過去的那個
    rotated_to.sharing.get_status_exc = _denied()
    runtime.set_clients([("owner@x", owner), ("rotated@x", rotated_to)])
    runtime.rotate_client()                   # 模擬「已經因為配額換到 slot 2」
    assert runtime.active_account() == "rotated@x", "前提:作用中的是看不到 notebook 那個"

    out = await basic.notebook_share_with_pool("nb-1")

    assert out["shared_by"] == "owner@x", "必須由看得見 notebook 的帳號執行"
    assert out["shared_with"] == ["rotated@x"], "要分享給的是另一個(執行者不分享給自己)"
    assert owner.sharing.calls, "分享必須由 owner 的 client 發出"
    assert not rotated_to.sharing.calls, "沒權限的那個不該被拿來打 add_user"
    # 游標不可以被借去做別的事 —— 它的語意是「配額輪替走到哪」。
    assert runtime.active_account() == "rotated@x", "掃描不得改動輪替游標"


async def test_share_reraises_non_permission_errors_instead_of_walking_the_pool(
    fake_client,
):
    """非權限問題不可以被「換一個帳號再試」吞掉。

    網路中斷、認證整個過期這類問題**每個槽位都會遇到**,一路吞下去只會把真正的根因
    埋掉,最後拋一個「沒有人看得到」的誤導訊息。
    """
    other = FakeClient()
    fake_client.sharing.get_status_exc = RuntimeError("connection reset")
    other.sharing.get_status_exc = _denied()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])

    with pytest.raises(RuntimeError, match="connection reset"):
        await basic.notebook_share_with_pool("nb-1")
    assert not other.sharing.calls, "不該繼續往下試"


async def test_share_reraises_a_client_error_whose_rpc_code_is_not_permission_denied(
    fake_client,
):
    """真實環境裡「非權限錯誤」的形狀是 **ClientError rpc_code=5**,不是別的型別。

    v0.9.1 驗收 Phase 2-2 實測:不存在的 notebook id 讓第一個槽位就回 rpc_code=5
    (not found),9 個帳號的 pool 只打了 1 趟 `get_status`。上面那個測試用
    `RuntimeError`,連 `is_permission_denied` 的 `isinstance(exc, ClientError)` 都不過,
    **走不到 `rpc_code` 的比較** —— 判準退化成「是不是 ClientError」時它仍然全綠,
    而那個退化正好把 not-found 當成權限問題,繼續掃 pool。

    `other` 刻意**看得到** notebook:掃下去的話這支工具會成功回傳,測試就是
    「DID NOT RAISE」——比讓兩邊都被拒更靈敏。
    """
    other = FakeClient()
    fake_client.sharing.get_status_exc = ClientError(
        "The server rejected this request (not found).", rpc_code=5
    )
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])

    with pytest.raises(ClientError, match="not found"):
        await basic.notebook_share_with_pool("nb-1")
    assert not other.sharing.calls, "not found 不是權限問題,不該換帳號再試"


async def test_share_fails_loud_when_nobody_in_the_pool_can_see_it(fake_client):
    """pool 全員都看不到 = 這個 notebook 不屬於這個 pool,沒有人分享得動。

    訊息要說出唯一的出路(用真正的擁有者帳號在網頁上分享),而不是只說「失敗」。
    """
    other = FakeClient()
    fake_client.sharing.get_status_exc = _denied()
    other.sharing.get_status_exc = _denied()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])

    with pytest.raises(NotebookAccessDenied, match="沒有任何帳號"):
        await basic.notebook_share_with_pool("nb-1")
