"""storage_state 的**唯一一份**必要-cookie 判準。

兩個入口共用:`app._write_credential_file`(pool 落檔前的預驗證)與 `auth_cli`
(headless 建檔)。**各寫一份是這個 repo 記過最多次的帳**——上游哪天改了
`MINIMUM_REQUIRED_COOKIES` 或 `extract_cookies_from_storage` 的語義,只有一處被改到,
而 `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 只守著
`app` 那一份,`auth_cli` 那份會靜默漂走。

真實 0.8.1 的 sanitizer 會在解析階段把空字串 value 的 cookie row 整列丟棄,所以
缺 key 與空 value 對 `extract_cookies_from_storage` 而言是同一種 `ValueError`。因此
這裡包住上游例外並統一成中文訊息,再補上非空值檢查作 backstop,擋繞過 sanitizer 的
呼叫端與上游未來改回放行空值的情況。routability 只回答 PSIDTS 能不能 refresh,
不回答這份憑證能不能用;真正擋住 inline RotateCookies heal 的承重牆是
`app._lifespan` 持有 rotation flock。上游原文留在例外 cause,方便追查版本差異。

判準本身的推導(為什麼**值**也要非空)在 `app._write_credential_file` 的 docstring:
本地非空值檢查是 backstop,用來擋繞過 sanitizer 的呼叫端與上游未來的語義變更;
`would_trigger_inline_heal` 只沿用上游 heal 的 predicate 產生可見 warning。真正的
rotation flock 在 `app._lifespan` 持有,所以不會讓 L2 inline PSIDTS recovery 在
本 process 內發出 RotateCookies,把 3 VM 共用的 cookie 重鑄掉。
"""
from __future__ import annotations

from typing import Any

from notebooklm._auth.cookies import _sanitized_auth_entries
from notebooklm._auth.psidts_recovery import (
    _PSIDTS_COOKIE,
    _iter_routable_psidts_cookies,
    _psidts_routes_to_rotate,
    _storage_cookie,
)
from notebooklm.auth import MINIMUM_REQUIRED_COOKIES, extract_cookies_from_storage


def would_trigger_inline_heal(storage_state: Any) -> bool:
    """判斷上游的 routability predicate 是否會觸發 inline heal。

    這個 predicate 問的是 `__Secure-1PSIDTS` 能不能 refresh,不是這份憑證能不能
    使用;沿用上游的 cookie sanitizer 與 expiry/domain 判準,避免本地重寫一份。
    """
    entries = list(_sanitized_auth_entries(storage_state))
    return not _psidts_routes_to_rotate(entries, to_cookie=_storage_cookie)


