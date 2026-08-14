"""storage_state 的**唯一一份**必要-cookie 判準。

兩個入口共用:`app._write_credential_file`(pool 落檔前的預驗證)與 `auth_cli`
(headless 建檔)。**各寫一份是這個 repo 記過最多次的帳**——上游哪天改了
`MINIMUM_REQUIRED_COOKIES` 或 `extract_cookies_from_storage` 的語義,只有一處被改到,
而 `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 只守著
`app` 那一份,`auth_cli` 那份會靜默漂走。

真實 0.8.1 的 sanitizer 會在解析階段把空字串 value 的 cookie row 整列丟棄,所以
缺 key 與空 value 對 `extract_cookies_from_storage` 而言是同一種 `ValueError`。因此
這裡包住上游例外並統一成中文訊息,再補上非空值檢查作 backstop,擋繞過 sanitizer 的
呼叫端與上游未來改回放行空值的情況。真正擋住過期或 scope 錯 PSIDTS 觸發 inline
RotateCookies heal 的承重牆是後面的 routability gate。上游原文留在例外 cause,方便
追查版本差異。

判準本身的推導(為什麼**值**也要非空)在 `app._write_credential_file` 的 docstring:
本地非空值檢查是 backstop,用來擋繞過 sanitizer 的呼叫端與上游未來的語義變更;
routability gate 則直接沿用上游 heal 的 predicate。兩道檢查都先於給 SDK 的明確
storage path,所以不會讓 L2 inline PSIDTS recovery 在本 process 內發出 RotateCookies,
把 3 VM 共用的 cookie 重鑄掉。
"""
from __future__ import annotations

from typing import Any

from notebooklm.auth import MINIMUM_REQUIRED_COOKIES, extract_cookies_from_storage
from notebooklm._auth.cookies import _sanitized_auth_entries
from notebooklm._auth.psidts_recovery import _psidts_routes_to_rotate, _storage_cookie


def assert_usable_storage_state(storage_state: Any) -> dict[str, str]:
    """驗一份已解析的 storage_state,回傳 cookie 對照表;不合用就 `ValueError`。

    兩個 caller 各自包裝自己的錯誤語意(CLI 的 `SystemExit` / startup 的
    `RuntimeError`),但**接受條件只有這裡這一份**。
    """
    try:
        cookies = extract_cookies_from_storage(storage_state)
    except ValueError as exc:
        raise ValueError(f"必要 cookie 缺少或值是空的:上游驗證失敗:{exc}") from exc
    if blank := sorted(name for name in MINIMUM_REQUIRED_COOKIES if not cookies.get(name)):
        raise ValueError(f"必要 cookie 缺少或值是空的:{blank}")
    entries = list(_sanitized_auth_entries(storage_state))
    if not _psidts_routes_to_rotate(entries, to_cookie=_storage_cookie):
        raise ValueError(
            "這份憑證會讓 SDK 在本 process 內重鑄 cookie（inline RotateCookies heal）；"
            "重鑄值寫不回 Doppler,還會作廢其他 VM 正在使用的那份。請更新 Doppler 憑證,"
            "並執行 scripts/sync-auth.sh。"
        )
    return cookies
