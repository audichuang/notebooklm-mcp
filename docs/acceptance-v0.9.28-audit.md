# notebooklm-mcp 多 agent 缺陷稽核 —— 裁決報告(2026-09-13)

基線:master `751e04b`(程式碼與 tag `v0.9.27` 逐檔相同)、全套 `13093 passed / 597 skipped`。
方法:第一輪 9 支獨立審查(Claude 8 支按檔案分區 + Codex 1 支看 attempt 狀態機,互不餵結論、不讀 CHANGELOG)
→ 59 條候選(58 條不重複)→ 第二輪 9 支反駁式驗證(每條先攻擊、攻不下才最小重現;測試品質那批用 worktree 突變驗證)
→ 本檔為主迴圈裁決。全程唯讀,repo 未動、未 commit、未碰真帳號。
各輪原始回報在 `docs/audit-2026-09-13/round1/`、`round2/`;驗證者的重現腳本已改寫成 tests/ 裡的案例,scratch 版不保留。

## 一眼看完

| 裁決 | 數量 |
|---|---|
| 成立 P1(程式碼) | 5 |
| 成立 P1(測試沒守住不變式) | 3 |
| 成立 P2 | 11 |
| 成立 P3(含純文件與 nit;§五的條目數) | 32 |
| 推翻 / 已載明 / 降到不值得列(§六的列數) | 17 |

條目數(51 + 17 = 68)大於候選數(59),因為同一條候選常被驗證拆成「成立的一半 + 推翻的一半」
(例:429 分類、標題閘、隱藏目錄、`.part` 殘留)。數字是在本檔成稿時用 grep 重數的,不是沿用審查者回報。
評等下修共 19 條:P1→P2 3、P1→P3 3、P2→P3 10、P3→P4 3;另有 2 條證據比原報告更硬(feed 身分、tag→latest)。

**反覆出現的形狀只有一種**:規則只補在 N 條兄弟路徑之一。第一輪 9 支審查者互不通氣,卻有兩組獨立收斂(adopt 的 feedback-source 分支手寫指引;`reconciliation_ambiguous` 兩個入口給相反答案),Codex 那條 `reconciliation_ambiguous` 被驗證者標成**第十三次現形**、建議排第一個修。

## 二、P1 —— 程式碼

### P1-1 retract 中段某集後,工具自己的 `next_step` 叫你用 `podcast_series` 重生,而它會把後面各集的回錄讀進那一集
- 位置:`tools_podcast.py:180`(`_regeneration_entry_point` 回 `ACTION_SERIES`)、`:209`(`_regeneration_hint` 對 series 形狀回空字串)、`:5046`(`_run_episode` 不傳 `source_ids`)
- 重現:EP01-03 跑完 → retract EP02 → `next_step` 尾句「用 podcast_series 重生。」→ 照做 → `generate_audio(source_ids=None)`,當下 notebook 含 `EP03 收尾篇` 回錄 → `complete=True` 零訊號
- 為什麼是 P1:repo 最忌諱的靜默內容錯置,由一個平常操作(QA 拒收中段集)+ 照工具回傳做觸發。`design-notes.md:21` 的安全論證「往前跑時筆記本只有 ≤ 當集來源」在這裡失效;skill `tool-reference.md:750` 寫「回頭重生走 `podcast_episode`」,工具卻說 series
- 修法落點:`podcast_series` 每集迴圈頂端、`_assert_source_cleanup_done` 之後(約 `:4515`):「本集無 output 且存在集號更大、有 output evidence 的列」→ `partial(ACTION_EPISODE)`。caps 層修不了(只拿單一 row)。測試:`tests/test_series_durable_resume.py`
- 來源:R2 → 驗證 A1 CONFIRMED

### P1-2 flat v1 legacy 集可被 `podcast_episode_resume(artifact_id=B)` 無審計換掉 artifact,舊回錄 source 變孤兒
- 位置:`_ensure_resume_attempt` 新建分支 `tools_podcast.py:1192-1200`(只查 `output_attempt_id is not None`,不查 `has_hard_output_evidence`)
- 重現:`episode.artifact_id` A→B、`output_attempt_id` 新建無 tombstone、`feedback_source_id` src-1→src-2、`pending_source_cleanup=None`、S1 仍在 notebook
- 反駁失敗的理由:`tools_podcast.py:3958-3965` 註解自己把此狀態命名為 retract amendment (2),寫明「只有這條新建分支能繞過 guard」、來源是真實事故 —— 是修復出口不是授權;`_create_audio_attempt:959-963` 對同一集會拒。`tests/test_reliability_followups.py:354` 只斷言 source 換了,沒固化「允許換 artifact」
- 修法落點:同一 guard 加「有 legacy 硬證據且 `legacy_artifact_id != artifact_id`」(同一顆 legacy artifact 必須繼續放行,P1-3 靠它)。測試:改寫 `test_reliability_followups.py:354` 並保留原閘
- 來源:Codex #1 → 驗證 G CONFIRMED

