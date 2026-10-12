# v0.9.33-rc 真實驗收結果(master `c765b01`)

受測:v0.9.32 之後的重構(`_complete_attempt` 收尾、`audio_finalize.adopt_feedback_source`、
`attempt_state.OUTPUT_PROJECTION_KEYS`),**未發 tag、從 repo 起 server**;`stg` 測試帳號 pool。
headless `claude -p --model sonnet` 三輪(主體 94 turns / $1.33;Phase 9 兩次 $0.60)。
下方〈原始紀錄〉是驗收 agent 的 FINDINGS 逐字(帳號遮罩成 `pool-account-N`、公網 base 與 token 遮罩)。

## 判定(主 session 逐條複核)

**結論:無功能性回歸。** 每一條收尾路徑在真帳號上走通,停點指引照著做都解得開;
manifest 已由主 session 另行讀檔對帳(EP01 作廢那顆的 `retracted_output` 恰為原有 5 鍵、
`title`/`label` 保留、重生 attempt 帶指名 `source_ids`、兩集首發時間不變)。公網 feed 亦由主 session
重新讀回(2 item、pubDate = 首發時間、enclosure 200 且 length 一致)。

| # | 驗收 agent 的觀察 | 判定 |
|---|---|---|
| ① | 命名是 `EP01 <標題>`,不是文件寫的 `EP01 正文` | **非問題**:「正文」指標題本文,實作與文件一致 |
| ② | retract 的 `next_step` 指向 `podcast_series` 重生,而後面集已有回錄 | **安全,但多繞一步**:驗收當下沒照做;主 session 補了離線測試 `test_following_retract_guidance_to_series_stops_before_leaking_later_feedback` —— 照做時 series 在 dispatch 前停在 `later_episode_has_output`、轉交 `podcast_episode(source_ids=…)`,不洩漏(突變驗證:拿掉守門會紅)。要不要讓 retract 直接指 `podcast_episode` 是指引行為變更,未做 |
| ③ | reconcile 對已完成輸出回 `complete=false` + 指向 retract | 既有措辭(本版未動 reconcile);manifest 不變。未修 |
| ④ | **slides 被配額拒絕後,遠端留下一顆 `pending` 的 slide_deck**,manifest 與回傳都沒記 | 🔴 **未結案**。ADR-0010/0011 的前提是「配額拒絕 = 零副作用」;音檔家族實測過(1.35 s、`artifact_id=null`),**slides 家族這次觀察到反例**(created_at 早於 failover 1 秒)。本版未動 slides 路徑,屬既有行為。待查:那顆會不會自己完成/計費、failover 是否因此產生重複生成。結案方式:下一輪刻意對 slides 撞配額,事後輪詢那顆的終態 |
| ⑤ | stg 槽 8、9 `not_in_pool: CSRF token not found` | 營運事項(與 prd 共用的付費兜底帳號),未修 |
| ⑥ | 錯誤 artifact 的 adopt 只測到「非未決狀態」那道閘 | inconclusive;「artifact 不屬於這集」那道有離線測試 |
| ⑦ O-2 | 公網 mp3 的 bytes/sha ≠ manifest 記的本地下載 | **預期**:本地下載是 MP4-dash,發布時轉成 Canonical Enclosure(見 GLOSSARY);enclosure URL 的雜湊對的是發布出去的 bytes |
| ⑧ O-3 | episodic feed 的 item 在 XML 裡是舊→新 | **預期**:`feed.py` 那句「由新到舊」說的是 Apple 依 pubDate 的顯示排序,不是 XML 文件順序 |
| ⑨ | EP01 沒有 description 時 preflight 拒發 | 預期(上傳前拒) |
| ⑩ | serial 首發因標題無 `EPnn. ` 前綴被拒 | 預期(上傳前拒);是 brief 給錯參數 |
| ⑪ | **憑證目錄殘留:這輪 4 個 headless session,4 個都留下 `/tmp/notebooklm-mcp-auth-*`** | 觀察:文件記的殘留率是 10–15%(互動 session);`claude -p` 結束時似乎更常來不及展開。已手動清。未結案,結案方式:量 `claude -p` 收尾時 server 收到的信號與展開時間 |

`artifact_retry_failed`:沒有真正 failed 的 artifact 可用(④ 那顆是 `pending`),刻意不測。

---

## 原始紀錄

# FINDINGS — notebooklm-mcp v0.9.33-rc (master c765b01)

## Phase 0 — 本機檢查
`bash local-checks.sh` → **通過 18 / 失敗 0**。(含 37 個 MCP 工具、15 passed 的新增行為斷言、`_complete_attempt` 為 tools_podcast 唯一呼叫 finalize/promote 處、skill 快照與上游逐字一致。)

## Phase 1 — 協定與認證
`auth_check(all_slots=True)`:`ok=true`、`all_usable=false`、**槽位數 = 9**(符合預期,env 有進 server)。

