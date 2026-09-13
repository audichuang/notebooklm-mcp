# 驗證 D:薄包裝四條(R5)—— 3 REFUTED、1 半 CONFIRMED

R5-P1-1 research NOT_FOUND → **REFUTED**。審查者漏看 _research.py:243 `_wait_observed_status` 鉤子,WebResearchAPI(_web/research.py:641-645)覆寫把 NOT_FOUND 中和成 NO_RESEARCH 再建 timeout 訊息;docstring「never returns NOT_FOUND」。實跑 timeout → 診斷有加上。fake 換成實裝 ResearchTimeoutError 41 passed。acceptance-v0.9.14-findings.md:689 是 0.8.1 真帳號量到 no_research。邊界:AndroidResearchAPI 無此覆寫但 _client_assembly.py:481 綁 Web。
  附帶新缺口(tripwire):_explain_no_research 承重於上游私有覆寫,test_contracts.py:745 只釘 enum 成員,未釘中和行為 → 建議補對 `notebooklm._web.research.WebResearchAPI._wait_observed_status` 的 getsource 斷言。P3。

R5-P2-1 _TEXTLESS_BLOCK_KINDS → **REFUTED(已載明)**。CHANGELOG.md:1063-1065 明文 IMAGE 情境刻意擋、訊息措辭自認不精確;THOUGHT 與 CODE_BLOCK 上游同類(carry text on wire、not decoded),FINDING-E 事故觸發物正是 CODE_BLOCK,擋 THOUGHT = 正確套用。無上游證據 chat answer 會夾帶這些 kind(proto 標 schema 推導、reportDoc)。P4 nit。

R5-P3-1 gRPC 5 vs 7 → **REFUTED**。真帳號兩次量到 7(acceptance-v0.9.3:164、v0.9.14:226);rpc_code=5 原樣拋是明文設計且突變驗證過(v0.9.14:570「不可被吞成權限問題」)。往這方向補會回歸判別力。

R5-P3-2 source_ids=[] → **半 CONFIRMED,只剩 chat_ask**。source_search 上游 _sources.py:110-112 明文「None or empty = 搜全部」,有定義。chat_ask:_chat.py:316 只判 is None,[] → nest_source_ids([],2) 零來源請求,語意未定,最壞無根據答案 → set_description → RSS。維持 P3。
  修法落點:tools_basic.py:744 用既有 to_source_ids 包一層;source_search 不動。測試:tests/test_source_selection.py 照 :37 範本。
