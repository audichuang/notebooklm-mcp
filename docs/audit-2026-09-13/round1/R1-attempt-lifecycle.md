# R1 審查者:attempt 建立/reconcile/adopt/retract(5 條)

## F1 — 清理義務對帳撞 CAS 衝突後,`violations` 沒跟著修正,錯誤訊息叫呼叫端刪掉別人剛認領的合法 continuity source
位置:tools_podcast.py:2327、:2354(建 violations)、:2147-2165(_settle_cleanup_state 衝突重算)、:2402-2409(raise)
不變式:_assert_source_cleanup_done docstring ③;gotchas-attempt「manifest 與回傳是兩個出口」
序列:snapshot R → await sources.list() → 另一 task 把 S claim 進 EP_b(R+1)→ 本輪判 S 為 EP_a 孤兒,進 discovered[ep_a] 與 violations → _settle_cleanup_state 撞 conflict 重算,del discovered[ep_a](manifest 正確)→ violations 未修剪 → :2402 仍 raise 叫 host source_delete(S)
測試:無(_settle_cleanup_state 衝突分支無測試驅動)
嚴重度:P2(浮現內容是破壞性指令,host 照做即 P1)。信心高。

## F2 — podcast_attempt_adopt 的 feedback-source 分支手寫 safe_next_action,沒有 next_step,交棒身分不在回傳裡
位置:tools_podcast.py:3840-3858,核心 :3846 `"safe_next_action": ACTION_RESUME if needs_rename else ACTION_SERIES`
不變式:gotchas-attempt 紅線①(不准手寫 if/else)、⑪⑫(身分要進公開回傳)。artifact 分支 :3652-3672 照規矩做。
後果:(1) needs_rename=False → ACTION_SERIES 是死路(_attempt_capabilities 會算出 ACTION_RESUME;對 podcast_episode 建的 attempt 呼叫 podcast_series 會被 _assert_series_owns_attempt :1642 raise);(2) needs_rename=True → 回傳無 artifact_id/safe_next_artifact_id;(3) 無 next_step 欄位(check_skill_sync.py:55 宣告 adopt 停點都回它)
測試:無(test_series_durable_resume.py:500-510 走此分支但只斷言 feedback_source_id)
嚴重度:P2。信心高。

## F3 — ManifestStore._validate 的「指標指向 tombstone」檢查沒掛 on_write,讀取端也會炸
位置:manifest_store.py:242-247(與 :234-241)
不變式:_validate docstring :148-156(讀取寬鬆、寫入 fail-closed);:242 註解自稱「寫入時」但條件沒 on_write
實測:on_write=False 與 True 都 RAISE「output_attempt_id points at retracted attempt a1」
後果:手改/legacy manifest 一律 read() 炸,retract/reconcile/backfill 全打不開
測試:無
嚴重度:P3。信心高(已實測)。

## F4 — _run_episode 的 finalize 例外處理器只接 Exception,姊妹分支(dispatch)接 (Exception, CancelledError)
位置:tools_podcast.py:2725 vs :2666
不變式:ADR-0009 v0.9.12 補記;_atomic.prepared_replacement:79 用 BaseException
後果:finalize 期間 client cancel → 續跑 payload(resume 五參數 + next_step)沒附上,呼叫端拿裸 CancelledError
測試:無
嚴重度:P3(manifest durable,reconcile 救得回)。信心高。

## F5 — podcast_attempt_retract 冪等重呼零變更也發 store.update,平白 bump revision
位置:tools_podcast.py:3927-3948 + :4163
不變式:_assert_source_cleanup_done docstring ④(零變更路徑一次 store.update 都不准發)
後果:空寫撞掉別人的 discovery CAS,強迫重算並丟 settled_attempt_ids(:2155)
測試:無
嚴重度:P3(自愈,5 次重試)。信心中高。

## 刻意排除(已查)
- reconcile 四回傳點鎖外重算 caps(gotchas F7 只記不改)
- abandon_in_flight 永久未解決(F8 刻意)
- standalone prior_mp3_path 不冪等(best-effort)
- _run_episode:2686 手寫指引句(不衝突)
- _create_audio_attempt 兩分支 / _ensure_resume_attempt 兩分支 guard 都齊
- _dispatch_audio_with_failover 兩呼叫端一致