| slot | account | usable | notebooks | 備註 |
|---|---|---|---|---|
| 1 (active) | pool-account-1 | true | 0 | refreshable |
| 2 | pool-account-2 | true | 0 | |
| 3 | pool-account-3 | true | 0 | |
| 4 | pool-account-4 | true | 4 | |
| 5 | pool-account-5 | true | 9 | |
| 6 | pool-account-6 | true | 0 | |
| 7 | pool-account-7 | true | 2 | |
| 8 | null | **false** | – | `not_in_pool: ValueError: CSRF token not found in HTML. Final URL: https://notebook.google.com/` |
| 9 | null | **false** | – | 同 slot 8 |

- slot 8/9 不健康 = 素材(CLAUDE.md §七),**不修**。兩槽皆為「啟動時建不出 client、被跳過」形狀(`account=null`,`not_in_pool`),符合工具文件描述。成因(帳號尚未開通 NotebookLM / cookie scope 導向 notebook.google.com)未判定 → inconclusive。
- 備註:stg 最後兩槽為付費兜底,恰好是不可用的這兩槽;可用池實為 7 槽。

`feed_info("zz-test-v0933rc")`:
```
token: <test-token>
feed_url: <PUBLIC_BASE>/feeds/<test-token>/feed.xml
show_page_url: <PUBLIC_BASE>/feeds/<test-token>/index.html
```
(不發布。)

憑證落檔:`/tmp/notebooklm-mcp-auth-1n597i7y` 目錄 `drwx------`(0700),`slot-1..9.json` 皆 `-rw-------`(0600)。✅ 權限正確。目錄建於 10:49:54,對應活的 server(pid 3710949)。

## Phase 2 — 建素材 + 讀取面回歸
- main notebook `e64331ed-38b5-4bca-8670-8b2cd13fa1d9`(`ZZ-TEST v0.9.33-rc main`),scratch `3787f45e-6437-4432-b5b3-a02da1bfab66`(`... scratch`)。兩本 `notebook_create` 皆回 `shared_with` = 其餘 6 個可用帳號(slot 8/9 不在池,未分享)。
- `notebook_get(main)`:`sources_count=0, is_owner=true, role="OWNER", created_at=2026-10-12T02:50:24+00:00`。`notebook_list` 找得到兩本。
- 來源(`source_list`,全 `ready=true, kind=markdown`):
  - A1 `cc971709-ac11-487b-9e5c-66bf42c555f1` 「A1-排隊與尾端延遲」(add_text → 回 `{source_id}`,無 char_count)
  - B2 `6401f8d8-b812-4aa8-8b11-dc36540ca42e` 「B2-簽章與選擇前綴攻擊」(add_text)
  - A2 `61fd0351-aebf-486a-9eb1-b6bc6252e557` 「A2-負載控制與百分位量測.md」(add_file,char_count 627)
  - B1 `9bead38a-837b-44eb-8008-1109be7ba730` 「B1-碰撞與生日問題.md」(add_file,char_count 698)
  - 觀察:add_file 的標題帶 `.md`、add_text 的不帶(預期行為,記錄)。
- `source_fulltext(A1, max_chars=200)`:`char_count=713, truncated=true`,內容為 CJK 逐段(有被插空行/空格)。
- `source_search("尾端延遲為什麼比平均重要", limit=3)`:`count=3`,rank 1 = A1(0-539)、rank 2 = A2、rank 3 = A1(539-681);text 為 markdown 化(`###`/`####`)。
- `chat_ask(同題)`:回 answer + `conversation_id=bcad9605-05e5-4ba0-855c-9c9e9117a2d4` + `references` 2 筆(A1、A2,含 `citation_number`/`cited_text`);answer 尾端有 💡 自我推銷句(工具文件已警告)。
- `notebook_share_with_pool(main)`:`shared_by=pool-account-1, shared_with=[], already_shared=[6 個帳號]`(冪等 ✅)。
- scratch:`source_add_url(Queueing_theory)` → `2f1c0aef-…`, `char_count=73017`。`research_start(fast, "M/M/1 queue tail latency")` → `task_id=5cd6c0ba-f1fa-43b3-aeab-c102ea608fd4, account=pool-account-1`;`research_wait` → `status=completed`,10 候選、`report=""`、`cited_url_count=0`。`research_import(urls=[1 個])` → imported 1(`7d92c062-…`)。`source_list(scratch)` 恰 2 筆(Wikipedia + 匯入的那一個)→ **未指名的 9 個候選沒進來 ✅**。

