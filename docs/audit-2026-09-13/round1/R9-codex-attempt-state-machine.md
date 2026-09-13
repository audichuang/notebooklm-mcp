# 稽核結論

確認 **7 個可具體觸發的缺陷：2×P1、4×P2、1×P3**；沒有為湊滿 8 項加入推測性問題。全程唯讀，未執行 `uv run` 或測試套件，結束時 worktree 為 clean。

## 入口／路徑枚舉

| 不變式 | 已逐一路徑覆核 |
|---|---|
| attempt 先持久化、禁止無審計取代 | `_create_audio_attempt`、`_ensure_resume_attempt` 的 claimed/new 兩分支、frozen reuse、reconcile/adopt binding、promotion |
| tombstone default-deny | `tools_podcast._attempt_record`、`audio_finalize._record`、`ManifestStore._validate` |
| 清理義務 | retract 已知 source、retract unresolved upload、adopt 淘汰 source；生成入口 `_run_episode`、resume、series preamble |
| finalize 冪等性 | wait、artifact rename、download、source upload/reconcile、source rename、promotion |
| safe handoff | `_attempt_capabilities`、`_attempt_next_step`、reconcile 回傳、adopt 回傳、series `partial` |
| 配額 failover | `_run_episode` 首次 dispatch、series inline redispatch；兩者均進共用 failover |
| 檔案 crash safety | manifest 原子寫入、MP3 temp/replace/fsync、失敗與取消路徑 |

## Findings

### 1. [P1] Legacy durable output 可被不同 `artifact_id` 透過 resume 無審計取代

- **位置：** [tools_podcast.py:1196](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:1196)、[tools_podcast.py:1234](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:1234)、[tools_podcast.py:1944](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:1944)
- **違反：** [ADR-0009 第 11 行](/home/user/research/audiskill/notebooklm-mcp/docs/adr/0009-retracted-attempts-are-tombstones.md:11) 明定 durable output 存在時，`_ensure_resume_attempt` 不得接受不同 artifact；也違反 [audio_finalize.py:42](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:42) 的 legacy hard-evidence 定義。
- **具體序列：** flat v1 episode 已有 `artifact_id=A`、`mp3_path`，但沒有 `attempts`／`output_attempt_id`。呼叫 `podcast_episode_resume(..., artifact_id=B, manifest_path=...)`，其中 B 是 notebook 裡另一顆有效 audio artifact。
- **錯誤結果／路徑：** claimed 分支找不到 B → new 分支只檢查 `output_attempt_id is not None`，未檢查 `has_hard_output_evidence()` → 建立 accepted attempt B → rename/download/upload B → `_promote_attempt_output` 把 episode 的 `artifact_id`、`mp3_path`、output pointer 改成 B。A 未經 retract 即被取代，舊 feedback source 也沒有進清理義務。
- **測試：** 不會捕捉；[test_legacy_source_is_not_carried_to_a_different_artifact](/home/user/research/audiskill/notebooklm-mcp/tests/test_reliability_followups.py:354) 反而執行同一條不同-artifact resume 並期待成功，固化了與 ADR 相反的行為。
- **信心：極高。**

### 2. [P1] Legacy 音檔遺失的官方 resume 路徑會重複上傳並遺忘原 feedback source

- **位置：** [tools_podcast.py:4608](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:4608)、[tools_podcast.py:4621](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:4621)、[tools_podcast.py:1234](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:1234)、[audio_finalize.py:733](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:733)
- **違反：** [ADR-0009 第 9 行](/home/user/research/audiskill/notebooklm-mcp/docs/adr/0009-retracted-attempts-are-tombstones.md:9) 的「不得留下兩筆同名 media 污染後續 context」及 finalize 重入冪等契約。
- **具體序列：** flat v1 episode 有 `artifact_id=A` 與遠端可驗證的 `feedback_source_id=S1`，但本機 `mp3_path` 已遺失，且舊 manifest 沒有 `feedback_source_adopted_at`。
- **錯誤結果／路徑：** `podcast_series` 實際驗證 S1 存在，回 `legacy_audio_missing`／`podcast_episode_resume(A)` → `_ensure_resume_attempt` 因缺 `feedback_source_adopted_at`，不把已驗證的 S1 seed 成 completed → finalize 下載 A 後另行上傳 S2 → promotion 把 episode projection 改成 S2。S1 沒有進 `previous_feedback_source_ids` 或 `pending_source_cleanup`，永久成為未追蹤的同名來源；下一集未指定 `source_ids` 時會同時讀入 S1、S2。
- **測試：** 無。最接近的 [test_series_treats_flat_v1_artifact_as_legacy_completed_output](/home/user/research/audiskill/notebooklm-mcp/tests/test_series_durable_resume.py:128) 保留了本機 MP3，因此沒有走 `legacy_audio_missing`。
- **信心：高。**

