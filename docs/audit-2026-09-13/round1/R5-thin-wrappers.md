# R5 審查者:薄包裝/SDK 面/研究工具(5 條)

## P1-1 _explain_no_research 判準在實裝 0.8.2 已失效,擋它的測試用假訊息
位置:tools_research.py:78(判準 `if "no_research" not in str(exc): return exc`)、:176(唯一呼叫點)、:248-256(import 連鎖)
上游事實:.venv notebooklm/_research.py `ResearchAPI.poll` 結尾 `if task_id: return ResearchTask.not_found(task_id)`;`ResearchStatus.NOT_FOUND.value == "not_found"`;`_wait_for_completion` 對 NOT_FOUND 非終態 → 1800s 後 ResearchTimeoutError(last status: not_found)
序列:start 由 a@x → wait 漏傳/傳錯 account → SDK 每 poll NOT_FOUND → 1800s timeout,訊息含 not_found → :78 原樣回傳無診斷 → host 重跑 research_start 再燒配額。research_import 亦然(:253 指引把 host 送進 30 分鐘洞)。任何 pinned miss(task 過期/打錯/錯 notebook)在 0.8.2 都走 NOT_FOUND
測試:tests/test_tools_research.py:350 test_research_wait_explains_no_research_on_timeout 自己手寫 `raise TimeoutError("... (last status: no_research)")` —— fake 與實裝不同形。test_contracts.py 沒釘 ResearchStatus 成員
嚴重度:P1(配額燒錯 + 30 分鐘白等)。信心高。

## P1-2 prior_mp3_path 與指名 source_ids 靜默互斥:EP{n-1} 上傳了改名了,但這一集沒讀它
位置:tools_podcast.py:2519-2523(守門)、:2598-2608(上傳+改名)、:2635(dispatch 只帶 selected_source_ids);根因 _sources.py:110-121(指名分支 `count = len(source_ids)`,pending_uploads 沒用到)
輸入:podcast_episode(..., prior_mp3_path="/…/ep02.mp3", source_ids=["src-ep03-doc","src-ep01-mp3"])
路徑:_validate_episode_args 只查 episode_n>=2(:1986)→ 守門指名分支丟 pending_uploads → :2598 真的上傳 ep02.mp3 並 rename EP02 → :2635 送出 source_ids 不含剛上傳那筆 → EP03 聽不到 EP02,ok=true
docstring(:2802-2821)完全沒提 prior_mp3_path
測試:無(test_source_selection.py:841 只跑 source_ids=None;grep prior_mp3_path tests/ 7 處無一同時傳 source_ids)
嚴重度:P1(靜默內容錯誤)。信心高。

## P2-1 _TEXTLESS_BLOCK_KINDS 只豁免 HORIZONTAL_RULE,THOUGHT/FUNCTION_CALL/FUNCTION_RESPONSE/IMAGE 會讓 chat_ask(strip_citations=True) 假性 fail-closed
位置:tools_basic.py:691-692(白名單)、:708-722(_dropped_blocks)、:725-737、:773
上游:_web/rows/documents.py:422-431 `_VARIANTS` 九種 kind;THOUGHT 上游自己說不解碼(必空);_types/documents.py:816 render() docstring 把 IMAGE 與 HORIZONTAL_RULE 並列
後果:回答夾帶 thinking/工具 block → raise「有 N 個 block 上游沒解出文字」→ show notes 路徑被關
測試:test_tools_basic.py 無 THOUGHT/FUNCTION_CALL/IMAGE 案例;test_contracts.py 沒釘 BlockKind
嚴重度:P2。信心中高(頻率無實測)。

## P3-1 gRPC 5 與 7 呼叫端兩種命運,上游兩者都當 account-routing
位置:_errors.py:73-83、tools_basic.py:897-903(notebook_get)、:256-265(_resolve_share_executor)、_sources.py:25-31
上游:_web/wire/decoder.py:203 `_ACCOUNT_ROUTED_STATUSES = (NOT_FOUND, PERMISSION_DENIED)`;_web/notebooks.py:754-770 只對 5 翻成 NotebookNotFoundError(非 ClientError 子類)
後果:若伺服器回 5:notebook_get 放行舊 authuser 訊息;_resolve_share_executor 看到未翻譯 ClientError(5) → is_permission_denied False → 當場 raise,pool 掃描中止
證據邊界:無實測證據 GET_SHARE_STATUS/GET_NOTEBOOK 在 pool 情境回 5
測試:test_contracts.py:454-461 只釘常數
嚴重度:P3。信心低-中。

## P3-2 source_ids=[] 在三個公開入口三種命運
位置:tools_basic.py:566(generate_audio 走 to_source_ids 拒空)vs :744(chat_ask)、:850(source_search)直通 SDK
上游:_web/chat.py ask `if source_ids is None: ...全部` ;[] 原樣送出,語意未定
測試:test_source_selection.py:37 只覆蓋 generate_audio
嚴重度:P3。信心中高。

## 逐條驗過不成立(9 條)
languages 白名單(81/81 差集空)、enum 死選項(AudioFormat 直通無表;_REPORT_FORMAT 正確排除 CONCEPT_EXPLANATION)、chat_ask strip_citations 漏清(render() 天生乾淨)、_share_each 洗 ACL(set_users 真 upsert)、source_fulltext char_count、_CITATION_RE 誤傷(9 條測試覆蓋)、source_add_file 路徑(p.name)、回傳大小(opt-in)、artifact_retry_failed 無守門(刻意)