## Phase 3 ⭐ — 主流程整季兩集
**出口 A:`podcast_series` 回傳**(第一次,wait_timeout=1500):`complete=true`,`manifest=.../output/series_manifest.json`
- EP01:`artifact_id=6c00145a-435b-4ac5-85c3-10a3e7821771`、`attempt_id=e182c895-8771-4874-8066-2a1336fdd394`、`feedback_source_id=c0f35ab7-4259-463a-80e2-96a938a025ba`、`mp3_path=.../output/ep01.mp3`、`published_at="Mon, 12 Oct 2026 11:03:15 +0800"`、`label="EP01 排隊與尾端延遲"`
- EP02:`artifact_id=eb01b0a2-2d56-4936-8750-c7fd383d1816`、`attempt_id=9c9360bf-e9f3-4234-b180-51d246b71f73`、`feedback_source_id=18092c0b-695a-4c98-9aea-be3326ada7c4`、`mp3_path=.../output/ep02.mp3`、`published_at="Mon, 12 Oct 2026 11:14:46 +0800"`、`label="EP02 簽章與碰撞"`

**出口 B:manifest**(`schema_version=2`):兩集 `output_attempt_id`/`artifact_id`/`mp3_path`/`published_at`/`feedback_source_id` 皆在,且**與回傳逐值相同 ✅**(EP01 published_at `…11:03:15`、EP02 `…11:14:46`)。每集 1 個 attempt,`dispatch.status=accepted`、`remote.status=completed`;`finalize` 四個 checkpoint 全 `completed`:`artifact_rename`、`download`(EP01 42350010 B / EP02 43777434 B,有 sha256)、`feedback_source_upload`(`source_id` = 上列回錄 id,`candidate_source_ids=[]`)、`feedback_source_rename`(`source_name` = `EP01 排隊與尾端延遲` / `EP02 簽章與碰撞`)。`errors=[]`。mp3 實際落在 `output/ep01.mp3`、`ep02.mp3`。
- 命名:`artifact_list` Studio 標題 `EP01 排隊與尾端延遲` / `EP02 簽章與碰撞`,與 `source_list` 回錄標題(kind=media)**逐字相同 ✅**。⚠️ 形狀是 `EP{nn} <集標題>`,**不是** README 寫的 `EP01 正文`(工具 docstring 也寫 `EP{n:02d} 正文`)。→ 文件/實作不一致,記為**文件漂移**(非本輪改動造成?inconclusive,未對照 v0.9.32)。
- 不指名來源的副作用(預期內):EP02 artifact `source_ids` 含 EP01 回錄 `c0f35ab7-…`(5 筆);EP01 為 4 筆。
- 兩集各約 10~11 分鐘等待(EP01 02:53:29→03:03:15;EP02 03:03:18→03:14:46),account 皆 slot 1(audi880208),未觸發 failover/配額耗盡。

**第二次呼叫同一組 `podcast_series`**(output 複驗 `_complete_attempt`):回 `{"episodes": [], "complete": true}`。`artifact_list` 仍 2 筆(同 id、同 created_at)、`source_list` 仍 6 筆(4 原始 + 2 回錄)→ **零副作用 ✅**。manifest 每集 `published_at`/`output_attempt_id`/`feedback_source_id` 不變,每集仍 1 attempt;只有 `revision` 28→32(複驗寫 checkpoint,內容欄位未變)。

## Phase 4a — `continuity_unverified` → adopt
1. `source_delete(main, 18092c0b-…)`(EP02 原回錄)→ `{"deleted":"18092c0b-…","was_present":true}`。
2. `podcast_series`(同參數)→ 停點(**無候選欄位 ✅**):
```
{"complete":false,"stopped_at_episode":2,"attempt_id":"9c9360bf-e9f3-4234-b180-51d246b71f73","observed_state":"continuity_unverified","safe_next_action":"podcast_attempt_adopt","attempt_count":1,"superseded_attempt_count":0,"episodes":[]}
```
3. `source_add_file(main, output/ep02.mp3, audio/mpeg, title="EP02 簽章與碰撞")` → `{"source_id":"4d6fc653-5270-438a-a3e6-3e235adcd87f","char_count":14235}`。`podcast_attempt_adopt(manifest, 2, feedback_source_id=4d6fc653-…, attempt_id=9c9360bf-…)` → `complete=true, observed_state="continuity_verified", safe_next_action="source_delete", safe_next_attempt_id=9c9360bf-…, safe_next_artifact_id=null, stale_source_ids=["18092c0b-…"], source_cleanup_obligations=[{source_id:18092c0b-…, notebook_id:main}]`,`next_step` 明講逐一 source_delete。
4. 照做 `source_delete(18092c0b-…)` → `{"deleted":"18092c0b-…","was_present":false}`(**非錯誤 ✅**,預期形狀)。
5. 再跑 `podcast_series` → `{"episodes":[],"complete":true}` ✅。manifest EP02(revision 41):`feedback_source_id=4d6fc653-…`、`output_attempt_id=9c9360bf-…` 不變、`published_at="Mon, 12 Oct 2026 11:14:46 +0800"` 不變;attempt `finalize.feedback_source_upload`:`source_id=4d6fc653-…`、`previous_source_ids=["18092c0b-…"]`、`adopted_at=2026-10-12T03:16:35.649213+00:00`;`feedback_source_rename.status="completed"`、`source_name="EP02 簽章與碰撞"`、`adopted_at` 同刻;其餘 checkpoint 仍 completed、`errors=[]`。
→ **4a 通過**(工具回傳與 manifest 兩出口一致)。`b9544e8` 搬移的欄位轉換(新 id / previous_source_ids / rename completed)全部對。

