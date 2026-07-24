# NotebookLM Podcast 生成可靠性與內容 QA 研究

**日期**：2026-07-24
**範圍**：針對 Audicast EP34／EP26 回饋，查證 NotebookLM 平台、`notebooklm-py==0.7.3`、本 repo 長流程，以及 Podcast 內容品質；本文只做研究與方案，不修改 production code。

> **設計邊界更新**：本文的人工 QA／protected-facts gate 是可選的 Audicast workflow 建議，不是 MCP 核心契約。MCP 提供可靠、冪等、可恢復的生成工具與可選 QA primitives；是否人工核准、逐集停等或阻擋發布由 skill／host 決定。見 [ADR-0007](../adr/0007-mcp-provides-capabilities-workflows-live-in-the-host.md)。

## 證據分級

- **官方保證**：Google／NotebookLM、MCP、IETF、POSIX、GitHub 等規格或第一方文件明寫的行為。
- **SDK 行為**：本 repo lockfile 實裝的非官方 SDK `notebooklm-py==0.7.3` 之固定 tag、source、release、issue；這不是 Google 官方 API 契約。
- **我們 repo 觀察**：目前工作樹的程式碼可直接證實的行為。
- **案例觀察**：使用者提供的 EP34／EP26 執行紀錄；本次沒有 live account、原始 log、音檔或 manifest，故不能獨立重現。
- **建議**：根據上述證據推導的設計，不代表平台已提供同名能力。

## 結論先講

1. 原回饋抓到的四個主要工程缺口都成立：重生仍保留舊 `artifact_id`、finalize 不可重入、`podcast_series` 不及時記錄已受理 artifact、manifest 非 crash-safe。它們應合成一個修復，而不是各自打補丁：**單一 Manifest Store + attempt history + 正交狀態面向 + 結構化 side-effect checkpoints + reconciliation**。
2. EP34 的 `failed` 可以判定為遠端 artifact 終態，卻**不能**只靠目前資料斷言是「NotebookLM 後端故障」；Google 與 SDK 都列出配額、內容拒絕、伺服器移除／短暫列表遺漏等可能性。`removed` 更是 SDK 對持續 `not_found` 的本地合成判定，而非 Google 回傳的精確 root cause。
3. 外層斷線不等於遠端取消。MCP transport 規格明寫斷線不應被解讀成取消，因此 EP34「本機 `-15`、雲端繼續」符合協定與實際觀察；正確方向是**受理後立即回 artifact/task identity，之後 poll/finalize**，而非單純把 timeout 拉長。
4. 內容品質不能靠 prompt 解決。Google 官方明示 Audio Overview 可能有不準確與聲音瑕疵；custom prompt 只是偏好，不是逐字契約。發布前必須建立「protected facts ledger」與獨立 QA gate。
5. NotebookLM 自己的音檔 transcript 只能當一個 ASR hypothesis。Faster-Whisper 是同一 Whisper 模型的 CTranslate2 重實作，不算真正獨立的第二模型；它適合當 decoder cross-check，但高價值數字／版本／PR 時態仍要由另一模型家族或人工抽聽確認。
6. 容量預檢值得做，但不要硬編單一上限，也不要設計不存在的「archive」。Google 現行方案是 100／200／500 notebooks 等不同上限，且標明可變；官方沒有可依賴的 archive workflow，刪除 notebook 會連來源一起移除。

## 1. NotebookLM 平台：能做什麼，不能保證什麼

### 1.1 Audio Overview 的官方能力

