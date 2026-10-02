# notebooklm-mcp v0.9.14 真實環境驗收 — FINDINGS

驗收日期:2026-08-15
工作區:`$HOME/research/audiskill/nblm-acceptance-v0.9.14`
帳號 pool:Doppler `notebooklm` / **`-c stg`**(測試帳號 pool,CLAUDE.md §一)
實裝:`notebooklm-mcp 0.9.14` / `notebooklm-py 0.8.1` / `mcp 1.29.0`
直譯器:`$HOME/.local/share/uv/tools/notebooklm-mcp/bin/python3`

---

## Phase 0 — 本機檢查(不打 RPC)

`bash local-checks.sh` → **通過 18 / 失敗 0**。

硬關卡兩條都綠:

- **§0 實裝版本**:`notebooklm-mcp 0.9.14`、`notebooklm-py 0.8.1`(= `../notebooklm-mcp/pyproject.toml`
  的下界,非硬編)、`mcp 1.29.0`(與 lock 相同)。
- **§8a rotation flock**:`expired` 與 `wrong-scope` 兩個 case 的**對照組(不持鎖)都確實觸發
  了 `_attempt_rotation`**(所以斷言不是空虛的),持鎖後兩者皆零 POST 且 cookie 值未被改動;
  `_PlatformLockGateway.acquire` 被強制回 `UNAVAILABLE` 的 fail-open 情境下,in-process
  `threading.Lock` 仍擋住。

其餘全綠:`_sync_auth_env` 已移除且 `snapshot()` 無 suspend 點;
`_dispatch_audio_with_failover` 回三元組;空值 PSIDTS 在落檔前 raise;憑證檔 `0o600`;
`abandon_in_flight` 預設 `False`;`notebook_access_denied` 停點存在;
`_has_sufficient_permission` 的 VIEWER/OWNER/大小寫判準正確;上游 sharing API 未漂移;
**35 個 MCP 工具**;`set_users` 單趟 + `notify=False` + `EDITOR`;
`answer_document.render()` 保留段落分隔而 `.text` 不會;`is_permission_denied` 走
`normalize_rpc_code` 且保留 `isinstance(ClientError)` guard;`Notebook.role`/`is_owner` 不變式一致。

**§8z skill 快照與上游逐字一致**(上游 HEAD `4d88ca9`)—— 驗到的是最新文件,不是落後版本。

---

## Phase 1 — 協定層

| 對帳項 | 結果 |
|---|---|
| `claude mcp list` 顯示 `notebooklm` | ✅ `doppler run -p notebooklm -c stg -- nblm-mcp --transport stdio - ✔ Connected` |
| 工具數 35 | ✅(local-checks §7 由實裝直譯器數出 35) |
| `IncompleteFieldDefinitionWarning` 在 stderr 且不影響協定 | ✅ 見下 |
| 唯讀工具回合法 JSON | ✅ `notebook_list` 回 3 本 |

`IncompleteFieldDefinitionWarning` 的實際文字(出現在 **stderr**,`2>&1` 才看得到;
stdio session 全程正常):

```
.../pydantic_settings/sources/utils.py:47: IncompleteFieldDefinitionWarning:
Field 'lifespan' has an incomplete definition: its annotation contains an unresolved
forward reference, so settings sources may fail to correctly resolve its value.
```

`notebook_list` 實際回傳(這輪開工前的既有狀態,全是**前輪驗收遺留**):

```json
{"notebooks": [
  {"notebook_id": "35820537-f970-4a60-93f0-f93dd087670b", "title": "v0.8.1 驗收 D —— 刻意不分享(SDK 直建)"},
  {"notebook_id": "a2390f53-0806-49dd-922c-e2b56a644483", "title": "v0.8.1 驗收 —— 重送 failover 與自動分享"},
  {"notebook_id": "bd73e28b-7211-402b-8852-a2127781c102", "title": "v0.8.0 驗收 —— 配額 failover"}
]}
```

> 📌 第一本「**刻意不分享(SDK 直建)**」是前輪為了 2c-3 造出來的,現在是**現成的權限不足素材**
> —— Phase 2c-3 直接拿它做,不必另外建。

---

## Phase 2 — 認證層

### 2a. stg 憑證形狀

**FINDING-A(README 的腳本掃描範圍不足)**:README §2a 的 python 片段寫死 `range(1, 8)`
(只掃槽位 1..7),但 **stg 實際有 9 個槽位**。第一次跑只看到 7 個,是把
`NOTEBOOKLM_AUTH_JSON_8/_9` 整段漏掉。改成 `range(1, 21)` 後才對得起來。
→ 收工時要把 README 的 `range(1, 8)` 修掉(prd 若也超過 7 槽會踩同一顆)。

9 個槽位的 `__Secure-1PSIDTS`(**全部 routable**):

| 槽位 | env key | email | domain | expires | value_len |
|---|---|---|---|---|---|
| 1 | `NOTEBOOKLM_AUTH_JSON` | `pool-account-1@example.invalid` | `.google.com` | +358.5d | 77 |
| 2 | `NOTEBOOKLM_AUTH_JSON_2` | `pool-account-2@example.invalid` | `.google.com` | +358.5d | 77 |
| 3 | `NOTEBOOKLM_AUTH_JSON_3` | `pool-account-3@example.invalid` | `.google.com` | +358.5d | 78 |
| 4 | `NOTEBOOKLM_AUTH_JSON_4` | `pool-account-4@example.invalid` | `.google.com` | +358.8d | 77 |
| 5 | `NOTEBOOKLM_AUTH_JSON_5` | `pool-account-8@example.invalid` | `.google.com` | +358.8d | 78 |
| 6 | `NOTEBOOKLM_AUTH_JSON_6` | `pool-account-6@example.invalid` | `.google.com` | +358.8d | 77 |
| 7 | `NOTEBOOKLM_AUTH_JSON_7` | `pool-account-9@example.invalid` | `.google.com` | +358.8d | 78 |
| 8 | `NOTEBOOKLM_AUTH_JSON_8` | `pool-account-7@example.invalid` | `.google.com` | +358.5d | 77 |
| 9 | `NOTEBOOKLM_AUTH_JSON_9` | `pool-account-5@example.invalid` | `.google.com` | +358.5d | 77 |

**真 RPC(`get_account_email`)9/9 全部成功** —— 沒有任何槽位退回 `#N`。
走的是我們自己的 `app._lifespan`(持有 rotation flock),不是 SDK 直建 client。

`stg` 與 prd 的形狀**不同**:prd 槽位 1(`pool-account-11@example.invalid`)的 PSIDTS scope 在
`.youtube.com`,stg 沒有這種槽位。

### 2b. pool 啟動:零 RotateCookies

| 對帳項 | 結果 |
|---|---|
| routability warning | **沒有出現** —— 與 2a「9 槽全 routable」一致(該閉嘴時有閉嘴) |
| 訊息洩漏 cookie 值 | 無(整段 stderr 只有 pydantic 那行) |
| Doppler 憑證被改動 | **沒有**:lifespan 正常結束後重掃,9 槽的 `domain`/`expires`/`value_len` 逐欄一致 |
| 憑證目錄權限 | `drwx------`(0700);內含 `slot-N.json` 與 `.slot-N.json{,.rotate}.lock`,全 `-rw-------`(0600) |
| 正常結束後殘留 | **清乾淨**:2a 的 lifespan 跑完沒有新增任何 `/tmp/notebooklm-mcp-auth-*` |

**判別力聲明**:stg 9 槽全 routable ⇒ 「沒看到 RotateCookies」在**自然狀態下沒有判別力**。
單靠 2b 這一段,結論是 **inconclusive**;真正的判別實驗見 2b-X(人為 not-routable,真憑證真網路)。

**補驗:stdio 正常結束(stdin EOF)的憑證清理** —— AGENTS.md 說這是「唯一實測可靠」的那條路徑,
但 README §2b-3 沒有單獨驗過:

```
啟動前殘留目錄數: 9   → doppler run … nblm-mcp --transport stdio < /dev/null (exit=0) → 結束後: 9
```

✅ **沒有留下新目錄**。(相對地,`claude mcp list` 的健康檢查每跑一次就留一個含真憑證的
目錄 —— 見收尾段的殘留清單。)

**順帶實測到 README 標為「推導不是實測」的那一條**:同一次啟動的 stderr 顯示
`HTTP Request: GET https://notebook.google.com/ "HTTP/1.1 302 Found"`
→ **0.8.1 的預設 host 確實已經是 `notebook.google.com`**(不是 `notebooklm.google.com`),
而**既有 Doppler 憑證直接通用**,9 槽全部照常啟動、`get_account_email` 全成功。
`REQUIRED_COOKIE_DOMAINS` 兩邊都在的推導**成立**,不再只是推導。

### 2b-X. ⭐ flock 判別實驗:真憑證 + 真網路(README Phase 2 的第一目標)