## Phase 4b — `output_unverifiable`
1. `tools/delete-artifact.py <main> 6c00145a-…`(EP01 artifact)→ `{'deleted': '6c00145a-…', 'still_listed': False}`。
2. `podcast_series` → 停點:
```
{"complete":false,"stopped_at_episode":1,"attempt_id":"e182c895-8771-4874-8066-2a1336fdd394","observed_state":"output_unverifiable","safe_next_action":"podcast_attempt_retract","attempt_count":1,"superseded_attempt_count":0,
 "next_step":"它是這一集的正式輸出:要作廢就直接 podcast_attempt_retract(**不需要** abandon_in_flight),照回傳的 stale_source_ids 逐一 source_delete,再用 podcast_series 重生。 這一集卡住不影響其他集:用 start=2 可以先跳過它、讓後面的集數繼續推進。"}
```
符合預期(`safe_next_action` = retract、`next_step` 提 `start=2`)。
3. 逃生口 `podcast_series(..., start=2)` → `{"episodes":[],"complete":true}`;`artifact_list` 仍只有 EP02 一顆(未重生 ✅)。
→ **4b 通過**。

## Phase 5 ⭐ — 作廢 → 指名重生 → 中斷 → 續完
**retract**(`podcast_attempt_retract(manifest,1,e182c895-…,reason="ZZ-TEST 驗收")`)回傳:`observed_state="retracted"`、`authorization_basis="output_owner"`、`abandon_in_flight=false`、`source_cleanup_unresolved=false`、`safe_next_action="source_delete"`、`safe_next_attempt_id=null`、`safe_next_artifact_id=null`、`regeneration_source_ids=null`、`stale_source_ids=["c0f35ab7-…"]`、`source_cleanup_obligations=[{c0f35ab7-…, main}]`、`retracted_output={output_attempt_id, artifact_id, mp3_path, published_at, feedback_source_id}`。
**manifest EP01**(對帳,`OUTPUT_PROJECTION_KEYS = output_attempt_id, artifact_id, task_id, mp3_path, published_at, feedback_source_id, feedback_source_adopted_at`):
- 原本存在的 5 鍵(`output_attempt_id`/`artifact_id`/`mp3_path`/`published_at`/`feedback_source_id`)**全從 episode 消失**且 = attempt `retraction.retracted_output` 內的值 ✅;`active_attempt_id` 也一併清掉。
- 殘留鍵集:`episode, label, notebook_id, title, previous_feedback_source_ids, pending_source_cleanup, retracted_attempt_ids` → **`title`/`label` 仍在 ✅**。
- 照做 `source_delete(c0f35ab7-…)` → `{"deleted":…,"was_present":true}`。
- ⚠️ **觀察(next_step 與實況不一致)**:retract 的 `next_step` 結尾是「用 podcast_series 重生」,且 `regeneration_source_ids=null`。但 `podcast_series` 不能指名來源:此時 notebook 內有 EP02 回錄,不指名重生 EP01 會把 EP02 回錄吃進去(= 本輪要驗的洩漏)。本輪依 README 改用 `podcast_episode(source_ids=[A1,A2])`。→ 這是**文案/指引缺口**(不是本輪改動引入?inconclusive),建議 retract 在「後面集已有回錄」時改指 podcast_episode 並填 `regeneration_source_ids`。

