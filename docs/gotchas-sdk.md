# Gotchas — notebooklm-py 的 API 契約

直接呼叫 SDK(而不是經過我們的工具)之前讀。**一律以實裝版本為準**,別信 `_research/` 的 HEAD clone;0.7.3 → 0.8.0 的完整升級推導在 [notebooklm-py-0.8-upgrade.md](notebooklm-py-0.8-upgrade.md),0.8.0 → 0.8.1 在 [CHANGELOG](../CHANGELOG.md) v0.9.14。

- **(0.8.2)公開 facade 變成 ABC,實作在 `notebooklm._web.*`;要讀 body 就得往下沉。**
  0.8.2 拆出 web / android 兩個 backend:`ArtifactsAPI` / `SourcesAPI` / `NotebooksAPI` /
  `SharingAPI` 這些名字現在只是抽象契約(方法體 `raise NotImplementedError`),web 實作在
  `_web.artifacts` / `_web.sources` / `_web.notebooks` / `_web.sharing`,另有幾支搬家:
  `_rpc_executor` → `_web.transport.executor`、`_artifact.generation` → `_web.artifact.generation`、
  `_source.{content,listing}` → `_web.sources.{content,listing}`。**呼叫端不受影響**(簽名沒變、
  `client.<namespace>` 拿到的就是 web 實例);受影響的是任何用 `inspect.getsource` 驗行為的東西 ——
  對 ABC 抓原始碼**不會爆,只會靜默恆真**(`tests/test_contracts.py` 實際踩過)。
  🔴 **推論的方向也跟著變了:「SDK enum 有這個成員」不再等於「我們生得出來」。**
  enum(`ReportFormat` 等)是 backend-neutral,**dispatch config 是 backend-specific** ——
  `_web.params.artifacts._STATIC_REPORT_CONFIGS` 只有三種靜態講義格式,第四種
  (`CONCEPT_EXPLANATION`)**只在 `_android` 有**。v0.9.25 一度照舊推理「白名單漏了成員」
  把它開放出去,結果是個必定 `Unsupported report format` 的死選項,真實驗收才擋下。
  **要開放某個 enum 值之前,先確認 web 的 dispatch table 收不收它**
  (`tests/test_enums.py::test_report_format_whitelist_matches_what_the_web_backend_can_dispatch`
  就是釘這件事的;它反向也會告訴你「上游把新格式加進 web 了,現在可以開放」)。

  我們**沒有**啟用 android backend(它走 master token,不吃 cookie)。預設是 web,但
  `from_storage()` 沒傳 `backend=` 時會讀 `NOTEBOOKLM_BACKEND` —— **自己直接呼叫 SDK 時
  那個變數是活的**,`app._INLINE_AUTH_ENV_OVERRIDES` 只在走 `app._lifespan` 的 inline auth
  路徑把它刪掉。`test_default_backend_is_still_web` 釘的是「預設值 + 那道覆寫還在 + 變數名沒改」,
  釘不到你 shell 裡設了什麼。
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
  見[升級筆記](notebooklm-py-0.8-upgrade.md)的 §2。
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

## 行為差異與 server 端的緩解(從 AGENTS.md 降層,2026-08-30)

- **(0.8.1)`answer_document.render()` 只處理 block 級標記,而且會靜默丟掉解不出的 block。**
  v0.9.14 真實驗收兩條,兩條都直通公開 RSS `<description>`,所以 server 端各補了一道:
  ①inline 的 `**粗體**` 它**不管**(`###` 標題、`*` 條列它會拿掉)→ `_text.strip_inline_emphasis`
  在 `chat_ask`(`strip_citations=True` 那條路)與 `episode_set_description` 兩處清。**底線刻意不清**:`_斜體_` 與
  `NOTEBOOKLM_AUTH_JSON` / `source_id` 同形。
  ②上游解不出 spans 的 block(實測 `CODE_BLOCK`)**整段消失,連 U+FFFC 都不留**
  —— 上游文件說 `.text` 會填 U+FFFC,**實測空 spans 時兩邊都不填**,而剩下的句子讀起來
  完全通順(「以下是一段示範程式碼:」直接接下一段)→ `tools_basic._assert_no_dropped_blocks` fail-loud。
  判準是**有沒有解出文字**,不是 block 的 kind:`CODE_BLOCK` 帶 spans 時 `render()` 照樣輸出它。
- **(0.8.1)`Artifact.source_ids` 在生產資料上**有值**,而且是生成當下的歷史快照。**
  v0.9.14 驗收結案(在此之前只當觀測面、不准當 gate):11 顆 audio artifact 全部帶出非空
  `source_ids`,值對得上生成當時 notebook 內的來源;**但其中一顆的 id 全部已不在
  `source_list` 裡** —— 它記的是生成當下的狀態,不是即時 join。
  所以可以拿來看「這顆是用哪些來源生的」,**不可以拿來反查現存 source**(會查到已刪除的 id)。
- **`get_fulltext` 會在 CJK 字元間插空格**;關鍵字比對前先 `"".join(text.split())`(`_text.norm` 已封裝)。
- **`chat_ask` 回答夾帶引用標記**(`[1]`/`[3, 4]`/`[8-10]`)。`strip_citations=True` 時由 server 端用
  `_text._CITATION_RE` 清 —— 注意 `chat_ask` 這個參數**預設 False**(`tools_basic.py`),`episode_set_description`
  預設 True 且會再清一次。**新程式碼別再自己寫 regex**,要改清理規則改 `_CITATION_RE` 一處。
- **homepage probe 會 false-positive**(jacob-bd #250):長跑工具(`podcast_episode`/`podcast_series`)在**本地驗證之後**
  用 `auth_probe.probe_auth` 做輕量真 RPC 認證預檢;獨立工具版是 `auth_check`。