stg 天生沒有 not-routable 槽位,所以在**本機 shell 的 env 層**人為造一個(CLAUDE.md §五;
**Doppler 一個字沒動**):取槽位 3(`pool-account-3@example.invalid`,非付費兜底槽)的真憑證,
**只把 `__Secure-1PSIDTS` 的 `domain` 改成 `.youtube.com`,值一個字不改**。

安全網 + 觀測點:`notebooklm._auth.psidts_recovery._attempt_rotation` 換成記錄器 ——
就算防線破了也不會真的重鑄帳號憑證,而「被呼叫幾次」正好就是判準。

| | `_attempt_rotation` 被呼叫 |
|---|---|
| **對照組**(不持鎖,`build_httpx_cookies_from_storage` 直接載入) | **1 次** ← 判別力成立:這份**真**憑證確實會觸發 heal |
| **實驗組**(真 `app._lifespan`,持有 rotation flock) | **0 次** ✅ |

同時對帳到的事實:

- **warning 出現且指名槽位、不含任何 cookie 值**,與 prd 實跑過的形狀逐字一致:
  > `[WARNING] NOTEBOOKLM_AUTH_JSON_3 的 __Secure-1PSIDTS 已過期或 scope 無法送到 accounts.google.com;0.8.1 會觸發 inline RotateCookies heal,但 _lifespan 的 rotation flock 會擋住 POST。請更新 Doppler 憑證,並執行 scripts/sync-auth.sh。`
- **該槽位的真 RPC 仍然成功**(`pool-account-3@example.invalid`)—— 與 prd 槽位 1「靠 SID 等其他 cookie 撐」
  的形狀一致;9 槽全部仍取得到 email。
- **Doppler 憑證未被改動**:實驗後重掃 9 槽,`domain`/`expires`/`value_len` 逐欄不變。
- 沒有新增 `/tmp/notebooklm-mcp-auth-*`(lifespan 正常結束清乾淨)。

→ **README Phase 2 的第一目標達成**:flock 在真實憑證、真實網路下擋住了 inline heal,
而且對照組證明了這個結論不是「沒看到問題」的假成立。

### 2c. 三種帳號狀態走三條不同的路

#### 2c-1 認證失效

**第一次嘗試沒有判別力,如實記錄**:只把槽位 1 的 `SID` 值改掉 → `auth_check` 仍回
`{'ok': True, 'notebooks': 3}`,游標 `_ACTIVE=0` 未動。
→ 副產品事實:**`SID` 單獨死掉不足以讓 NotebookLM 認證失效**(靠 `__Secure-*PSID*` 家族撐)。

第二次把 `SID` / `__Secure-1PSID` / `__Secure-3PSID` / `__Secure-1PSIDTS` / `__Secure-3PSIDTS`
的值全部改掉 → 真的失效了,但**失敗點在 `_lifespan` 建 client 的那一步,`auth_check` 根本沒跑到**:

```
notebooklm._auth.extraction._LoginRedirectError:
  Authentication expired or invalid. Final URL: https://accounts.google.com/<redacted>
  Run 'notebooklm login' to re-authenticate.
```

對帳結果:

| 要對帳的事實 | 結果 |
|---|---|
| fail-loud(不靜默 rotate) | ✅ server 起不來,連 pool 都沒建成,**零 rotate** |
| 訊息說得出是認證問題 + 重登指引 | ✅ `Authentication expired or invalid` + `Run 'notebooklm login'` |
| 不洩漏 cookie 值 | ✅ URL 被上游 `<redacted>` |
| 輪替游標不該動 | ✅(啟動即死,游標尚未存在) |

> **FINDING-B(可修的小缺口)**:2c-2 的落檔預驗證會把訊息包成
> 「`NOTEBOOKLM_AUTH_JSON_5` 不是可用的 storage_state」**指名槽位**,但這條
> 「憑證結構合法、cookie 已死」的路徑走的是 `app.py:341` 的
> `NotebookLMClient.from_storage(path=...)`,**外面沒有 try/except 補上槽位資訊**,
> SDK 的 `_LoginRedirectError` 原樣穿透。
> 後果:9 槽 pool 裡任何一槽 cookie 過期 → 整台 server 起不來,而**人看不出是哪一槽要重登**。
> 修法很小:把那個 `enter_async_context` 包起來,重拋時帶上 `_slot_env_name(slot)`。

#### 2c-2 憑證結構不合法 —— 三種形狀全部 fail-loud 且指名槽位

在槽位 5(`NOTEBOOKLM_AUTH_JSON_5`)上各造一次:

| 形狀 | 實際行為 |
|---|---|
| `{"cookies":[]}` | `RuntimeError: NOTEBOOKLM_AUTH_JSON_5 不是可用的 storage_state:ValueError: 必要 cookie 缺少或值是空的:上游驗證失敗:Missing required cookies: SID, __Secure-1PSIDTS` |
| `''`(空字串) | `RuntimeError: NOTEBOOKLM_AUTH_JSON_5 是空的——憑證沒設好,不是「沒有這個帳號」` |
| PSIDTS 值清空(結構合法) | `RuntimeError: NOTEBOOKLM_AUTH_JSON_5 不是可用的 storage_state:…Missing required cookies: __Secure-1PSIDTS` |

三種都**指名了槽位**、都在**啟動時**擋下、**沒有靜默跳過**那個槽位。
第三種的訊息會列出 cookie **名稱**與 Google domain 清單,**但不含任何 cookie 值**。

#### 2c-3(前置)現成素材的實際狀態

**現成素材反而證明了別的事**:前輪留下的「v0.8.1 驗收 D —— 刻意不分享(SDK 直建)」
現在對槽位 2 回 `role="EDITOR"` —— 它**早就被前一輪照指引解開了**,標題已經名不副實。
→ 這條當下沒有 `NotebookAccessDenied` 素材,改在 Phase 10-3 用 SDK 直建一本新的來做。

它免費驗掉了 **Phase 10-2 / Phase 3 的 `role` 對帳**(見下)。

#### 2c-3 權限不足 —— ⭐ 完整走完「照著做」(v0.9.0 唯一功能性 FAIL 的複驗)

素材是**新造的**:用 SDK `notebooks.create` 直建一本 `ZZ-TEST v0.9.14 不分享`
(`a3c4fd30-1a04-4f66-a015-07fc6463645f`),**繞過 `notebook_create` 的自動分享**,
確認協作者清單只有 owner 自己,再把作用中帳號切到槽位 2(`pool-account-2`)。

**(a) 兩支讀取工具的行為不對稱**:

| 呼叫 | 例外型別 | 帶指引? |
|---|---|---|
| `_list_sources`(`source_list` 走這條) | **`NotebookAccessDenied`**(是 `RuntimeError` 子類 ✅) | ✅ 訊息含「呼叫 notebook_share_with_pool(notebook_id=…) 補分享給其餘帳號(EDITOR)後再重試」 |
| `notebook_get` | **原生 `ClientError`**(`NotebookAccessDenied=False`) | ❌ 只有上游訊息:`…this is commonly an account-routing mismatch… See issues #114 and #294` |

> **FINDING-F**:`notebook_get` 撞到權限不足時**沒有被翻譯**,呼叫端拿到的是上游那句
> 講 `authuser` account-routing、指向 SDK issue #114/#294 的訊息 —— 那與我們的 pool
> 情境**無關**,而且**沒有任何修復指引**。而 skill 正是引導呼叫端「生成前先
> `notebook_get` 確認目標對不對」,所以這是實務上很容易先撞到的那一支。
> `_sources.py` 已有現成的翻譯層,補上去是小改動。
> 底層判定本身是對的:RPC log 明確是 `ClientError rpc_code=7`(PERMISSION_DENIED)。

**(b) `podcast_series` 回結構化安全停點(不是外拋、不是無限重試)** —— 逐字:

```json
{
  "episodes": [], "complete": false, "stopped_at_episode": 1, "attempt_id": null,
  "observed_state": "notebook_access_denied",
  "safe_next_action": "notebook_share_with_pool",
  "attempt_count": 0, "superseded_attempt_count": 0,
  "error": "…這個帳號對 notebook a3c4fd30… 沒有存取權。多帳號 pool 模式要求 notebook 對 pool 全員可存取 —— 呼叫 notebook_share_with_pool(notebook_id=...) …"
}
```

| 對帳點 | 結果 |
|---|---|
| `observed_state` 是 `notebook_access_denied` | ✅ 不是 `acceptance_unknown` |
| **沒進 `_REFUSED_WITHOUT_DISPATCH`** | ✅ `_COOLING={}` 全程空 |
| **不該 rotate** | ✅ 游標 `_ACTIVE=1` 前後不動 |
| 零配額 | ✅ `attempt_count=0`、`attempt_id=null` —— **在任何 generation 副作用之前就停了** |