**指名來源重生 + 故意逾時**:`podcast_episode(main, 1, title="排隊與尾端延遲", source_ids=[A1,A2], manifest_path, wait_timeout=60)` → 例外:
```
Task b75e8546-5cef-4a55-aa79-3762d1f58d03 in notebook e64331ed-… timed out after 60.0s (last status: in_progress; status history: in_progress)
音檔已在雲端生成(artifact_id='b75e8546-…')但後續步驟失敗。既有 attempt_id='08d79994-0a57-4ece-89db-1d57fa52e613'。續完(不會重新生成)的完整呼叫:podcast_episode_resume(notebook_id='e64331ed-…', episode_n=1, title='排隊與尾端延遲', artifact_id='b75e8546-…', output_dir='…/output', manifest_path='…/output/series_manifest.json')
**只有下面這句仍指向 resume 時才執行上面那個呼叫**:遠端有 artifact:先 podcast_episode_resume 續完 finalize。確定那次生成要作廢的話,artifact_list 查過雲端之後帶 abandon_in_flight=true 呼叫 podcast_attempt_retract。
```
✅ 帶 artifact_id、完整 resume 呼叫、capabilities 算的 next_step。
**原樣執行 resume**(wait_timeout=1500)→ 回傳:`artifact_id=b75e8546-…`、`attempt_id=08d79994-…`、`feedback_source_id=4aefc4d1-237f-4788-be11-37e3ef7d3e72`、`published_at="Mon, 12 Oct 2026 11:03:15 +0800"`、`mp3_path=…/output/attempts/08d79994-0a57-4ece-89db-1d57fa52e613/ep01.mp3`、`label="EP01 排隊與尾端延遲"`。
**對帳(兩出口)**:
- 回傳 `published_at` = manifest EP01 `published_at` = **Phase 3 首發 `Mon, 12 Oct 2026 11:03:15 +0800`** ✅(不是重生時刻)。
- mp3 落在 `output/attempts/08d79994-…/ep01.mp3`(35076532 B)✅;標題 `排隊與尾端延遲` 未變 ✅。
- manifest EP01(revision 62):`output_attempt_id=active_attempt_id=08d79994-…`、`artifact_id=b75e8546-…`、`feedback_source_id=4aefc4d1-…`;新 attempt `settings.source_ids=[A1,A2]`;finalize 四 checkpoint 全 completed(`feedback_source_rename.source_name="EP01 排隊與尾端延遲"`);`previous_feedback_source_ids=[c0f35ab7-…]`、`retracted_attempt_ids=[e182c895-…]`;`pending_source_cleanup` 已清。
- `artifact_list`:新 EP01 artifact `source_ids=[A1,A2]`(**只有指名的兩筆 ✅**),title `EP01 排隊與尾端延遲`。
**內容隔離(兩段式)**:`source_fulltext(4aefc4d1-…, max_chars=0, contains=…)`,`char_count=11463`:
- 自家詞 `排隊/延遲/尾端/負載/百分位` = true、`吞吐` = false → 命中 5/6 ≥ 2,探針有效 ✅。
- 對方詞 `簽章/碰撞/生日` = **全 false** ✅(EP02 回錄就在同 notebook,指名來源擋住了洩漏)。
**再跑 `podcast_series`** → `{"episodes":[],"complete":true}`;`artifact_list` 2 顆(EP01 新、EP02)、`source_list` 6 筆(4 原始 + EP01 新回錄 `4aefc4d1-…` + EP02 回錄 `4d6fc653-…`)→ **零新增 ✅**。
→ **Phase 5 通過**(含 `3d19196` 投影鍵清單:無殘留、無誤清身分)。

## Phase 6 — reconcile 與 adopt artifact 面
manifest sha256 前後:`e8479d7a…dc61`(reconcile 前)= `e8479d7a…dc61`(reconcile + 被拒 adopt 之後)→ **兩次呼叫完全不改 manifest ✅**。
- `podcast_episode_reconcile(manifest,2,9c9360bf-…,wait_timeout=120)` → `{"complete":false,"episode_n":2,"attempt_id":"9c9360bf-…","observed_state":"accepted","artifact_id":"eb01b0a2-…","safe_next_action":"podcast_attempt_retract","next_step":"它是這一集的正式輸出:要作廢就直接 podcast_attempt_retract(**不需要** abandon_in_flight),…"}`。
  - ⚠️ 觀察:對已完成的正式輸出,`complete=false` + `safe_next_action=podcast_attempt_retract`(README 預期「accepted / 無變更」— 無變更成立,但 `complete=false` 與下一步指向「作廢」讀起來像有事要做,易誤導;不影響正確性,記為措辭疑義,未對照 v0.9.32 → inconclusive 是否為回歸)。
- 錯誤 artifact adopt:`podcast_attempt_adopt(manifest,2,artifact_id=b75e8546-…(EP01 新那顆),attempt_id=9c9360bf-…)` → 拒絕,錯誤訊息:`artifact adoption requires an unresolved reconciliation state`;manifest 不變 ✅。(此拒絕發生在「狀態非未決」這道閘,更前面,**沒測到**「artifact 不屬於這集」的那道驗證;要測後者得先造出 reconciliation_ambiguous 狀態,人為造不出來 → 該分支 inconclusive/刻意不測。)

