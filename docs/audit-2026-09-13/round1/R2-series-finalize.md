# R2 審查者:series/resume/finalize/capabilities(5 條)

## P1 retract 一集中段的 series attempt 後,caps 交棒給 podcast_series,而它拿「後面各集的回錄」重生那一集
位置:tools_podcast.py:180(_regeneration_entry_point 回 ACTION_SERIES)、:209(_regeneration_hint 對 series 形狀回 "")、:712-716/:737(_attempt_next_step post_retract/is_output)、:488(caps safe_next_action=regeneration_entry)、:5046(_run_episode source_ids 未傳)
不變式:_sources.py:1-8 + design-notes「series 不開 source_ids 的唯一安全論證是往前跑」;AGENTS.md/app.py instructions「重生不指名 = 讀整本,後面各集回錄洩進」;gotchas-attempt 紅線①
序列:series 跑完 EP01-05(各集回錄)→ QA 拒 EP03 → retract(EP03) 並 source_delete → 冪等重呼 retract:covered 空 → safe_next_action="podcast_series"、regeneration_source_ids=None、hint 空字串 → 重呼 series:EP03 output/active 已 pop、attempts 非空 → 三分支跳過 → :5041 refuse_if_too_many_sources(<10 放行)→ :5046 _run_episode(source_ids=None) → 讀整本含 EP04/EP05 回錄 → complete=True 無訊號
根因:白名單判準問「settings 是不是 series 生得出來」,但交棒真正要保證「series 能不能重建同一組來源」;_regeneration_hint 對 resume 形狀說「⚠️ 要自己指名 source_ids」(:229-233),對 series 形狀回 "",兩者 regeneration_source_ids 都是 None
測試:test_attempt_capabilities::test_series_is_only_ever_offered_for_settings_series_can_reproduce 只鎖 settings;test_series_durable_resume 無「retract 中段集 → 重呼 series」
信心:機制高;是否「已知取捨」不確定(若是,hint 這格必須說話)

## P2 已完成集的遠端 artifact 不見時,podcast_series 裸拋 RuntimeError,無結構化停點無 safe_next_action
位置:audio_finalize.py:574-577(raise RuntimeError "cannot be verified in the remote list")、tools_podcast.py:4544-4578(series output-repair except RuntimeError 只翻譯一種,最後 raise)
序列:EP03 已完成發布,Studio artifact 被刪 → 重呼 series → finalize_attempt 早退要求 _artifact_title_state is True,不在清單回 None → 跳過 → :574 raise → :4556 只在 source 也不見時回 continuity_unverified;source 在 → :4578 裸拋 → 前面集的 run_results 一起消失
第二觸發:audio_finalize.py:855 "feedback source ... postcondition is not satisfied" 同路
逃生口:start=N+1 可跳但裸例外沒說;resume 撞同一行 :574
測試:test_finalize_idempotency::test_completed_resume_fails_if_feedback_source_was_deleted 只覆蓋 source 不見;title drift 測 is False 非 is None
信心高

## P2 兩個 _TRANSIENT_TRANSPORT_ERRORS handler 對 reconciliation_ambiguous 給 podcast_series 且不帶候選
位置:tools_podcast.py:4964-4985(active attempt finalize)、:5109-5139(_run_episode 圈);對照 :4986-5028 except RuntimeError 正確回 ADOPT + candidate_source_ids
不變式:gotchas 紅線十二;podcast_series docstring :4263-4266;check_skill_sync REQUIRED_CONTRACT_TERMS 鎖 candidate_*
序列:上一輪已寫 reconciliation_ambiguous + candidate_source_ids → 這輪 sources.list 丟 ConnectionError → transient handler observed="reconciliation_ambiguous" 但 safe_next_action=ACTION_SERIES、extra 空 → 讀 candidate_source_ids KeyError
可恢復(重試一次拿到正確停點);:3149-3153 F6 只補 reconcile 兩格沒補 series transient 兩格
測試:無
信心中高

## P3 podcast_attempt_adopt feedback-source 分支手寫 safe_next_action(與 R1-F2 同一條,獨立收斂)
位置:tools_podcast.py:3838-3858,:3846。補充:attempt_id 在此路可為 None(legacy_output_unverified 只帶 feedback_source_id)→ 回傳兩個身分都沒有
測試:test_series_durable_resume::test_adopt_explicit_source_id_migrates_legacy_output_without_upload 只走 needs_rename=False
信心高

## P3 podcast_episode_resume 是四個吃 wait_timeout 的公開入口裡唯一沒驗證的
位置:tools_podcast.py:3240-3262;_validate_wait_timeout 呼叫點只有 :2828/:2959/:4276。resume 不 claim dispatch 不寫 manifest,只是 nan/inf/0/負數直交 wait_for_completion。docstring 寫「三個」入口,實際四支。
測試:無。信心高,影響低。

## 乾淨的
(4) 已完成集不重生:series output 分支只 drift 複驗;finalize 早退;_promote_attempt_output ownership guard;_ensure_resume_attempt 兩分支都有 output guard;四個 finalize 呼叫點(2706/3289/4544/4945)後都接 promote 且用同一顆 mutate 過的 output dict
(6) except Exception 吞 CancelledError:CancelledError 是 BaseException,except Exception 不會吞它。⚠ 這與 R1-F4 的判讀相反 —— R1 說 :2725 只接 Exception 所以 CancelledError 不進去、續跑 payload 沒附上(這是「沒接住所以沒補指引」不是「吞掉」,兩者其實一致:都同意 CancelledError 穿過 :2725)。audio_finalize.py:776 與 :676 finally 兩點顯式接住;無裸 except、無 wait_for/shield;唯一 except BaseException(:2580)清理後 raise
(5) QA gate:ADR-0004/0005/0006 已 superseded by ADR-0007,MCP 內無強制 QA gate;ADR-0002 checkpoint 在四入口成立,attempt["finalize"] 讀寫全走 audio_finalize._record
_safe_next_target 本身乾淨(五分支不誤配,regeneration_source_ids 整組換,遞迴一層)
丟棄:podcast_series:4703-4713 defers_to_output_guard 條件比註解寬("failed" ∈ _TERMINAL_REMOTE 使 not_accepted 也讓位),但構造不出前置狀態
