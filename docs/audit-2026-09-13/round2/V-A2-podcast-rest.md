# 驗證 A2:tools_podcast 其餘七條 + R1-F3/R4 張力(腳本 verify-A2/,VERDICTS.md)

R1-F2 / R2-P3a adopt feedback-source 手寫 safe_next_action → **CONFIRMED 收窄,P2**。
  重現:needs_rename=False 回 podcast_series,caps 算出 podcast_episode_resume → MISMATCH;無 next_step;needs_rename=True 回傳無 artifact_id(caps.safe_next_artifact_id=task-123)。
  REFUTED 子項:「ACTION_SERIES 是死路被 :1642 raise」—— _attempt_can_adopt_source:3380 前提要求 remote.status==completed → _series_will_redispatch 必 False → 守門到不了,實跑 series 帶不帶 source_ids 都成功。R2「attempt_id 可為 None 兩身分都無」—— legacy 路 needs_rename 結構上恆 False,回 series 是對的。
  額外(兩審查者都沒提):`"complete": not needs_rename` 在 False 時回 true 但 attempt 尚未 promote(無 output_attempt_id/mp3_path)→ host 直接 publish_series 撞 _ensure_local_mp3:178。
  修法落點:tools_podcast.py:3840-3858 adopt_source closure 比照 :3666 回 _attempt_capabilities,外層組 caps + _attempt_next_step。測試:test_series_durable_resume.py(缺 attempt-backed + needs_rename=False)。

R1-F3 _validate tombstone 指標讀端也炸 → **CONFIRMED 機制,P3,修註解不修 code**。git log -S 三字串全命中 07ce43c(v0.3.2)—— retract/tombstone/pop 同 commit 落地,無任何發版會產生此形狀 manifest;唯一產生者是手改 JSON。
  **R1-F3 vs R4 張力裁決**:R4 機制宣稱 REFUTED —— retract 把 output_attempt_id/artifact_id/mp3_path 一起 pop(:4054),_validate:244 在任何 API 可達狀態不會觸發;真正擋 retracted 集上 feed 的是 tools_publish.py:177-181 _ensure_local_mp3 與 :246-250 _mp3_provenance_gap(fail-closed 帶集號)。**但若照 docstring 改成 on_write-only,手改過的那種就零守門**:_mp3_provenance_gap:252 撈到 tombstone 本體,retract 不清 finalize checkpoint,download.sha256 原封 → sha 相符放行;整個 tools_publish + publish/ 無一行讀 retraction。→ 若動 F3,同 commit 必在 tools_publish.py:252 後加 attempt.get("retraction") 判斷。測試:test_publish_tools.py。

R1-F4 finalize handler 只接 Exception → **CONFIRMED P3**。重現:CancelledError str='' 無 resume payload;manifest durable(accepted, task-123),事後 caps 正確。耐久性已由 audio_finalize:677 finally + :778 覆蓋。CHANGELOG:2443 同形狀修過四處。修法:tools_podcast.py:2725 一處。測試:test_audio_attempts.py。

R1-F5 retract 冪等重呼 bump revision → **CONFIRMED P3**。revision [15,16,17,18],扣 revision 後 manifest 完全相同。不變式引用是外推(docstring ④ 射程在 gate 路徑);retract 不特殊,ManifestStore.update:53-57 無條件 +1。修法落點在 manifest_store.py:44-58(candidate 扣 revision 與 current 相同就不寫);⚠ 動到 test_manifest_store.py:180 既有斷言。

R2-P2a artifact 不見裸拋 → **CONFIRMED P2 維持**。重現:series 與 resume 都 RAISED "cannot be verified in the remote list",訊息無 start= 逃生口、無 safe_next_action;實測 start=2 有效但例外一字未提 → 整季永卡。修法:audio_finalize.py:572-576 改具名例外(比照 TerminalGenerationError),由 tools_podcast.py:4544-4578 既有 except 階梯翻結構化停點(resume 也吃得到)。測試:test_finalize_idempotency.py(缺「source 在、artifact 不在」格)。

R2-P2b transient handler reconciliation_ambiguous → **CONFIRMED 事實,P2→P3**。紅線十二不適用(回 series 原參數重呼自足);docstring :4262-4264 兩條件並列(ambiguous 且 action=adopt)未被逐字違反。缺陷收窄:observed_state 報歧義而同 payload 給不出化解工具,兩欄位矛盾;keyed on observed_state 的 host KeyError。順帶:也無 next_step;R2「重試一次拿到正確停點」未驗成(08b 重呼得 acceptance_unknown/series,fake artefact 或真行為未確認)。修法:tools_podcast.py:4964-4985 observed 計算 + :5109-5139 同形姊妹同 commit。測試:test_series_durable_resume.py。

R2-P3b resume wait_timeout → 「沒驗證」CONFIRMED;「漏數一支」REFUTED(docstring「三個」數的是會持久化的入口,resume 不走 _claim_prepared_dispatch,dispatch 無 wait_timeout key,manifest 無 NaN)。**P3-**。殘留:nan 流進 wait_for_completion 永不逾時。修法:tools_podcast.py:3263 附近一行。測試:test_resume.py。