### P1-3 `legacy_audio_missing` 停點交棒 `podcast_episode_resume`,照做會第二次上傳同名回錄、忘掉 S1
- 位置:停點 `tools_podcast.py:4620-4630`;seed 條件 `:1234-1250` 要求 `feedback_source_adopted_at`,而該標記只有 `podcast_attempt_adopt`(`:3770`)會寫,`_promote_attempt_output:1968` 明講不寫
- 重現:真 legacy manifest(無 adopted_at)→ series 回 `legacy_audio_missing` + resume → `add_file` 1、兩筆同名 media;有 adopted_at → 0
- 修法落點:`:4620-4630` 停點改交棒 adopt(或 adopt→resume 串接);**不要**弱化 `:1238` 的 seed 條件。測試:`test_series_durable_resume.py`(`legacy_audio_missing` 零覆蓋)
- 來源:Codex #2 → 驗證 G CONFIRMED

### P1-4 feed 身分(`show_id` × `PODCAST_TOKEN_SALT`)沒有機器圍籬,同一份 manifest 可無警告發到另一條 feed
- 位置:`tools_publish.py:472`(`show_id or saved_show.get("show_id")` 不比對既存值)、`:823`(覆寫 manifest show_id)、`:819`(`persisted_show` 不含 token)
- 重現(fake uploader):第一次 token `5tash…`,第二次 `show_id="audicast-v2"` → token `nhlch…`,8 個檔全 PUT 到新目錄、無例外、manifest 被覆寫、回傳零提及舊身分;不同 salt 同 show_id → 不同 token;整支 `tools_publish` 唯一 GET 是 `/healthz`(零 read-back)
- 加重:錯誤自我延續(覆寫後之後每次滾動加集永遠落新 feed,manifest 不記舊身分);salt 分支零痕跡
- 已排除「刻意設計」:`design-notes.md:116`「永不改」、`identity.py`「NO global registry」、CHANGELOG / gotchas-publish 零句「換 show_id 是 host 決定」;ADR-0013 graphify 換 show_id 的做法是新 manifest + 舊標 retired
- 修法落點:`tools_publish.py:471-480` 加一條比對,`:819` 存 `token`(一條比對蓋兩分支;缺席 = 照發,逃生口 = `ManifestStore.update` 移除欄位)。⚠ token 是 feed URL 秘密成分,會落到 podcast-lab 磁碟 —— 要確認那份 manifest 是否進版控。salt 分支歸 ADR-0003 已決策未實作(`deployment_id` / `latest_verified` 全 repo 零命中)
- 來源:R4 → 驗證 C CONFIRMED

### P1-5 tag → `latest` 之間沒有 CI 閘;v0.9.23 那次 `latest` 比 CI 綠燈早 66 秒
- 位置:`.github/workflows/ci.yml:3-7` 只對 master push/PR 觸發;`retag-latest.yml` 收到 `v*.*.*` 就 force-push `latest`,無 `needs` / `workflow_run`
- 實測五次發版 retag 起跑 − CI 結束:v0.9.23 **−77s**、v0.9.24 +33s、v0.9.25 +349s、v0.9.26 +17s、v0.9.27 +27s。五次 4 守 1 破,破的那次無人察覺(且 v0.9.23 正是 tag 樹 lock/pyproject 不自洽那版)
- branch protection API 403(免費方案設不了),「靠人記得」是唯一防線;retag 單調不可退
- 修法落點:`retag-latest.yml:37` 那個 step 之前插 gate(`gh api` 查 `github.sha` 的 ci.yml conclusion,非 success 就 exit 1)。不放進 `retag-latest.sh`(`:61` 是本機救援路徑)
- 來源:R8 → 驗證 F CONFIRMED(原報告拿 27 秒當破口是誤讀,真破口在 v0.9.23)

## 三、P1 —— 測試沒守住不變式(突變全綠)