**(c) 照著 `safe_next_action` 執行(不是讀完判斷它看起來對不對)**:

`notebook_share_with_pool(a3c4fd30…)` → `shared_by: "pool-account-1@example.invalid"`。
**注意這裡的重點**:作用中帳號是**看不到那本 notebook** 的 `pool-account-2`,
工具**自己掃 pool 找出分享得動它的帳號**來執行(`_resolve_share_executor`),
沒有跟著游標走 —— 跟著游標走的話指引自己也會 permission denied,呼叫端只是從一個死路
換到另一個(那正是 v0.9.0 Phase 9-1 實測到的形狀)。

- `shared_with` 8 個帳號、`already_shared` 空 ✅
- **執行後游標仍是 `_ACTIVE=1`** ✅(這支不動輪替游標,游標語義只表示「配額走到哪」)
- **解得開**:同一個帳號重讀 → `notebook_get` 回 `role="EDITOR"`、`is_owner=false`;
  `source_list` 讀得到那筆 source ✅

→ **v0.9.0 唯一的功能性 FAIL,這一版確認修好了,而且是照著工具自己給的指引走完的。**

#### 2c-4 配額耗盡

這一輪**沒有自然撞到**(9 槽全新鮮,主流程一集就跑完了)。
→ 標記 **未觀測**。不人為製造(README 說自然撞到才記;偽造配額拒絕的形狀反而會
把 `_REFUSED_WITHOUT_DISPATCH` 的判準測成假的)。

#### 2c-5 不 routable 的 PSIDTS

見 2b-X:**已在 stg 上實跑並確認形狀**(不是只靠 prd 的舊紀錄)。

---

## Phase 3 — 讀取面回歸(SDK 換大版的直接受害面)

| 工具 | 對帳結果 |
|---|---|
| `notebook_list` | ✅ 每筆有 `notebook_id`/`title`,3 本 |
| `notebook_get` | ✅ **`role` 真的解得出來**,見下表 |
| `source_list` | ✅ `source_id`/`title`/`kind`/`ready` 齊全;`kind` 有 `markdown`/`media` 兩種;0.8.1 新增的 `word_count`/`content_mime` 我們沒轉發,**沒有因此炸掉** |
| `source_fulltext` | ✅ `char_count=1304`、`truncated=True`(給了 `max_chars=400`);**CJK 之間沒有被插入空格**,段落換行保留 |
| `artifact_list` | ✅ **`source_ids` 有值** —— 見下,這是這一版唯一完全未知的東西 |
| `artifact_wait` | ✅ 對已完成 artifact **立刻回傳**不卡住:`{"task_id": "5aef…", "artifact_id": "5aef…"}` |
| `feed_info` | ✅ 純本機:`show_id=zz-test-v0914` → `token=vakeje4oi4bs3iu6a4q5oab2`,feed/show URL 成形 |

### `notebook_get` 的 `role`(0.8.1 語義翻轉,實測值)

| notebook | 呼叫帳號 | `role` | `is_owner` | 一致? |
|---|---|---|---|---|
| `35820537…`(驗收 D) | 槽位 1 `pool-account-1` | `"OWNER"` | `true` | ✅ |
| `35820537…`(同一本) | 槽位 2 `pool-account-2` | `"EDITOR"` | `false` | ✅ |
| `a2390f53…` / `bd73e28b…` | 槽位 1 | `"OWNER"` | `true` | ✅ |
| `14960fa0…`(這輪新建) | 槽位 2 / 5 / 9 | `"EDITOR"` | `false` | ✅ |

→ **`role` 在真實 notebook 上完全解得出來,而且分得出 OWNER / EDITOR**;
沒有出現 `role is None` 的樂觀 `is_owner=true` 案例(那個危險分支這輪**沒撞到**,標記為
**未觀測**,不是「不存在」)。升版前的 `is_owner` 在多帳號 pool 下實務上恆為 False,
現在反映真實歸屬 —— 語義翻轉確認生效。

### ⭐ `Artifact.source_ids` 在生產資料上**有值**(這一版唯一的未知數,結案)

`bd73e28b…`(v0.8.0 驗收 notebook,11 個 audio artifact)**每一個都帶出非空 `source_ids`**,
而且值是有意義的:

- `5aef3c6d EP01 排隊與延遲` → 4 個 id,正好是生成當下 notebook 內的 4 份 markdown
  (`778b30ca` ep01 / `50b4da07` ep02 / `aecc047a` ep03 / `46402b89` ep04)。
- 後生成的 artifact 的 `source_ids` 逐步變長(6 → 7 → 8 個),對得上中途回錄進去的 media source。
- `b0b874eb EP01 P9 正向對照 咖啡` 的 4 個 id(`2c2be983`/`29833638`/`9eff5964`/`8a1e577a`)
  **全部不在現在的 `source_list` 裡** → **`source_ids` 保留已被刪除的 source id**,
  是生成當下的歷史快照,不是即時 join。

→ 建議:這個欄位可以從「純觀測」升格為可用資訊,**但不可拿來反查現存 source**
(會查到已刪除的 id)。收工時把這條寫回 AGENTS.md / docstring。

---

## Phase 4 — 分享:單趟 `set_users`

1. `notebook_create("ZZ-TEST v0.9.14 分享")` → `14960fa0-18a0-4313-b8f3-cdc7581eaf1e`,
   `shared_with` **列出 pool 其餘 8 個帳號**(9 槽 − 自己)。
2. 用 SDK `sharing.get_status` 對帳真實協作者清單(代替看網頁,更硬):
   `is_public=False` / `access=RESTRICTED` / `view_level=FULL_NOTEBOOK`,
   自己是 `OWNER`,其餘 8 個全是 **`EDITOR`**(不是 VIEWER)。
   → **收件匣零新信**這條**需要使用者自己確認**(`notify=False` 只有真信箱看得到),標 **需人工確認**。
3. `notebook_share_with_pool` 對同一本再跑一次 → **冪等**:`shared_with=[]`、
   `already_shared` 涵蓋全部 8 個。
4. **upsert 不踢人 —— 第一次跑沒有判別力,補了一次真的會動的**:
   - 判別力問題:第 3 步 `already_shared` 全命中 ⇒ 根本沒發出 `set_users`,「不踢人」是自動成立的。
   - 補做:先 `remove_user` 踢掉槽位 9(`pool-account-5`),再跑 `notebook_share_with_pool`
     → 這次 `shared_with=['pool-account-5@example.invalid']`(**非空 = set_users 真的送出了**)。
   - 結果:槽位 9 補回 `EDITOR`,而 pool 以外的協作者
     `pool-account-11@example.invalid` **仍在、權限仍是 `VIEWER` 沒被覆寫**。
   → **上游宣稱的「`set_users` 是 upsert 不是覆寫」在真實伺服器上確認成立。**
5. **failover 看得到**:槽位 2 / 5 / 9 分別呼叫 `notebook_get` 同一個 id,
   全部成功且 `role="EDITOR"` / `is_owner=false`,沒有 permission denied。

---

## Phase 3b — 低階工具面

素材:`sources/flock-and-rotation.md`(自寫,**刻意內嵌可對帳的識別碼**:Issue #2125、
commit `a3f9c21`、版本 0.8.0/0.8.1/0.9.14),供後面的識別碼 QA 比對用。

### 來源新增

| 工具 | 實際回傳 / 對帳 |
|---|---|
| `source_add_file`(`.md`) | `source_id=1a3c98c9…`,`char_count=930`;`source_list` 的 `kind="markdown"`、`ready=true` ✅ |
| `source_add_text` | `source_id=23495ec2…`;`kind="pasted_text"`、`ready=true` ✅ |
| `source_add_url`(Wikipedia *File locking*) | `source_id=634cf30f…`,`char_count=38506`(非 0,不是 paywall 空殼);`kind="web_page"` ✅ |
| `source_add_file`(`.py`,endpoint 不吃的副檔名) | ✅ **`converted_from: "lock-probe.py"`** —— 自動包成 `<原檔名>.md` 上傳,caller 不必自己改名;`char_count=443` |
| `source_add_file`(`.mp3` + 顯式 `mime_type="audio/mpeg"`) | ✅ 顯式宣告優先(**無** `converted_from`);`char_count=9209` —— 那是 **ASR 逐字稿**的字數,證明音檔真的被轉錄了 |

> **FINDING-C(README 的對帳點與實際契約不符)**:README §3b 要求驗
> `source_add_text` 的「`idempotent` 語義(重送同一份不應長出第二筆)」。
> **實測不冪等**:同一組 `title`+`content` 重送一次,長出**第二筆**
> (`23495ec2…` 之外多了 `39e6d39c…`,`source_list` 看得到兩筆同名 `pasted_text`)。
> 工具 docstring 本身也**沒有**宣稱冪等(只寫 "Add plain text as a source.")。
> → 判定:**不是 bug,是 README 寫錯了對帳點**。但呼叫端要知道:
> 這支重試會產生重複來源,而重複來源會推高「>= 10 筆就 fail-closed」的守門筆數,
> 也會讓生成品質劣化。收工時修 README。