def describe_inline_heal_reason(storage_state: Any) -> str:
    """`would_trigger_inline_heal` 為真時,**是哪一種**不可 refresh —— 只給訊息用。

    **這不是判準,是措辭。** 決策只有 `would_trigger_inline_heal` 一個出口
    (紅線見 `docs/gotchas-pool.md` §一③:routability 只發 warning、不是接受條件),
    這支永遠不參與任何 gate。所以它算錯的代價上限是「log 裡一個詞不準」,而不是
    誤拒一份還能服役的憑證 —— v0.9.14 已經為後者付過學費,別用另一個名字走回去。

    存在的理由是那則 warning 把兩個原因寫在同一個「或」裡
    (「已過期**或** scope 無法送到 accounts.google.com」),讀的人分不出命中哪一支。
    prd 槽位 1 命中的一直是後者(PSIDTS scope 在 `.youtube.com`,cookie 本身還有效),
    而它被讀成「登入要死了」,引出過一次不必要的重登建議。

    **分類直接複用上游同一組零件,不自己判 expiry/domain**:
    `_psidts_routes_to_rotate` 內部就是「先 `_iter_routable_psidts_cookies` 濾掉過期、
    再問 domain 路不路得到」,所以在這裡各叫一次就能分流,語義天然跟著上游走。
    實測四種情境的 yield/routes:

    | 情境 | iter yield | routes | 這裡回 |
    |---|---|---|---|
    | `.google.com` 未過期 | 1 | True | `""`(不會 heal) |
    | `.youtube.com` 未過期 | 1 | False | `wrong_scope` |
    | `.google.com` 已過期 | 0 | False | `expired` |
    | **同** domain:一筆過期 + 一筆未過期 | 0 | False | `expired`(上游的保守規則) |
    | **不同** domain:過期 `.google.com` + 未過期 `.youtube.com` | 1 | False | `wrong_scope` **+ `dropped=1`** |
    | 完全沒有 PSIDTS 列 | 0 | False | `missing` |

    全部**量出來的、不是推出來的**,而最後兩列以外的那兩列是重點:

    - 「**同** domain 一新一舊」上游採「一律不 routable」,所以 yield=0,歸 `expired`。
    - 「**不同** domain 一新一舊」**不一樣**:未過期那筆照樣進候選,於是落在 `wrong_scope`。
      ⚠️ **這一格是獨立 review 抓到的**:此時「有一筆已經過期」仍然成立,所以訊息**不可以**
      斷言「沒有過期」(第一版就是這樣寫的,在這一格是假的)。`_heal_facts` 的 `dropped`
      就是為了讓訊息講得出這件事而存在。
    """
    return _heal_facts(storage_state)["reason"]


def _heal_facts(storage_state: Any) -> dict[str, Any]:
    """分類 + 支撐它的計數,一次算完(訊息與分類必須看同一組數字,否則會各講各的)。

    `dropped` = PSIDTS 列數 − refresh 候選數,也就是**被上游的前置篩選丟掉的那幾筆**。
    刻意不自己判 expiry:`_iter_routable_psidts_cookies` 是上游用來濾的那一支,拿它的
    輸出去減就得到同一個答案,而且語義自動跟著上游走。**它丟掉的不一定只有過期**
    (格式不合的列也會被丟),所以措辭一律寫成「過期或格式不合」,不要講死。
    """
    entries = list(_sanitized_auth_entries(storage_state))
    rows = [e for e in entries if str(e.get("name", "")).strip() == _PSIDTS_COOKIE]
    candidates = list(_iter_routable_psidts_cookies(entries, to_cookie=_storage_cookie))
    facts = {
        "rows": len(rows),
        "candidates": [str(c.domain) for c in candidates],
        "dropped": len(rows) - len(candidates),
    }
    if _psidts_routes_to_rotate(entries, to_cookie=_storage_cookie):
        facts["reason"] = ""  # routable = 不會 heal,沒有原因要講
    elif not rows:
        facts["reason"] = "missing"
    elif candidates:
        # 有候選但路不到 = scope 問題。⚠️ **但這不代表「沒有任何一筆過期」** ——
        # 混合情境(過期的 `.google.com` + 未過期的 `.youtube.com`)會落在這裡,
        # 而 `dropped > 0` 就是那個證據。訊息要據此加註,不可以斷言「沒有過期」。
        facts["reason"] = "wrong_scope"
    else:
        facts["reason"] = "expired"
    return facts


def psidts_domains(storage_state: Any) -> list[str]:
    """storage_state 裡每一筆 `__Secure-1PSIDTS` 的 domain(去重、排序)。

    `wrong_scope` 時唯一可操作的資訊就是「它現在 scope 在哪」,而 domain **不是秘密**
    (值才是),放進 warning 與唯讀狀態回傳都安全。分開成一支是因為它與分類是兩件事:
    分類回答「哪一種」,這支回答「證據長什麼樣」。
    """
    entries = _sanitized_auth_entries(storage_state)
    return sorted(
        {
            str(entry.get("domain", "")).strip()
            for entry in entries
            if str(entry.get("name", "")).strip() == _PSIDTS_COOKIE
            and str(entry.get("domain", "")).strip()
        }
    )


