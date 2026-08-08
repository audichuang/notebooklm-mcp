#!/usr/bin/env python3
"""暫時替代 `notebooklm login` 的登入腳本 —— 上游修好就刪掉這支。

## 為什麼需要它(2026-08)

Google 把 NotebookLM 的**未認證登入流程**轉到 `notebook.google.com`(少了 `lm`),
但 notebooklm-py 的登入偵測寫死等 `notebooklm.google.com/**`:

    page.wait_for_url(f"{get_base_url()}/**", ...)   # playwright_login.py

於是使用者在瀏覽器登入完成、分頁停在新網域,SDK 永遠等不到,卡滿 5 分鐘後 timeout
(關掉瀏覽器則是 TargetClosedError)。**我們已經升到 0.8.0,它的 host 白名單仍然只有**
`notebooklm.google.com` / `notebooklm.cloud.google.com` —— 升級沒有解掉;`NOTEBOOKLM_BASE_URL`
也不能指到新網域(白名單會 raise)。

**已認證的 RPC 仍走舊網域且正常**,所以壞的只有登入這一段。

## 這支做了什麼

**只換掉那一行偵測**:改成「等網址落在 NotebookLM 的任一已知 host」。其餘每一步都
呼叫 SDK 自己的 helper(cookie domain 過濾、原子寫檔 0600、帳號 metadata 修補),
確保產出的 `storage_state.json` 與 `notebooklm login` 完全等價 —— 我們不自己發明格式,
只補上游還沒補的那個判斷。

## 廢棄條件

上游把新 host 納入白名單/偵測之後(追蹤 notebooklm-py 的 release note 與
`_research/notebooklm-mcp-cli` 的 CHANGELOG),刪掉這支、改回 `uv run notebooklm login`,
並拿掉 docs/test-account.md 的對應段落。

用法:
    uv run python scripts/login_notebooklm.py --profile test
    bash scripts/sync-auth.sh --profile test --config stg
"""
from __future__ import annotations

import argparse
import sys

# 這些是 notebooklm-py 的內部 API。刻意直接用:目的就是「跟 SDK 走同一條路,只改偵測」,
# 自己重寫 cookie 過濾/寫檔反而會與 SDK 產出的檔案不一致。上游若改名,這支就該退場了。
from notebooklm._env import get_base_url
from notebooklm.cli.services.playwright_login import (
    GOOGLE_ACCOUNTS_URL,
    ensure_chromium_installed,
    filter_storage_state_cookies_by_domain_policy,
    repair_playwright_account_metadata,
)
from notebooklm.io import atomic_write_json
from notebooklm.paths import get_browser_profile_dir, get_storage_path

# 上游只認舊 host;Google 現在會把登入流程帶到 notebook.google.com。兩個都收,
# 因為兩者都代表「這個分頁已經是登入後的 NotebookLM」。
_NBLM_HOSTS = ("notebooklm.google.com", "notebook.google.com", "notebooklm.cloud.google.com")


def _on_notebooklm(url: str) -> bool:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "") in _NBLM_HOSTS


class _Io:
    """SDK 的 helper 需要一個有 emit/fail 的 io 物件。"""

    def emit(self, message: str) -> None:
        # SDK 用 rich markup,這裡只要人看得懂,粗暴地拔掉標記即可
        import re

        print(re.sub(r"\[/?[a-z ]+\]", "", message))

    def fail(self, code: int) -> None:
        raise SystemExit(code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", default=None, help="notebooklm profile 名稱(預設 active)")
    parser.add_argument("--timeout", type=int, default=300, help="等待登入秒數,預設 300")
    args = parser.parse_args()

    storage_path = get_storage_path(args.profile)
    browser_profile = get_browser_profile_dir(args.profile)
    browser_profile.mkdir(parents=True, exist_ok=True)
    io = _Io()

    ensure_chromium_installed(io)

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    base = get_base_url()
    print(f"Profile: {args.profile or 'default'}")
    print(f"儲存到:  {storage_path}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(browser_profile),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",
            ],
            ignore_default_args=["--enable-automation"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # wait_until="commit":SPA 不會觸發 "load",用預設會白等(上游 #1697)
            page.goto(f"{base}/", wait_until="commit", timeout=30_000)

            if _on_notebooklm(page.url):
                print("已經是登入狀態。")
            else:
                print("\n請在瀏覽器完成 Google 登入(用你要綁的那個帳號)。")
                print("登入後不必按任何鍵,也不必管網址是 notebooklm 還是 notebook —— 兩個都算。")
                print(f"等待中(最多 {args.timeout} 秒)...")
                try:
                    # ★ 與上游唯一的差別:上游是 wait_for_url(f"{base}/**"),
                    #   只認舊 host,所以 Google 轉到 notebook.google.com 之後永遠等不到。
                    page.wait_for_url(
                        lambda url: _on_notebooklm(url),
                        wait_until="commit",
                        timeout=args.timeout * 1000,
                    )
                except PlaywrightTimeout:
                    print(f"❌ {args.timeout} 秒內沒偵測到登入。重跑一次,或改用 "
                          "`uv run notebooklm -p <profile> login --browser-cookies chrome`")
                    return 1
                except PlaywrightError as exc:
                    if "Target page, context or browser has been closed" in str(exc):
                        print("❌ 瀏覽器在登入過程中被關掉了。重跑一次,不要關視窗。")
                        return 1
                    raise
                print("✅ 偵測到登入。")

            # cookie forcing:區域使用者可能拿到 .google.co.uk 的 cookie,繞一圈把
            # .google.com 的 cookie 補齊(上游 #214 同樣做法)。commit 即可,不等 load。
            for url in (GOOGLE_ACCOUNTS_URL, f"{base}/"):
                try:
                    page.goto(url, wait_until="commit")
                except PlaywrightError:
                    pass  # 導向被中斷不影響 storage_state,下面會驗

            page_html = ""
            try:
                page_html = page.content()
            except PlaywrightError:
                pass

            # 與 SDK 逐字相同的收尾:過濾 cookie domain(避免把同瀏覽器登入的 mail/
            # youtube 等姊妹產品 cookie 一起寫進去)→ 原子寫檔(0600)。
            state = filter_storage_state_cookies_by_domain_policy(dict(context.storage_state()))
            atomic_write_json(storage_path, state)
            n_cookies = len(state.get("cookies", []))
            print(f"✅ 已寫入 {storage_path}({n_cookies} 個 cookie)")
        finally:
            context.close()

    # 補上 profile 的帳號 metadata(notebooklm profile list 的 Account 欄位靠它)
    try:
        repair_playwright_account_metadata(storage_path, io, page_html=page_html)
    except Exception as exc:  # metadata 失敗不該讓已經成功的登入變成失敗
        print(f"(帳號 metadata 補寫略過:{exc})")

    print("\n下一步:bash scripts/sync-auth.sh --profile "
          f"{args.profile or 'default'} --config stg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