| # | 缺口 | 突變 → 結果 | 補在哪 |
|---|---|---|---|
| T1 | `_promote_attempt_output` 的**回傳值**只有 `_run_episode` 一條被驗,resume / series 三條只驗 manifest(gotchas 明文「兩個出口都要斷言」,v0.6.0 原姿勢) | 三呼叫點後把回傳 `published_at` 蓋成 2099 → 12451 passed 全綠 | `tests/test_attempt_retract.py` 照 :172-195 複製兩份走 resume / series |
| T2 | `audio_finalize.py:695-700` mp3 下載的 post-commit dir fsync 容忍零測試(四站點三有一無);NAS 上每次重下載同一行失敗、永遠 finalize 不了 | `:699` errno 容忍改 `if True:` → 全綠 | `test_finalize_idempotency.py` patch `notebooklm_mcp.audio_finalize._fsync_parent` 丟 `OSError(EINVAL)` |
| T3 | `test_attempt_capabilities.py:440-443` 手抄 `_NEVER_DISPATCHED` / `_TERMINAL_REMOTE`(第三份拷貝);跳掉的 12 格全是 `authorization_basis=None` 那些;守門 `:4458-4485` 不看 dispatch/remote,哪天前移就 skip 掉、全綠 | 事實核對 | `:440-443` 換成 `p._NEVER_DISPATCHED` / `p._TERMINAL_REMOTE`,刪 `:392-394` inline 拷貝 |

## 四、P2

| # | 缺陷 | 位置 | 修法落點 / 測試 | 來源 |
|---|---|---|---|---|
| 2-1 **建議最先修** | `reconciliation_ambiguous` 的中央 caps 導向 `podcast_episode_resume`,而 resume 對同兩個候選再拋一次 ambiguous → 自迴圈;`podcast_series:4994` 對同一狀態手寫 adopt(兩入口矛盾,紅線 :10 第十三次)。`test_attempt_capabilities.py:156` 鎖錯答案,且 fixture 沒有 `candidate_source_ids` → 永遠綠 | `tools_podcast.py:491`、`:719`;`:4994` | `:491` ambiguous 且候選非空 → `ACTION_ADOPT` + caps 帶出 `candidate_source_ids`;修好刪 `:4994` 手寫。測試 `:156` fixture 真填候選 + e2e `test_reliability_followups.py:410` | Codex #5 → G |
| 2-2 | `podcast_attempt_adopt` feedback-source 分支手寫 `ACTION_RESUME if needs_rename else ACTION_SERIES`,與 caps 分岔(caps 算出 resume);`needs_rename=True` 回傳無 `artifact_id`(resume 認的身分);無 `next_step`;**`complete: true` 但尚未 promote**(host 直接 publish 撞 `_ensure_local_mp3:178`)。「ACTION_SERIES 是死路」子項被推翻 | `tools_podcast.py:3840-3858` | closure 比照 artifact 分支 `:3666` 回 `_attempt_capabilities`。測試 `test_series_durable_resume.py` | R1 + R2 獨立收斂 → A2 |
| 2-3 | 已完成集的遠端 Studio artifact 被刪 → series 與 resume 都裸拋 `RuntimeError("cannot be verified in the remote list")`,無停點、無 `safe_next_action`、不提 `start=N+1` 逃生口(實測有效)→ 整季永卡 | `audio_finalize.py:572-576`;`tools_podcast.py:4544-4578` 只翻譯 source 也不見那種 | `:572-576` 改具名例外(比照 `TerminalGenerationError`),由既有 except 階梯翻結構化停點。測試 `test_finalize_idempotency.py`(缺「source 在、artifact 不在」格) | R2 → A2 |
| 2-4 | `prior_mp3_path` 與指名 `source_ids` 靜默互斥:EP{n-1} 真的上傳改名了,dispatch 不帶它,`ok=true`;src 成無人引用孤兒吃掉 ≤9 一格。只打 standalone(routed 路徑 `:2830` 已拒 prior)。skill 兩句話合起來推 host 走這個輸入 | `tools_podcast.py:2519-2523` / `:2598-2608` / `:2635`;`_sources.py:110-121` 指名分支忽略 `pending_uploads` | `_run_episode :2507-2512` 之後、任何副作用前(兩呼叫端唯一交會點)。測試 `test_source_selection.py :841` 旁 | R5 → A1(P1→P2) |
| 2-5 | 隱藏目錄繞過多節目容器偵測:`base/showA/…` + `base/.archive/showB/series_manifest.json` → `others=[]` → showA 吃下 showB 的 bundle(ADR-0012 事故同形)。symlink / 不可讀目錄兩子項被推翻(執行端擋住)。今天無觸發佈局,但 ADR-0013 退役是搬進可見 `archive/`,一次 `mv … .old` 就靜默失效 | `generation_input.py:220` | 刪那一行 hidden-skip(`shows/` 底下隱藏目錄只有 `.venv*`,無套件 ship `series_manifest.json`)。測試 `test_generation_input_bundle.py:895` 參數化隱藏 sibling | R6 → E(P1→P2) |
| 2-6 | bundle 換手 tripwire 恆真:`test_generation_input_bundle.py:171-200` 靠 `patch(Path.is_dir)` 但 loader 全程 `os.open/fstat`,零呼叫;且 `except ValueError: return` 在 with 內。突變證實:目錄段 `O_NOFOLLOW` 另有 `:653` 守,真正無人守的是成員檔 `:51` 與 inode 重驗 `:62-68`。生產碼正確(掛在第一次 `fstat` 上 REFUSED) | 測試檔同一支 | 改掛 `os.fstat` 或 patch `_read_frozen_bundle_files`;`pytest.raises(match="changed or became a symlink")` | R6 → E(P1→P2 測試債) |
| 2-7 | `test_window_closed_narrative_…` 掛 600 格只 15 格跑到斷言;`:654/:656` skip 條件讀 SUT 輸出 → 優先序回歸變 skip 不變紅(突變:597→612 skip,那支 0 failed) | `test_attempt_capabilities.py:634-660` | 加 meta 斷言「跑到斷言的格數 == 15」或 skip 改靜態白名單 | R7 → H |
| 2-8 | `test_tool_annotations.py` docstring 承諾「忘加 annotations 要紅」,但 `_names_with` 的 `if ann is not None` 濾掉無 annotations 的工具 → 新工具全過(突變:38 支、`annotations=None`,全綠) | `tests/test_tool_annotations.py:66` vs `:143-170` | 加 `assert all(ann is not None …)` + 白名單句 | R7 → H |
| 2-9 | fake `rename` 查無回 `None`,實裝 0.8.2 raise `ArtifactNotFoundError` / `SourceNotFoundError`(`_web/artifacts.py:462`、`_web/sources/__init__.py:650`);無測試把此形狀餵我方(只有 `ConnectionError` 泛型) | `tests/conftest.py:191-198`、`:360-366` | `test_reliability_followups.py :43` 旁加 NotFound 案例 | R7 → H |
| 2-10 | README / docs 四處教 `uv pip install -e`(`README.md:16`、`:60`、`docs/auth-and-config.md:17`、`docs/test-account.md:42`),AGENTS.md 與 release-checklist 兩處明文禁止;dev extras 無上界(本機 pytest 9.0.3) | 同上 | → `uv sync --extra dev [--extra login]` | R8 → F(範圔 2→4 處) |
| 2-11 | 目標集的 unresolved 清理義務屬舊 notebook 時 gate 靜默放行(具名義務那半擋、unresolved 那半不擋);`:2270` 註解「目標這一集自己的另外擋,見下」但「見下」只覆蓋具名。可達性:公開工具無門,只有 fixture / 手改到得了 → P2 下緣。順帶:具名那半自己也是死路(搬到新本後義務無入口結案) | `tools_podcast.py:2282` `target_elsewhere` | 也掃目標集 unresolved tombstone,連同 discharge path 一起想。測試 `test_attempt_retract.py :1493` 旁 | Codex #3 → G |