## Phase 7 — 附件與 episode 級 writer
- `generate_slides(main,manifest,2,source_ids=[B1,B2],slide_length="short")` → `{"episode":2,"slides_pdf_path":".../output/ep02-slides.pdf","artifact_id":"29f240a3-af3a-41b9-a3e2-5c1805bfb61c"}`。**真實撞到配額**:manifest EP02 `attachment_errors=[{"phase":"attachment_dispatch_failover","kind":"slides","type":"RateLimitError","message":"API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)","recorded_at":"2026-10-12T03:27:41.185570+00:00","from_account":"pool-account-1","to_account":"pool-account-2"}]`,`slides_account="pool-account-2"` → **配額 failover 在真帳號上走通(rotate 換帳號、原地重送、帳號與錯誤落 manifest)✅**。slot 1 在做完兩集音檔 + 重生一集(共 3 次音檔)後 slides 被拒 —— 走的是 §七 的「配額」路(RateLimitError → rotate),非 auth 失效路。
- `artifact_download_slides(同顆 29f240a3-…)` → 同 path/id(冪等)。PDF 11373746 B 落地。
- `artifact_revise_slide(slide_index=0, prompt=改標題)` → `{"artifact_id":"67e824be-07c9-493f-990b-90e24a25c978","slide_index":0,"superseded_artifact_id":"29f240a3-…","slides_pdf_path":同}`;manifest `slides_artifact_id=67e824be-…`(回寫為新那顆 ✅)。舊顆 `29f240a3-…` 留在遠端(工具文件明示),屬 notebook 一併刪。
- `generate_report(...,2,source_ids=[B1,B2])` → `{"report_md_path":".../ep02-report.md","report_format":"study_guide","artifact_id":"4395caeb-c120-4f37-97cd-ee9cbf2764f6"}`(10486 B);manifest `report_account=None`(未 failover,slot 1 直接生成)、`report_artifact_id=4395caeb-…`。`artifact_download_report(同顆)` → 同 path/id。
- `episode_set_description(2, "…[1]…**選擇前綴碰撞**…")`(預設 strip)→ `{"description":"本集談數位簽章為何押注在雜湊碰撞抵抗力,以及生日問題與選擇前綴碰撞的威脅。","stripped":true}`;manifest `description` 同值 ✅(`[1]` 與 `**` 已清)。
- `episode_set_publication_state(2,"deferred",reason="ZZ-TEST 驗收標記")` → `{"publication_state":"deferred","previous_state":null,"changed":true,"withheld_from_publish":true}`;再 `state=null` → `{"publication_state":null,"previous_state":"deferred","changed":true,"withheld_from_publish":false}`;manifest EP02 不再有 publication_state 三欄 ✅。

## Phase 8 — 低階 artifact 工具(scratch notebook)
- `generate_audio(scratch, audio_length="short")` → `{"task_id":"8c0e94b1-109e-424a-8fd6-fa4f2c07cb26","artifact_id":同值}`(無 failover 的低階入口,本次未撞配額)。`artifact_wait(task_id)` → `{"task_id":…,"artifact_id":…}`(完成)。
- `artifact_download_audio(…, output_path=output/scratch-audio.mp3)` → `{"path":…}`,檔 11146910 B、權限 `-rw-------`。
- `artifact_rename(…, "ZZ-TEST v0.9.33-rc scratch audio")` → `{"artifact_id":…,"title":"ZZ-TEST v0.9.33-rc scratch audio"}`;`artifact_list(scratch)` 確認新標題、`source_ids` 只有 scratch 的 2 筆 ✅。
- `artifact_retry_failed`:**刻意不測**:沒有 `completed=false` 且 failed 的 artifact 可用。⚠️ 但見下方**觀察 O-1**:main 上有一顆 `pending` 的孤兒 slide_deck。

### 觀察 O-1(Phase 7 的副產物)
Phase 7 `generate_slides` 被 slot 1 配額拒絕並 failover 後,`artifact_list(main)` 出現一顆**標題為 notebook 名**的孤兒:`76cb6516-b888-40c0-ba19-16e12c93f977`、`kind=slide_deck`、`completed=false`、`status="pending"`、`created_at=2026-10-12T03:27:40+00:00`(早於 failover 的 03:27:41、也早於實際那顆 29f240a3 的 03:27:42)、`source_ids=[B1,B2]`。判讀:被配額拒的那次送出在遠端留下 pending 空殼,failover 重送另生新顆;manifest(`attachment_errors`/`slides_artifact_id`)**不記這顆**,工具回傳也沒提 → 公開面看不到它,只有 `artifact_list` 對帳會發現。`pending` ≠ failed,所以不拿它測 `artifact_retry_failed`(README 規則:只測真的 failed)。是否為 v0.9.33-rc 引入:**inconclusive**(slides failover 路徑 6 個 commit 未動,多半是既有行為)。隨 notebook 一併刪除。

## Phase 9 — 發布(補跑;show_id=`zz-test-v0933rc`,token `<test-token>`)
base=`<PUBLIC_BASE>/feeds/<test-token>`

