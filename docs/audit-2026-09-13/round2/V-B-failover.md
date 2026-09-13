# 驗證 B:failover/pool 四條(R3)—— P1 事故敘事 REFUTED、契約層 CONFIRMED P3;其餘三條 P3 CONFIRMED

R3-P1-1 429 分類 → **事故層 REFUTED、契約層 CONFIRMED,P1→P3**。
  (a) TransportRateLimited 唯一生產路徑 errors.py:126-133 是收到 HTTP 429 回應之後(請求已上線);傳輸鏈無送出前限流器。→ v0.9.26 第一個理由「被擋在 handler 之前」是推測冒充查證,附的行號 executor.py:328 對「handler 有沒有跑過」一字未講。
  (b) _idempotency.py 是按翻譯來源做型別分組(「might」),對 RateLimitError 無個別實證;idempotent_create 三個呼叫端全非 artifact 生成;policy.py:492-500 CREATE_ARTIFACT 的 PROBE_THEN_CREATE 只做「關內層 retry」,probe「in a follow-up」尚未實作;notebooks.py copy() 是 NON_IDEMPOTENT_NO_RETRY 另一家族。→ R3 三處新證據全屬別的 RPC 家族。
  (c) artifacts.py:67 with_rate_limit_retry 在 0.8.2 完整存在,docstring「Fall back to USER_DISPLAYABLE_ERROR sentinel when the exception carries no rpc_code」= 上游明確預期無 rpc_code 的 429 進迴圈並原地重送;_app/generate_retry.py:115 實際在用。→ v0.9.26 第二個理由成立,是真正撐住結論的那條。
  (d) 剩下真的一半:_failover.py:39-46 把 RateLimitError 定義成 USER_DISPLAYABLE_ERROR 拒絕但 except 抓型別;tools_podcast.py:2660 硬寫「沒建 artifact、不多燒配額」在 429 臂是「幾乎必然」非契約。實害:傳輸 429 綁 IP/host 不綁帳號,換帳號是錯的解藥 → 一次 429 風暴讓 N 個帳號逐一撞同一限流器、N-1 筆假稽核、整 pool 冷卻 600s。
  判別特徵實測:decoder rpc_code='USER_DISPLAYABLE_ERROR' retry_after=None;transport rpc_code=None retry_after=37(無 Retry-After 標頭時也 None → 不可靠);__cause__ 是實作細節。**只有 rpc_code 可靠**(前例 _errors.py:83)。
  修法落點:_failover.py:175 except 內加 rpc_code 判準(不動 REFUSED_WITHOUT_DISPATCH tuple,:2654/4843/5091 與測試釘著);修 :2660 契約文字與 :39-46 docstring。測試:test_quota_failover.py 新增「rpc_code=None 的 429 不 rotate」。
  (e) conftest.py:702 fake → **CONFIRMED P3 獨立**:RateLimitError("每日配額已用盡") rpc_code=None retry_after=None __cause__=None —— 兩種生產者都不是;整套 failover 測試跑的是 decoder 產不出的形狀,一旦加 rpc_code 判準會靜默翻到 transport 分支。修法:conftest.py:702 補 rpc_code="USER_DISPLAYABLE_ERROR"。
  給 CHANGELOG:v0.9.26「查證後不成立」的記法要修(結論對、第一理由錯、第二理由才是實錘),否則下個獨立審查者還會再提。

R3-P3-1 record 拋例外 → **CONFIRMED P3**(fail-safe)。重現:OSError(ENOSPC) → on_clean_refusal 未觸發、attempt 停 prepared(:919)、_ACTIVE=1 指 B(從未 dispatch)、_COOLING={0:…}。ADR-0010「不留幽靈紀錄」沒破;呼叫端泛用 except 導向 acceptance_unknown 指引方向 fail-safe。修法:_failover.py:178 與 :221 **兩處** rotate_for_quota 都要包(:221 是 0.7.x status 分支)。測試:test_quota_failover.py :653 旁。

R3-P3-2 BACKEND 護欄 → **CONFIRMED P3**。app.py:69-75 註解自己否掉「inline 專屬」;SDK client.py:629 逐字確認;resolve_backend_preference(None, env="android") → android。範圍收窄:多帳號分支蘊含 inline,只有 app.py:410 單 client 分支暴露。修法:app.py:395 與 :410 兩個 from_storage 顯式 backend="web"。測試:test_client_pool.py :375 旁。

R3-P3-3 ADR 冷卻方向 → **CONFIRMED P3 純文件**。600s < 1560s;runtime.py:35 實值 600.0,:32-34/:172-174 中文方向正確(保守=短,成本不對稱)。修正 R3 次要主張:紅線④對任何有限冷卻都成立;真正意義是 600s 短於實測恢復時間。修法:ADR-0010:17 above → well inside。