## 五、P3(成立;多為一行修法或純文件)

**attempt 狀態機**
- `_assert_source_cleanup_done` CAS 衝突後 `violations` 未跟 `discovered` 一起修剪,raise 訊息叫 host 刪已被合法認領的 source(manifest 對、回傳錯,兩出口矛盾);公開 API 造不出並行 claim,觸發率未證。修 `:2402` 前重算。—— R1-F1 → A1
- `ManifestStore._validate:242-247` tombstone 指標檢查沒掛 `on_write`,讀端也炸;但無任何發版會產生此形狀(git log -S 三字串同 commit `07ce43c`),只手改 JSON 到得了 → **改註解不改 code**。⚠ 若真改成 on_write-only:今天擋 retracted 集上 feed 的是 pop(`:4054`)+ `tools_publish.py:177-181` / `:246-250`,不是 `_validate`;手改形狀會被 `_mp3_provenance_gap:252` 撈到 tombstone、sha 相符放行,`tools_publish` + `publish/` 無一行讀 `retraction` → 同 commit 必補。—— R1-F3 vs R4 → A2
- `_run_episode:2725` finalize handler 只接 `Exception`(姊妹 `:2666` 接 `CancelledError`),取消時續跑 payload 沒附上;manifest durable、事後 caps 正確。一處改。—— R1-F4 → A2
- retract 冪等重呼零變更仍 revision+1(`[15,16,17,18]`);不是 retract 專屬,修在 `ManifestStore.update:44-58`(⚠ 動到 `test_manifest_store.py:180`)。—— R1-F5 → A2
- 兩個 transient handler(`:4964-4985`、`:5109-5139`)對 `reconciliation_ambiguous` 回 series 不帶候選、無 `next_step`:`observed_state` 報歧義而同 payload 給不出化解工具;紅線十二與 docstring 未被逐字違反。同 commit 動兩處。—— R2-P2b → A2
- `podcast_episode_resume` 不驗 `wait_timeout`(nan 直流 `wait_for_completion` 永不逾時);「漏數一支」被推翻(docstring「三個」數的是會持久化的入口)。`:3263` 一行。—— R2-P3b → A2
- 併發 finalizer 晚到失敗盲寫倒退 checkpoint(`upload_unknown:742` / `download_failed:666` 不比對當前 status,`_mutate` 不帶 expected_revision);`source_id` 不清故 caps 仍對、下次自癒,零重複上傳。兩 callback 只准從自己 claim 的 `dispatching` 降級。—— Codex #4 → G
- adopt 回傳把跨 notebook 清理義務全標成目前 notebook(`_queue_pending_source_cleanup` 回 `list[str]` 丟身分;`_cleanup_obligations` docstring 明文禁止「拿 episode 當下 notebook 回推」;retract 姊妹 `:435` 帶逐筆身分);需先經 2-11 的洞。`:3853` 改由 `_cleanup_obligations(..., fallback_notebook=)` 組。—— Codex #6 → G
- process death 後 download `.part` 永久失去引用(`audio_finalize.py:634` 不讀既有 `temp_path`);「會重下載」子項是 `:676` 明寫的 checkpoint 契約(DESIGNED-AS-IS)。mkstemp 前 unlink,~3 行。—— Codex #7 → G
- `_ensure_resume_attempt` claimed 分支標題閘零守(new 分支有 `test_resume_cannot_rename_the_episode` 守);被 `:1139` attempt.title 檢查遮蔽,只手改可達。—— R7-F1 → H
- `_reset_attempt_for_resend` 的 `status` 有 4 支行為級守;殘留的 `status_origin/observed_at/error/error_code` 四欄零斷言。`test_quota_failover.py:407-427` 加三行。—— R7-F3 → H