### 第一次嘗試(serial)— preflight 在任何上傳前拒發 = 正確行為,brief 參數錯誤
**步驟 1** `episode_set_publication_state(manifest, 1, "deferred", reason="ZZ-TEST:驗 deferred 不進 feed")` →
```json
{"episode":1,"publication_state":"deferred","previous_state":null,"reason":"ZZ-TEST:驗 deferred 不進 feed","changed":true,"withheld_from_publish":true}
```
**步驟 2** 首發 `publish_series`(show_id=zz-test-v0933rc, itunes_type=serial, require_slides/report=false)→ 工具錯誤,於上傳前拒發:
```
episode 2: serial 節目的標題必須以 'EP02. ' 開頭(現在是 '簽章與碰撞')—— itunes:episode 多數播放器不顯示,集號要進標題才是給人看的。title 生成時就鎖死、沒有 episode_set_title,缺前綴補不回去;整個節目不編號請把 itunes_type 設成 episodic
```
- 事實:manifest EP01 title=`排隊與尾端延遲`、EP02 title=`簽章與碰撞`(皆無 `EPnn. ` 前綴;`label` 才是 `EP01 …`)。本輪 Phase 1~8 的標題是在沒有 serial 前綴規則的前提下生成的。
- 觀察:這是 preflight 閘(未寫 NAS、未產生 enclosure/feed URL);步驟 2~5 因此**未執行**,公網無任何讀回。
- 發現:serial 發布要求標題帶 `EPnn. ` 前綴,而 `podcast_episode` 的產出標題/無 `episode_set_title` 使事後補不回去 —— 若 title 生成流程未自動加前綴,serial 路徑對本輪產物等於走不通(是否為文件/流程缺口 = inconclusive,未查 tool-reference)。
- 狀態:EP01 目前仍為 `deferred`(未解除);未對任何 show_id 上傳過。
- 使用者後續決定:改 episodic 重跑(見下方「第二次嘗試」)。


### 第二次嘗試(episodic)
**步驟 1**:EP01 已是 `deferred`(manifest 讀回 `publication_state_reason="ZZ-TEST:驗 deferred 不進 feed"`),未重做。

**步驟 2** 首發 `publish_series`(episodic、require_slides/report=false;**測試範圍取捨**:EP01 無簡報/講義)→
`episode_count=1`、`deferred_episodes=[1]`、`sha_unverified_episodes=[]`,EP02 `guid=318991ed7d779b3546b62a73df96e41d3a1f7725`、url `base/EP02-d2d8a6d1.mp3`、duration 00:22:40。公網讀回:
- feed 只有 1 個 `<item>`(EP02)✅;`itunes:type=episodic` ✅;channel artwork `base/artwork-c2098430.jpg` → 200、`image/jpeg`、522602 B(= 本地 show-cover.jpg 522602 B)✅;EP02 cover 200。
- EP02 enclosure:200、`Content-Type: audio/mpeg` ✅、`Content-Length: 43953501`(= feed `length`)。
- ⚠️ **與 brief 預期不符(觀察 O-2)**:`Content-Length` ≠ 本地 `ep02.mp3`(43777434 B),`sha256(公網)=d2d8a6d17fd282c7…5830d` ≠ manifest sha `088bb4d1…ac509`。**原因(已查證)**:本地 `ep02.mp3` 實為 **MP4(dash)容器**(`ftyp`,`format_name=mov,mp4`,encoder=Google),公網版是 **轉成真 MP3**(`ID3` 開頭、`format_name=mp3`、encoder=Lavf)。時長兩者相同(1360.20 s)。公網 sha 前 8 碼 = URL 檔名 `d2d8a6d1`,即 enclosure URL 是對**發布出去的位元組**取雜湊。結論:發布層做了轉碼,「公網 sha = manifest 記錄的本地 sha」這個對帳條件**本身不成立**,改以「URL 雜湊 = 公網內容雜湊」「時長一致」驗。這是否為預期設計:inconclusive(未查 tool-reference);但 `sha_unverified_episodes=[]`。