### `source_delete` 的三條契約(全部符合)

1. 刪掉重複那筆 → `{"deleted": "39e6d39c…", "was_present": true}` ✅
2. **再刪一次** → `{"deleted": "39e6d39c…", "was_present": false}`,**不拋錯** ✅(冪等)
3. **已刪除的 id 仍讀得回全文** → `source_fulltext(39e6d39c…)` 回 `char_count=357`、
   三個關鍵詞 `hits` 全 `true`。這是 AGENTS.md 記過的**預期行為**,不是清理失敗 ✅
4. **跨 notebook 誤刪保護**(docstring 明講的):拿屬於 `14960fa0…` 的 source_id 配
   **別本** `a2390f53…` 呼叫 → `was_present=false`,**沒有發出 destructive RPC**;
   事後 `source_list` 確認該 source 在原 notebook **還在** ✅

> 附帶驗到:`source_fulltext(max_chars=0, contains=[...])` 的 CJK 比對確實在 server 端
> 做了正規化 —— 關鍵詞不含空格仍全部命中,證明 NotebookLM 對 CJK 插空格的問題被 `_text.norm` 吸收掉了。

---

### 生成三支與 artifact 操作

| 工具 | 對帳結果 |
|---|---|
| `generate_audio` | 走主流程驗(見 Phase 6);**低階單支未直接呼叫**,理由:它與 `podcast_series` 共用同一條 dispatch,而低階那支**沒有 failover**(SKILL.md 明講),單獨燒一次的判別力低於主流程 |
| `generate_report` | ✅ `artifact_id=0fb2782c…`,`report_md_path=output/ep01-report.md`(7,133 bytes),回寫 manifest |
| `generate_slides` | ⚠️ **client timeout,遠端照跑** —— 見下方「真實 timeout 事件」 |
| `artifact_download_slides` | ✅ **救援路徑**:同一顆 `artifact_id` 接手等待 + 下載回寫,**沒有重新生成、沒有再燒配額** |
| `artifact_rename` | ✅ 講義改名為 `EP01 講義(改名測試)`,`artifact_list` 立刻反映 |
| `artifact_download_audio` | ✅ 重下載的 **sha256 與主流程 manifest 記的 `413128f9…` 逐位元組相同**(29,807,191 bytes) |
| `artifact_download_report` | ✅ **救援路徑**:用同一個 `artifact_id` 重下載並回寫 manifest,回傳與生成時逐欄相同(`report_md_path` 同路徑),檔案內容未變(7,133 bytes / sha256 `268c3b10…`),**沒有重新生成** |
| `artifact_revise_slide` | ✅ **preflight 判別實驗**:故意用**錯誤的 `notebook_id`**(`a2390f53…`)配正確的 `artifact_id`(`bc5185e7…`)→ 當場擋下:`artifact bc5185e7… 不在 notebook a2390f53…(用 artifact_list 確認 ID 與筆記本)`。`get_or_none` 同時驗存在與歸屬,**沒有發出任何遠端 mutation**,沒有改到別本的 artifact |
| `artifact_retry_failed` | **inconclusive** —— 這一輪沒有任何 failed artifact,**刻意不為了測它去弄壞東西**(README 明示)。0.8.1 起改回 `status="pending"` 的那個行為因此**未觀測**。註:卡住 85 分鐘的 `bc5185e7…` 全程是 `in_progress` 從未轉 `failed`,所以連它都當不了素材 |

> 附帶觀察:`artifact_download_audio` 落地的檔案是 **`-rw-------`(0600)**,而主流程 finalize
> 落地的 `ep01.mp3` 是 **`-rw-r--r--`(0644)**。同內容、兩條路徑、兩種權限。

### ⭐ 真實 timeout 事件:`generate_slides` 逾時,救援入口接得回來

**不是刻意製造的,自然撞到的**(我把 `wait_timeout` 傳成 900 而非預設 1800):

```
Error executing tool generate_slides: Task bc5185e7… in notebook 14960fa0… timed out
after 900.0s (last status: in_progress; status history: in_progress)
```

事實對帳:

- **遠端沒有被取消**:`artifact_list` 顯示同一顆 `bc5185e7…` 仍是 `in_progress`
  —— 印證 MCP instructions 那句「timeout 只殺 MCP request,已送出的在遠端照樣生完」。
- **操作資料點**:這一份簡報(3 筆來源、deep-dive)遠端跑了 **25 分鐘仍未完成**。
  → `generate_slides` 的預設 `wait_timeout=1800` 是有道理的,**不要自作聰明調小**。
- **照 docstring 的救援路徑走**:`artifact_download_slides` 帶同一個 `artifact_id`
  接手等待 → 下載 → 回寫 `slides_pdf_path`,**沒有重新生成、沒有再燒一次配額**。
  (docstring 說的「不確定是哪一筆別猜 —— 重生比綁錯便宜」在這裡不成問題:
  `artifact_list(kind="slide_deck")` 只有一筆。)

### 中途 poll 到的 artifact status(0.8.1 的 wire 值 1↔2 轉置修正)

生成進行中實際 poll 到的值:`status="in_progress"`、`completed=false` —— **合理值**,
沒有出現轉置後的錯置(例如生成中回 `completed`)。完成後轉為 `status="completed"`、
`completed=true`。manifest 端 dispatch 當下記的是 `"pending"` +
`status_origin="sdk_heuristic"`,完成後變 `"completed"` + `status_origin="remote"` ✅

### 識別碼比對(README 說「這是免費的」)

凍結來源 `sources/flock-and-rotation.md` 內含的識別碼:`#2125`、`a3f9c21`、
`0.8.0`/`0.8.1`/`0.9.14`。

生成出來的講義(7,133 字元)以 regex 掃 issue/PR 編號、commit SHA、版本號、RFC/標準編號:

**四類全部零命中** —— 講義**沒有編造任何來源以外的識別碼** ✅

> ⚠️ **判別力聲明**:講義也**沒有引用**來源裡真實存在的那些識別碼,所以這次檢查
> 只證得了「沒有編出來」,證不了「引用得正確」。要驗後者需要一份會逼模型複述編號的
> 來源。→ 這一輪對 `generate_report` 的識別碼品質結論是**單向的**。

內容品質(人工掃讀):結構完整(標題/表格/分節),`fcntl` vs `flock` vs OFD locks 的
語義差異、advisory vs mandatory、interceding update 範例全部忠實對得上兩份來源,
沒有看到明顯的張冠李戴。

---

## Phase 6 — 主流程:一集端到端 ✅

`podcast_series` 一集(`EP01 判別實驗`),notebook `14960fa0…`,
`output_dir=output/`。**一次跑完,沒有中斷、沒有配額拒絕。**

| manifest 欄位 | 實際值 |
|---|---|
| `dispatch.status` / `account` | `accepted` / **`pool-account-1@example.invalid`** |
| `dispatch.dispatched_at` → `accepted_at` | `13:50:20.110` → `13:50:27.019`(7 秒被受理) |
| `remote.artifact_id` | `8c65db3e-26d1-4a11-ac9a-ac822b91ce70` |
| `remote.status` / `status_origin` | `completed` / **`remote`**(不是 heuristic) |
| `task_id` | `8c65db3e…`(= artifact_id) |
| `mp3_path` | `output/ep01.mp3` |
| `published_at` | `Sat, 15 Aug 2026 22:01:55 +0800` |
| `finalize.download` | `completed`,**29,807,191 bytes**,`sha256=413128f9710c82e0c1b23ccb213df92961e244bda9be4a261aa4a49f67aa0cc1` |
| `finalize.feedback_source_upload` | `completed`,`source_id=82dae7ca…`,`expected_title="ep01.mp3"` |
| `finalize.feedback_source_rename` | `completed`,**`source_name="EP01 判別實驗"`** |
| manifest `revision` | 4 →**14**(每個 checkpoint 一次原子更新) |

**回錄 source 與 artifact 完全同名** ✅:
`artifact_list` 的 `title="EP01 判別實驗"` == `source_list` 的 `title="EP01 判別實驗"`。

`ffprobe` 對帳(**MP4/AAC 容器、副檔名叫 `.mp3` 是既有設計**,README 已註明):

```
format_name=mov,mp4,m4a,3gp,3g2,mj2   codec_name=aac   channels=2
sample_rate=44100                      bit_rate=257477  duration=926.13s (15分26秒)
file: ISO Media, MPEG v4 system, DASH   檔頭: 00000018 66747970 64617368  (ftypdash)
```

---

## Phase 7 — 狀態機全入口