**failover / pool**
- **429 分類(v0.9.26 反駁再審)**:事故敘事(第二顆 artifact / 雙燒)**推翻** —— R3 引的三處上游證據全屬別的 RPC 家族;上游 `artifacts.with_rate_limit_retry`(`_app/generate_retry.py:115` 實際在用)對無 `rpc_code` 的 429 原地重送,是撐住結論的實錘。**但** v0.9.26 給的第一個理由「429 被限流器擋在 handler 之前(executor.py:328)」是推測冒充查證 —— `TransportRateLimited` 唯一生產路徑是收到 HTTP 429 回應之後,那段對 handler 有沒有跑一字未講。剩下真的一半:`_failover.py:39-46` 把 `RateLimitError` 定義成 USER_DISPLAYABLE_ERROR 拒絕但 except 抓型別;`tools_podcast.py:2660` 硬寫「沒建 artifact、不多燒」在 429 臂是「幾乎必然」非契約;傳輸 429 綁 IP/host 不綁帳號,**換帳號是錯的解藥**(一次 429 風暴讓 N 帳號逐一撞同一限流器、N−1 筆假稽核、整 pool 冷卻 600s)。判別特徵**只有 `rpc_code` 可靠**(decoder 硬寫 `'USER_DISPLAYABLE_ERROR'`;transport 永遠 None;`retry_after` 無標頭時也 None)。修 `:175` except 內加 `rpc_code` 判準,不動 tuple。—— R3-P1-1 → B(P1→P3)
  - 附帶:`tests/conftest.py:702` 的 fake `RateLimitError("每日配額已用盡")` **兩種生產者都不是**(無 rpc_code、無 retry_after、無 cause);整套 failover 測試跑的是 decoder 產不出的形狀,一加 `rpc_code` 判準會靜默翻到 transport 分支。補 `rpc_code="USER_DISPLAYABLE_ERROR"`。
  - 給 CHANGELOG:v0.9.26 那筆「查證後不成立」的**理由**要改(結論對、第一理由錯、第二理由才是實錘),否則下一個獨立審查者還會再提一次 —— R3 就是。
