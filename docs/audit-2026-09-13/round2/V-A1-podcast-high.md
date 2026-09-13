# 驗證 A1:tools_podcast 三條高風險 —— 全部 CONFIRMED(腳本 verify-A1/,VERDICTS.md)

R2-P1 retract 中段集 → series 重生讀整本 → **CONFIRMED,P1 維持**。
  重現:整季 EP01-03 跑完 → retract EP02 → 公開回傳 safe_next_action=source_delete、regeneration_source_ids=None、next_step 尾巴「用 podcast_series 重生。」→ 刪完 → 重呼 series → generate_audio(source_ids=None),dispatch 當下 notebook 含 'EP03 收尾篇' 回錄 → complete=True 無訊號。
  與 R2 出入:冪等重呼 retract 後 safe_next_action 仍是 source_delete(義務要 gate 對帳才掉),交棒由 next_step 尾句完成 —— 不影響裁決。
  文件講反話:design-notes.md:21「往前跑時 ≤ 當集來源」前提失效;skill tool-reference.md:750「回頭重生走 podcast_episode」而工具 next_step 說 series。
  修法落點:podcast_series 每集迴圈頂端、_assert_source_cleanup_done 之後(:4515 附近):「本集無 output 且存在 episode > n 有 output evidence 的列」→ partial(ACTION_EPISODE)。涵蓋全新一集 / failed-removed supersede / not_accepted re-arm 三條;caps 層修不了(只拿單一 row 看不到 sibling)。測試:test_series_durable_resume.py。

R5-P1-2 prior_mp3_path × source_ids 互斥 → **CONFIRMED,P1→P2**。
  重現:指名 ['src-1','src-2'] → add_file ep02.mp3、rename src-3→EP02 → generate_audio(source_ids=['src-1','src-2']),src-3 未進生成輸入 → False。附帶症狀:src-3 無人引用留在 notebook,吃掉 ≤9 一格,之後不指名生成都會讀進。
  降 P2 理由:routed 預設路徑(manifest_path)在 :2830 就 fail-closed 拒 prior,只打 standalone best-effort。skill 兩句話合起來把 host 推向這個輸入(troubleshooting.md:344 + tool-reference.md:747)。
  修法落點:_run_episode :2507-2512 之後、任何副作用之前(兩呼叫端唯一交會點)。測試:test_source_selection.py :841 旁。註腳:_sources.py:110-121 指名分支忽略 pending_uploads。

R1-F1 CAS 衝突後 violations 未修剪 → **CONFIRMED,P2→P3**。
  重現(gate 停在 sources.list await 注入 claim):raise 訊息點名 src-2 叫 source_delete,而 manifest 認定 src-2 已被認領、pending_source_cleanup 無它 → 兩出口矛盾。
  降 P3 理由:公開 API 造不出並行合法認領(adopt identity 守門 :3696-3706 擋、_reconcile_source_upload 只認同 expected_title、義務在時 gate 擋同集第二顆),真實觸發率未證;若窗不可達則 _settle_cleanup_state ownership 重驗是死碼 —— 兩種情況都停在「兩出口不一致」紅線。
  修法落點:_assert_source_cleanup_done :2402 `if violations:` 前用修剪後 discovered 重算。測試:test_attempt_retract.py 緊鄰 test_concurrent_zero_and_positive_reconciliation_never_forgets_the_orphan(:2402 組,helper 已備)。