| 入口 | 實測行為 |
|---|---|
| `podcast_episode_resume` | ✅ **完全冪等**:對已完成的 EP01 用同一個 `artifact_id` 重跑,回傳逐欄與原輸出相同(`artifact_id`/`mp3_path`/`published_at`/`attempt_id`/`feedback_source_id` 一字不差),**沒有新增第二筆回錄 source** |
| `podcast_episode_reconcile` | ✅ 認出這顆已被 claim 且是正式輸出:`observed_state="accepted"`、`safe_next_action="podcast_attempt_retract"`,`next_step` 明講「它是這一集的正式輸出:要作廢就直接 `podcast_attempt_retract`(**不需要** `abandon_in_flight`),照回傳的 `stale_source_ids` 逐一 `source_delete`,再用 `podcast_series` 重生。」**不做重複收養** |
| `podcast_attempt_adopt` | ✅ **安全拒絕**:`artifact adoption requires an unresolved reconciliation state` —— 不會把已解析的 attempt 重新綁定。**成功路徑 inconclusive**:需要真實的 unresolved 狀態(client cancellation 剛好落在 dispatch 那 7 秒內),這輪沒撞到,**不人為製造**(與對 `artifact_retry_failed` 的態度一致) |
| `podcast_attempt_retract` | ✅ 見下(排在 Phase 9 發布之後執行,因為它會作廢 EP01) |

**`podcast_attempt_retract` 實測**(對 EP01 的 `output_attempt_id`,`abandon_in_flight` 不傳):

```json
{"observed_state": "retracted", "safe_next_action": "source_delete",
 "authorization_basis": "output_owner", "abandon_in_flight": false,
 "dispatch_status_at_retraction": "accepted", "remote_status_at_retraction": "completed",
 "source_cleanup_unresolved": false,
 "stale_artifact_id": "8c65db3e…", "stale_source_ids": ["82dae7ca…"],
 "source_cleanup_obligations": [{"source_id": "82dae7ca…", "notebook_id": "14960fa0…"}],
 "next_step": "先把這幾筆 source_delete 掉:source_delete(notebook_id='14960fa0…', source_id='82dae7ca…')。用 podcast_series 重生。"}
```

- **不需要 `abandon_in_flight`** —— 與 docstring 的分流一致:它是該集正式輸出、遠端已 `completed`。
- **照著 `next_step` 執行**(不是讀完覺得對):`source_delete(14960fa0…, 82dae7ca…)`
  → **`was_present: true`** ✅ 清理義務履行完畢。
- `source_cleanup_unresolved: false` —— 回錄 source 的 id 已落盤,沒有懸而未決的遠端 media。

---

## 收尾

### 覆蓋自檢:35 / 35 支工具全部碰過

跑 README 收工段的自檢腳本,**零未記錄**。刻意不完整驗證的三項已在上方標明理由:
`artifact_retry_failed`(無 failed 素材,不弄壞環境)、
`podcast_attempt_adopt` 的成功路徑(無 unresolved 素材)、
`generate_audio` 低階單支(與主流程共用 dispatch,單獨燒配額判別力更低)。

### 遠端清理

這一輪建的三本 notebook **已全部刪除**:

| notebook | 標題 |
|---|---|
| `14960fa0…` | ZZ-TEST v0.9.14 分享 |
| `15f21992…` | ZZ-TEST v0.9.14 research scratch |
| `a3c4fd30…` | ZZ-TEST v0.9.14 不分享 |

> 📌 **留著沒動的**(不在 README §八「標題含 ZZ-TEST v0.9.14」的範圍,交使用者決定):
> `a2390f53…` v0.8.1 驗收 —— 重送 failover 與自動分享 /
> `bd73e28b…` v0.8.0 驗收 —— 配額 failover /
> `35820537…` v0.8.1 驗收 D —— 刻意不分享(SDK 直建)
> —— 前兩輪驗收的遺留,會一直累積下去。

### 憑證殘留清理(CLAUDE.md §三 的方法,**沒有用 `fuser`/`lsof`**)

盤點時機器上有 **9** 個 `/tmp/notebooklm-mcp-auth-*`。用「目錄 ctime 對照活 process 的 `lstart`」判別:

| 目錄 | ctime | 判定 |
|---|---|---|
| `1ra115yn` | 21:32:36 | **保留** —— 對上活著的 stg server(pid 3174000,啟動 21:32:27),就是本 session 這台 |
| `3d__nwxl` | 06:39:09 | **保留** —— 對上活著的 **prd** server(pid 2787852,啟動 06:39:02),**別人的 session,絕不能刪** |
| 其餘 7 個 | 08-13 ~ 16:20 | **孤兒,已刪**(共 103 個檔) |

> **這一輪再次印證 CLAUDE.md 的警告**:`7s6pi7bu`(21:33:15,27 個檔 = 9 槽 × 3)是
> **`claude mcp list` 的健康檢查**留下的 —— 它每跑一次就把 9 份真憑證寫到磁碟然後被殺掉,
> 不展開 `AsyncExitStack`。這是「零星測試一個下午就留下好幾個目錄」的主要來源之一。
>
> 也再次確認**不能用 `fuser`/`lsof` 判斷**:當下有兩台活 server,其中一台是 `-c prd`
> 的正式帳號 pool,而兩台都不持有目錄的 open FD。

清理後只剩上述兩個活 server 在用的目錄;**沒有孤兒 server**(`pgrep -af nblm-mcp` 只有那兩台)。

### 收回修正(README 收工 §1/§2)—— 五條都修了實作並補離線測試

| # | 修在哪 | 做法 |
|---|---|---|
| **D** | `_text.strip_inline_emphasis`,`chat_ask` + `episode_set_description` 兩處呼叫 | 清成對星號強調(`**粗體**` → `粗體`)。**底線不清**:`_斜體_` 與 `NOTEBOOKLM_AUTH_JSON`/`source_id` 同形,清掉會毀正文 —— 所以 skill 那句改成精確描述,不是讓實作去追文件 |
| **E** | `tools_basic._assert_no_dropped_blocks`(只掛 `strip_citations=True`) | 掃出「有 block 但一個 span 文字都沒解出來」的 block → fail-loud。**判準是文字不是 kind**;**整份**都沒文字則放行(那是既有的 `render().strip()` fallback 路徑) |
| **F** | `_errors.raise_if_access_denied`,`_sources` 與 `notebook_get` 共用 | 把原本只長在 `_list_sources` 的那段指引抽出來共用 |
| **G** | `tools_research._explain_no_research` | timeout 且全程 `no_research` 時,把「handle 綁發起帳號」的診斷帶進訊息。`_handle_client` 留在 try **外面**(它自己的錯誤訊息裡也有 `no_research`,包進去會二次包裝) |
| **B** | `app.py` 的 `from_storage` 包 try/except | 重拋成 `RuntimeError` 並帶 `_slot_env_name(slot)`,原因用 `from exc` 保留在 `__cause__` |

**新增 13 個離線測試**,並逐條做**突變驗證**(把修正改回舊行為,確認測試真的會紅):

```
✅ D 清 inline 強調          — 改回舊行為後測試變紅(rc=1)
✅ E 偵測被丟棄的 block      — 改回舊行為後測試變紅(rc=1)
✅ F notebook_get 翻譯       — 改回舊行為後測試變紅(rc=1)
✅ G research timeout 診斷   — 改回舊行為後測試變紅(rc=1)
✅ B 槽位名                  — 改回舊行為後測試變紅(rc=1)
```

每條都另外配了**判別力那一半**(只驗「修好了」不夠,要驗「沒有誤傷」):

- D:`NOTEBOOKLM_AUTH_JSON_2`、`* 條列一`、`2 * 3 * 4` 三種形狀都不可被動到。
- E:`CODE_BLOCK` **帶** spans 時必須放行(離線實測 `render()` 照樣輸出它);
  `HORIZONTAL_RULE` 本來就沒文字,不算丟失;預設路徑(`strip_citations=False`)逐字不變。
- F:`rpc_code=5`(not-found)必須原樣拋出,不可被吞成權限問題。
- G:單純「等太久」(`last status: in_progress`)不可被說成「你傳錯帳號」
  —— 這一輪的 slide_deck 卡 85 分鐘就是這種形狀。

**修改期間改壞了三個既有測試,三個都是真的判別力發揮作用**,已修:

1. `_handle_client` 被我包進 try → 一條乾淨的 `ValueError` 被二次包裝成 `RuntimeError`。
2. 全空白 document 被我誤判成「部分丟失」→ 破壞既有的 fallback 契約。
3. pool teardown 測試斷言舊的例外型別 → 改成斷言新契約(`RuntimeError` + 訊息指名槽位
   + `__cause__` 仍是原例外),**同時保留原測試的意圖**(teardown 有做、env 有還原)。