- `rotate_for_quota` 的 `record` 拋非 post-commit 例外(磁碟滿)時,A 已冷卻 600s、`_ACTIVE` 已指向從未 dispatch 的 B、`on_clean_refusal` 未觸發、attempt 停 `prepared`;fail-safe 方向。修 `_failover.py:178` **與 `:221`**(0.7.x status 分支)兩處。—— R3-P3-1 → B
- `NOTEBOOKLM_BACKEND` 護欄掛在 `if inline_auth:`(`app.py:316`),但 `:69-75` 註解自己說 BACKEND 理由與 inline 無關;只有 `:410` 單 client 分支暴露(登入/開發機)。`:395` 與 `:410` 兩個 `from_storage` 顯式 `backend="web"`。—— R3-P3-2 → B
- ADR-0010 v0.9.7 amendment「600s — chosen conservatively **above** that 26-minute observation」方向寫反(600 < 1560);`runtime.py:32-34` 中文方向正確(保守=短)。`above` → `well inside`。—— R3-P3-3 → B

**發布 / 附件**
- `cover_cli` 批次模式 `--show-name` default `"Audicast"` 永遠 truthy → `manifest.get("title")` fallback 死碼;首發前 manifest 無 `show` 鍵 → 品牌圍籬放行。gotchas-publish:162 正規指令帶 `--show-name Audicast`,wordmark 一眼可見 → P3。`:164` default=None、`:201` 兩者皆無就 `ap.error`(⚠ 打到 `test_cover_cli.py:198/229/261/161`)。—— R4-F2 → C
- `itunes:category` 無驗證:`''` / `'Bogus'` / `'   '` 照發,且 `''` 回寫 manifest 後 `saved_show.get(..., "Technology")` 因 key 存在回 `''` → 黏性。只補 non-empty,不做 Apple 白名單。`:520` 附近一行。—— R4-F3 → C
- `validate_xml_text` preflight 無集號(`:611-612` 自己立的規矩五處補了這處沒);`episode_set_description` 不跑它(U+000C 中段放行、publish 才爆)。docstring 只列三項具名檢查非全稱,但 `:185-189` 行內註解意圖為真。兩處各一行。—— R4-F4 → C
- `publish/feed.py:152` `build_index_html` 自己寫第二份 `EP{n}` 剝除 regex,牴觸 `naming.py`「only place」。—— R4-F5 附帶 → C

**薄包裝**
- `chat_ask(source_ids=[])` 直通 SDK → `_chat.py:316` 只判 `is None` → 零來源請求,最壞無根據答案 → `set_description` → RSS。`source_search` 那半被推翻(上游明文 `[]` = 搜全部)。`tools_basic.py:744` 用既有 `to_source_ids` 包一層,`source_search` 不動。—— R5-P3-2 → D
- `_explain_no_research` 承重於上游私有覆寫 `WebResearchAPI._wait_observed_status`(把 NOT_FOUND 中和成 NO_RESEARCH),`test_contracts.py:745` 只釘 enum 成員 → 補對 `notebooklm._web.research.WebResearchAPI._wait_observed_status` 的 `getsource` 斷言。—— 驗證 D 附帶新缺口

**bundle / scripts**
- sidecar 三支(`write_attempt_binding:390/408`、`read:446`、`rollback:511`)不套 load 的 containment;窗口數秒級(load → `probe_auth` → 三趟 RPC → sidecar);對手面邊際價值≈0(能在窗內換目錄者可在 load 前換整顆自洽 bundle),殘留是意外重整;harm 有界(重複燒配額 + 稽核說謊,不會 dispatch 錯內容)。`prepared` 帶 `(st_dev, st_ino)`,寫前 `lstat` 比對。—— R6-P2-3 → E
- `scripts/setup-test-config.sh:27` `sed -n '2,20p'` 現在多印 17/19/20 三行程式碼,`--from`(`:26`)未在說明出現;sibling `sync-auth.sh:47-51` 已改 awk 並記下這個坑。—— R6-P3-6 → E
- `scripts/eval_harness/run.sh:30-33` 把**活的** skill working tree `ln -sfn` 進受測 box,agent 帶 `--dangerously-skip-permissions` → 實測寫穿 symlink 蓋 SKILL.md、`rm -rf box` 救不回。→ `cp -r`。—— R6-P3-8A → E
- `run.sh:22` worktree 競態:README:16-18 推薦的平行迴圈 10 併發搶同一 `$SRC`,實測 `fatal: already exists` ×9、樣本消失;`grade.py:47-58` 不查兩臂 id 對齊 → 半靜默。冷啟動必然發生(`results-v0.9.26.json` 20/20 是因單題示範先建了 worktree)。冪等建法或 flock;`grade.py:58` 印各臂樣本數。—— R6-P3-8B → E(比原估更強)

