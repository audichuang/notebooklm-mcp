# R7 審查者:測試品質與 tripwire(10 條;原始完整回報見 R7-test-quality-raw.txt)
總評:無 spec-less Mock、無裸 assert result、getsource 10 處全在 _web.*/_client_assembly、_text/notes_html false-positive 案例齊。缺口集中在「多路徑不變式只鎖一條」。

F1 P1 對映缺漏:_ensure_resume_attempt 兩分支的標題閘(tools_podcast.py:1157-1159 claimed、:1227-1229 new)零測試;唯一標題閘測試 test_attempt_retract.py:316 打的是 _create_audio_attempt:1032。"resume cannot rename an episode" 在 tests/ 零出現。
F2 P1 假綠:_promote_attempt_output 四呼叫點(2714/3297/4585/5029)只有 _run_episode 那條驗回傳值(test_attempt_retract.py:195);resume/series 三條只驗 manifest。gotchas 明文「兩個出口都要斷言」。
F3 P1 對映缺漏:_reset_attempt_for_resend 的 remote 重設零斷言;test_audio_attempts.py:565-573、test_quota_failover.py:426-427 fixture 帶 remote.status="failed" 但只看 dispatch。
F4 P1 對映缺漏:audio_finalize.py:695-700 mp3 下載 post-commit dir fsync 容忍零測試(四站點三有一無)。後果:NAS 上每次重下載同行失敗、永遠 finalize 不了。
F5 P1 skip 過寬(第三份手抄):test_attempt_capabilities.py:437-448 guard_trips 手抄可達性,不用 _NEVER_DISPATCHED/_TERMINAL_REMOTE(:157/:159);跳掉 12 格正是 authorization_basis=None 的格;守門(4458-4485)不看 dispatch/remote。今天不可達,結構性風險。
F6 P2 skip 過寬:test_window_closed_narrative_… 掛 600 格只 15 格跑到斷言;:654/:656 skip 條件讀 SUT 輸出(回歸變 skip 非紅);:638 白名單是 :392-394 inline tuple 第二份拷貝。
F7 P2 tripwire 承諾不成立:test_tool_annotations.py:66 docstring 說忘加 annotations 要紅,但五條只收 `is True`,annotations=None 的新工具全過。
F8 P2 mock 失真+名實不符:conftest fail_generate 造 task_id=""/is_failed=True 是 0.7.x 契約;test_errors.py:90 註解「NOT by raising」與 test_contracts.py:235 相反;五檔靠它驅動終態分類。
F9 P2 mock 失真:FakeArtifacts.rename(:192-200)/FakeSources.rename(:360-366)查無回 None,實裝 0.8.2 raise ArtifactNotFoundError/SourceNotFoundError;無測試餵此形狀。影響中低(有 get_or_none preflight)。
F10 P3:check_skill_sync.py 208 行零測試(fail-closed);test_artifact_rename_is_fire_and_forget 名字講的是 0.8.0 後不成立的性質。

597 skip:實跑 :444 12 / :638 300 / :654 240 / :656 45;585 格在現行實作下真不可達(抽樣人工推導確認)。
tripwire 大致問對問題;mock 錯誤契約大致正確(偏差只有 F8/F9)。
無測試守的不變式表:_ensure_resume_attempt 標題閘、promote 回傳值三路徑、_reset remote、mp3 fsync、failover callback 完整性(ADR 承認)、E2E 自足性紅線(無機械 tripwire)、check_skill_sync 自身。