Google 目前將 Audio Overview 描述為在背景生成的 Studio artifact；使用者可離開頁面或同時生成其他 artifact。生成面板提供四種格式（Deep Dive、Brief、Critique、Debate）、語言、長度選項，以及用 prompt 指定關注主題或專業程度；生成完成後也能查看當時的 custom prompt。[Google：Generate Audio Overview](https://support.google.com/notebooklm/answer/16212820?hl=en)

這些能力的工程含義是：

- **官方保證**：custom instructions、格式與語言是可用輸入。
- **不是官方保證**：模型逐字念對每個數字、版本、年份，或絕不改寫時態。相同頁面明示語音與內容都是 AI 生成，可能有 inaccuracies 或 audio glitches。[Google：Generate Audio Overview](https://support.google.com/notebooklm/answer/16212820?hl=en)
- **沒有 SLA**：官方只說可能花「幾分鐘」，沒有承諾 300、1,200 或 1,800 秒內必定完成。[Google：Generate Audio Overview](https://support.google.com/notebooklm/answer/16212820?hl=en) 因此 timeout 是我方「這次等多久」的政策，不是遠端失敗界線。

語言文件還有一個要實測、不可臆測的漂移點：現行 Help 頁說 Audio Overview 支援 80+ 語言，但把 Shorter／Default／Longer 標為 English Only；Google 2025-08 的官方產品公告卻說非英文 Audio Overview 已有 full-length 並可產生 shorter overview。[Google Help](https://support.google.com/notebooklm/answer/16212820?hl=en)、[Google 產品公告](https://blog.google/innovation-and-ai/models-and-research/google-labs/notebook-lm-audio-video-overviews-more-languages-longer-content/) 兩份第一方資料彼此不完全一致，所以 `zh_Hant + long` 應當是 **live contract test 的對象**，不是文件推導出的永久保證。

Interactive mode 可讓使用者用語音插話，但目前官方只保證英文，而且只適用新生成的 Audio Overview；它不是繁中成品的自動 QA 管線。[Google：Interactive mode](https://support.google.com/notebooklm/answer/16212820?hl=en)

### 1.2 音檔 source transcript 的定位

Google 官方只保證「匯入本機音檔時會轉錄，並把文字保存成新 source」，沒有承諾逐字正確；低音質甚至可能匯入失敗。[Google：Import a local audio file](https://support.google.com/notebooklm/answer/16215270?hl=en) 因此：

- `source_fulltext` 適合做 continuity、關鍵詞存在性與大範圍語意檢查。
- 它不適合做「音檔真的念出 1,505 而不是 155」的唯一裁判。
- EP34 的「音檔正確、NotebookLM transcript 把一千五百零五辨成五」是合理且重要的案例觀察，但需要保留原音檔與第二份 ASR／人工時間戳，才能成為可重跑 regression fixture。

### 1.3 Notebook 與生成配額

Google 目前列出的 consumer 方案上限為 Standard／Plus／Pro／Ultra：notebooks 分別為 100／200／500／500，Audio Overviews 每日為 3／6／20／100 或 200；來源數也隨方案不同，且頁面明示 limits subject to change。[Google：Upgrade NotebookLM](https://support.google.com/notebooklm/answer/16213268?hl=en-GB) Workspace 方案又有自己的 100／200／500 梯度。[Google：Workspace usage limits](https://support.google.com/notebooklm/answer/16337734?hl=en)

所以「EP28 清掉約 188 本後才恢復」與撞到 200 本級距**相容**，但光靠這個數字無法反推出帳號方案或精確錯誤。容量預檢應讀實際 notebook count，再套一個**可設定、可觀測的 plan limit**，不能在 code 內永久硬編 100 或 200。

目前官方文件沒有 notebook archive 契約；反而明示刪 notebook 會連 sources 一起移除。[Google：Deleting notebooks](https://support.google.com/gemininotebook/answer/17003757?hl=en) 因此「archive／cleanup workflow」要拆成：

1. read-only 的 `capacity_status` 與 `cleanup_candidates`；
2. 本地匯出／已發布／manifest 引用檢查；
3. 人工確認後才呼叫獨立 delete；
4. 不把「archive」包裝成平台具備的可還原功能。

## 2. `notebooklm-py==0.7.3`：實裝版本能證實什麼

本 repo `uv.lock` 鎖定 `notebooklm-py==0.7.3`；上游 v0.7.3 發布於 2026-06-30，是 0.7.x 維護版。[上游 release](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.7.3) 以下一律以固定 commit `a6c5441` 為準，不用 GitHub HEAD 推論已安裝行為。

### 2.1 Artifact identity、狀態與 timeout

`GenerationStatus` 明寫 `task_id` 與 `artifact_id` 是同一個 identifier，狀態集合包含 `pending`、`in_progress`、`completed`、`failed`、`not_found`、`removed`，另有 `error`／`error_code`。[v0.7.3 source](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_types/artifacts.py#L362-L450)

`wait_for_completion()` 的 SDK 預設 timeout 仍是 300 秒；本 repo 音檔 wrapper 改傳 1,200 秒、slides／report 改傳 1,800 秒，並不會改變任何外層 MCP client／subprocess／transport timeout。[SDK polling](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_artifact/polling.py#L157-L170)、[audio wrapper](../../notebooklm_mcp/tools_podcast.py#L269-L281)、[attachments](../../notebooklm_mcp/tools_artifacts.py#L67-L125)

`removed` 需要特別精準表述：SDK 在 artifact 持續缺席於 list、跨過次數與時間窗後，**自行合成** `GenerationStatus(status="removed")`；它的文件說這通常見於每日配額拒絕，但也可能是 transient list omission。[SDK type 說明](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_types/artifacts.py#L397-L450)、[polling heuristic](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_artifact/polling.py#L357-L417)、[上游 issue #1168](https://github.com/teng-lin/notebooklm-py/issues/1168) 因此：

- `failed`：遠端列表仍有 artifact，終態為失敗；可以判「這個 attempt 不可 finalize」，不能由此唯一判斷是平台故障、quota 或內容拒絕。
- `removed`：SDK 觀察到持續缺席；可以判「目前不可再靠此 id finalize」，但 root cause 仍應標 `unknown/likely_quota`，不能寫死 quota。
- `timeout`：本地等待預算耗盡；不是遠端終態，必須保留 artifact id 並進 reconciliation。

### 2.2 `artifact_list` 欄位不是想加就有

上游 `Artifact` list model 只有 id、title、type、status、created_at、url 等欄位，沒有 `error`、`error_code`、`updated_at` 或 `is_removed`。[v0.7.3 Artifact model](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_types/artifacts.py#L140-L209) `removed` 又代表 artifact 已不在 list，邏輯上也不可能由單次 list row 直接讀到。

所以原回饋的「補強 `artifact_list` 錯誤欄位」方向只**部分成立**。正確介面應分成：

- `artifact_list`：忠實列可取得的 remote rows，可加由現有 status 推導的 `is_pending/is_failed/is_completed`。
- `artifact_status(notebook_id, artifact_id)`：呼叫 poll/status surface，回 `error/error_code/is_removed/is_rate_limited`。
- `artifact_recovery_plan(...)`：結合 status 與 manifest phase，推導 `wait | finalize | start_new_attempt | reconcile | manual_review`；明標這是**我們的建議**，不是 SDK 欄位。
- 不捏造 upstream 沒給的 `last_updated_at`。

### 2.3 Auth／cookie

SDK 的 inline `NOTEBOOKLM_AUTH_JSON` 是「no file needed」路徑；`AuthTokens.from_storage()` 在有 inline JSON 且未指定 path 時不建立可寫 storage path。[v0.7.3 auth source](https://github.com/teng-lin/notebooklm-py/blob/a6c54417058bd5e43e0162dd93a390308d2f99f6/src/notebooklm/_auth/tokens.py#L129-L179) 上游文件也說底層 session cookies 仍會過期，持續 auth failure 需要更新 secret。[上游 configuration](https://github.com/teng-lin/notebooklm-py/blob/v0.7.3/docs/configuration.md#session-expiration)

上游 0.4.1 起曾加入 `RotateCookies` 與 file-backed persistence，但 `NOTEBOOKLM_AUTH_JSON` 沒有可持久化 backing store；第三方 keepalive 若在被導向登入頁後無條件覆寫 storage_state，還會毀掉原本好 cookie。[release notes](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.4.1)、[issue #312](https://github.com/teng-lin/notebooklm-py/issues/312) v0.7.3 另外修了互動登入因 SPA 不觸發 `load` 而卡五分鐘的問題。[v0.7.3 release](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.7.3)、[issue #1697](https://github.com/teng-lin/notebooklm-py/issues/1697)

這支持現行策略：inline auth 不做 process-local rotation、長跑前真 RPC probe、真正失效時回 GUI 主機重登再同步。仍需補兩點：

- `podcast_series` 若跑數小時，只在季首 probe 一次不夠；每次**提交新的昂貴生成前**再做輕量 probe，失敗就停在可 resume checkpoint。
- Auth 健康與 workflow 狀態分離；認證過期是 `blocked_auth`，不是把 artifact attempt 標 `failed`。

## 3. 原回饋逐項裁決

| 原回饋 | 裁決 | 依據與修正 |
|---|---|---|
| EP34 第一份長 pending 後 failed 是 NotebookLM 後端生成失敗，不是本機 | **部分證實，成因過度斷言** | 案例 status 可證實 remote terminal `failed`；沒有原始 `error/error_code` 與 server log，不能排除 quota、內容拒絕或其他遠端原因。只能說不是「單純本機 wait timeout」。 |
| 本機 `exit_code=-15`，雲端仍繼續 | **案例證實，且符合協定** | `-15` 通常是本地 SIGTERM；案例後續 `artifact_list → wait → resume` 是關鍵證據。MCP transport 規格明寫 disconnection 不應被當成取消，必須用明確 cancellation。[MCP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) |
| MCP/client timeout 小於遠端生成時間 | **已證實為架構風險** | SDK 300 秒預設、wrapper 1,200／1,800 秒、外層 client timeout 是不同層。只調大內層 timeout 無法解決回應遺失。 |
| 第二份音檔數字、版本、年份、PR 時態錯 | **案例觀察；平台限制已證實** | 本次無原音檔，無法獨立驗證四個錯誤；Google 官方明示生成可能不準確，故 QA gate 必要。 |
| NotebookLM 自己 transcript 把正確的「一千五百零五」辨成「五」 | **案例觀察；方法論正確** | 官方只保證上傳音檔會被轉錄，不保證轉錄精度；不得把 transcript 當 oracle。 |
| Notebook limit，清約 188 本後恢復 | **案例觀察；容量問題方向已證實** | 官方確有 100／200／500 等級距且可變。188 與 200 級距相容，不足以證明精確 plan 或 root cause。 |
| auth probe 與停用 inline cookie rotation 已改善 | **repo 已證實，SDK 理由成立** | 單集與整季入口都有 `probe_auth`；SDK inline JSON 無可寫 path。[podcast code](../../notebooklm_mcp/tools_podcast.py#L293-L309)、[series code](../../notebooklm_mcp/tools_podcast.py#L390-L400) |
| `_upsert_manifest_stub()` 重生仍保留舊 artifact | **已證實，P0** | 所有 stub 欄位都用 `setdefault`，既有 `artifact_id` 不會更新。[repo](../../notebooklm_mcp/tools_podcast.py#L75-L121) |
| `podcast_episode_resume` 不具真正冪等性 | **已證實，P0** | 每次都從 wait→rename→download→upload→source rename 重跑；docstring 也明說只能續一次，否則重複來源。[repo](../../notebooklm_mcp/tools_podcast.py#L145-L185)、[resume warning](../../notebooklm_mcp/tools_podcast.py#L312-L352) |
| `podcast_series` 生成受理後未立即寫 stub | **已證實，P0** | 呼叫 `_run_episode()` 沒傳 `manifest_path`，只在整集 finalize 成功後 `_write_manifest()`。[repo](../../notebooklm_mcp/tools_podcast.py#L402-L421) |
| slides／report 無 resume | **已證實，P1** | 兩者把 submit、wait、download、manifest update 包成單次呼叫；雖取得 artifact id，例外沒有通用 resume cursor。[repo](../../notebooklm_mcp/tools_artifacts.py#L67-L125) |
| manifest 非 crash-safe、無跨 process lock | **已證實，P0/P1** | podcast 與 attachments 都直接 `open(...,"w") + json.dump`；attachments docstring 也承認不是跨 process lock。[repo](../../notebooklm_mcp/tools_podcast.py#L56-L72)、[repo](../../notebooklm_mcp/tools_artifacts.py#L16-L45) |
| `artifact_list` 應加 error、removed、updated、recovery | **缺口成立，欄位方案部分不可行** | 現行只回 id/title/kind/completed/status/created_at。[repo](../../notebooklm_mcp/tools_basic.py#L152-L180) 但上游 list model 沒有 error/updated，removed 也不在 list；需另做 status/recovery tool。 |
| Notebook capacity preflight／archive／cleanup | **preflight 成立；archive 缺官方能力** | `notebook_list` 已足以計數；plan limit 要可設定。cleanup 必須 read-only 候選→人工確認 delete，不能宣稱可 archive/recover。 |
| targeted tests 71 passed | **歷史陳述，非本次證據** | 本次依任務不改 code，也未用 dirty worktree 的測試數冒充當時 EP34 證據；「71 passed」不能證明 live timeout、quota 或內容正確。 |

## 4. 建議的可靠性設計

### 4.1 先統一概念：episode、attempt、remote status、QA disposition

不要再讓單一 episode row 同時扮演「本集規格、目前遠端 artifact、執行游標、已發布資料」。建議 schema：

```yaml
episode:
  episode_id: 34
  title: ...
  active_attempt_id: "uuid-local"
  approved_attempt_id: null
attempts:
  - attempt_id: "uuid-local"
    episode_id: 34
    dispatch:
      status: prepared | dispatching | accepted | acceptance_unknown | failed_before_send
      artifact_ids_before: []
      dispatched_at: null
      accepted_at: null
    remote:
      artifact_id: null
      status: unknown | pending | in_progress | completed | failed | removed
      observed_at: null
      error: null
      error_code: null
      status_origin: remote | sdk_heuristic
    finalize:
      artifact_rename: {status: not_started}
      download: {status: not_started, path: null, size: null, sha256: null}
      feedback_source_upload: {status: not_started, source_id: null}
      feedback_source_rename: {status: not_started}
    qa:
      disposition: pending | passed | rejected
      subject: {artifact_id: null, audio_sha256: null, duration_ms: null}
      policy_version: null
      protected_facts_snapshot_hash: null
      evidence: []
      rejection_reasons: []
publication:
  published_attempt_id: null
  published_audio_sha256: null
  feed_revision: null
  guid: null
  enclosure_url: null
  published_at: null
  verified_at: null
```

關鍵是不把 `failed` 與 `rejected` 混為一談：前者是遠端生成狀態，後者是我們聽完後的內容裁決。新生成要 append attempt 並切換 `active_attempt_id`；舊 attempt 永遠留在 history。`publishable` 是由 approved pointer、remote status、finalize postconditions 與 QA disposition 共同推導的資格，不是持久化 phase。公開版本另由 Publication 明確指向 attempt 與音檔 hash。RFC 9110 對 idempotency 的核心定義是重複請求與一次請求有相同 intended effect，而且只有知道操作具冪等語意時才應自動 retry。[RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods) NotebookLM 的非官方 generate/upload RPC 沒有我方可依賴的 idempotency-key 契約，因此我們能保證的是「**已拿到 identity 後不重做 side effect**」與「結果不確定時先 reconcile」，不能宣稱 exactly-once。

### 4.2 Submit 與 finalize 拆開

推薦通用介面（名稱可調整）：

```text
artifact_submit(kind, notebook_id, episode_n, parameters, manifest_path)
  -> 立即記 submission intent
  -> 快照提交前 artifact ids
  -> 呼叫 generate
  -> 拿到 artifact_id 後立即 checkpoint accepted
  -> 回傳 resume cursor

artifact_reconcile(notebook_id, attempt_id)
  -> 若 artifact_id 已知，poll 它
  -> 若 submit 回應遺失，用「提交前 ids + started_at + kind」找新增 artifact
  -> 0 個：仍未知，不自動重生
  -> 1 個：adopt
  -> >1 個：manual_review，不猜 latest

artifact_finalize(attempt_id)
  -> 每一步先檢查 postcondition；已完成就跳過
  -> 僅執行下一個未完成的 side effect；各 side effect 分別保存 intent、identity、結果不明與驗證證據
```

MCP 官方 transport 明示斷線不等於取消，且可做 resumable stream；較新的 MCP tasks 也提供 task id、polling 與 terminal result retrieval，但實際使用前要確認 host/server 有協商該 capability，不能假設所有 client 都支援。[MCP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)、[MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) 在未能端到端採用 MCP Tasks 前，上述 application-level task/attempt 就是正確落點。

### 4.3 Finalize 必須是可重入、以 postcondition 驅動

每個 finalize side effect 的規則：

1. `artifact_renamed`：遠端 artifact title 已等於 label 就跳過。
2. `downloaded`：本地檔存在且 size/hash 等於 manifest 才跳過；temp download 完成後原子 replace。
3. `upload_intent`：**先 checkpoint 再呼叫外部 upload**。
4. `feedback_source_uploaded`：
   - manifest 有 `feedback_source_id` 且 `source_list` 找得到，就跳過；
   - upload 回應遺失時先 reconcile，不直接重傳；
   - 找到唯一可信候選才 adopt；多筆則 `manual_review`，不自動刪資料。
5. `feedback_source_renamed`：用 source id 檢查／補命名。
6. Finalize 不修改 QA 或 Publication；可發布資格由共用規則從正交事實推導。

這是 at-least-once side effect 世界中的常見做法：AWS Durable Execution 文件也明說中斷後 step 可能 replay，只有冪等操作、upsert 或接受 idempotency key 的 API 才可安全重跑，且 per-attempt 語意不等於整體 exactly-once。[AWS：Idempotency and retries](https://docs.aws.amazon.com/durable-execution/patterns/best-practices/idempotency/)

### 4.4 Manifest Store 的 crash-safe 寫入

所有 manifest 讀改寫必須收斂到唯一 `ManifestStore`，流程為：

1. 對固定 lock file 取得 exclusive `flock`；
2. 讀取、schema validate、套 mutation；
3. 在**同目錄**建立 temp file，寫 JSON；
4. `flush` + `fsync(temp_fd)`；
5. `os.replace(temp, manifest)`；
6. `fsync(parent_dir_fd)`；
7. release lock。

POSIX 要求 rename replacement 原子可見；Python `os.replace` 也說成功時是原子操作，但跨 filesystem 可能失敗，所以 temp 必須同目錄。[POSIX rename](https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html)、[Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace) `fsync(file)` 才要求把 file data/metadata 推向儲存裝置，而目錄 entry 還需要 directory fd 的 `fsync`。[Linux `fsync(2)`](https://man7.org/linux/man-pages/man2/fsync.2.html)

`flock` 是 advisory lock，其他 writer 可以選擇忽略。[Linux `flock(2)`](https://www.man7.org/linux/man-pages/man2/flock.2.html) 因此「加鎖」只有在 podcast、attachments、publish 與外部 maintenance script **全部走同一 store** 才成立；不能一邊鎖、一邊保留任意 `open(...,"w")`。

### 4.5 Reconciliation 是常態，不是事故處理

每次 resume／啟動整季前先做：

- 比對 manifest desired state 與 `artifact_list/source_list` actual state；
- adopt 可唯一識別的遠端成果；
- completed 但未下載者進 finalize；
- pending/in_progress 者只 wait；
- failed/removed 者關閉該 attempt，等待顯式 new attempt；
- 本地檔存在但 hash/size 不符者不覆蓋，標 `needs_review`；
- 遠端有多個同名來源者不自動刪，列出 source ids 讓使用者選。

這不是 Kubernetes-specific 實作，但其 controller pattern 是合適的一手模型：control loop 觀察 actual state，讓它逐步靠近 desired state，並把 current status 回寫給其他流程觀察。[Kubernetes Controllers](https://kubernetes.io/docs/concepts/architecture/controller/)

### 4.6 Series 與附件

- `podcast_series` 每次呼叫 `_run_episode` 都必須傳 manifest/attempt context，在 generate 受理後就 checkpoint；不能等整集 finalize。
- `podcast_series_resume` 不以人工 `start=N` 當唯一 cursor，而是掃每集 active attempt：衍生為 publishable 的單集跳過、accepted/pending 繼續 wait、completed 繼續 finalize、terminal failure 停下要求 new attempt。
- audio／slides／report 共用 submit/status/finalize 骨架，只在 download 與 manifest result field 上分流。
- 不再用一個多小時的 MCP call 代表整季完成；短 call 回 identity，長工作由 poll/resume 推進。

## 5. Podcast 內容 QA：數字、版本、年份與 PR 時態

### 5.1 生成前建立 protected facts ledger

每集 brief 除自然語言大綱外，新增 machine-readable facts：

```yaml
protected_facts:
  - id: fact-1505
    type: integer
    canonical: "1505"
    allowed_spoken_zh: ["一千五百零五"]
    forbidden: ["一百五十五", "五"]
    source_url: "..."
    checked_at: "2026-07-24T..."
  - id: version-1
    type: version
    canonical: "v1.3.1"
    allowed_spoken_zh: ["v 一點三點一", "版本一點三點一"]
  - id: pr-379
    type: delivery_status
    canonical: "PR #379 尚未 merge／尚未 release"
    checked_at: "..."
```

GitHub 的 PR 與 release 都是時間敏感資料；官方 REST API 分別提供 PR details，以及 release 的 `draft`、`prerelease`、`tag_name`、`published_at` 等欄位。[GitHub Pull Requests API](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)、[GitHub Releases API](https://docs.github.com/en/rest/releases/releases) 所以：

- 大綱定稿時抓一次 snapshot；
- 生成前再抓一次；
- 發布前再抓一次；
- narration 必須帶「截至 YYYY-MM-DD」；
- PR 未 merge 不得說「已可用」，merge 但未出現在 release 也不得說「已發布」。

custom prompt 可明確寫 spoken form 與禁語，但它只是第一道 soft control；Google 已明示 Audio Overview 可能不準確，不能因 prompt 寫得很硬就跳過成品 QA。[Google：Audio Overview limitations](https://support.google.com/notebooklm/answer/16212820?hl=en)

### 5.2 兩份 ASR 的正確用法

Whisper model card 明示弱監督資料可能讓輸出包含音檔裡根本沒說的文字，且不同語言、口音表現不均；官方建議在特定 domain 上做 robust evaluation。[Whisper model card](https://github.com/openai/whisper/blob/main/model-card.md#performance-and-limitations) Faster-Whisper 官方則明說自己是 OpenAI Whisper 的 CTranslate2 reimplementation，目標是 same accuracy。[Faster-Whisper README](https://github.com/SYSTRAN/faster-whisper#faster-whisper-transcription-with-ctranslate2)

因此 QA 分級應是：

- **NotebookLM transcript + Faster-Whisper**：兩個 pipeline／decoder 的 cross-check，但共享模型家族，不能稱為 independent oracle。
- **NotebookLM transcript + 非 Whisper 模型家族**：較好的 second-ASR。
- **任何 ASR + 人工聽原音檔**：protected fact 的最終裁決。

另一個 ASR 最好能回 word-level timestamps、alternatives 與 confidence，方便直接切 10–20 秒音檔讓人核對；Google Cloud STT 的一手文件提供 timestamps/confidence 與 phrase adaptation，但也警告 confidence 可能不準確、而且不應當必填欄位。[Google STT config](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v1/RecognitionConfig)、[confidence limitations](https://docs.cloud.google.com/speech-to-text/docs/basics#confidence-values) 所以 confidence 只能用來**提高抽查率**，不能讓高 confidence 自動免驗。

數字尤其要單獨對待。ASR 對 numeric expressions 還要判斷同一聲音應格式化成年份、時間、金額或數量，相關研究把它列為獨立問題。[Huber & Waibel, 2024](https://arxiv.org/abs/2408.00004) 全域 WER 也不夠：Google 的 accuracy 文件把 ground truth 定義為通常由人提供的 100% accurate transcript，並說 confidence 與 WER 不應期待相關。[Google：Measure and improve speech accuracy](https://docs.cloud.google.com/speech-to-text/docs/v1/speech-accuracy) 我們需要的是 protected-fact accuracy，不是整集平均 WER。

### 5.3 發布 gate

建議固定四道 gate：

1. **Artifact gate**：遠端 completed、音檔可解碼、duration 合理、容器／codec／MIME 已由現行 publish 正規化驗證。
2. **Protected-fact gate**：
   - 對每個 protected fact 搜兩份 ASR 的 normalized candidates；
   - 任一 missing、mismatch、ASR disagreement 都 fail；
   - 即使兩份都通過，數字、版本、日期、PR/release status 仍人工抽聽時間戳附近 10–20 秒。
3. **Semantic gate**：人工或 source-grounded reviewer 檢查「已 merge／已 release／計畫中」時態，以及內容是否超出來源。
4. **Publish gate**：PR/release snapshot 未過期、`qa_disposition=passed`、所有 rejection reason 已解決，才允許 `publish_series`。

若本集沒有 protected fact，也至少抽聽開頭／中段／結尾、speaker transition 與一段英文專有名詞。任何 QA 失敗要新增 rejected disposition／reason，保留原 attempt，不以刪檔抹掉證據。

## 6. 優先順序

### P0-A：先止住錯 artifact 與重複 side effect

1. 以 regression test 鎖住「同 episode 新 attempt 必須更新 active artifact、保留舊 attempt」。
2. 導入單一 Manifest Store；再移除所有直接 manifest write。
3. `_upsert_manifest_stub` 改成 append attempt + switch active，不再用全欄位 `setdefault`。
4. finalize 以每個 side effect 的結構化 checkpoint 做成可重入；upload response loss 先 reconcile。
5. `podcast_series` 受理後立即存 cursor，加入 series resume。
6. slides/report 採同一 artifact resume 骨架。

### P0-B：內容 QA 未過不得發布

1. protected facts schema；
2. 數字／版本／日期／PR fixtures；
3. second-ASR + timestamped human spot-check；
4. `qa_disposition != passed` 時 publish fail-closed。

這條與 P0-A 可平行設計，但兩者最後都落在同一 manifest schema。

### P1：觀測與營運

1. `artifact_status`／`artifact_recovery_plan`，不把拿不到的欄位硬塞進 list。
2. `capacity_status(configured_limit, reserve)`，預設在 limit 前保留 5 本 headroom；limit 來源與 plan 名稱要顯示。
3. read-only `cleanup_candidates`：只列「已發布、manifest 不再引用、無 pending artifact、早於 cutoff」候選。
4. delete 維持獨立、顯式確認，不自動清帳號。
5. 每次昂貴 submit 前 auth probe，auth failure checkpoint 為 `blocked_auth`。

### 保留、不需重寫

- `ensure_started` 已擋 failed／空 task id；`ensure_completed` 已 fail-close failed／removed。[repo](../../notebooklm_mcp/_status.py#L19-L50)
- `task_id == artifact_id` 的 targeting 已正確。
- 現有 `artifact_list` 是 reconciliation 的好基礎。
- `podcast_episode_resume` 已證明「遠端繼續、本地重接」的方向正確；要做的是 checkpoint 化，不是刪掉重寫整條 podcast 流程。
- `probe_auth` 與保留 SDK 原例外型別的處理應保留。

## 7. Acceptance criteria

| 範圍 | 可驗收條件 |
|---|---|
| New attempt | 同一 episode 連續建立 3 次 attempt，active artifact 永遠指最新一次；前兩次 artifact id/status/error 保留；description/cover/slides/report 不被重生清掉。 |
| Submit response loss | fault injection 在遠端受理後、本地拿 response 前中斷；resume 先以 pre-submit id snapshot + kind + time window reconcile，不直接再 generate；多候選必須 manual review。 |
| Checkpoint resume | 在 rename、download、upload call 前後、source rename、manifest update 每個邊界 kill process；每次重跑只做尚未完成步驟，最後得到一個 adopted `feedback_source_id`。 |
| Duplicate safety | 同 artifact 連續呼叫 resume 兩次，第二次不得新增 source；若第一次 upload outcome unknown 且遠端已有多筆候選，系統 fail-closed 並列 ids，不自動 delete。 |
| Series durability | EP N 一受理就能從 manifest 找到 artifact id/attempt；中斷後不用手填 `start=N` 即可續；依共用規則衍生為 publishable 的集數不重跑。 |
| Attachments | audio、slide deck、report 都能以 `(notebook_id, artifact_id, kind)` 在新 process 中完成 wait/download/manifest update。 |
| Manifest atomicity | 在 temp write、file fsync、replace 前後 kill；manifest 永遠是舊完整 JSON 或新完整 JSON，不能截斷。兩個 process 併發更新不同 episode 100 次，兩邊欄位都保留。 |
| Artifact diagnosis | failed 回原始 error/error_code（若 SDK 有）；removed 明標 `SDK_HEURISTIC`；timeout 明標 `LOCAL_WAIT_EXHAUSTED`；不得把三者顯示成同一「NotebookLM backend failed」。 |
| Capacity | 以 100、200、500 三種 configured limit 測試；達 `limit-reserve` 時 submit fail-fast 並列 count/limit/reserve/candidates；沒有人工確認不刪 notebook。 |
| Auth | 季中第 N 集前 auth probe 失敗時，不啟動新 generation；manifest 保留 `blocked_auth` cursor，重登同步後從同一集續。 |
| Protected facts | fixture 包含 1,505／155／5、v1.3.1／v1.7.15、2026／202、PR #379 未 merge／已 merge 未 release／已 release；每個錯誤都使 QA fail。 |
| ASR independence | 報告要顯示 `model_family`；Faster-Whisper 與 Whisper 不得計為兩個獨立家族。 |
| Human gate | 每個數字、版本、日期、PR/release claim 都保存抽聽時間戳與 reviewer/pass；缺一項 publish fail-closed。 |
| Live contract | 對測試帳號驗 `zh_Hant + long`、pending→completed、failed、removed/not-found、auth expiry；記錄 plan、日期、SDK tag，避免把一次 live 行為寫成永久平台保證。 |

## 最後判斷

最值得立刻做的不是再增加 timeout，而是把「呼叫」改成「可對帳的工作」：每個遠端 side effect 都先有 intent、受理後有 identity、每一步有 checkpoint、回應遺失先 reconcile。內容面也同理：不要問「transcript 看起來對不對」，而是先定義哪些 facts 絕不能錯，再讓不同 ASR 與人耳對那些 facts 給出可追蹤的 pass/fail。

這樣 EP34 類事件會從一次性的人工救火，變成明確狀態：

```text
本機斷線
  → active attempt 仍 accepted/pending
  → reconcile remote artifact
  → finalize 從 checkpoint 繼續
  → protected-fact QA
  → passed 才 publish
```

遠端 failed／removed 時則關閉該 attempt、保留證據、開新 attempt；不覆蓋歷史、不誤續死 artifact，也不因一次 timeout 多燒 quota。