**打包 / CI / 文件**
- `ci.yml:31` `uv sync` 無 `--locked`,lock 髒不紅(v0.9.23 tag 樹 pyproject 0.9.23 / lock 0.9.21,CI 綠);對消費端零影響(`uv tool install` 不讀 lock)。→ `--locked`。—— R8-P2-2 → F
- 實裝 vs lock **23** 個 transitive 不同版(anyio、starlette 跨 4 minor、cryptography 跨 2 major、pydantic-settings…);現象 release-checklist:72-77 已載明,殘留是對帳 recipe(`:109-112`)只 grep `mcp`。→ 全量 diff。—— R8-P2-3 → F
- `pyproject.toml:28` `markdown>=3.5` 唯一無上界的直接依賴,與同檔「上界是硬需求」矛盾,commit / CHANGELOG 查無取捨;落在 `notes_html` 半信任渲染路徑。→ `<4`。—— R8-P2-4 → F
- CI action 浮動 major tag(`checkout@v4` ×2、`setup-uv@v6`)而 job 持 deploy key / `contents: write`;`persist-credentials: false` 已關主要攻擊面。→ 釘 SHA + Dependabot。—— R8-P3-8 → F
- `conftest.py:45-47` 與 `test_errors.py:90` 註解仍寫 0.7.x「NOT by raising」契約,`test_contracts.py:235` 釘的是相反;live raise 形狀有測(`test_attachment_failover:234` parametrize 兩形狀)→ 純文件。—— R7-F8 → H
- `scripts/check_skill_sync.py` 208 行零測試(fail-closed 可示範);`test_artifact_rename_is_fire_and_forget` 名稱講的是 0.8.0 後不成立的性質(斷言正確)。—— R7-F10 → H
- AGENTS.md §按需載入表可在「動到什麼」欄補 `manifest_store.py` / `_cookies.py` / `audio_finalize.py` 檔名(三條紅線都在被路由的 gotchas 裡,`a690e72` 刻意外移;純導覽)。—— R8-P2-6 → F(nit)

## 六、被推翻 / 已載明 / 不值得列