**完整套件:12,938 passed / 0 failed / 597 skipped**(原本 12,925,新增 13)。
`local-checks.sh` 重跑仍 **18/18**,skill 快照與上游逐字一致。

### 文件同步

- `../audi-skill/notebooklm/references/tool-reference.md`:改掉 FINDING-D 那句不準確的宣告,
  補上「底線不清」與「解不出的 block 會 raise」兩條;快照已同步回本工作區。
- `../notebooklm-mcp/AGENTS.md`:0.8.1 那條補上 ④(flock 在 stg 得證 + 對照組紀律);
  新增 `render()` 兩道守衛那條;**`Artifact.source_ids` 從未結案改成已結案**
  (可看「用哪些來源生的」,不可反查現存 source)。
- `../notebooklm-mcp/CHANGELOG.md`:v0.9.14 節補「真實環境驗收結果」。
- 本工作區 `README.md`:修掉 FINDING-A(`range(1, 8)` → 掃到沒有為止)與
  FINDING-C(`source_add_text` 的「idempotent」對帳點寫錯)。

**未 commit** —— 所有改動都留在工作樹,由使用者決定。

---

## Phase 5 — ⭐ `chat_ask` 的 show notes 形狀

同一個問題,兩種 `strip_citations` 的實際輸出(notebook `14960fa0…`,
`conversation_id=7ddedf26-da65-42b7-809d-d783fa881c26`)。

### 5-1 `strip_citations=True, include_references=False`

| 對帳點 | 結果 |
|---|---|
| 段落分隔還在(有 `\n`) | ✅ 每個段落之間單一 `\n` |
| 沒有 `￼`(U+FFFC) | ✅ 整段找不到 |
| 沒有 `[1]` 之類標記 | ✅ 全部清掉 |
| `###` 標題標記 | ✅ 被清掉 |
| `*   ` 條列標記 | ✅ 被清掉 —— 但**條列因此看不出是條列**,變成連續行 |
| **Markdown 強調 `**`** | ❌ **沒有被清掉**,見 FINDING-D |

原始字串節錄(逐字):

```
…這在本質上是一種**競態條件（race condition）**中的「干預更新（interceding update）」問題…
…有效的解決方案並非繞過設計，而是利用作業系統層級的**檔案鎖（file locking）**機制。
```

> **FINDING-D(skill 文件與實際輸出不符 —— 這是 Phase 10-1 的對帳點)**
> skill 的 `references/tool-reference.md` 說「`strip_citations=true` 回的是純文字,
> **沒有 Markdown 強調**」。**實測 `**…**` 原樣留在輸出裡。**
> `render()` 拿掉的是 **block 級**標記(`###` 標題、`*` 條列),**inline 的 `**` 不受影響**。
> 影響半徑:這個字串正是 `episode_set_description` → manifest → **公開 Apple Podcast
> `<description>`** 的來源,`**` 會原樣出現在 RSS 裡。
> → 要嘛修 skill 文件的措辭,要嘛在 server 端補清 inline 標記。

### 5-2 ⭐ CODE_BLOCK:不是 U+FFFC,是**整段靜默消失**

問「請用一段 C 語言程式碼示範 POSIX 的 `fcntl` 檔案鎖」,`strip_citations=True` 的回答:

```
…以下是一段示範如何使用 fcntl 取得**排他鎖（Exclusive Lock，又稱寫入鎖）**的 C 語言程式碼：
關鍵概念說明：
建議性鎖定（Advisory Locking）：在 Unix 中…
```

**「以下是一段…程式碼:」後面直接接下一段,程式碼整段不見,而且沒有任何 `￼` 佔位符。**

**判別實驗(確認程式碼真的有生成、不是模型沒產)**:同一個 conversation 追問
「請再貼一次剛才那段程式碼」,`strip_citations=False` → **完整回傳**,包在 ` ```c ` fence 裡
(`#include <fcntl.h>` / `fl.l_type = F_WRLCK` / `F_SETLKW` … 60 餘行俱全)。

> **FINDING-E(比 README 的預期更嚴重)**
> README 說「上游說 `CODE_BLOCK`/`THOUGHT` 目前不解碼,**那些位置會出現 `￼`(U+FFFC)**」。
> 那是 `.text` 的行為。**`render()` 的行為是整個 block 靜默丟棄,連佔位符都不留。**
> 危險之處在於**輸出讀起來完全通順**——U+FFFC 至少看得見,靜默丟棄看不出來。
> 影響:show notes 若含程式碼/圖片區塊,公開 RSS 會出現「以下是程式碼:」後面接別的內容。
> → 呼叫端紀律:**產 show notes 的提問不要引出程式碼區塊**;或在 server 端對
> `render()` 與 `.text` 的 block 數做一致性檢查,不一致時 fail-loud。

### 5-3 `strip_citations=False`(預設)—— 原封不動 ✅

同一個問題:`[1]` / `[2, 3]` 引用標記、`### ` 標題、`*   ` 條列、`**` 強調**全部保留**,
段落是 `\n\n`。`include_references=True` 回 3 筆 references,各帶 `source_id`、
`citation_number`、`cited_text`(cited_text 的 CJK 原文正確,無亂碼)。
→ 既有 caller 依標記對照 references 的行為**沒有被這一版改動波及**。

FINDING-D 的逐字依據 —— skill `references/tool-reference.md:588-590`:

> ⚠️ **`strip_citations=true` 回的是純文字,沒有 Markdown 強調。** …段落分隔保留,
> 但 `**粗體**` / `_斜體_` 之類的標記不會出現 —— show notes 用途是刻意這樣選的。

---

## Phase 8 — research 三支(上游大改,我們零改動)

照 skill `references/research.md` 的鐵律跑在**拋棄式 scratch notebook**
(`ZZ-TEST v0.9.14 research scratch` = `15f21992-1d50-4b03-a856-703d928e9036`),
不污染 episode notebook 的來源集。

`research_start` 兩支都成功,**兩個都回了 `account`**:

| mode | task_id | report_id | account |
|---|---|---|---|
| `fast` | `d3a6d333-1672-477f-bb7b-ef1a782e9f59` | `null` | `pool-account-1@example.invalid` |
| `deep` | `b884c695-6578-4f21-ba5a-87cd3d981d6c` | **= task_id** ✅ | `pool-account-1@example.invalid` |

### ⭐ 「換帳號輪詢」的兩種形狀 —— README 的描述只涵蓋其中一種

README 說「用**另一個帳號**呼叫 `research_wait` 應該**當場 raise 並列出可用帳號**」。
實測要分成兩種,行為**完全不同**:

| 傳進去的 `account` | 實際行為 |
|---|---|
| **不在 pool 裡**(`nobody-not-in-pool@example.com`) | ✅ **當場 raise 並列出全部 9 個可用帳號**:`account '…' 不在這個 server 的 pool 裡(可用:[…9 個…]);research handle 只有發起它的那個帳號輪詢得到,換帳號會拿到 no_research` |
| **在 pool 裡、但不是發起者**(`pool-account-2@example.invalid`) | ❌ **不 raise**,一路輪詢到 timeout 才死:`Research task d3a6d333… timed out after 60.0s (last status: no_research)` |

→ **v0.9.13 的修正本身沒有被 0.8.1 換版波及**(第一種形狀完好)。
但第二種是真實的可用性缺口:

> **FINDING-G**:`research_start` 明明回了 `account`,server 卻沒有把「這個 handle 屬於誰」
> 記下來,所以 `research_wait` 無法在**輪詢前**比對。傳錯 pool 內帳號的人要等滿
> `timeout`(**預設 1800 秒 = 30 分鐘**)才知道自己傳錯,而錯誤訊息只說 `no_research`,
> 完全沒提「你可能傳錯帳號了」。
> 諷刺的是**正確的診斷文字已經寫在另一條路徑的錯誤訊息裡**(「research handle 只有發起
> 它的那個帳號輪詢得到」),只差沒有在這條路徑上講。
> 便宜的修法:`last status == no_research` 且 `account` 與作用中帳號不同時,把那句提示
> 一起帶進 timeout 訊息。

### 正常路徑(fast)

`research_wait(account=發起者)` → `status="completed"`,10 筆 `candidates`,
`report_chars=0`、`report_importable=false`(fast 沒有報告,符合契約)。

`research_import` 指名 3 個 URL:

| `imported` 回傳 | `source_list` 實際 |
|---|---|
| `74b9e9c5…` fcntl_locking(2) - man7.org | ✅ 同 id,`kind="web_page"`,`ready=true` |
| `c0daf5a8…` File locking in Linux - Victor Gaydov | ✅ 同 id |
| `dba08b23…` On the Brokenness of File Locking | ✅ 同 id |

**`requested=3` / 回傳 3 筆 / notebook 實際增量 3 筆,逐 id 對得上** ✅
(0.8.1 大改的 `import_sources_with_verification` 在真實環境跑通,沒有出現重試風暴或漏匯入。)

