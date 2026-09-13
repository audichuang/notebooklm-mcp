# R3 審查者:pool/lifespan/cookie/failover(4 條)

## P1-1 REFUSED_WITHOUT_DISPATCH 把兩個語義相反的 RateLimitError 生產者當同一種
位置:_failover.py:39(假設宣告)、:47(判準)、:175(except)
兩個生產者(實裝 0.8.2):
 (a) _web/wire/decoder.py:816 HTTP 200 + USER_DISPLAYABLE_ERROR,rpc_code="USER_DISPLAYABLE_ERROR",retry_after=None —— ADR-0010 量到的那種
 (b) _web/transport/executor.py:326-342 真 HTTP 429 → TransportRateLimited → RateLimitError,rpc_code=None、retry_after 有值、__cause__ httpx.HTTPStatusError
上游證據:_idempotency.py:59-76 `_RETRYABLE_TRANSPORT_ERRORS = (RateLimitError, ServerError, NetworkError)`「might have committed the write」;_web/policy.py:492-500 CREATE_ARTIFACT 註冊 PROBE_THEN_CREATE 關掉內層 retry;_web/notebooks.py:639-650 copy() 對同組例外 mark_unconfirmed()
序列:generate_audio → 429 → 命中 REFUSED_WITHOUT_DISPATCH → rotate 到 B → 同 attempt_id 重送 → 第二顆 artifact、A 那顆孤兒、配額雙燒
稽核面:describe_refusal:66-67 兩形狀都寫 "RateLimitError",事後分不出
呼叫端:tools_podcast.py:2660 硬寫「沒有建立任何 artifact…不會多燒配額」在 429 臂是假的
測試:conftest.py:702 `RateLimitError("每日配額已用盡")` 無 rpc_code = 傳輸層形狀;test_quota_failover.py:34/66/217/544、test_attachment_failover.py:220/409/729、test_source_selection.py:180/198 全斷言會 rotate;全 repo 無任何讀 rpc_code/retry_after/__cause__ 分流
嚴重度 P1。信心:形狀存在+無分流=高;committed-then-429 真實發生率=中
⚠ 裁決註:CHANGELOG v0.9.26 記載這條「查證後不成立」(429 被限流器擋在 handler 之前;上游 artifacts.py retry 迴圈也對 RateLimitError 重試)。R3 未讀 CHANGELOG 而獨立再提,且引出新證據(_idempotency / PROBE_THEN_CREATE / mark_unconfirmed)。需第二輪專門裁決。

## P3-1 record 拋非 post-commit 例外時,槽位已冷卻、游標已推進,兩個終態 callback 都沒跑
位置:_failover.py:99-113(rotate_for_quota),受害 :178-185
序列:A 拒絕 → rotate_client 冷卻 A、_ACTIVE→B → record() 因磁碟滿/EACCES 拋非 ManifestPostCommitError → 穿出 except → :184 _mark(on_clean_refusal) 未執行 → attempt 沒標 not_accepted;A 冷卻 600s、_ACTIVE 停 B 但沒 dispatch 過 B
測試:test_quota_failover.py:653 只覆蓋 tried 命中
P3(fail-safe 方向)。信心高。

## P3-2 NOTEBOOKLM_BACKEND 護欄掛在 inline_auth 上,但理由非 inline 專屬(補一半)
位置:app.py:48、:69-83、:316(`if inline_auth:` 閘)、:410-412(非 inline 分支 from_storage 沒傳 backend=)
序列:登入機無 NOTEBOOKLM_AUTH_JSON → inline_auth=False → override 跳過 → shell 有 NOTEBOOKLM_BACKEND=android → SDK client.py:629 讀 env 換成 android
測試:test_client_pool.py:420 只 inline;:375 非 inline 不看 override
P3(只打登入/開發機)。信心高。

## P3-3 ADR-0010 v0.9.7 amendment 對 _COOLDOWN_SECONDS 方向寫反
ADR「600s — chosen conservatively above that 26-minute observation」;600s < 1560s。runtime.py:32-33 中文註解方向正確。紅線④立論取決於冷卻期短於一輪 failover。P3。信心高。

## 剔除
auth_probe 漏判 _LoginRedirectError(無具體路徑;429 不會誤判認證失效);憑證洩漏(逐條驗 LEAK: False);_ACTIVE race(37 支全 async、rotate 與 snapshot 間無 await);streamable-http 跨 session(已載明);_MAX_POOL_SLOTS/auth_cli umask;artifact_revise_slide 孤兒 (2)(已載明待確認)