**步驟 3** `episode_set_publication_state(1, null)` → `{"publication_state":null,"previous_state":"deferred","changed":true,"withheld_from_publish":false}`。
- 再 `publish_series(manifest_path)` 第一次被 preflight 拒:`episode 1: description is required (真 show notes,不可空白)`(上傳前拒,正確行為)。EP01 在 Phase 1~8 從未設過 description。補救:`episode_set_description(1, "本集談排隊、利用率與尾端延遲,以及百分位量測與負載控制。(ZZ-TEST 驗收用)")`(只改 manifest,未碰 notebook),再 `publish_series(manifest_path)`(**只傳 manifest_path**,沿用 show 參數成功 ✅)→ `episode_count=2`、`deferred_episodes=[]`。
- feed 兩個 `<item>` ✅。**EP02 `<item>` 區塊與步驟 2 逐位元組相同**(`diff` 無輸出)、enclosure URL 與 GUID 同 ✅(冪等)。
- EP01:`pubDate=Mon, 12 Oct 2026 11:03:15 +0800` = manifest `published_at` ✅(非重生時刻);GUID `b575dee85b46705b56457a6746fddb98621ecaa7`;enclosure `base/EP01-3d8d1136.mp3` 200、`audio/mpeg`、35316548 B(= feed length)。EP01 同樣是 MP4→MP3 轉碼(本地 35076532 B、dash;公網 mp3、時長同 1089.85 s),`sha256(公網)=3d8d1136197e…16aa1`,前 8 碼 = URL 檔名 ✅。
- 🔴 **排序與預期相反(觀察 O-3)**:feed 文件順序是 **EP01(11:03)在前、EP02(11:14)在後**(pubDate 由舊到新);brief 與 tool docstring 都寫 episodic「由新到舊」。`lastBuildDate=Mon, 12 Oct 2026 11:14:46 +0800` = 最新一集的 pubDate(不是重生時刻,所以 feed 可重現)。是否影響播放器顯示:**inconclusive**(Apple 對 episodic 通常依 pubDate 自行排序,未實測播放器);但文件順序與文件宣稱不符是可觀測事實。

**步驟 4** 再 `publish_series(manifest_path)`:回傳的 feed_url / show_page_url / token / 兩集 guid / enclosure URL / cover_url / pdf_url / html_url **逐欄與步驟 3 相同**。公網 `diff feed3.xml feed4.xml` → **無差異(整份 feed 位元組相同)** ✅(連 lastBuildDate 都相同,無時間戳類欄位漂移)。兩集 enclosure 公網 sha 與步驟 3 相同(EP01 `3d8d1136…`、EP02 `d2d8a6d1…`)。

**步驟 5** show page `base/index.html`:`<h1>ZZ-TEST v0.9.33-rc`、列出 `EP01 — 排隊與尾端延遲`、`EP02 — 簽章與碰撞`(各 1 次)✅;grep `deferred|withheld|publication_state|ZZ-TEST:驗` = 0,無 deferred 殘留 ✅。(show page 也是 EP01 在前。)

**副作用/清理**:已永久推上 NAS 的檔案(uploader 不刪):EP01/EP02 mp3、兩張 cover、EP02 pdf+html、artwork、feed.xml、index.html,全在 `zz-test-v0933rc` 的 token 目錄下。manifest 現況:EP01 `publication_state` 已清、EP01 多了測試 description。未對其他 show_id 發布、未碰任何 notebook。

## 覆蓋補註
`source_add_text`(A1/B2 兩次,Phase 2)已使用,回傳形狀 `{"source_id": …}`(無 char_count,與 `source_add_file` 的 `{source_id, char_count}` 不同)。

## 收工
- 覆蓋自檢:37 支工具全部出現在本檔(`artifact_retry_failed` 與 `publish_series` 為「刻意不測/待核准」)。
- 兩本 notebook 已刪(`delete-notebook.py`):`e64331ed-…` main、`3787f45e-…` scratch;`notebook_list` → `[]`。
- 憑證目錄:僅 `/tmp/notebooklm-mcp-auth-1n597i7y`(0700,建於 10:49:54),對應本 session 仍存活的 server(pid 3710949,啟動 10:49:45)→ **不是孤兒,依 CLAUDE.md §三不自動刪**;本 session 結束後請再跑一次 §三 的安全版指令確認清空(本輪是否殘留 = 待 session 結束後驗,**inconclusive**)。
- 本地產物留在 `output/`(mp3、pdf、md、manifest)。

## 總結
- `_complete_attempt` 各收尾路徑(首發、output 複驗、continuity_unverified→adopt、output_unverifiable→retract→指名重生→逾時→resume)在真帳號上全部走通,且停點指引照做都解得開;`b9544e8`/`3d19196` 的欄位搬移對帳無誤。無功能性回歸。
- 待跟進(皆非阻擋):①命名實為 `EP01 <標題>`,README/docstring 寫 `EP01 正文`(文件漂移);②retract 的 next_step 在後面集已有回錄時仍指向 `podcast_series`(會洩漏),且 `regeneration_source_ids=null`;③reconcile 對已完成輸出回 `complete=false`+retract;④slides 配額 failover 在遠端留下 pending 孤兒 deck(O-1);⑤slot 8/9 `not_in_pool`(CSRF token not found)未判成因;⑥錯誤 artifact adopt 只測到「非未決狀態」那道閘。
- `publish_series`:Phase 9 已以 episodic 完成(見該節)。待跟進:⑦ 發布層把本地 MP4-dash「.mp3」轉成真 MP3,manifest 記的 sha/bytes 與公網不同(O-2,是否預期 inconclusive);⑧ episodic feed 文件順序為舊→新,與 docstring「由新到舊」不符(O-3);⑨ EP01 沒設 description 時 preflight 擋下(正確,但 `podcast_episode` 不自動產 description)。