### 正常路徑(deep)

`research_start(mode="deep")` 21:56 送出 → `research_wait` 在 **約 20 分鐘後**回 `completed`。

| 對帳點 | fast | deep |
|---|---|---|
| `report_id` | `null` | **= task_id** ✅ |
| `report_chars` | 0 | **23,716** |
| `report_importable` | `false` | **`true`** ✅ 兩種模式**分得出來** |
| `cited_url_count` | 0 | **44** |
| `candidates` | 10 筆,`cited` 全 `false` | **60 筆,`cited` 有 true/false 之分** ✅ |
| `max_report_chars=1200` | — | `report_truncated=true`,回傳確實截在 1200 附近 ✅ |

`research_import(include_report=True, urls=[2 筆])`:

- `requested=3`(**報告本身也算一筆**),回傳 3 筆
- 報告以 **`kind="markdown"`** 落地,標題就是報告標題
  (`分散式系統中共享憑證輪替的協調架構與並行衝突控制策略深度研究報告`)
- scratch notebook 最終 6 筆(fast 3 + deep 3),**全部 `ready=true`,逐 id 對得上** ✅

→ 0.8.1 改掉的三件事(FAILED_PRECONDITION 不再盲目重投、per-attempt timeout 被
`max_elapsed` 夾住、baseline 改 `strict=False`)在 fast 與 deep 兩條路上**都沒有引發可觀測的問題**。
我們零改動受惠的部分,這一輪是第一次真的跑過。

---

## Phase 10 — skill × MCP 搭配(「照著做」,不是讀完判斷)

| # | skill 怎麼說 | 照著做的結果 |
|---|---|---|
| 1 | `tool-reference.md:588` —「`strip_citations=true` 回的是純文字,**沒有 Markdown 強調**…`**粗體**` / `_斜體_` 之類的標記不會出現」 | ❌ **不符**,見 FINDING-D:`**` 原樣留下。追加驗到影響半徑:把含 `**粗體標記**` / `_斜體標記_` / `[1]` / `[2, 3]` 的字串餵進 `episode_set_description`,回寫值是 `…含 **粗體標記** 與 _斜體標記_,以及一個引用標記 與。…` —— **引用標記被清掉、markdown 強調原封不動**,也就是它會**直達公開 RSS `<description>`** |
| 2 | `notebook_get` 的新警告 —「不要單看 `is_owner` 判斷歸屬,要看 `role`」 | ✅ **符合**:同一本 notebook,owner 帳號回 `role="OWNER"`/`is_owner=true`,被分享的帳號回 `role="EDITOR"`/`is_owner=false`。**`role` 真的區分得出來**,而且是在「別人分享給你」的真實 notebook 上驗的 |
| 3 | `notebook_share_with_pool` 的指引 | ✅ **符合且解得開**,見 2c-3(c)。工具**自己找分享得動的帳號**執行,不跟著游標走 —— 這正是 v0.9.0 唯一功能性 FAIL 的形狀,這一版照著指引就走通了 |
| 4 | SKILL.md §Auth 的安裝指令(pin `@v0.9.14`) | ✅ **符合**,見下 |

**Phase 10-4 逐字執行結果**:

```
$ uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.14"
   Updated https://github.com/audichuang/notebooklm-mcp.git (42cc988db9090fae2d7a94c0164cacf49e376498)
Resolved 42 packages ... Installed 2 executables: nblm-mcp, notebooklm-cover
```

- tag `v0.9.14` 解析到 commit **`42cc988`**,`uv tool list` 顯示 **`notebooklm-mcp v0.9.14`**
- 裝出來的是 **`nblm-mcp`**(不是 `notebooklm-mcp`)—— 與 CLAUDE.md §二 的紀律一致,
  **正式 tag 這次也驗過了**(README 風險表最後一列的「pin `>=0.8.1`」對帳點)
- 重裝後重跑 `local-checks.sh` 仍 **18/18 全過**,執行中的 MCP server 未受影響

---

## Phase 9 — 發布(**使用者已明確授權:跑,而且發布兩次**)

### 前置對帳

- **salt 隔離確認**(唯讀,只比 hash 不印值):
  `stg` 的 `PODCAST_TOKEN_SALT` sha256[:12]=`76a420f93399`(len 40)、
  `prd` 的 sha256[:12]=`0f068dc5a81d`(len 64)—— **不同**,測試 feed 落在與正式節目
  完全不同的 URL 空間 ✅(`PODCAST_PUBLIC_BASE_URL` / `PODCAST_UPLOAD_URL` 兩邊相同,
  隔離完全靠 salt,與 `docs/test-account.md` 的說法一致)
- **preflight 硬性要求**(`tool-reference.md:1426-1431`):每集必帶 `description`、
  `cover_path`、`slides_pdf_path`、`report_md_path`;缺任一或 `description` 等於標題
  → 任何 PUT 之前 raise。
- 節目層封面用 `notebooklm-cover --show` 產:`output/show-cover.jpg`,
  **3000×3000 JPEG / RGB**(Apple 規格)✅
- 單集封面用 `notebooklm-cover --manifest` 產並回寫 `cover_path`。
  查證過它**用的是同一個 `ManifestStore` + `_atomic.prepared_replacement`**
  (不是自己 `json.dump`),所以與 MCP 工具的回寫共用 flock,不會 lost update ✅

### 第一次發布(`publish_series`)

```json
{"feed_url": "https://podcast.example.com/feeds/FEED_TOKEN/feed.xml",
 "token": "vakeje4oi4bs3iu6a4q5oab2", "episode_count": 1,
 "episodes": [{"n": 1, "guid": "dd931a3f2fca05af8587871d941d4273171dab5f",
   "url": ".../EP01-06bff2ea.mp3", "cover_url": ".../EP01-cover-eb7ba286.jpg",
   "pdf_url": ".../EP01-aa7f1ec2.pdf", "html_url": ".../EP01-bd221aa7.html",
   "duration": "00:15:26"}]}
```

**`token` 與這一輪稍早 `feed_info("zz-test-v0914")` 算出的完全一致** ✅
—— 確定性 HMAC(same show_id → same URL)在真實發布上得證。

read-back(全部走公開 HTTPS,不是內網 uploader):

| URL | HTTP | content-type |
|---|---|---|
| `feed.xml` | **206** | `application/rss+xml; charset=utf-8` |
| `index.html` | **206** | `text/html; charset=utf-8` |
| `EP01-06bff2ea.mp3` | **206** | `audio/mpeg` |
| `EP01-cover-eb7ba286.jpg` | **206** | `image/jpeg` |
| `EP01-aa7f1ec2.pdf` | **206** | `application/pdf` |
| `EP01-bd221aa7.html` | **206** | `text/html; charset=utf-8` |

feed.xml 內容對帳:`<itunes:type>serial</itunes:type>`(我顯式傳了,連載節目必要)、
`<itunes:episode>1</itunes:episode>`、`duration=00:15:26`(與 ffprobe 的 926.13s 一致)、
`<description>` 就是寫進 manifest 的那段、**沒有 `**` 殘留**(因為我寫回的是乾淨版本;
若照 FINDING-D 的原始 `chat_ask` 輸出寫進去就會有)。

### ⭐ `_embed_cover` 真的把 MP4/AAC 轉成真 MP3(README 說的既有設計,實測得證)

| | 檔頭 | 大小 |
|---|---|---|
| 本機 `output/ep01.mp3` | `00000018 66747970 64617368` = **`ftypdash`(MP4/AAC)** | 29,807,191 |
| 發布後的 enclosure | `49443303` = **`ID3` v2.3.0(真 MP3)** | **30,063,385** |

差 256,194 bytes = 轉檔 + 內嵌封面。**副檔名叫 `.mp3` 但本機是 MP4 容器**這件事,
到發布這一哩才被修正 —— 與 README 的說明一致,不是 bug。

### 第二次發布 —— ⭐「重新發布不下架舊產物」(skill 新加的那條)

先讓一個 asset 的內容真的改變:`notebooklm-cover --hue 210` 重繪單集封面,
sha256 `eb7ba286…` → `f6662753…`。然後**只傳 `manifest_path`**(驗 show 七欄沿用):

| 欄位 | 第一次 | 第二次 | 判定 |
|---|---|---|---|
| `token` | `vakeje4o…` | **相同** | ✅ 同一個 feed |
| `guid` | `dd931a3f…` | **相同** | ✅ Apple 視為**同一集的更新**,不會變成新一集 |
| `cover_url` | `EP01-cover-eb7ba286.jpg` | `EP01-cover-**f6662753**.jpg` | content-hash 跟著改 |
| `url`(mp3) | `EP01-**06bff2ea**.mp3` | `EP01-**1f28ace9**.mp3` | ⚠️ **我沒動 mp3,它照樣換了 URL** |
| `enclosure length` | 30,063,385 | 30,053,977 | 同上 |
| `pdf_url` / `html_url` | `aa7f1ec2` / `bd221aa7` | **相同** | ✅ 內容沒改就不換 |

