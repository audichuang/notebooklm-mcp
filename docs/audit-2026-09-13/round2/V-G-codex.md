# 驗證 G:Codex 七條 —— 6 CONFIRMED、1 半(#7);無整條 REFUTED(腳本 verify-G/,VERDICTS.txt)

#1 legacy output 被不同 artifact 經 resume 無審計取代 → **CONFIRMED P1 維持**。
  攻擊不成立:test_reliability_followups.py:354 只斷言 feedback_source_id 換了 + add_file==1,鎖的是 :1238「legacy source 不得帶到別的 artifact」閘,resume 成功只是背景;tools_podcast.py:3958-3965 註解自己命名為 retract amendment (2),寫明「只有 _ensure_resume_attempt 新建分支能繞過 has_hard_output_evidence guard」、來源是真實事故 —— 是修復出口非授權;_create_audio_attempt:959-963 同一集會拒「refusing to silently overwrite」。git:07ce43c 動五個測試檔沒動這支(漏掉的第三支)。
  重現:artifact_id A→replacement、output_attempt_id 新建無 tombstone、feedback_source_id src-1→src-2、previous/pending 皆 None、S1 仍在 notebook。
  修法落點:_ensure_resume_attempt 新建分支 :1192-1200 guard,條件「有 legacy 硬證據且 legacy_artifact_id != artifact_id」(同一顆 legacy artifact 必須繼續放行,#2 靠它)。測試:改寫 test_reliability_followups.py:354 並保留原閘。

#2 legacy_audio_missing → resume 重複上傳、遺忘 S1 → **CONFIRMED P1 維持**。
  seed 路徵 :1234-1250 要三者齊備(feedback_source_id + feedback_source_adopted_at + legacy_artifact_id==artifact_id);停點 :4620-4630 實際回 ACTION_RESUME 且要求 source_verified —— series 用 id+label 驗過 S1,resume 卻要一個只有 adopt(:3770)會寫的人工標記(_promote_attempt_output:1968 明講不寫)→ 沒跑過 adopt 的真 legacy manifest 預設走壞的那支。CHANGELOG 對 legacy_audio_missing / feedback_source_adopted_at 零筆。
  重現:adopted_at=False → add_file 1、src-1→src-2、兩筆同名 media;adopted_at=True → add_file 0。
  修法落點:tools_podcast.py:4620-4630 停點改交棒 adopt 或 adopt→resume 串接;不要弱化 :1238 seed 條件。測試:test_series_durable_resume.py(legacy_audio_missing 零覆蓋)。

#3 目標集 unresolved 義務屬舊 notebook 時 gate 略過 → **CONFIRMED,P2 下緣/P3 上緣**。:2270 註解「目標這一集自己的另外擋,見下」但「見下」只覆蓋具名義務。可達性:notebook_id 不在 _RETRACTED_EPISODE_KEYS,_create_audio_attempt:955 與 _ensure_resume_attempt:1271 都擋 belongs to another notebook → 公開工具無門,只有 fixture/手改到得了(test_attempt_retract.py:1493/:2296 都是 ManifestStore.update 直搬)。重現:CONTROL 同本 fail-closed;TARGET nb-new 放行且 generate_audio+1、義務仍未結案;SIBLING 具名擋。順帶:具名那半自己也是死路(搬到 nb-new 後義務無入口結案)。修法:tools_podcast.py:2282 target_elsewhere 也掃 unresolved tombstone,連同 discharge path 一起想。測試:test_attempt_retract.py :1493 旁。

#4 併發 finalizer 晚到失敗倒退 checkpoint → **CONFIRMED,P2→P3**。CAS 反駁不成立:audio_finalize._mutate 走 store.update 不帶 expected_revision,upload_unknown(:742)/download_failed(:666)盲寫。但後果高估:source_id 不清 → unresolved_upload_descriptor None → caps feedback_upload_unresolved=False、safe_next 仍正確;retract stale_source_ids(:4081)照收;下次 finalize/series 自癒(零重複上傳零重生)。殘留:此集再無人 finalize 時永久留假 acceptance_unknown。修法::742 與 :666 兩 callback 只准從自己 claim 的 dispatching 降級。測試:test_finalize_idempotency.py:205 補時序 + 最終 status。

#5 reconciliation_ambiguous 中央 caps 指向自迴圈 resume → **CONFIRMED P2 維持,建議排最前(第十三次現形)**。
  test_attempt_capabilities.py:156 docstring「resume 是唯一能把 source 身分認回來的路」對 ambiguous 是假的,且 fixture 沒有 candidate_source_ids → 鎖錯答案永遠綠(問錯問題的 tripwire)。candidate_selection_required 是呼叫端旗標只在 artifact 歧義(:3116/:3140)設 True。:4994 手寫 if/else 本身就是紅線 :10,之所以必須手寫正因中央給的是錯的。
  重現:caps safe_next=resume、無 candidate 欄位、next_step「它會把那筆 source 身分對回來」;e2e series 停點=adopt+候選 vs caps=resume;resume#1/#2 都 RuntimeError ambiguous,零前進。
  修法落點:tools_podcast.py:491 feedback_upload_unresolved 分支(ambiguous 且候選非空 → ACTION_ADOPT)+ caps 帶出 candidate_source_ids;修好後刪 :4994 手寫分支。測試:test_attempt_capabilities.py:156 fixture 真填 candidate_source_ids + e2e test_reliability_followups.py:410。

#6 adopt 回傳把跨 notebook 義務全標成目前 notebook → **CONFIRMED,P2→P3**。_cleanup_obligations docstring :3394-3404「身分要跟著義務走,不能拿 episode 當下 notebook 回推(盲審 P1)」;retract 姊妹(:435)帶逐筆身分。可達性需先經 #3 的洞。重現:adopt 回傳 S-old→nb-new(錯)vs retract 版 nb-old(對);照做 source_delete no-op 義務永不結案。修法:tools_podcast.py:3853 改由 _cleanup_obligations(current_episode, fallback_notebook=notebook_id) 組(stale_source_ids 欄位名別動,check_skill_sync 鎖著)。測試:test_attempt_retract.py:2474 旁。

#7 process death 後 download temp_path → (b) 重下載 **REFUTED/DESIGNED-AS-IS**(audio_finalize.py:676 finally 註解明寫「下次重新從 mkstemp 開始」是 checkpoint 契約);(a) .part 永久失去引用 **CONFIRMED P3**(gotchas-files/CHANGELOG 零筆;:634 不讀既有 temp_path)。重現:resume 後 temp_path=None、.ep01.KILLED.part 仍在。修法:audio_finalize.py:634 mkstemp 前 unlink 既有 temp_path(~3 行)。測試:test_finalize_idempotency.py 家族。
