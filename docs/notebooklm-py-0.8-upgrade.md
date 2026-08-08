# 升級到 notebooklm-py 0.8.0(pin `>=0.8,<0.9`)

2026-08-08 從 `0.7.3` 升上來。這份是**細節正本**;AGENTS.md 只留一行指標。

0.8.0 的主軸是 ADR-0019 錯誤契約(umbrella #1346)落地:

> 回傳值只表達**成功**與**非同步生命週期狀態**;資源缺席、伺服器拒絕、schema 漂移一律 **raise**。
> `None` / `""` / `"not_found"` 字串 / `ValueError` 不再用來表示「出事了」。

`NOTEBOOKLM_FUTURE_ERRORS`(0.7.0 的預覽旗標)已移除,設了也沒作用。

---

## 一句話結論

**咬到我們的只有兩件事**,都在 `tools_podcast.py`;其餘 breaking change 全部擦邊而過。
全套離線測試在 0.8.0 下 **513 passed**。

---

## 咬到我們的(必須改 code)

### 1. 生成 kickoff 的同步拒絕改成 raise(#1342)

`generate_*` / `revise_slide` / `retry_failed` 碰到 `USER_DISPLAYABLE_ERROR`(配額、限流)
不再吞成 `GenerationStatus(task_id="", status="failed")`,改拋 `RateLimitError` / `RPCError`;
artifact id 缺席則拋 `ArtifactFeatureUnavailableError` / `DecodingError`。

**為什麼這對我們是大事**:podcast 的 attempt 終態分類原本綁在「回傳 failed status →
`ensure_started` 拋 `RuntimeError`」這條路上。新例外會掉進通用 handler:

| 情境 | 0.7.3 | 0.8.0(不改 code) |
|---|---|---|
| 配額爆掉 | `not_accepted`,可直接重試 | `acceptance_unknown`,逼跑一次撈不到東西的 reconcile |
| 整季 | 回結構化安全停點 | **原始例外直接拋給呼叫端** |

第二列比第一列嚴重得多:`podcast_series` 的契約是「預期內的停止用**回傳值**表達」,
而它靠 `except RuntimeError` 接住 —— `RateLimitError` 繼承 `NotebookLMError`,不是
`RuntimeError`,漏收就整個穿出去。

**修法**:

```python
# tools_podcast.py
_REFUSED_WITHOUT_DISPATCH = (RateLimitError, ArtifactFeatureUnavailableError)
```

**只收契約講死「沒有建出 task」的那兩種**。理由是誤判代價不對稱:

- 拒絕誤判成 unknown → 多跑一次對帳,便宜。
- **已受理**誤判成拒絕 → 呼叫端直接重生 → 重複 artifact + 重燒配額。

所以 `RPCError` / `DecodingError` / 網路錯誤 / `CancelledError` 一律留在 unknown。
**別為了訊息好看讓這個集合長大。**

**三個點要一起改**(踩過的坑就是只補了前兩個):

1. `podcast_episode` 的 kickoff except
2. `podcast_series` 內層的 kickoff except(`ensure_started` 那圈的兄弟分支)
3. `podcast_series` 呼叫 `_run_episode` 的**外層** `except RuntimeError` →
   改成 `except (RuntimeError, *_REFUSED_WITHOUT_DISPATCH)`

第 3 點是整季契約破掉的地方。真正的判準是它裡面那行「attempt 是不是 `not_accepted`」,
例外型別只是入場券。

**相容性**:`ensure_started` 與 `_mark_not_accepted` 的舊 status 分支全部保留,0.7.x 形狀
仍會被正確處理。`_mark_not_accepted` 因此吃兩種形狀 —— 例外走 `str(exc)` + 型別名,
status 物件走 `.error` / `.error_code`。不轉換的話 manifest 只會留一句 `"failed"`,
把「為什麼被拒」這個操作者唯一需要的資訊丟掉。

測試:`test_sdk_raised_rate_limit_is_not_accepted_not_acceptance_unknown`、
`test_series_raised_rate_limit_stops_as_not_accepted`,以及反向鎖
`test_generic_rpc_failure_stays_acceptance_unknown`(證明集合沒有誤收)。

### 2. `rename(return_object=False)` 不再短路(#1362)

0.7.x 的 `False` 是真 fire-and-forget:

```python
if not return_object and not future_errors_enabled():
    return None          # ← 不查、不 raise
```

0.8.0 把短路拿掉,兩種模式都做存在性檢查,查不到就 raise
`ArtifactNotFoundError` / `SourceNotFoundError`;`False` 只剩「成功時回 `None`」。

- `artifacts.rename`:**無條件**多一趟完整 `LIST_ARTIFACTS`。
- `sources.rename`:RPC 有回 echo 時仍短路,只有 null echo 才查(較便宜)。

**我們仍一律傳 `False`**(不用回傳物件;sources 那邊還省一次 fetch),但呼叫點心態要換:

- `audio_finalize.py` 兩處本來就 `try/except` + 回讀對帳 → 新的 raise 被正確吸收,不用改。
- `tools_podcast._finalize_episode` **刻意不加防護**:走到那裡代表 `ensure_completed` 剛過
  (而 `poll_status` 本來就是靠 artifact list 判定的),artifact 幾毫秒前還在清單裡;
  此刻查不到基本上只有「被伺服器下架(配額)」一種解釋,而後面的 `download_audio` 一樣會
  失敗 —— 提早爆掉反而誠實。失敗語意由呼叫端補完(附 artifact_id + resume 指引)。

測試:`test_contracts.py::test_rename_false_no_longer_short_circuits`。上游哪天又改回短路,
那些例外處理會變成死碼、`_finalize_episode` 的註解會變成謊話 —— 先讓它紅。

---

## 擦邊而過的(不用改 code,但要知道為什麼安全)

| 0.8.0 breaking change | 為什麼打不到我們 |
|---|---|
| **`.get()` 改成 raise**(最大的一條) | 我們**一次都沒用過** `sources.get()` / `artifacts.get()`,紀律上全走 `get_or_none` |
| typed 回傳拿掉 dict-subscript | `tools_research.py` 從頭到尾 `getattr(...)`,沒有一處 `result["key"]` |
| `research.wait_for_completion(interval=)` 移除 | 我們只傳 `timeout=` |
| `task_id=None` 多 task 時 raise `AmbiguousResearchTaskError` | 我們每次都顯式傳 `task_id` |
| `notebooks.share()` 移除 | 沒用過 |
| `sources.refresh()` / `chat.delete_conversation()` 改回 `None` | 沒用過 |
| `settings.get_account_tier()` / `AccountTier` 移除 | 沒用過 |
| `sources.check_freshness()` 等 lister drift 改 raise `DecodingError` | 沒用過 |

**`Source.created_at` 翻面**是唯一需要動測試的:0.7.x 是 host-local **naive**,
0.8.0 改回 **aware UTC**。`audio_finalize._created_at_utc()` 兩種都吃,所以邏輯不用改;
但 `tests/conftest.py` 的 fake source 必須跟**實裝版本**同形,否則重演那次
「測試綠、production 把每一筆 source 都濾掉、永遠卡 `acceptance_unknown`」的事故。
兩條測試各守一半:`test_created_at_utc_normalises_both_naive_and_aware` 守正規化器,
`test_contracts.py::test_source_created_at_is_timezone_aware` 守「fake 有沒有說謊」。

其餘實測仍在位:`sources.delete` 依然冪等、`poll_status` 依然回 `"not_found"`、
`is_removed` / `is_failed` / `is_rate_limited` 都在、`from_storage()` 仍是同步 context
manager、`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 仍生效、`_HTML_UPLOAD_SUFFIXES` 與兩顆 URL
normalizer 都還在、`chat.ask` / `notebooks.*` / `download_*` / `wait_for_completion` 簽名未動。

新增(純加法,contract 測試已跟上):`sources.add_url(title=…)`。
**目前刻意不用** —— podcast 的命名鐵律靠顯式 rename 的 fail-loud 後檢守著,
而 `add_file` 的 `title=` 內部就是 add→rename 兩步、失敗只 log。

---

## 升級**沒有**解掉的事

**登入還是壞的。** 0.8.0 的 `_ALLOWED_BASE_HOSTS` 仍然只有
`notebooklm.google.com` / `notebooklm.cloud.google.com`,沒有 Google 現在用的
`notebook.google.com`。`scripts/login_notebooklm.py` 仍是必要的,而
`test_login_script_should_be_retired_once_upstream_knows_the_new_host`
這條退場 tripwire 在 0.8.0 下**維持綠燈** —— 它正確地說「還不能退」。

---

## 新能力:評估過、**這輪刻意不採用**

- **Master-token headless auth**(`headless` extra,`notebooklm login --master-token`)。
  存一顆長效 Google master token 就能無瀏覽器重鑄 cookie,理論上終結
  「GUI 機重登 → `sync-auth.sh` → 3 VM」的儀式。
  **不採用的理由**:(a) full-account 長效憑證,上游自己建議用專用/拋棄式帳號;
  (b) 它重鑄出來的 cookie 寫在**本機**、不會回到 Doppler,等於在唯讀真相來源旁邊開第二個
  真相來源 —— 跟 keepalive 當初搞壞我們的失敗模式同一類。真要用,**先只在測試帳號上試**。
- `auth.fetch_tokens_passive()` / `auth check --test --passive`:唯讀認證探測,不觸發
  keepalive poke、不寫檔。可以取代 `probe_auth` 現在打的真 `notebooks.list()`,更便宜也
  **可證明**不會動到共用 cookie。值得做,但屬於獨立改動。
- `artifacts.get_prompt()`:讀回 artifact 當初的生成 prompt,對帳/稽核 attempt 有用。
- `rpc_decode_errors` 計數器:上游把 schema drift 從一般錯誤裡分離出來 —— 對「Google 改版」
  這個頭號風險是新的觀測點。

## 新風險

- **`notebooklm-py` 0.8.0 自己也有一支叫 `notebooklm-mcp` 的 console script**
  (它自己的 MCP server,34 tools,在 `mcp` extra 裡)—— **跟我們的同名**。
  消費端不受影響:`uv tool install` 只 link 被指名套件的 entry point(實測 `uv tool list`
  只有我們的 `notebooklm-mcp` / `notebooklm-cover`)。**但 repo 的 dev venv 兩支都在**,
  `uv run notebooklm-mcp` 會變成看安裝順序 → repo 內開發一律用
  `uv run python -m notebooklm_mcp.server`。
  **永遠不要裝 `notebooklm-py[mcp]`**:除了撞名,它還硬 pin `fastmcp==3.4.2`。
- **L3 headless re-auth**(0.8.0 新增):`NOTEBOOKLM_HEADLESS_REAUTH=1` 或
  `refresh_auth(allow_headless=True)` 會用持久瀏覽器 profile 無頭重鑄 cookie。
  預設關,但只要環境裡有人設了 `=1` 就會在 RPC 中途自動觸發 —— 跟 keepalive 同一類災難。
  `app.py` 的 lifespan 在 inline auth 模式**顯式把這個 env 刪掉**(退出時原樣還原),
  由 `test_lifespan_with_inline_auth_suppresses_headless_reauth` 鎖住。
- 0.8.0 是 2026-08-03 發的大版(changelog 該節逾千行),而我們的測試全是 mock,
  **證明不了 live RPC 行為** —— 真實驗收是唯一的下一道關。

---

## 驗證方式(下次升級照抄)

別只讀 changelog,拿實裝版本跑:

```bash
# 1. 抓 sdist 讀 CHANGELOG / docs/upgrading-to-X.Y.Z.md（別信 _research/ 的 HEAD clone）
python3 -m pip download --no-deps --no-binary :all: notebooklm-py==X.Y.Z -d /tmp/nb

# 2. 開獨立 venv 實裝新版 + 我們的套件（--no-deps 避開舊 pin），跑全套
uv venv --python 3.12 /tmp/vX && uv pip install --python /tmp/vX/bin/python \
  "notebooklm-py==X.Y.Z" "mcp[cli]>=1.27,<2" Pillow markdown mutagen pytest pytest-asyncio
uv pip install --python /tmp/vX/bin/python --no-deps -e .
/tmp/vX/bin/python -m pytest -q

# 3. 逐條把新舊版的**原始碼**擺在一起看（docstring 的 versionchanged 最準）
#    行為變更不會出現在簽名裡 —— rename 的短路就是這樣溜過去的
```

新測試寫完要**先證明它會紅**:把修復暫時關掉(例如 `_REFUSED_WITHOUT_DISPATCH = ()`
就等於完全回到修改前),跑一次確認紅,再還原。