### 3. [P2] 目標 episode 的 unresolved 清理義務屬於舊 notebook 時，生成 gate 靜默略過

- **位置：** [tools_podcast.py:2239](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:2239)、[tools_podcast.py:2251](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:2251)、[tools_podcast.py:2282](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:2282)
- **違反：** [ADR-0009 第 9、25 行](/home/user/research/audiskill/notebooklm-mcp/docs/adr/0009-retracted-attempts-are-tombstones.md:9) 及 `_assert_source_cleanup_done` docstring 的「目標這一集自己的別本義務也必須 fail-closed」。
- **具體序列：** 合法 manifest 狀態中，episode 1 已切到 `nb-new`；其 retracted tombstone 仍依 schema 保留 `notebook_id=nb-old`，且 `source_cleanup_unresolved=True`、upload 無 `source_id`。接著以 `nb-new` 對同一 episode 1 呼叫生成。
- **錯誤結果／路徑：** unresolved scan 得到 `owner=nb-old` → 因 owner 非目前 notebook 且非 `None`，既不放入 `unresolved_here` 也不放入 `unresolved_elsewhere` → `target_elsewhere` 只掃描已具名的 `pending_source_cleanup`，不掃 unresolved tombstone → gate 放行生成，舊本的孤兒義務未對帳。
- **測試：** 無。[test_cleanup_identity_follows_the_attempt_not_the_episode](/home/user/research/audiskill/notebooklm-mcp/tests/test_attempt_retract.py:1493) 使用的是另一個 target episode；[test_cleanup_obligation_identity_survives_switching_the_episode_notebook](/home/user/research/audiskill/notebooklm-mcp/tests/test_attempt_retract.py:2296) 只涵蓋已知 `source_id` 的 sibling 路徑。
- **信心：高。**

### 4. [P2] 併發 finalizer 的晚到失敗可把已驗證 checkpoint 倒退

- **位置：** [audio_finalize.py:742](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:742)、[audio_finalize.py:778](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:778)、[audio_finalize.py:860](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:860)；download sibling 同型於 [audio_finalize.py:639](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:639)、[audio_finalize.py:666](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:666)
- **違反：** [ADR-0002](/home/user/research/audiskill/notebooklm-mcp/docs/adr/0002-separate-attempt-facts-from-publication.md:3) 要求 side-effect outcome 與 verified postcondition 是可靠的獨立事實；也違反 [audio_finalize.py 模組契約](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:1) 的可重入性。
- **具體序列：** finalizer A claim upload 後，遠端已建立 source，但 response 延遲並最終拋例外；finalizer B 同時 list 到該 source，reconcile、rename、寫成 `completed`，甚至 promotion 成功；之後 A 的例外才返回。
- **錯誤結果／路徑：** A 的 `upload_unknown` 不檢查目前狀態，直接把 B 已寫好的 `completed` 改成 `acceptance_unknown`。`source_id` 仍存在，形成 output pointer 已指向該 attempt、但 finalize checkpoint 宣稱未完成的矛盾狀態。Download 路徑亦可由晚到失敗把另一 caller 的 `completed` 改成 `failed`。
- **測試：** 無。[test_concurrent_resume_only_one_caller_uploads_feedback_source](/home/user/research/audiskill/notebooklm-mcp/tests/test_finalize_idempotency.py:205) 只斷言 upload 次數及 `source_id`，未斷言最終 upload status，也未安排「成功 caller 先完成、失敗 caller 後寫」的時序。
- **信心：高。**

### 5. [P2] `reconciliation_ambiguous` 的中央 capabilities 指向必然自迴圈的 resume