def heal_warning_detail(storage_state: Any) -> str:
    """啟動時那則 routability warning 的正文(不含槽位名)。

    **這則訊息被讀錯過,所以措辭本身是修正對象**:原文把兩個原因寫在同一個「或」裡
    (「已過期或 scope 無法送到…」),而某個槽位命中的一直是後者。有人據此判定
    「登入要死了、憑證從某天起在過期」,並提議立刻重登 —— 對過 Doppler 與真 RPC 之後
    診斷不成立。所以這裡三件事一定要留著:①講**哪一種**、②講「這不等於不能用」、
    ③把「能不能用」指向 `auth_check`(真 RPC),不要讓人拿這則 warning 當健康度。

    ⚠️ **不准反過來過度宣稱。** 這一段第一版寫了「cookie 本身沒有過期,這個槽位仍在服役」,
    兩句都超出量到的範圍:混合情境(過期的 A + 未過期但 scope 錯的 B)會落在 `wrong_scope`
    而「沒有過期」是假的;而 routability **從來就證明不了槽位可不可用**。在一個專門修
    「訊息宣稱過頭」的改動裡寫出過頭的訊息 —— 所以現在只陳述量到的計數,能不能用一律
    指向 `auth_check`。
    """
    facts = _heal_facts(storage_state)
    reason = facts["reason"]
    if not reason:
        return ""
    if reason == "missing":
        detail = "沒有 __Secure-1PSIDTS 這筆 cookie。"
    elif reason == "wrong_scope":
        detail = (
            f"的 __Secure-1PSIDTS 進不了 refresh 路由:候選的 scope 在 "
            f"{'/'.join(facts['candidates']) or '?'},送不到 accounts.google.com。"
        )
    else:
        detail = (
            f"的 __Secure-1PSIDTS 有 {facts['rows']} 筆,"
            "沒有任何一筆能成為 refresh 候選(過期或格式不合)。"
        )
    if reason == "wrong_scope" and facts["dropped"]:
        # 這一句就是「沒有過期」那個假宣稱的替代品:講出被丟掉幾筆,不替它定性。
        detail += f"另有 {facts['dropped']} 筆連候選都進不了(過期或格式不合)。"
    return (
        f"{detail}"
        "0.8.1 會想觸發 inline RotateCookies heal,但 _lifespan 的 rotation flock 會擋住 POST"
        "(刻意的:3 VM 共用同一份 Doppler 唯讀憑證,不准任何一個 process 自行重鑄)。"
        "**這則訊息只說「refresh 做不到」,不說「現在不能用」** —— "
        "routability 證明不了可用性,要量請跑 auth_check(真 RPC)。"
        "真要換憑證時,正式環境是:"
        "`uv run notebooklm login` 之後 `bash scripts/sync-auth.sh --config prd`"
        "(**漏了 --config prd 會寫進 dev,正式 server 讀不到**)。"
    )


def assert_usable_storage_state(storage_state: Any) -> dict[str, str]:
    """驗一份已解析的 storage_state,回傳 cookie 對照表;不合用就 `ValueError`。

    兩個 caller 各自包裝自己的錯誤語意(CLI 的 `SystemExit` / startup 的
    `RuntimeError`),但**接受條件只有這裡這一份**。這裡只驗必要 cookie 存在且
    非空;上游 routability 問的是能不能 refresh,不是能不能用,所以不可 routable
    的憑證不在這裡拒收,由 `app._lifespan` 的 rotation flock 擋住 inline heal。
    """
    try:
        cookies = extract_cookies_from_storage(storage_state)
    except ValueError as exc:
        raise ValueError(f"必要 cookie 缺少或值是空的:上游驗證失敗:{exc}") from exc
    if blank := sorted(name for name in MINIMUM_REQUIRED_COOKIES if not cookies.get(name)):
        raise ValueError(f"必要 cookie 缺少或值是空的:{blank}")
    return cookies
