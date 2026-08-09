# Gotchas — Research（NotebookLM 內建 Web / Deep Research）

動 `research_start` / `research_wait` / `research_import` 之前讀。呼叫端政策(scratch notebook、query recipe、cited-only 篩選)的正本在 skill `references/research.md`,這裡只放 API 事實。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

- **`research.start` 只送 `[query, source_type] + notebook_id`**(`_research.py` 的 `start`):
  筆記本裡已有的來源**對搜尋內容毫無影響**,notebook_id 只決定 task 掛在哪、import 進哪。
  種子只能寫進 query 字串——這條 API 事實推導出的 caller 政策(scratch notebook、query
  recipe)正本在 skill `references/research.md`,別在這裡重抄。
  `mode="deep"` 只支援 `source="web"`;`wait_for_completion` 對 timeout 丟
  `ResearchTimeoutError`(TimeoutError 子類),對 **FAILED 是回傳而非 raise** → 工具端自己擋。
  匯入一律走 `import_sources_with_verification`:`IMPORT_RESEARCH` 在 deep 負載下常超過 30 秒、
  client 先 timeout 但伺服器已 commit,它用 source list 對帳只補送缺的那幾筆。
- **`select_cited_sources` 不是 `ResearchAPI` 的方法**,是 `notebooklm/research.py` 的
  module-level 純函式(不打 RPC)。ResearchAPI 本體只有 5 個 RPC 方法。cited 判定因此是純本地
  計算,我們只用 `extract_report_urls` / `normalize_citation_url` 算出事實標記回傳,不把
  cited-only 做成 MCP 參數(那會把選擇政策塞進 capability layer,違反 ADR-0007)。
- **兩顆 URL normalizer 不可混用**(SDK docstring 自己寫明 distinct,contract 測試有鎖):
  `research.normalize_citation_url` 給「報告 markdown 裡的引用」比對——strip 尾端標點、
  **保留 fragment**;`_research._normalize_import_verification_url` 給 import identity——
  **丟掉 fragment**(伺服器存的時候剝掉)、不 strip 標點。`research_import` 選來源必須用
  **後者**,否則 `#a`/`#b` 兩個候選在我們眼中是兩筆、在 SDK 的 timeout readback 對帳中是
  同一筆,筆數就對不起來。**兩顆都不 strip 前後空白**,呼叫端傳進來的 URL 要自己先 strip。
  後者是私有 API,靠 `test_import_identity_differs_from_citation_identity` 當 tripwire。
- **`research_import` 必須自己驗 `status == completed`**:SDK 的 importer **完全不做**
  lifecycle 檢查,而 `failed` 的 task 仍可能留著已解析的 `sources` —— 少了這道 gate 就能
  繞過 `research_wait` 匯入半套或作廢的候選。identity 碰撞則**只檢查被選取的那些**:
  候選清單裡兩筆不相干的來源剛好 canonical 相同,不該讓一次合法 selection 整批失敗
  (v0.4.0 曾這樣過度 fail-closed)。
- **`import_sources_with_verification` 的 readback 只涵蓋 SDK 自己的 `RPCTimeoutError`**,
  **不涵蓋**外層 MCP client timeout / coroutine cancellation / server 被砍。外層結果不明時
  重呼 `research_import` 會重複匯入 —— 先 `source_list` 對帳。之所以只算 P2 而非 P1,是因為
  ADR-0008 把 research 綁在拋棄式 scratch notebook:對不清楚就丟掉整個 notebook,
  episode notebook 不受影響。
