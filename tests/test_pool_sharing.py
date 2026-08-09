"""pool 模式下 `notebook_create` 自動把 notebook 分享給其餘帳號(v0.8.0 驗收 F-2)。

failover 換帳號後是拿新帳號對**同一個 notebook_id** 送出 —— 新帳號看不到那個 notebook
的話,整條 pool 是空談。而 MCP 自己建的 notebook 預設只屬於建立它的帳號,**沒有任何
機制建立那個前置狀態**:驗收時是人工用 SDK 補上才走得動,真實使用者不會知道要做這件事。
"""
import pytest

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


async def test_single_account_creates_without_sharing(fake_client):
    """單帳號(現行所有機器的樣子):完全照舊,不多打任何 RPC。"""
    runtime.set_client(fake_client)

    out = await basic.notebook_create("單帳號")

    assert out["shared_with"] == []
    assert fake_client.sharing.calls == []


async def test_unresolvable_account_label_fails_loud(fake_client):
    """label 退回 `#N`(啟動時拿不到 email)就分享不了,必須當場爆掉。

    靜默略過會讓那個帳號永遠沒有權限,而症狀要等到 failover 換過去才出現 ——
    中間隔著整段生成時間。
    """
    runtime.set_clients([("a@x.com", fake_client), ("#2", fake_client)])

    with pytest.raises(RuntimeError, match="#2"):
        await basic.notebook_create("壞 label")


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