| 候選 | 為什麼 |
|---|---|
| research `NOT_FOUND` 讓 `_explain_no_research` 失效(R5 P1) | 審查者漏看 `_research.py:243` 鉤子:`WebResearchAPI._wait_observed_status`(`_web/research.py:641-645`)把 NOT_FOUND 中和成 NO_RESEARCH;實跑診斷有加上;fake 換實裝例外 41 passed;v0.9.14 真帳號量到的就是 `no_research` |
| `_TEXTLESS_BLOCK_KINDS` 只豁免 HORIZONTAL_RULE(R5 P2) | CHANGELOG:1063-1065 明文 IMAGE 刻意擋;THOUGHT 與 CODE_BLOCK 上游同類(carry text、not decoded),FINDING-E 事故觸發物正是 CODE_BLOCK;無證據 chat answer 會夾帶 |
| gRPC 5 與 7 兩種命運(R5 P3) | 真帳號兩次量到 7;`rpc_code=5` 原樣拋是 v0.9.14 突變驗證過的判別力,往這方向補會回歸 |
| `source_search(source_ids=[])`(R5 P3 半) | 上游 `_sources.py:110-112` 明文 `[]` = 搜全部 |
| 429 事故敘事(R3 P1 的那一半) | 見上;契約層縮成 P3 |
| `naming` 邊界表(小寫/全形/NBSP)(R4 F5) | `naming.py` docstring「leave anything else」+ `test_bare_leaves_another_episode_prefix_alone`「publish-gate problem, not naming」;雙前綴無 code path 產得出來 → P4 won't-fix |
| `files[*].bytes` 收 `true`(R6 P3) | 只在 1-byte brief 成立(多位元組 mismatch REFUSED);另兩成員必為合法 JSON → P4 cosmetic |
| backfill 鎖外閘 / 零變更寫入(R6 P3) | 需競爭 writer;publish preflight 會擋(loud);一次性人工腳本 → P4 |
| `_atomic.prepared_replacement` 路徑版 chmod(R6 P3) | 契約是 close(fd) 後把路徑交呼叫端寫(cover_cli / auth_cli),結構上拿不到 fd;留 fd 反而更糟;需目錄寫入權者可直接寫最終檔 |
| 隱藏目錄那條的 symlink / 不可讀子項 | 執行端 `O_NOFOLLOW` 與 `O_RDONLY` 都擋,兩端效果對稱 |
| AGENTS.md 漏 `compare_sdk_surface.py` / `retag-latest.sh`(R8 P3) | 後者就在 AGENTS.md §Conventions;前者在 release-checklist:118-122 且路由表指過去 |
| `dist/` 舊 wheel(R8 P3) | gitignored;CI 乾淨 checkout;`uv tool install` 只收單一 positional → 當場大聲紅 |
| 跨 repo sync check 觸發面(R8 P2) | CHANGELOG:113-136 指名修法「留給下一輪」;「無手動觸發口」被 `gh run rerun`(checklist:24)推翻;抓 HEAD 非釘版是檢查目的 |
| adopt 的 `ACTION_SERIES` 是死路(R1 F2 子項) | `_attempt_can_adopt_source:3380` 前提 `remote.status==completed` → `_series_will_redispatch` 必 False → `:1642` 守門到不了,實跑 series 成功 |
| `_ensure_resume_attempt` new 分支標題閘零測試(R7 F1 半) | `test_attempt_retract.py:424 test_resume_cannot_rename_the_episode` 抓到(match 前半句) |
| `_reset_attempt_for_resend` remote 重設零斷言(R7 F3 半) | `status` 由 4 支行為級測試守住 |
| `.part` 會導致重下載(Codex #7 半) | `audio_finalize.py:676` 明寫「下次重新從 mkstemp 開始」是 checkpoint 契約 |

## 七、獨立收斂與跨審查者張力(裁決價值最高的信號)

- **兩支 Claude 審查者互不通氣各自提了 adopt 手寫分支**(R1-F2 = R2-P3a) —— 收斂成立,驗證再多抓到 `complete: true` 說謊。
- **Codex #5 與 R2-P2b 是同一個狀態的兩個角度**(`reconciliation_ambiguous`):Codex 抓到根(中央 caps 給錯答案),R2 抓到葉(transient handler 不帶候選)。修根即可。
- **R1-F3 與 R4「已查證乾淨」直接相反** —— 裁決:兩人都對一半。今天 retracted 集上不了 feed 是靠 retract 的 pop,不是 `_validate`;`_validate` 那條在 API 可達狀態下不會觸發,但它是手改 manifest 的唯一守門,拆掉前要在 publish 側補。
- **R3-P1-1 重提 v0.9.26 已「反駁」的條目** —— 結論不變(不是事故),但反駁理由要重寫,否則會一直被獨立審查者再提。
- **Codex 七條有六條是 Claude 八支都沒提的**,且兩條 P1 都站住 —— 不同模型家族確實看到不同的東西,值得保留在 playbook 裡。

## 八、順帶:文件對程式的漂移(會把人帶錯路的那幾條)

- `design-notes.md:21` / skill `tool-reference.md:750`「回頭重生走 `podcast_episode`」vs 工具 `next_step`「用 podcast_series 重生。」(P1-1 的根)
- `test_attempt_capabilities.py:156` docstring「resume 是唯一能把 source 身分認回來的路」對 ambiguous 是假的(2-1 的根)
- CHANGELOG v0.9.26 429 反駁的第一個理由(P3 那條)
- ADR-0010 `above`(P3)
- `conftest.py:45-47` / `test_errors.py:90` 0.7.x 契約(P3)
- `_validate:242` 註解「寫入時」(P3)

## 九、另一個問題:記憶 / 知識頁

見同目錄 `memory-audit.md`(已另行回報):Hindsight bank `omp-notebooklm-mcp` `facts=0`、六頁全 `Generating content...`、背景作業已跑完 —— 「非同步生成中」的判讀不成立;`omp-audi-skill` bank 在 server 不存在;跨工作區 feedback 放在子 repo 讀不到的工作根 memory;`mcp improve.txt` 257KB untracked 未 gitignore(無 secrets,含 11 處被 v0.9.27 更正掉的舊數字)。

## 十、本輪對 playbook 的兩條增補建議(只提,不寫入)

1. 「派獨立複審不餵 findings」這條可以再加一句:**不同模型家族**的獨立審查抓到的東西幾乎不重疊(Codex 7 條有 6 條 Claude 沒提),而 Claude 內部的收斂(兩支各自提同一條)才是「這條一定是真的」的信號。
2. 反駁式驗證這一輪對 59 條候選下修了 19 條的評等(P1→P2 3、P1→P3 3、P2→P3 10、P3→P4 3),整條推翻或歸入已載明 17 列;也有 2 條證據比原報告更硬(feed 身分、tag→latest)。第一輪交出的 15 條 P1(R2 1、R3 1、R4 1、R5 2、R6 2、R7 5、R8 1、Codex 2)只有 8 條以原評等站住。**第一輪的評等不要直接進 CHANGELOG**,這與 playbook 第 5 條「子 agent 回報的數字是階段值」同型。