> **附帶發現(值得寫進 skill)**:**換單集封面會連帶換掉 mp3 的 enclosure URL**
> —— 因為 `_embed_cover` 把封面嵌進 ID3,mp3 的位元組因此改變,content-hash 跟著變。
> 也就是「只改封面」在 feed 層不是零成本操作:訂閱端會看到 enclosure 換 URL
> (guid 不變所以不會重複出現在清單裡,但可能觸發重新下載),而 NAS 上會**永久**
> 多一份完整音檔。

**判定:舊產物沒有被下架** ✅ —— 被取代的兩個舊 URL 現在仍 **HTTP 206 公開可讀**:

```
https://podcast.example.com/feeds/FEED_TOKEN/EP01-06bff2ea.mp3        ← 舊音檔(30,063,385 bytes,仍在 NAS)
https://podcast.example.com/feeds/FEED_TOKEN/EP01-cover-eb7ba286.jpg  ← 舊封面
```

`feed.xml` 已改指新版,節目層 `artwork-3bdcb81e.jpg` 兩次相同(沒改就沒換)。

### ⭐⭐ 下架比想像中難:刪掉 NAS 上的檔案**不會**讓 mp3 的公開 URL 失效

收尾清理時撞到的,**這條對正式節目有實質意義**。

`uploader/server.py` 只有 `do_GET` / `do_PUT`,**沒有 `do_DELETE`** —— 「uploader 不刪檔」
是程式碼層級的設計,不只是慣例。所以下架只能上 NAS 直接刪檔案。

刪掉整個 `feeds/vakeje4oi4bs3iu6a4q5oab2/`(71M)之後 read-back:

| URL | 刪除後 | `cf-cache-status` |
|---|---|---|
| `feed.xml` / `index.html` | **404** ✅ | `DYNAMIC`(不快取) |
| `EP01-cover-*.jpg` / `*.pdf` / `*.html` | **404** ✅ | — |
| **`EP01-06bff2ea.mp3`(舊)** | **仍 HTTP 206** ❌ | **`HIT`** |
| **`EP01-1f28ace9.mp3`(新)** | **仍 HTTP 206** ❌ | **`HIT`** |

原因在 `podcast-feed-host/Caddyfile:11`:

```
header @mp3     Cache-Control "public, max-age=31536000, immutable"   ← 一年
header @feedxml Cache-Control "no-cache"                              ← 所以 feed 立刻消失
```

mp3 的檔名是 content-hash,所以標成 `immutable` 是對的(播放器與 CDN 都不必回源);
**代價是它同時讓「刪檔」失去下架的效果** —— 回應的 `age: 22182` 顯示 Cloudflare edge
已經握著它 6 小時,而 TTL 是 **31,536,000 秒(一年)**。

> **推論(對正式節目)**:哪天要下架某一集(講錯內容、版權、隱私),
> **刪 NAS 上的檔案不夠,必須同時 purge Cloudflare 快取**。
> 而「重新發布不下架舊產物」那條要再加一層:舊 URL 不只留在 NAS,
> 還會留在 CDN,即使 NAS 上的檔案已經不在。

**這一輪的處置**:NAS 上已刪乾淨(整個 feed 目錄消失,旁邊 12 個正式節目一個沒動),
但 `~/.cf_token` 這顆 token **只有 zone 讀取權、沒有 `Zone.Cache Purge`**
(`/user/tokens/verify` 過、查 zone 過、`purge_cache` 回 `code 10000 Authentication error`),
所以兩個 mp3 的 edge 快取**還沒清掉**,要使用者自己處理 —— 指令見報告。
內容是 15 分鐘的技術討論測試音檔,不含任何敏感資料。

---

### ⚠️ slide_deck 長時間卡在 `in_progress`(這一輪的異常)

`bc5185e7…` 於 `14:09:36Z`(台北 22:09)dispatch,到 **23:06 台北仍是 `in_progress`
—— 57 分鐘**,期間:

| 時間 | 事件 |
|---|---|
| 22:24 | `generate_slides` client timeout(900s),遠端不受影響繼續跑 |
| 22:57 | 第一次 `artifact_download_slides` 救援等待 timeout(1700s) |
| 23:06+ | 第二次救援等待進行中;同時另送一份新的 slide 生成(不同 `source_ids` 組合)當對照 |

`status history` 全程只有 `in_progress`,**沒有轉成 `failed`**,所以連
`artifact_retry_failed` 都用不上(那支只吃 failed 的)。

> **可記錄的事實**:同一本 notebook、同一批來源,`generate_audio` 11 分鐘完成、
> `generate_report` 約 1 分鐘完成,只有 `slide_deck` 卡住 —— 不是帳號問題也不是來源問題。
> **紀律上沒有為了趕進度關掉 `require_slides`** —— 那個旗標不是 await barrier,
> 關掉它正好是 skill 記載的 EP36 事故形狀(「還在生成中」與「使用者不要」長得一樣)。

---

## Findings 總表(依可行動性排序)

| # | 標題 | 性質 | 建議處置 |
|---|---|---|---|
| **D** | `strip_citations=True` **沒有**清掉 `**粗體**` / `_斜體_`,而 skill 明文說會 | **文件與實作不符**,影響公開 RSS | 二選一:改 `tool-reference.md:588-590` 的措辭,或在 server 端補清 inline 標記。**`episode_set_description` 也不清**,所以會直達 `<description>` |
| **E** | `render()` 把 CODE_BLOCK **整段靜默丟棄**(不是 U+FFFC) | **靜默資料遺失**,讀起來通順所以難發現 | 加一致性檢查:`render()` 與 `.text` 的 block 數不一致就 fail-loud;skill 補「show notes 的提問不要引出程式碼區塊」 |
| **F** | `notebook_get` 撞權限不足時拋**原生 `ClientError`**,沒翻成 `NotebookAccessDenied`、沒有修復指引 | **不對稱**(`_list_sources` 有翻譯) | 套用 `_sources.py` 現成的翻譯層。skill 引導「生成前先 `notebook_get` 確認」,這是最容易先撞到的一支 |
| **G** | `research_wait` 傳**pool 內但非發起者**的帳號 → 等滿 timeout(預設 30 分鐘)才死,訊息只說 `no_research` | **可用性缺口** | 正確的診斷文字已經寫在另一條路徑上,把它帶進 timeout 訊息即可 |
| **B** | 憑證結構合法但 cookie 已死時,`from_storage` 的 `_LoginRedirectError` 原樣穿透,**不指名槽位** | **可觀測性缺口**;9 槽任一槽過期就整台起不來且看不出是哪槽 | `app.py:341` 的 `enter_async_context` 包 try/except,重拋時帶 `_slot_env_name(slot)` |
| **A** | README §2a 的掃描腳本寫死 `range(1, 8)`,但 stg 有 **9** 槽 | **驗收腳本本身的漏洞** | 改成掃到沒有為止(prd 若也超過 7 槽會踩同一顆) |
| **C** | README §3b 要求驗 `source_add_text` 的「idempotent」,但它**不冪等**,工具也沒宣稱過 | **README 對帳點寫錯** | 修 README;另記呼叫端紀律:重試會長出重複來源,推高 9 筆守門 |

**正向結案(從未知變成已知)**:

- `Artifact.source_ids` **在生產資料上有值**,而且是**生成當下的歷史快照**
  (含已刪除的 source id)→ 可從「純觀測」升格,但**不可拿來反查現存 source**。
- 0.8.1 的**預設 host `notebook.google.com`** 與既有 Doppler 憑證**通用**(原本只是推導)。
- `notebook_get` 的 `role` 在真實 notebook 上**解得出來且分得出 OWNER/EDITOR**。
- `set_users` 的 **upsert 語義**(不踢掉 pool 外協作者)在真實伺服器上**成立**。

**仍然未結案 / 未觀測(如實標記,不猜成通過)**:

- `role is None` 時 `is_owner` 樂觀回 `true` 的那個危險分支 —— 這輪**沒撞到**。
- **配額耗盡**(2c-4)—— 9 槽全新鮮,自然沒撞到;不人為偽造。
- `artifact_retry_failed` —— 沒有 failed artifact,不為了測它去弄壞東西;
  0.8.1 改回 `status="pending"` 的行為**未觀測**。
- `podcast_attempt_adopt` 的**成功路徑** —— 需要真實的 unresolved reconciliation 狀態
  (client cancellation 剛好落在 dispatch 那 7 秒內),這輪只驗到安全拒絕。
- `generate_report` 的**識別碼引用正確性** —— 講義零識別碼,只證得了「沒編造」。

</content>
