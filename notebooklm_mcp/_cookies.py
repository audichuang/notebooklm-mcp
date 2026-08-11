"""storage_state 的**唯一一份**必要-cookie 判準。

兩個入口共用:`app._write_credential_file`(pool 落檔前的預驗證)與 `auth_cli`
(headless 建檔)。**各寫一份是這個 repo 記過最多次的帳**——上游哪天改了
`MINIMUM_REQUIRED_COOKIES` 或 `extract_cookies_from_storage` 的語義,只有一處被改到,
而 `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 只守著
`app` 那一份,`auth_cli` 那份會靜默漂走。

判準本身的推導(為什麼**值**也要非空)在 `app._write_credential_file` 的 docstring:
`extract_cookies_from_storage` 只看 `name`,空字串 value 照樣算「這個 cookie 存在」,
strict loader 則把空值當不存在。兩邊分岔時,預驗證放行、SDK 開檔時 raise,正好掉進
L2 inline PSIDTS recovery 那條 `except` —— 而它會在本 process 內發一次 RotateCookies,
把 3 VM 共用的 cookie 重鑄掉。
"""
from __future__ import annotations

from typing import Any

from notebooklm.auth import MINIMUM_REQUIRED_COOKIES, extract_cookies_from_storage


def assert_usable_storage_state(storage_state: Any) -> dict[str, str]:
    """驗一份已解析的 storage_state,回傳 cookie 對照表;不合用就 `ValueError`。

    兩個 caller 各自包裝自己的錯誤語意(CLI 的 `SystemExit` / startup 的
    `RuntimeError`),但**接受條件只有這裡這一份**。
    """
    cookies = extract_cookies_from_storage(storage_state)
    if blank := sorted(name for name in MINIMUM_REQUIRED_COOKIES if not cookies.get(name)):
        raise ValueError(f"必要 cookie 缺少或值是空的:{blank}")
    return cookies