- **位置：** [tools_podcast.py:491](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:491)、[tools_podcast.py:719](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:719)、[audio_finalize.py:400](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:400)；series sibling 在 [tools_podcast.py:4994](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:4994) 給出不同答案。
- **違反：** [gotchas-attempt「safe_next_action 必須可執行」](/home/user/research/audiskill/notebooklm-mcp/docs/gotchas-attempt.md:10)。
- **具體序列：** feedback upload response 遺失，reconcile 找到兩個 candidate，checkpoint 成為 `reconciliation_ambiguous` 且帶 `[S1,S2]`。
- **錯誤結果／路徑：** `_attempt_capabilities` 因 `feedback_upload_unresolved` 且 `can_resume=True` 回 `podcast_episode_resume`；`_attempt_next_step` 同樣教 resume → resume 再進 `_reconcile_source_upload`，仍得到相同兩個 candidate 並再次拋 ambiguous，無法前進。真正可解除歧義的是 `podcast_attempt_adopt`；`podcast_series` 已針對同一狀態手寫成 adopt，造成 sibling 路徑互相矛盾。
- **測試：** 不會捕捉；[test_unresolved_feedback_upload_prefers_resume_but_never_blocks_retract](/home/user/research/audiskill/notebooklm-mcp/tests/test_attempt_capabilities.py:156) 明確把三種 unresolved status（包含 `reconciliation_ambiguous`）都鎖成 resume。Series 的 adopt 測試只保護手寫 sibling。
- **信心：極高。**

### 6. [P2] Adopt 回傳把既有跨-notebook 清理義務全部標成目前 notebook

- **位置：** [tools_podcast.py:3450](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:3450)、[tools_podcast.py:3464](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:3464)、[tools_podcast.py:3853](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/tools_podcast.py:3853)
- **違反：** ADR-0009 的「清理義務自帶 notebook identity」及 [gotchas-attempt 的自足 handoff 規則](/home/user/research/audiskill/notebooklm-mcp/docs/gotchas-attempt.md:40)。
- **具體輸入：** episode 現在屬於 `nb-new`，但已存在 `{source_id:S-old, notebook_id:nb-old}` 清理義務；同時對目前 attempt 在 `nb-new` 執行 feedback source adoption。
- **錯誤結果／路徑：** `_queue_pending_source_cleanup` 刻意回傳「全部尚未結案義務」，但只回 `list[str]`，丟掉逐筆 notebook identity → public response 再把每個 ID 都組成 `{"notebook_id": nb-new}`。呼叫端照回傳執行會在錯的 notebook 刪 S-old，得到 no-op；冪等重呼 adopt 仍回同一個錯誤 payload。
- **測試：** 無。[test_adopt_also_returns_both_source_delete_arguments](/home/user/research/audiskill/notebooklm-mcp/tests/test_attempt_retract.py:2474) 只涵蓋所有義務都在同一本 notebook 的情況。
- **信心：高。**

### 7. [P3] Process death 後持久化的 download `temp_path` 從未被恢復或清理

- **位置：** [audio_finalize.py:634](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:634)、[audio_finalize.py:639](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:639)、[audio_finalize.py:676](/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/audio_finalize.py:676)
- **違反：** `audio_finalize` 的 crash-reentrant 模組契約，以及 [gotchas-files 的原子下載紀律](/home/user/research/audiskill/notebooklm-mcp/docs/gotchas-files.md:35)。
- **具體序列：** manifest 已 checkpoint `download.status=dispatching` 與 `temp_path=/…/.ep01.X.part`；process 在 download 寫入後被 SIGKILL。
- **錯誤結果／路徑：** `finally` 無法執行。下次 resume 不讀既有 `temp_path`，直接 `mkstemp` 新路徑並覆寫 checkpoint；舊 `.part` 永久失去引用。每次在此窗口被殺都會再留一份；若死亡點在 `os.replace` 後、`download_completed` 前，完整 MP3 也會被重新下載。
- **測試：** 無。`test_interrupted_mp3_download_leaves_no_partial_file` 與 `test_cancelled_download_is_cleaned_up_without_being_marked_failed` 都是可執行 `finally` 的 Python exception/cancellation，不涵蓋 process death。
- **信心：高。**

## 其他覆核結果

- 未找到吞掉 `asyncio.CancelledError` 的路徑；顯式捕捉處均會重新拋出。
- `ManifestStore` 的 tempfile、`fsync`、`os.replace`、parent fsync 與 tombstone pointer validation 未發現額外缺陷。
- `_status.py` 未發現缺陷。
- 配額 failover 的兩個 scope 內呼叫入口均使用共用 dispatch helper，未發現只修其中一路的分岔。
- 依 Ponytail 的去臆測準則，只列出上述 7 個有具體狀態／序列可達的問題。

Codex session ID: 01a09a5e-2d9f-73a3-b888-cd0a1dd1820c
Resume in Codex: codex resume 01a09a5e-2d9f-73a3-b888-cd0a1dd1820c
