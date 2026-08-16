# Gotchas — notebooklm-py 的 API 契約

直接呼叫 SDK(而不是經過我們的工具)之前讀。**一律以實裝版本為準**,別信 `_research/` 的 HEAD clone;0.7.3 → 0.8.0 的完整升級推導在 [notebooklm-py-0.8-upgrade.md](notebooklm-py-0.8-upgrade.md),0.8.0 → 0.8.1 在 [CHANGELOG](../CHANGELOG.md) v0.9.14。

- **(0.8.1)`from_storage(path=…)` 的載入會在 PSIDTS 過期或 scope 不對時發 `RotateCookies`,
  而 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管不到它。** 那是 L2 inline recovery,不是 keepalive
  poke;`_resolve_recovery_path` 對明確 `path` 一律優先,所以 `NOTEBOOKLM_AUTH_JSON` 那道
  decline 也擋不到。自己直接呼叫 SDK 而不是經過 `app._lifespan` 時,**沒有那道 rotation flock
  護欄** —— 共用憑證(Doppler)在你的 process 裡被重鑄,其他機器下次啟動就掛。
  推導與擋法見 [gotchas-pool.md](gotchas-pool.md) §一。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

- `from_storage()` 是**同步函式**,回傳可直接 `async with` 的 context →
  `async with NotebookLMClient.from_storage()`(0.4.x「coroutine 必須 await」慣用法已走入
  歷史)。MCP **不傳 `keepalive=`**:Doppler `NOTEBOOKLM_AUTH_JSON` 是 3 VM 共用、唯讀
  真相來源;RotateCookies 會把新 cookie 留在單一 process 記憶體卻寫不回 Doppler,下一個
  stdio process 反而拿舊 cookie 啟動。`app.py` 在 inline auth 模式會暫時設
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`,連 `from_storage()` 冷啟動的 poke 一起關掉。
  Doppler `notebooklm/dev` 也**常駐設了 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`**:0.4.1 起
  fetch-token 路徑(`_auth/refresh.py` 的 `_fetch_tokens_with_jar`)無條件先打一次
  RotateCookies、無函式參數可關,少了這顆連 `doppler run -- notebooklm <cmd>` 的 CLI
  呼叫都會作廢一次共用 cookie。跨 session 老化照舊靠 GUI 機重登 + `sync-auth.sh`。
- `GenerationStatus` **無 `artifact_id`**;`task_id` 本身就是 artifact id(download/rename 用它)。
- **`status="removed"` ≠ `is_failed`(0.6.0 起)**:被伺服器下架的 artifact(NOT_FOUND
  輪詢耗盡,通常是每日配額)回 `status="removed"` 且 `is_failed=False`(0.4.x 是合成
  `"failed"`)。`ensure_completed` 一併擋 `is_removed` 才不會把配額下架當成功放行。
- `sources.add_file` 有 `title`(0.7.x),**但內部仍是 add→rename 兩步且改名失敗只 log 不
  raise** → podcast 流程維持顯式 add_file → rename 兩步(fail-loud);`source_add_file` 工具
  的 title= 有回傳後檢,未生效會 raise。
- **(0.8.0)`rename(return_object=False)` 不再是 fire-and-forget**(#1362):兩種模式都做
  存在性檢查,查不到就 raise。`artifacts.rename` 因此每次多一趟 `LIST_ARTIFACTS`,而且**多了
  一條原本不存在的失敗路徑**。我們仍一律傳 `False`;哪些呼叫點該防、哪些**刻意不防**,
  見[升級筆記](docs/notebooklm-py-0.8-upgrade.md)的 §2。
- 0.7.0 起 source add API 尾端參數(`wait`/`wait_timeout`/`title` 等)**keyword-only**,
  位置呼叫直接 TypeError(contract 測試有鎖)。0.8.0 起 `add_url` 也有 `title=`,**刻意不用**
  ——命名鐵律靠顯式 rename 的 fail-loud 後檢守著。
- `wait_for_completion` 的 `poll_interval` 已移除(0.7.x);呼叫只用 `timeout=`。
- **`Source.created_at` 的 tz 在 0.7.x/0.8.0 之間翻過一次面**(naive → aware UTC)。邏輯端
  統一走 `audio_finalize._created_at_utc()`,兩種形狀都正確,**升級不用改邏輯**。
  **陷阱在 fake**:`tests/conftest.py` 的 fake source 必須跟**實裝版本**同形,否則重演那次
  「測試綠、production 把每一筆 source 都濾掉、永遠卡 `acceptance_unknown`」的事故
  (曾用 `tzinfo is None` 排除候選)。兩條測試各守一半:正規化器本身、以及「fake 有沒有說謊」。
- **`generate_report` 的三種靜默吞噬**(`_artifact/payloads.py`):`custom` 沒給
  `custom_prompt` 會套通用預設句、靜態格式給了 `custom_prompt` 會被丟掉、`custom` 的
  `extra_instructions` 不串接。SDK 全都不 raise,要燒完一次配額拿到錯的講義才發現 →
  `_validate_report_prompt` 在打 RPC 前擋掉三種。
- **(0.8.0)`retry_failed` 與其他 generate 的錯誤契約現在一致了**:全部對同步拒絕 raise
  (0.7.x 只有 `retry_failed` 這樣)。「哪支會 raise」不再需要記;`ensure_started` 在所有
  呼叫點仍保留(擋空 task_id,且 0.7.x 形狀萬一回來也仍被正確處理)。
