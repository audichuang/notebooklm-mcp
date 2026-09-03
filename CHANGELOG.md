# Changelog

各版本**改了什麼、為什麼**。AGENTS.md 只放**還在生效的紀律**;版本敘事、事故經過、
已經修掉的舊行為都放這裡,免得 AGENTS.md 被歷史撐爆。

深入的專題另有獨立文件:
[notebooklm-py 0.8.0 升級筆記](docs/notebooklm-py-0.8-upgrade.md)、[ADR](docs/adr/)。

## v0.9.25 — 跟上 notebooklm-py 0.8.2:contract tripwire 下沉 `_web.*`,新增 `source_search`

### 跟上 notebooklm-py 0.8.2:contract tripwire 下沉到 `_web.*`,pin 抬到 `>=0.8.2`

**症狀**:上游 2026-09-02 發 0.8.2。我們的 pin 是 `>=0.8.1,<0.9`,**它本來就吃得下**
——四台生產機下一次 `uv tool install` 就會靜默換上 0.8.2,不需要我們同意。對 0.8.2 跑
全套離線測試:13065 passed / 3 failed,三條全在 `tests/test_contracts.py`。

**根因**:0.8.2 把 client 拆成 web / android 兩個 backend。公開 facade
(`ArtifactsAPI` / `SourcesAPI` / `NotebooksAPI` / `SharingAPI` …)變成 ABC,方法體只剩
`raise NotImplementedError`,web 實作搬到 `notebooklm._web.*`;`_rpc_executor` →
`_web.transport.executor`、`_artifact.generation` → `_web.artifact.generation`、
`_source.{content,listing}` → `_web.sources.{content,listing}`。**production code
一行都不用改**(我們只碰 `notebooklm.types` / `exceptions` / `rpc.types` / `_auth.*` /
`_runtime.is_auth_error` / `_research` 這些沒搬家的東西,13065 條全綠可證)。

**這次真正的收穫是那條沒紅的**:`test_rename_false_no_longer_short_circuits` 只紅了
sources 那一半,artifacts 那一半**照樣綠**——因為 `getsource(ArtifactsAPI.rename)` 抓到
的是 ABC 的 stub,而 `"if not return_object and not future_errors_enabled()" not in stub`
恆真。同一個機制也讓 `getsource(NotebooksAPI.get_raw)` 裡的 `GET_NOTEBOOK` 斷言變成
死條文。**對 ABC facade 抓原始碼不會爆,只會靜默失去意義**——tripwire 半盲比全紅危險。

**修法**:讀 body 的 `getsource` 斷言全部下沉到 `_web.*` 實作;簽名斷言(`_params`)
留在 facade——那是兩個 backend 共同的契約,也正是我們實際呼叫的東西。紅線寫進 AGENTS.md
的 `test_contracts.py` 那一列。另補一條 `test_default_backend_is_still_web`:預設翻成
android 的話,cookie 那一整套(`_auth.psidts_recovery` / keepalive /
`_sanitized_auth_entries`)會變死碼,而下沉到 `_web.*` 的斷言驗的是 web 實作、**照樣全綠**,
所以預設值本身要有人釘。

**pin**:`notebooklm-py>=0.8.2,<0.9`(含 `login` extra)+ `uv.lock` 0.8.1→0.8.2。抬下界
不是功能需求(production 兩版都跑),是為了「測的」與「裝的」是同一版。0.8.2 另外兩項
碰到我們的變更,查完都不用動:①#2296 寫入操作被拒不再回報成功——`_web.sharing.set_users`
仍是 `allow_null=True` + `return get_status(...)`,**沒有**驗 grant 有沒有生效,層次跟
`_share_each` 的 `get_status` 語意後檢不同(RPC 層 vs 語意層),兩道都留;②web chat 的配額
耗盡改 raise `RateLimitError`——`chat_ask` 不吃例外、chat 也不在 `_failover` 迴圈裡,純粹是
更好的錯誤;③SourceType code 14 由 `GOOGLE_SPREADSHEET` 改判 `GOOGLE_DRIVE`——我們的工具
不序列化 source type,無感。另記一筆:#2268 修掉的正是我們 `requires-python <3.13` 註解裡
點名的 3.14 `inspect.signature` bug(上游 CI 現在跑 3.10–3.14 全矩陣),**但 3.13 我方仍未驗,
這輪不動 ceiling**。

**不靠上游 CHANGELOG,逐項對帳過**(方法留著,下次升級照跑):兩版各開一個 venv,把我們實際
依賴的表面 dump 成文字再 diff——① 所有 enum 成員與**整數值**(`AudioFormat` / `AudioLength` /
`SlideDeckFormat` / `SlideDeckLength` / `ReportFormat` / `SharePermission` / `GrpcStatusCode` /
`ShareViewLevel` / `ArtifactType`)**逐字相同**;② 我們呼叫的每一支 method 簽名相同(只有型別
註解的字串化形式變了);③ exception 階層零移除、零改父類別,只新增 `PlayBookNotExportableError`
與 `UnsupportedOperationError` —— `RateLimitError` / `ArtifactFeatureUnavailableError` 沒動,
`_failover.REFUSED_WITHOUT_DISPATCH` 的前提完好;④ 13 個我們直接 import 的私有符號全在、簽名相同;
⑤ 把行為函式的原始碼 hash 起來對(`StructuredDocument.render/.text/.slice`、
`SourceFulltext.find_citation_context`、`normalize_rpc_code`、`is_auth_error`、
`_sanitized_auth_entries`、psidts 四支、`_file_lock_try_exclusive`)——**17 支裡 16 支逐字相同**,
唯一變的 `_normalize_import_verification_url` 用 19 個 URL 實測輸出完全一致(它變成 alias,邏輯照搬)。
`_auth/` 裡我們碰的四個檔(`cookies` / `keepalive` / `psidts_recovery` / `storage_lock`)**0 行變動**;
變動集中在 `browser_capture`(互動登入,只有本機)、`mint_service`(master token)、
`session`(新增 epoch fence,是 in-process 的,取代不了我們跨機的 flock)。

**唯一真的有行為變化、而且離線測不到的一塊:下載路徑的憑證附掛方式。**
`download_audio` / `download_report` / `download_slide_deck` 每一集都跑。0.8.2 引入
`CredentialPolicy`:`_on_request` 每一跳先把 `cookie` / `authorization` /
`proxy-authorization` **全部拔掉**,再依政策(https + 可信 host 才給)重套;另外新增
`_on_response` 把 redirect 的 `Set-Cookie` 收回外部 jar 供下一跳用。

⚠️ **這一段第一版寫錯了,v0.9.25-rc 驗收糾正**:原本寫「0.8.2 改成 `cookies=None`」——
那行**只在 curl_cffi 分支**(要 `NOTEBOOKLM_TRANSPORT=curl_cffi` 才走得到)。我們走的
**httpx 分支仍然傳 `cookies=cookies` 給 constructor**;真正的改動是 policy 被串進
redirect hook,由 hook 逐跳覆蓋。驗收工作區的 §8a 斷言照著這個錯誤描述寫,**會過,
但驗的是走不到的那條路** —— 已改成驗「policy 有沒有串進 hook」。

**信任邊界沒動**——`_TRUSTED_DOWNLOAD_DOMAINS`(`.google.com` / `.googleusercontent.com`
/ `.googleapis.com`)與 `_is_trusted_download_host` 兩版逐字相同。

✅ **v0.9.25-rc 驗收實跑通過**:4 跳跨 host 全通、兩次下載位元組相同。這一塊因此從
「離線測不到的核心風險」降級成下一輪的順帶對帳。

**`NOTEBOOKLM_BACKEND` 改成 fail-closed**:0.8.2 起 `from_storage()` 沒傳 `backend=` 時會讀這個
環境變數,`"android"` 會把整個 client 換成 master-token + gRPC 的 namespace ——
我們的憑證是 cookie snapshot,換過去等於拿一個沒有的憑證去打沒驗過的傳輸層,而
`test_default_backend_is_still_web` 驗的是 `env=None` 的預設值、**看不見環境裡真的有人設了**。
所以把它加進 `app._INLINE_AUTH_ENV_OVERRIDES` 設成 `None`(inline auth 期間刪掉),
理由與另外四個不同(不是 cookie 重鑄),在該處另列。上游沒導出常數,名字由
`test_default_backend_is_still_web` 對回 `_client_assembly` 的原始碼釘住,改名時會紅。

**`Source.kind` 的標籤會變(但我們碰不到)**:`SourceType` 新增 6 個成員,code 14 由
`GOOGLE_SPREADSHEET` 改判 `GOOGLE_DRIVE`。`source_list` 確實序列化 `kind`
(`tools_basic.py:849`),所以**如果**筆記本裡有 Drive 檔案,那一列的 `kind` 字串會變 ——
我們只加 URL / 檔案 / 純文字來源,實務上碰不到,但呼叫端若對 `kind` 做字串比對要知道這件事。

**`audio_finalize` 的 rename 不用改**:#2296 讓寫入被拒不再回報成功。那條路本來就是
「catch `Exception` → `_artifact_title_state` 實查 → 沒落地就標 `outcome_unknown` 並 re-raise」,
成功時也照樣後檢。新增的 raise 只是走進既有的實查分支,而真正危險的方向(假成功)本來就擋著。

**順帶挖到一筆跟 0.8.2 無關的舊帳**:`_REPORT_FORMAT` 白名單漏了
`ReportFormat.CONCEPT_EXPLANATION`(0.8.1 / 0.8.2 都有),跟當年漏掉 `CUSTOM` 看起來是同一個
根因 —— 上游有這個成員、白名單沒有。**於是我把它加進白名單,而那是錯的** ——
它在 web backend 生不出來,真實驗收才抓到。完整推導與修法見下面那節;
**發出去的這一版沒有 `concept_explanation`**。

**同一輪補的 tripwire**:`test_every_sdk_enum_member_is_mapped_or_explicitly_declined`
逐個 enum 檢查「SDK 的每個成員,要嘛在我們的 map 裡、要嘛在 `_DECLINED` 裡且寫得出理由」。
它擋的是「沒人注意到」,**擋不了「注意到但判斷錯」** —— 而且它問的問題本身就把人推向錯答案
(見下)。所以最後這一版是兩條一起守:這條保證**有人做過決定**,新增的
`test_report_format_whitelist_matches_what_the_web_backend_can_dispatch` 保證**那個決定對**。

**方法本身收成 `scripts/compare_sdk_surface.py`**:這一輪的 dump/diff 不是一次性腳本 ——
上游是月更節奏(0.8.0 七月 / 0.8.1 八月 / 0.8.2 九月),而 CONCEPT_EXPLANATION 這種帳
**只有逐項比對 enum 值才看得到**,讀 CHANGELOG 一萬次都不會出現。四個區塊(enum 值 /
dataclass 欄位 / 呼叫簽名 / 行為函式 hash)各防一種漂移,輸出是穩定排序的純文字所以
`diff` 就是報告。`release-checklist.md` 的依賴對帳一節指向它,並寫明**新增 SDK 呼叫點時
要一起加進腳本的 `_CALLS`** —— 那份清單就是「我們的依賴表面」的定義。

### 配上 0.8.2 的新能力:新增 `source_search`(13 支新 API 只收這一支)

把 0.8.2 的 13 支新公開 API 逐一對回**我們自己有紀錄的痛**,只有一支對得上:

| 新 API | 判斷 |
|---|---|
| `sources.search` | **收**。見下 |
| `chat.cancel` / `chat.session_status` | 不收。SDK 自己的 docstring 講明「Google 停止送 answer frame,但**不會關掉既有的 Web streaming response**」,而我們在這個 repo 的取消曝險是在**數分鐘**的工具(`podcast_episode` / `research_wait`),`chat_ask` 不是。配額 failover 那條路另外被 ADR-0010 擋著(`chat_ask` 沒有 manifest = 沒有稽核面 = 不准換帳號) |
| `research.discover` | 不收。`research_start(mode="fast")` **本來就立刻回 task_id**;`discover` 是阻塞 ~8s 才回,而被取消的呼叫**沒有落地的 task_id** —— 那正是 ADR-0001 拆成三支要保住的性質。省一次往返,賠一條紀律 |
| `artifacts.get_customization_choices` | 不收。要連網,進不了離線 contract test;它能抓的白名單漂移,已由這一輪新增的 `test_every_sdk_enum_member_is_mapped_or_explicitly_declined` 離線擋掉 |
| `notebooks.copy` / `sources.copy` / `artifacts.copy` / `sources.append_text` / `sources.add_urls_async` / `notebooks.suggest_next_steps` | 不收。對不到任何有紀錄的痛(每集只加一篇文章;回錄外洩的根因是 `source_ids`,ADR-0007 已解) |
| `sources.list_play_books` / `add_play_book` | 不收。用不到 |

**為什麼 `source_search` 收**:我們**已經手工做過這件事的低配版** —— `source_fulltext` 的
`max_chars=0, contains=[…]` 是把**整份全文拉回來**在自己的 Python 裡做精確子字串比對
(還得先 `_norm` 繞開 NotebookLM 對 CJK 插空格)。`sources.search` 是伺服器端的語意排序檢索,
回的是**段落原文 + 全域 rank + source 內 offset**,不是布林值。

**兩支不互相取代,所以都留**:`contains` 是**精確**比對,答「這個詞有沒有真的進到 body」——
驗擷取用它,語意檢索在這裡會答錯(會回「在講那件事」但不含該詞的段落);`source_search`
答「哪幾段在談 X」並附證據原文。tool 與 skill 兩邊的文件都把這條寫死。

真正的價值在**生成之前**:本 repo 最貴的失敗是 semantic QA 拒收(`gotchas-publish.md` 記的 EP46
連五次拒收、五個 attempt 全撤回 = 五次生成作廢)。生成前用檢索核對 brief 的說法在來源裡站不站得住,
比生成完再聽出問題便宜一個數量級。

**實作是薄的,而且刻意薄**:參數驗證(空 query、`source_ids` 型別、`limit` 正整數)全部交給 SDK 的
**backend-neutral** `_sources.validate_search`,在打 RPC 之前擋;我們不重寫任何一條 —— 第二份規則就是
會漂的規則。這個前提由兩條 contract 測試釘住(簽名/欄位 + `validate_search` 的實際行為,含
「`str` 也是 Sequence,不擋就會把 `"abc"` 拆成三個 id」這種)。第一版把 `getsource` 斷言指向
`_web.sources.search` 時**紅了**,才發現驗證住在 backend-neutral 層——那比原本的猜測更好,
斷言因此不必跟著 `_web.*` 下沉。

**沒加 contract term**:`REQUIRED_CONTRACT_TERMS` 收的是「host 照 skill 跑流程時必須傳/必須讀、
否則走不完」的欄位;`rank` 的 `0` 語意是使用建議,不是流程閘,由 tool docstring 與
tool-reference 的 ⚠️ 承擔。工具名本身已被 `check_skill_sync` 的 tool-name 檢查涵蓋。

**未實測、文件已寫明**:索引涵蓋範圍不知道 —— 上傳的 mp3 逐字稿搜不搜得到沒有實跑證據,
docstring 與 tool-reference 都要求呼叫端別預設它一定在。這是唯讀 RPC,零遠端副作用,
所以照 AGENTS.md 以離線測試 + contract pin 結案;要確認索引涵蓋範圍再開一次唯讀實跑即可。

跨 repo:`audi-skill/notebooklm` 的 `SKILL.md`(路由表 + §來源對帳)與
`references/tool-reference.md` 同步加了 `source_search`,`check_skill_sync` 從 36 → 37 支。

**驗收**:沒碰遠端副作用路徑(只改測試 + pin),照 AGENTS.md 的準則以離線測試結案。
SDK 內部搬家記進 [docs/gotchas-sdk.md](docs/gotchas-sdk.md);
`test_notebooklm_py_lower_bound_excludes_0_8_0` 改名為
`…_excludes_versions_we_cannot_import`,同時擋 0.8.0(缺私有符號)與 0.8.1(無 `_web.*`)。

### 真實驗收(v0.9.25-rc,stg)抓到一個必須擋發版的東西:`concept_explanation` 是死選項

**症狀**:`generate_report(report_format="concept_explanation")` 在我們唯一能用的 backend 上
**必定失敗** —— `Unsupported report format <ReportFormat.CONCEPT_EXPLANATION>; expected one of:
briefing_doc, study_guide, blog_post, custom`。也就是說,上一段那個「白名單漏了 SDK 成員」
的修正**開出了一個永遠生不出來的選項**,而且它已經寫進了 skill 文件。

**根因(比表面深一層)**:0.8.2 把 client 拆成 web / android 之後,`ReportFormat` 仍是
**backend-neutral 的 enum**,但 dispatch config 是 **backend-specific** ——
`_web.params.artifacts._STATIC_REPORT_CONFIGS` 只有三個,`CONCEPT_EXPLANATION` **只在
`_android` 有**,而我們釘在 web(`test_default_backend_is_still_web`)。

**離線為什麼沒抓到 —— 是我新寫的 tripwire 把人推向錯誤答案。**
`test_every_sdk_enum_member_is_mapped_or_explicitly_declined` 斷言「每個 enum 成員都要進
白名單或進 `_DECLINED`」,於是「加進白名單」看起來就是讓它變綠的正解。**它驗錯了對象**:
拿 backend-neutral 的 enum 當「我們生得出什麼」的判準。

**修法不是把它放回 `_DECLINED` 了事**,那治不了根因。新增
`test_report_format_whitelist_matches_what_the_web_backend_can_dispatch`,直接對
`_STATIC_REPORT_CONFIGS` 驗:**我們開放的每個靜態格式,web 都要生得出來**
(`CUSTOM` 走 `custom_prompt` 不是靜態模板,單獨排除)。反向也擋 —— 上游哪天把新格式加進
web 的表,它會告訴我們可以開放了。已做突變驗證:把 `concept_explanation` 加回白名單即紅,
訊息直接指名是哪個格式、web 現在支援哪三個。

**連帶修掉一個誤標**:那次失敗被記成 `attachment_acceptance_unknown`,而實測 `CREATE_ARTIFACT`
打了**零次** —— 呼叫端被叫去跑一次註定撈不到東西的對帳。`tools_artifacts` 其實**早就有**
正確紀律(「純本地轉換擋在 closure 外」,`to_report_format()` 本來就在外面);是白名單放行
讓本地錯誤穿過護欄跑進 closure。補了零 dispatch 回歸測試(釘 `assert not …calls`,
不釘錯誤訊息長相 —— 後者會隨上游措辭漂)。

### 驗收把三個「未實測」關掉了,也糾正了我對下載路徑的描述

覆蓋 33/37 工具實跑(4 支寫明理由;唯一實質缺口是 PDF 下載型別判定 —— 簡報三次都沒生出來)。

**可以拿掉「未實測」的三件**:
- **`source_search` 的索引涵蓋上傳媒體** —— 一集回錄 mp3 的 **27 段全部檢索得到**。
- **`rank` 實跑一次都沒遇到 `0`** —— 但契約沒改,`0 = 沒給排名` 的判斷保留。
- **下載路徑 4 跳跨 host 全通、兩次位元組相同** —— 這一塊從「離線測不到的核心風險」
  降級成下一輪的順帶對帳。

**新量到、已回寫文件的四件**(全部進 docstring + skill,細節見
[docs/acceptance-v0.9.25-rc-findings.md](docs/acceptance-v0.9.25-rc-findings.md)):
- **`start`/`end` 只對無標記純文字準**:markdown 來源錯位 **44~170 字**(伺服器索引去標記後的
  文字),而**回傳裡沒有欄位分得出是哪一種**。offset 只能當「大概在哪」。
- **`limit=N` 是全域前 N 名**,不是每筆來源各 N 段 —— 某一筆沒出現 ≠ 它沒有。
- **`notebook_list` 只列「這個帳號開過的」**:9 個 pool 帳號全部 `notebook_get` 得到同一本,
  卻只有 **2/9** 在清單裡。配額 failover 換帳號後照標題比對會得到**假的「找不到」** ——
  這是 skill「manifest 優先」那條路由的**正確性**理由,不只是省一趟 RPC。
- **`chat_ask` 的回答尾端常被伺服器接一句自我推銷**(💡/🧠 + 「我可以為您設計一份測驗」),
  `strip_citations=True` 清不掉(那不是 citation),`episode_set_description` 的 preflight
  也攔不住(非空/不等於標題/自包含三條全過)。實測 **2/2 次都出現** ——
  直通就會進 Apple Podcast 的單集簡介。server 端刻意不自動剝:判準是語意不是字面。

**還有一件是我自己寫錯、驗收糾正的**:上一段原本說「0.8.2 把下載改成 `cookies=None`」——
那行**只在 curl_cffi 分支**。我們走的 httpx 分支仍傳 `cookies=cookies`;真正的改動是
policy 被串進 redirect hook 逐跳覆蓋。已更正,驗收工作區照著錯誤描述寫的 §8a 斷言也一併改成
驗「policy 有沒有串進 hook」——它原本**會過,但驗的是走不到的那條路**。

**驗收工作區的兩個範本缺陷也修了**(它們從 v0.9.0 起被逐字複製進每一個工作區):
§2 對 `tools_podcast` 源碼做字面比對,而 v0.9.16 把 failover 迴圈抽到
`_failover.dispatch_with_failover`、那行 return 跟著搬家 → 從此一路假紅;改成驗不變式
(keyword-only、三元組標註、直接 import 共用迴圈驗)。

## v0.9.24 — 凍結輸入的圍籬由 host 宣告

### `podcast_episode` 新增 `workspace_root`;推導出多節目容器就 fail-closed

**症狀**(podcast-lab 2026-08-30 獨立審查實測):五個把 manifest 放 show root 的節目
(`shows/<show>/series_manifest.json`),`load_frozen_generation_input` 推出的 workspace
= manifest 的祖父目錄 = **整個 `shows/`**。用 graphify 的 manifest 傳
`input_bundle_path="audicast/episodes/EP46-…/attempt-005"`,loader 照吃、`read_attempt_binding`
回 `None`;帶認證跑下去就是拿別節目的 brief 燒配額,並留下一筆說謊的 binding。

**根因**:圍籬規則刻意不限定 manifest 父目錄的**名字**(v0.4.x 教訓),卻隱含限定了**深度**
——manifest 一定在節目目錄下兩層。深度跟名字一樣不是安全邊界。

**修法**(ADR-0012):`workspace_root` 讓 host 宣告節目目錄,manifest 必須在它底下、
bundle 相對於它;沒傳時仍推祖父目錄,但祖父目錄裡還有**別的** `series_manifest.json`
(`os.walk`,不限深度、不跟 symlink)就拒,錯誤訊息指名要傳 `workspace_root`。
單節目佈局行為不變(既有 31 條 bundle 測試全綠,新增 3 條)。`manifest_workspace_sha256`
語義不變。**show-root 佈局的節目從此必傳 `workspace_root`**,bundle 路徑不再帶節目前綴。

### `publish_series`:manifest 頂層 `retired: true` → 任何 PUT 之前 raise

**症狀**(podcast-lab graphify 2026-08-30 Codex 審查):兩份退役 manifest 搬進 `archive/`
之後仍通過 ManifestStore schema、`show_id` 仍能打到第一季的舊 feed;今天 publisher 會在
resolve 擋下只是因為 8 個 `mp3_path` **恰好不存在**——legacy 集不比 sha,路徑一被「修好」,
Audicast 的音檔就會以舊節目名義上公網。目錄名與 README 只擋得住讀過它們的人。

**修法**(ADR-0013):`publish/state.assert_not_retired`,讀完 manifest 的第一步就叫,在
auth probe 之前。判準同 `publication_state`:缺席=照發、`true`=擋、其他值 fail-loud
(`is True`,`1` 不算)。只管發布層;不加寫入工具,標記走 `ManifestStore.update` 一次。
`retired` 進 `check_skill_sync` 契約詞。新增 7 條 publish 測試(含 `healthz_status=500`
證明閘排在任何網路動作之前)。

## v0.9.23 — 送出即正本:brief 全文進 attempt,發布端驗 mp3 出身

### attempt 內嵌送出的 brief 全文(314709b)

**症狀**(podcast-lab agent-memory 一季五集實測):送出的 inline brief 與磁碟
`brief.md` 的 sha256 全數不符——貼上會剝檔尾換行(傳輸層),更糟的是貼上瞬間的手改;
EP23 手改未記錄,事後三輪重建都對不回 attempt 記的 `brief_sha256`,實際生成輸入從此
沒有正本。

**根因**:attempt 只存 `brief_sha256`。雜湊證明得了「不一樣」,證明不了「送了什麼」;
「靠人記 divergence」這條補償路徑實測不可靠(五集完整度呈梯度,最差的一集直接失傳)。

**修法**:`_create_audio_attempt` 在 `brief_sha256` 旁存 `"brief"` 原文,episode/series
兩入口共用一處。既有比對(`_is_resendable_same_request`、series prepared-attempt 的
settings 逐字等值)都是逐欄位比,不受新 key 影響;manifest 進版控,送出 bytes 從此有正本。

### publish preflight 的 mp3 provenance 閘(e6c96d5)

**症狀**(podcast-lab retracted-slot-rename 2026-08-19):QA 拒收的音檔坐在預設檔名槽,
preflight 只 resolve「檔案存在」——存在即過,拒收版能冒充 canonical 上公網。
「認 canonical 一律比 sha」一直是 host 手動紀律,publisher 自己不驗。

**修法**:`_ensure_local_mp3` 之後、任何 PUT 之前,逐集比對 mp3 檔案 sha256 與
`output_attempt_id` 那顆 attempt 的 `finalize.download.sha256`:不符 raise(訊息帶兩邊
雜湊前綴)、attempts 非空但無 `output_attempt_id`(分不出 canonical)也 raise;
legacy(無 attempts)與舊 finalize 沒記 sha 的集放行,集號列在新回傳欄位
`sha_unverified_episodes`——「沒驗」明講,不能讀成「驗過了」,legacy 整季 republish
也不因驗不動而死(deferred 事故的形狀)。幽靈 `output_attempt_id` 由 ManifestStore
載入驗證的既有防線擋(這次補了測試鎖住)。

### 文件債的代價:`regeneration_source_ids` 誤修事件(818b2fe;audi-skill 33ddbde)

40ee6e7 加進 retract/series 停點回傳的 `regeneration_source_ids` 漏寫進 skill docs
半個月——兩份 host 紀錄如實引用它,反而被拿著文件當否定證據的稽核「修正」掉,
還連鎖到 skill 短暫寫入「沒有這個欄位」的假斷言(同日撤回)。契約詞閘補上
`regeneration_source_ids` 與 `sha_unverified_episodes`;教訓寫進 docs:**否定斷言
(「沒有 X」)也要對真 code 驗,文件不能當自己的否定證據**。

## v0.9.22 — 認證的兩盞燈:警告講得出「哪一種」,auth_check 看得到整個 pool

### 認證的兩盞燈:啟動 warning 講得出「哪一種」,`auth_check` 看得到整個 pool

**症狀是一次誤判,不是一次故障。** 有人讀了啟動時那則 routability warning
(「`__Secure-1PSIDTS` 已過期**或** scope 無法送到 accounts.google.com」),把它讀成
「prd 槽位 1 的憑證從 8/16 起在過期、續命已死」,並建議立刻重登。對過 Doppler 與真 RPC
之後診斷不成立:五槽 RPC 全通,PSIDTS 還有 280–340 天,8/16 到期的是與登入無關的 OTZ;
槽位 1 命中的一直是那個「或」的**後半**(scope 在 `.youtube.com`),而那是 v0.9.14 就記錄
在案、刻意接受的已知狀態。

**這則訊息在結構上就分不出兩種原因**:`would_trigger_inline_heal` 只回一個 bool
(它委派上游的 `_psidts_routes_to_rotate`,而那支把 expiry 與 domain 摺進同一個 predicate),
所以文案只能兩種都寫、用「或」連起來。讀的人沒有辦法從訊息本身判斷是哪一種。

- **`_cookies.describe_inline_heal_reason`** 把失敗拆成 `wrong_scope` / `expired` / `missing`,
  `heal_warning_detail` 據此產生精確文案,並明說**「refresh 做不到」不等於「現在不能用」**、
  要量後者請跑 `auth_check`。分類**直接複用上游同一組零件**(先 `_iter_routable_psidts_cookies`
  濾過期、再問 domain),不自己判 expiry/domain —— `_cookies.py` 的模組 docstring 講過
  「各寫一份是這個 repo 記過最多次的帳」。
  ⚠️ **它只產生訊息,永遠不 gate。** 決策仍然只有 `would_trigger_inline_heal` 一個出口
  (紅線見 `docs/gotchas-pool.md` §一③),所以分類算錯的代價上限是 log 裡一個詞不準,
  不是誤拒一份還能服役的憑證 —— v0.9.14 已經為後者付過學費。
  `test_heal_reason_is_message_only_and_never_gates` 守著這條。
- **`auth_check(all_slots=True)`**。原本它只探**當下作用中**那一個 client,而配額 failover
  會在生成中途換帳號 —— 所以「長跑前 fail-fast」這個承諾**只覆蓋 1/N**,真正被用到的槽位
  可能從頭到尾沒被探過。新增的掃描模式每槽各打一次真 RPC,回傳 `usable`(真 RPC)與
  `refreshable`(與啟動 warning **同一個 predicate**)兩欄。
  兩欄刻意分開:`usable=true` + `refreshable=false` 正是槽位 1 那種已知可服役的狀態,
  合成一盞燈就會被讀成故障 —— 那個誤讀真的發生過。`usable` 是三態,`null` 代表探測撞到
  **非認證**錯誤(網路/限流)、沒量到,不是壞掉。只在**每一槽都失效**時才 raise
  (還有一槽活著就回表,丟例外會把你要的診斷一起丟掉)。預設回傳形狀一個字沒動。

#### 獨立 review 抓到的三條(都已修,值得留下的是第一條)

1. **新訊息自己過度宣稱了。** 第一版 `wrong_scope` 的文案寫「cookie 本身沒有過期,這個
   槽位仍在服役」—— **兩句都超出量到的範圍**:混合情境(過期的 A + 未過期但 scope 錯的 B)
   會落在 `wrong_scope`,「沒有過期」在那裡是假的;而 routability **從來就證明不了槽位
   可不可用**。在一個專門修「訊息宣稱過頭」的改動裡寫出過頭的訊息。
   改成只陳述量到的計數(候選的 scope 在哪、另有幾筆連候選都進不了),可用性一律指向
   `auth_check`。`test_mixed_identity_wrong_scope_does_not_claim_nothing_expired` 釘住。
2. **`ok` 也必須是三態。** 早一版是 `any(active and usable is True)`,於是作用中槽位撞到
   Timeout(= 沒量到)時 `ok` 變 `false` —— 把「沒量到」壓回「不能用」,正是這支工具要
   消滅的那個混淆;而預設模式在同一情況是**原樣拋出**,根本產生不了 `ok:false`。
3. **`--help` 被自己的說明擠掉了。** `sync-auth.sh` 的 `-h` 是 `sed -n '2,25p'` 寫死行號,
   補上 `--config` 的警語之後,help 剛好停在「參數」標題,`--config` / `--profile` /
   `--storage` 一個都印不出來 —— 諷刺的是那次補的正是「`--config` 不能省」。
   改成 awk 動態印開頭那整段註解,行號不再會腐化。

### 重登指引會把人導向正式環境讀不到的 config

`RELOGIN_HINT`(認證真的失效時給人看的那段字)寫的是 `bash scripts/sync-auth.sh`,
而那支腳本的 `--config` **預設是 `dev`**,正式 server 跑的是 `doppler run -c prd`。
照著做的人會看到「✅ 同步完成!驗證成功」、重啟、然後**照樣壞**,而畫面上沒有任何東西
指向原因。這段字出現的時機正好是最沒餘裕慢慢查的時候。

- `RELOGIN_HINT` 與 `sync-auth.sh` 的用法區塊改成 `bash scripts/sync-auth.sh --config prd`,
  並點名「漏了它會寫進 dev」。
- **預設值刻意不改成 prd**:`dev` 是活的(AGENTS.md:prd 的 `PODCAST_TOKEN_SALT` 必須逐字
  等於 dev 的),改預設會讓既有 dev 流程靜默改指向。改成**不傳 `--config` 就出聲**:
  互動時停下來要一句 `yes`,非互動只警告不擋(不改壞既有自動化)。
- 兩處都補上「這支只更新**不帶後綴**的 `NOTEBOOKLM_AUTH_JSON`(＝槽位 1)」——
  多帳號 pool 的 `_2..N` 要各自重登再寫回,而原本的指引讀起來像一條命令修好全部。

### 安裝：`latest` 是 CI 維護的移動指針

消費端改裝 `@latest`,不再在 README / AGENTS / mcp-setup 寫死 `v0.9.x`。
`vMAJOR.MINOR.PATCH` 仍是不可變的發版錨點;推上去之後
`.github/workflows/retag-latest.yml` 跑 `scripts/retag-latest.sh`,把 `latest`
指到**最高**的 semver 發版(不是剛推上去的那個,誤推舊 tag 不會把指針往回拉)。
回滾仍用 `@vX.Y.Z`。uv 的 git cache 對移動 tag 不敏感,metadata 對不上就
`uv cache clean notebooklm-mcp` 再 `--force` 重裝。

## v0.9.21 — 封面模板的 Audicast 品牌變成 fail-closed 閘

**症狀**:《Spring 拆解室》整季封面上印著 `AI AGENTIC ENGINEERING`、
`audicast-agent-core.ts`、`import { AudicastAgent } from "@audicast/core"`,單集封面右上角
還有 `REF_ID: AUDICAST_SR2`。**全部上了公網**,而且發布全程零警告。

**根因是整條鏈沒有一環在看封面內容。** `cover_show.html` / `cover_episode.html` 是為
Audicast 寫死的——副標、裝飾用的 TypeScript 程式碼區塊、REF_ID 都是字面值,不是參數;
`--show-name` 只換 wordmark。而下游 `validate_artwork` 只驗尺寸與色彩空間,`readback.py`
也只驗這兩樣:**封面上寫了什麼,沒有任何一環看得到**。唯一的攔截點是人眼,那次沒有人打開圖。

**修法是擋,不是改模板。** `_guard_audicast_branding` 在 `_find_chrome` / render 之前跑,
節目名不是 Audicast 就 `SystemExit`,訊息直接說明模板寫死了什麼、以及要沿用 Audicast 視覺
請加 `--allow-audicast-branding`。三個路徑都擋:批次(含 manifest 的 show title)、
`--show`、單集 one-off。

**為什麼不把模板參數化**:`_render` 對 `__HUE__` 以外的值全跑 `html.escape`,而那行 import
含語法高亮的 `<span>` 標記。escape 一個帶標記的預設值會改變 render 出來的位元組(破壞
content-hash 決定性),不 escape 則開出 raw-HTML 注入面。閘擋住了事故,參數化留給真的要
做多節目視覺時再處理。

**驗證**:同參數加 `--allow-audicast-branding` 的輸出與修正前 baseline **sha256 逐位元組
相同** —— 這是純加法,既有 Audicast 產物不會漂移。

### 同版另一項:`build_index_html` 的雙前綴

v0.9.20 之後 serial 的 title 本身一定帶 `EP{NN}. `,而節目頁的 `<li>` 自己又加一次
`EP{NN} —`,顯示成 `EP01 — EP01. 標題`。只在顯示層剝**該集自己的**集號前綴;錯配前綴
(第 2 集帶 `EP07.`)、半匹配(`EP1.`、`EP03.5`)與裸標題都不動,RSS `<item><title>`
完全不經過這裡。純觀感修正,不影響任何 URL 或 GUID。

## v0.9.20 — serial 節目的標題必須自己帶集號

**症狀**:三個連載節目上架後,訂閱者在播放器的清單上看不出集序。feed 完全合法 ——
`<itunes:episode>` 1–12 連續、`<itunes:type>serial</itunes:type>` 也在,`readback.py`
全綠。問題是**那個欄位多數播放器不顯示**:Apple 只在部分視圖秀,Spotify / 清單視圖
根本不顯示,聽眾看到的就只有標題。

**根因**:集號一直只存在於機器讀得到的地方(`itunes:episode`、manifest 的 `label`、
檔名),從來沒進過人看得到的地方(`<item><title>`)。三個節目 35 集都是這樣上線的。

**修法**:`publish_series` 的 preflight 多一道 —— `itunes_type == "serial"` 時,每集標題
必須以 `EP{NN}. ` 開頭,且**前綴的集號等於該集集號**。擋在第一個 PUT 之前。

四個決定:

1. **射程只到 `serial`。** `episodic` 是時事型、由新到舊聽,集號沒有意義 —— Audicast
   五十集都沒有前綴,把閘門套上去等於讓那個 feed 從此發不出去。不另加旗標:節目型態
   已經是季級設定,再開一個 `require_episode_number` 只是把同一件事說兩次。
2. **集號要比對,不是只驗 `EP` 開頭。** 複製上一集 brief 時留下的 `EP07. ` 會讓標題與
   `<itunes:episode>` 各說各話,那比沒有集號更糟(聽眾會相信標題)。
3. **只驗,不改寫。** 自動補前綴會讓 manifest 與 feed 說不同的話,而 manifest 才是
   canonical state;要改標題就走 `ManifestStore`。
4. **擋在 publish 而不是靠人記。** 同一個 feed 半數有前綴半數沒有,**只有訂閱者看得見**
   —— host 這邊發布成功、read-back 全綠、feed 合法,沒有任何一處會亮紅。

**生成端配套**(skill 才能叫 host 從大綱就寫 `EP01. 心法篇`):`naming.episode_label`
剝掉**匹配該集**的 `EP{NN}. ` / `EP{NN} ` 再套鐵律,工作室 artifact 與回錄仍是
`EP01 心法篇`。封面 `__TITLE__` 走同一道(徽章已經是 EPISODE NN)。publish **仍然
不改寫** feed 標題。

**已知的相鄰瑕疵(未修)**:`build_index_html` 自己會加 `EP{NN} — ` 前綴,標題帶集號後
那張 `<link>` 落地頁會顯示成 `EP01 — EP01. 標題`。只影響那張頁面,RSS item 不受影響;
修它要讓三個已完結的季各重發一次(約 1.3 GB),所以先留著。

## v0.9.19 — pubDate 隨集號遞增變成 preflight,不再只是一句註解

**症狀**:整季發布成功、GUID 與 URL 都對,但訂閱者的 App 裡集數是亂序的 —— EP01 顯示成
「最新一集」卡在列表第一個,EP04/EP05 互換。feed.xml 本身合法,`publish_series` 也沒有
任何錯誤。

**根因是一個從來沒被驗過的前提。** `published_at` 存的是**生成完成時間**,而整季幾乎都是
亂序生成的(EP01 最後才補:21:33,比 EP12 的 21:15 還晚)。播放器 —— 包含 Apple 自己的
列表視圖 —— 照 `pubDate` 排,於是集號順序與顯示順序脫鉤。`itunes:type=serial` 救不了:
Apple 文件說 serial 用 `itunes:episode` 排,**實測多數 App 仍以 pubDate 為準**;而
`build_feed_xml` 的 `lastBuildDate` 取 `eps[-1]`(集號最大那集)，非單調時它也不是真正
最新的時間。

「RSS 排序假設 pubDate 隨集號遞增」這句話**早就寫在 `tools_podcast._first_published_at`
的註解裡**(v0.6.0 修重生漂移時寫的),但它只在「一集一集依序生成」時自動成立,而
**沒有任何 preflight 驗過它**。註解不是檢查:同一條前提已經以兩種不同形狀出事(v0.6.0
的重生漂移、這次的亂序生成),兩次都是發布出去以後才在 App 上看到。

**修法:`publish_series` 的 preflight 多一道「嚴格遞增」**(`_assert_pub_dates_ascend`),
擋在第一個 PUT 之前。四個決定:

- **不綁 `itunes_type`。** 兩種型別都壞:episodic 的「最新在前」與 serial 的「依序收聽」
  都建立在同一個前提上。給 serial 一條分支就是只補一半 —— 而 episodic 節目照樣會亂序。
- **相等也擋。** 同一秒的兩集在播放器裡順序不定,而「不定」不是可接受的發布結果。
- **列出所有違反的相鄰對,不只第一對。** 只報第一對的話,12 集的季度要跑十幾輪 publish
  才知道要改哪些,而每一輪都是一次「以為修好了」。真實那次要動 11 集。
- **順手 fail-loud 掉解不出的 `published_at`。** 它原本**原樣**進 `<pubDate>`(壞字串會讓
  播放器自己編一個時間、或整條 item 掉),而單調性檢查本來就得 parse 才比得出大小 ——
  同一段程式碼涵蓋,不多花一分錢。`_effective_pub_date` 抽成一顆共用 helper,preflight
  與渲染看同一個值:各算一次的話,擋下的與發出去的可以是不同的東西。

**擋下之後要有受支持的修法,否則等於死路**(v0.9.18 學到的同一條:只做讀取端不算做完)。
`scripts/reorder_published_at.py` 把**現有**時間戳排序後依集號重新配對 —— **不發明新時間**,
只換配對,所以「這一季是什麼時候做的」這個稽核事實不變。走 `ManifestStore.update`
(ADR-0009 禁止手改 JSON),dry-run 預設,冪等。三件刻意不做的:
- **只重排會進 feed 的集**,`publication_state` 被扣下的(deferred)完全不碰 —— 與 preflight
  的檢查範圍同語義。把它丟進池子會讓 live 集拿到不屬於自己的時間,還改掉那一格的稽核時間。
- **缺 `published_at` 的混合狀態直接報錯不猜**:缺值的集在 feed 裡走 2020 的 fallback,
  「重排現有值」修不好順序,那需要**發明**一個時間戳,不在這支的授權範圍內。
- **池子裡有重複時間戳也報錯**:嚴格遞增靠換配對修不了。

**驗收**:離線測試 + 真實 manifest 推導結案,沒開 acceptance workspace —— 改動**只減少**
遠端寫入(preflight 多擋一種 manifest,不新增任何 RPC/PUT 路徑;腳本純本機 manifest 寫入)。
突變驗證九道全部轉紅(拿掉 preflight 呼叫、相等放行、只報第一對、preflight 改看全 manifest
含 withheld、吞掉解不出的時間、缺值就擋成誤殺 fallback 混合、腳本重排 withheld、腳本不擋
缺值、腳本不排序池子)。

**獨立複審(codex,只看成品、問「什麼是新的且沒被測試守住」)抓到七條,全部採納。**
最嚴重兩條都長在這一輪新加的東西上 —— 又是 v0.9.8 的同一個形狀,所以那個問法值得每次
修正輪之後都跑一次：

- **`published_at` 缺時區 offset 時,我原本「就地當成 +0800」是錯的。** 它只修好「比較」
  那一半:渲染端送出的仍是**原字串**,於是 preflight 與播放器看到的是不同的時刻 ——
  EP01 `10:00`(無 offset)、EP02 `03:00 +0000` 會被判成遞增而放行,播放器按 UTC 讀卻是
  `10:00Z > 03:00Z`,亂序照樣出去。改成**拒絕**(RFC-2822 的 `-0000` = 時區未知,同樣拒)。
  寬容製造了語意分歧,而本 repo 寫入一律帶 `+0800`,拒絕不會誤殺真實資料。
- **`ep.get("published_at") or fallback` 的 truthiness 判準有兩個漏網方向。**
  falsy 的值(`false` / `0` / `[]` / `""`)與「根本沒這個欄位」同形 → 靜默發布 2020 的
  fallback;truthy 的非字串(`123` / `[1]` / dict)讓 `parsedate_to_datetime` 漏一個沒被接住的
  `AttributeError` 出去。改成看**欄位在不在**(與 `publication_state` 同一條紀律),
  存在就必須是非空字串。
- **修法與 `backfill_published_at.py` 會互相打架,而且真實資料正好命中。** reorder 只改
  episode 頂層,但重生過的集在 `retraction.retracted_output.published_at` 另有一份首發時間
  (`_first_published_at` 讀它),backfill 會照它把頂層**改回去**。實測 podcast-lab 那份 50 集:
  6 集有這種歷史,而 EP42/EP43 的歷史值(07:18:43 / 07:17:36)**本身就是亂序的** —— 也就是說
  backfill 現在跑下去就會讓整季發不出去,這個洞在 reorder 出現之前就在了。
  **修法刻意不是 Codex 建議的新欄位**(`rss_published_at`):那要動 promote / backfill / 渲染 /
  文件四處,而它買到的只是「少跑一次 reorder」。改成①backfill 回填前先模擬「回填後還遞增嗎」,
  不遞增就拒絕並指向 reorder;②reorder 對這種集印警告。**最終防線是 preflight** ——
  任何路徑(含未來重生)造成亂序都會在上傳前被擋下,而修法可以重複跑,迴圈閉合。
- **`--apply` 要不要寫盤原本用鎖外 snapshot 判斷。** 兩個方向都錯:A 讀到已排序、B 隨即寫成
  亂序 → A 回「不需重排」而 manifest 留在亂序;反向序列則是零變更仍 `revision + 1`,平白撞掉
  別的 writer 的 `expected_revision` CAS。改成計畫與判斷全部在 `update` 的鎖內 snapshot 上算,
  零變更用 `_NoChange` 從 mutator 中止(v0.9.18 建的機制,這輪該用沒用)。
- **三條假綠**:①「preflight 與渲染看同一顆值」沒有測試守住(混合測試只斷言
  `episode_count`,把渲染端改成永遠 fallback 照樣綠)→ 改成直接鎖 `show.json` 的投影值;
  ②「列出所有違規對」只斷言 `EP01`/`EP02`/`EP03` 三個字串出現,誤報成 `EP01 >= EP03`
  (漏掉真正的第二對)也綠 → 改成斷言兩對完整的「左 >= 右」含時間戳;③腳本的重複時間戳
  拒絕分支完全沒測試。

順帶收掉的:「哪些集進 feed」的判準(含未知值 fail-loud)原本 inline 在 `publish_series`
的迴圈裡,而兩支腳本各自需要同一條 —— 收成 `publish/state.is_withheld`,三處共用。
漂掉的兩個方向都會出事:發布端扣下、腳本卻把它算進排序(live 集拿到不屬於自己的時間),
或反過來讓該扣下的集參與發布。`tools_publish` 那個迴圈同時短了 12 行。

**真實資料上當場抓到一筆還沒被發現的錯位**:`podcast-lab` 那份 50 集的 manifest(49 live +
EP46 deferred)EP42/EP43 的 `published_at` 顛倒(相差 49 秒)—— 上一季手動修掉的是另一份
12 集的,這份還壞著而沒人知道。preflight 指名了那一對,腳本 dry-run 給出兩行對調;另外
兩份 manifest 回「已遞增,不需要重排」(冪等路徑也一起驗到)。

## v0.9.18 — deferred 集擋掉整季 republish + 補上它缺的寫入端

**症狀**:`podcast-lab/output/series_manifest.json` 有 50 集,跑一次整季 republish
(例如只想換封面重發)在**任何 PUT 之前**整批 raise:

```
episode 46: mp3_path missing and no artifact_id to re-download
```

不是只掉 EP46,是**整季發不出去**。

**根因是 manifest 有一個發布層沒實作的狀態。** EP46 的音檔經五次 semantic QA 拒收、
全部 attempt 已撤回,host 依稽核紀律把它標成 `publication_state: "deferred"`
(附 `publication_state_reason` / `publication_state_at`),`mp3_path: null`、沒有
`artifact_id` —— 集數留在 manifest 才保得住完整 audit。但 `publish_series` 不認識這個
欄位,於是那一集照樣進 preflight 的「每集 mp3 一律 resolve 成真正存在的本機檔」迴圈
(v0.3.3 刻意提前的那一段),而它本來就沒有檔可以 resolve。**preflight 的 fail-fast 在這裡
反過來咬人**:它做對了自己的事(任何 upload 之前失敗),但判斷依據少了一格。

**修法是在 preflight 之前分流,不是放寬 preflight。** deferred 集完全不進那個迴圈 ——
封面/描述/附件/mp3 一項都不驗。**理由是它不進 feed,驗它的檔沒有意義,不是「它一定沒有
那些檔」**(真 EP46 的 description / cover / 簡報 / 講義都在,只缺音檔 —— 見下面驗收那段)。
三個一起補的決定:

- **未知 `publication_state` 值 fail-loud,不 fall through 成照發**
  (白名單正本在 `publish/state.py`)。第一版寫成 `!= "deferred"`,那是 fail-open:
  拼錯 `defered` 或未來新增一個狀態,就在這個欄位**該生效的時候**靜默公開,
  而 **feed host 永不刪檔,送出去的 mp3 收不回來**。獨立複審又抓到同一個洞的第二個形狀:
  判準若寫成 `.get() is None`,**顯式 `null`** 與「欄位不存在」同形而照發 —— 改成看
  **欄位在不在**,值只認明列的那幾個(空字串、`null`、unhashable 的 list/dict 都 raise;
  後者要先驗型別,否則 `in frozenset` 會漏 `TypeError` 出去)。
- **被扣下的集號回在 `deferred_episodes`。** 50 集的 manifest 回 `episode_count=49`
  而不說是哪一集不見了,讀起來就是「發布漏集」—— 這是本 repo 反覆出現的
  「不准靜默截斷」的同一條。
- **全季都被扣下 → raise**,不發空 feed:空 `show.json` 會把既有 feed 的集數整批清掉。

**新工具 `episode_set_publication_state`,因為第一版只做了讀取端。** 獨立複審指出:
`publish_series` 讀這個欄位,卻沒有任何工具寫得動它 —— 而 `series_manifest.json`
只由工具寫入的紀律不允許 host 手改 JSON,於是「解除」在受支持的路徑上是**死路**
(EP46 之後生出可用音檔也解不開)。這支是唯一正門:三個稽核欄位一起寫、一起清,
可設的值 = publish 端會扣下的值(同一份白名單)。兩件實作紀律:
- **白名單搬到 `publish/state.py`。** 放在 `tools_publish` 讓 `tools_artifacts` 去 import,
  會在「先 import `tools_publish`」的路徑上炸循環 import(`tools_publish` → `app` →
  註冊 `tools_artifacts` → `tools_publish` 只初始化到一半)。**實測被
  `test_publish_tools.py` 的 module-level import 抓到**,不是預防性重構。
- **「沒變更就不寫盤」用 `_NoChange` 從 mutator 中止。** `ManifestStore.update` 的 revision
  無條件 +1,冪等重放平白 +1 會撞掉別的 writer 的 `expected_revision` CAS;而**不能**先
  `read()` 再決定要不要 `update()` —— 兩次呼叫之間別的 writer 插進來,判斷就過期了。
- **不自動解除**:生成完成不等於 QA 通過,放行必須是顯式的第二次呼叫。

**刻意沒做的**:`podcast_series` 與 attempt / artifact / 清理義務掃描
(`_claimed_artifact_ids`、`_unresolved_attempt_ids`、`_settle_cleanup_state`)**一律不看
這個欄位**。deferred 的語意是「待有受支持的生成入口後再修復」,拿它擋重生會把暫緩變成
永久除名;而 attempt 級掃描要看**全** manifest 才算得出正確的清理義務與 artifact 歸屬。
契約寫進 [gotchas-publish](docs/gotchas-publish.md)、skill 的 `tool-reference.md`
**與每次載入的 `SKILL.md`**(host 是寫入方,只活在一行註解裡等於沒有契約),
`publication_state` / `deferred_episodes` 也進了 `check_skill_sync.py` 的硬檢查 ——
只補 references 的話,主路由層的「每集音檔全綠才可發布」交付清單會讓 agent 停在清單那一步,
**根本走不到已經會正確跳過它的 publisher**(這是 v0.9.12 那個洞的同一個形狀)。

**第二輪複審(只看成品、問「什麼是新的且沒被測試守住」)抓到的,最嚴重兩條都長在
上面那支新工具上** —— 正是 v0.9.8 的同一個形狀,所以那個問法值得每次修正輪之後都跑一次:
- **`_NoChange` 的回傳原本在鎖外重讀 manifest。** 那會拼出一份不屬於任何 snapshot 的
  response:「在 revision R 判斷沒變更」+「在 R+2 讀到的欄位」。改成整份 outcome 從
  mutator 裡帶出來。**序列跑的測試看不到這件事**(沒人插進那個縫),所以絆線直接鎖不變式
  —— 把 `ManifestStore.read` 換成會爆的版本,no-op 路徑仍必須成功。
- **`has_output` 欄位整個刪掉,因為它會說謊。** 它只看 `mp3_path`/`artifact_id` 的
  truthiness,卻在文件裡宣稱能回答「扣下是否等於下架」:已生成未發布的回 `True`
  (沒東西可下架)、已上線但 output 欄位被 retract 清掉的回 `False`(其實會下架)、
  路徑指向不存在的檔也回 `True`。真的要答準得讀 ADR-0003 的 deployment snapshot,
  不是 episode projection 推導得出來的 —— **寧可不給,也不留一個好看的近似值**。
- **「沒變更」的判準要求三欄齊全。** legacy 資料(有 state + reason、沒有
  `publication_state_at`)只比對 state+reason 的話會直接 `_NoChange`,那個缺欄從此永遠
  補不上 —— 而「重呼一次修好它」正是呼叫端唯一能做的補救。
- **deferred 測試原本證明不了「整集跳過 preflight」**:`_publish` helper 顯式關掉
  `require_slides`/`require_report`,而 production 預設是 True。補一條走
  `_publish_with_defaults`、live 集帶齊附件、deferred 集刻意不帶。
- 另外三個假綠:`return_episodes` 若把 `deferred_episodes` 也拿去取交集(滾動加集的常見
  呼叫就永遠回空);兩邊同時把 `"deferred"` 寫死、共用常數沒人用(identity 測試照樣綠 →
  改用 sentinel 狀態跑真往返);`openWorldHint=False` 被刪掉沒有任何測試會紅
  (其他四份 annotation 清單都只收集 `is True`)。
- 四處文件互相矛盾一併修掉:tool-reference 還寫著「host 寫入、MCP 只讀」(新工具出現後就
  不對了)、兩處還在說 deferred 集「本來就沒有那些檔」、gotchas 指向改名前的常數。

**第三輪(告知「上一輪之後改了什麼」再打)只剩一條 blocker,而它又是同一個形狀** ——
**刪掉 `has_output` 時漏了 skill 的 `tool-reference.md`**:runtime 改乾淨了,文件還留著範例
與「照它判斷是否下架」的說明,照文件寫 `result["has_output"]` 直接 `KeyError`,而 CI 全綠。
根因是 `check_skill_sync.py` 只驗「必要詞存在」、**從不驗「已移除的契約不存在」**,所以補了
`REMOVED_CONTRACT_TERMS` 這道 forbidden-term 檢查(刻意只做字串黑名單:真正的失敗模式是
「忘了刪文件」,不是「文件寫錯型別」)。同輪順手把「三欄齊全」收緊成**時間戳解析得動且
是 UTC** —— `"publication_state_at": null` 是 key 存在的,而它承載的稽核脈絡是零,
當成合法就等於讓那個壞欄永遠補不上。

**刻意留著沒做的**:與既有 writer 的「互不碰欄位」只有 targeted-update 的實作保證與一條
attempt/output 不變測試,沒有 set→publish→promote→clear 的全欄位整合測試(複審點出
「在 attempt promotion 裡自動 pop 掉 publication_state,所有測試仍綠」)。那是一條真的缺口,
但它要跨 audio finalize 那一整條路,規模不對稱 —— 記在這裡,不假裝守住了。

**驗收**:離線測試 + 既有實測前提推導結案,沒開 acceptance workspace ——
改動**只減少**遠端寫入(過濾掉一集,不新增任何 RPC/PUT 路徑;新工具純本機 manifest 寫入),
`publish/` 是純邏輯,而「EP46 這個形狀會讓 resolve 迴圈 raise」是拿生產 manifest 直接驗到的。
突變驗證做了兩輪:自查三道(未知值改成照發、拿掉 `deferred_episodes`、deferred 不過濾),
複審又點出四道會存活的(過濾條件順手多看 `cover_path`、缺席判斷改回 truthiness、
全季守衛綁 `len(all_eps)==1`、`withheld` 覆寫成單元素)—— 七道現在全部會紅。
其中「順手多看 `cover_path`」那道也修正了一個**事實錯誤**:真 EP46 的 description /
cover / 簡報 / 講義**全部齊全**,只缺 `mp3_path` 與 `artifact_id`;第一版的測試與文件
都寫成「它本來就沒有那些檔」,於是極簡 fixture 對那個假綠毫無防護
(`_deferred_ep_production_shape` 就是為此存在)。

## v0.9.16 — 附件家族接上配額 failover,迴圈收成一份

**症狀**:pool 裝了 5 個帳號,`generate_slides` 連敲三次全部落在同一格 —— 而同一時間
`generate_report` 兩次都成功(同一個帳號),證明那個帳號還活著,只是 slides 這項被限流。

**根因不是「忘了接」,是稽核面的形狀。** failover 的入口 `_rotate_for_quota` 第一行就是
`if store is None: return None`,而它要 `(store, episode_n, attempt_id)` 三個東西才寫得出
`_record_dispatch_failover`。附件家族**沒有 attempt 記錄**(manifest 裡只有 `slides_pdf_path`
一個欄位),所以即使 `generate_slides` 手上有 `manifest_path`,也沒有地方寫「A 拒絕 → 換 B」
—— ADR-0010 §Transparency 的紅線是「沒地方記錄就不准靜默換帳號」,於是它只能
`runtime.get_client()`。**這條在 v0.9.3 驗收就被抓出來(FINDING-4),v0.9.5 明確決定不修**,
理由是「真要修得先有 durable dispatch receipt」。

**這一版推翻的是那個理由的適用範圍,不是那條紅線。** ADR-0010 要的不是 attempt 結構,是兩個
問題答得出來 ——「這集簡報是哪個帳號生的」「哪個帳號被拒過」。兩者都寫得進 episode 記錄。
所以附件的稽核面是 `slides_account` / `report_account` + append-only `attachment_errors`,
**durable attachment attempt 這筆債刻意留著**(理由見
[ADR-0011](docs/adr/0011-attachment-failover-buys-audit-with-an-episode-field-not-an-attempt.md):
failover 的正確性不需要 attempt,重入的正確性需要,而重入在**沒有** failover 時就已經壞了)。

**比「接上去」更重要的是沒有複製一份。** 迴圈整條抽到 `_failover.py`,音檔改成 wrapper:

- 五條紅線(集合不准長大、「有 id + failed」不准 rotate、權限不 rotate、終止性不靠冷卻
  時鐘、`tried` 擋在稽核寫入之前)現在只有一個產地。每一條都是一次真實事故 —— 而這一條有
  前例:v0.8.0 的 failover 只補了 `_run_episode`、漏了 `podcast_series` 的重送分支,pool 對
  「重試」這條最需要它的路完全無效(驗收 F-4)。
- 家族差異用 callback:`record_failover=None` = 沒有稽核面 = 一律不換帳號;
  `on_clean_refusal` / `on_acceptance_unknown` 只有「有 durable attempt」的呼叫端才傳
  (附件沒有,終態就是原樣拋 —— 遠端什麼都沒建出來,沒有東西要對帳)。
- 順手收掉兩份重複:「拒絕原因 → `(type, message)`」的判讀(兩種形狀:0.8.0 的例外、0.7.x
  的 status 物件)抽成 `describe_refusal`;「補分享」的指引文字原本有兩份(`_errors` 那份指名
  notebook、音檔迴圈那份指名帳號,內容一樣連結尾都差一句),合成 `access_denied_error`
  之後兩個都指名得出來。

**沒被咬住的那一條值得記下來。** 五條紅線各做突變驗證時,「拿掉 `tried` 終止性」那一條
**新測試全綠** —— 因為真實 `rotate_client` 的冷卻在測試的時間尺度下自己就回 `None`,
`tried` 拿掉也看不出來。音檔那邊是靠 monkeypatch 的乒乓 `rotate_client`(從不回 None)守的,
附件補上同一條之後才咬住(而且**兩種 rotate 形狀各驗一次** —— 共用迴圈有兩處 rotate,
歷史上正是分開補的兩處)。**「全綠」不等於「守住了」,突變驗證是唯一分得出來的方法。**

### 這一版真正的教訓:第二輪複審要只看成品

離線實作之後派了 **兩輪** codex 獨立複審(`--effort max`),第二輪**刻意不給第一輪的
findings**,只問「這裡面有什麼是新的、而且沒被任何測試守住的」。結果:**第二輪回的五條裡
有三條長在第一輪修正新加的東西上**,而且它跑 probe 全部實測重現。

那三條的共同根因是一個概念錯誤:第一輪為了回應「crash 之後稽核答不出來」而加的**受理憑據**
(受理成功就先落 account + artifact_id)借用了 provenance 的欄位,而那組欄位的語意是
「**現在磁碟上這份**是誰生的」。於是:

- 舊成品還在磁碟上、重生受理後 `wait` 回 failed → 檔案是舊的,manifest 已聲稱是新的。
- 為它配套的並行 fence 自己是 TOCTOU:檢查通過後在下載的 `await` 裡被接手,最終
  「檔案 X、manifest Y」而**兩個工具呼叫都回成功** —— 那正是 fence 的 docstring 聲稱它
  換掉的東西(「把安靜地服務錯的 PDF 換成大聲失敗」)。**加了它比沒加更糟:它讓人以為
  競態被處理了。**
- 救援下載同形(先讀 provenance → `await` 下載 → 盲寫回去)。

**憑據與 fence 連同配套測試一起退掉了**,provenance 退回「成品落地時寫、與路徑同一次
`update`」。殘留的兩件事誠實寫進 ADR-0011 而不假裝修好:跨資源(檔案 vs manifest)沒有
原子性;同一集同一 kind 的並行仍是 last-writer-wins。

另外兩條也是第一輪修正引入的:①`language` / enum 的純本地轉換寫在 dispatch closure 裡,
`ValueError` 落進共用迴圈的泛用 except,被記成一筆憑空的「遠端受理不明」(實測 SDK 呼叫
次數 0),而那張表 append-only、清不掉;②`ManifestPostCommitError` 的容忍只補在 rotation
recorder,終態 callback 那半漏了 —— 單帳號撞配額 + parent-dir fsync EIO 時,呼叫端拿到的
從 `RateLimitError` 變成 `ManifestPostCommitError`,直接違反「原樣重拋」的承諾。
**又是「補一半」,而且是在修「補一半」的那一輪裡犯的。**

AGENTS.md 早就記著這個病灶(v0.9.8:三個視角、每條突變驗證、全套綠,發版後外部 review
仍抓到四條,兩條長在那一輪新加的東西上),根因是 prompt —— 審查者拿到的是「這幾條修正
對不對」,於是對照原缺陷逐條驗證,沒有人對成品重新問一次。**這一版特意分開問,而它立刻
兌現。以後大型修復輪一律照這個順序:實作 → 獨立複審 → 修正 → 只看成品的第二輪複審。**

順帶修掉一條與附件無關的既有缺陷:`_mark_acceptance_unknown` 收到 **status 物件**
(「有 task_id 卻 is_failed」那條分支傳的就是)時,用 `type(error).__name__` / `str(error)`
把稽核寫成 `type: "GenerationStatus"` / `message: "<... object at 0x7f...>"` —— 真正的原因
整條蒸發,卡在 `acceptance_unknown` 的人打開 manifest 只看到一個記憶體地址(實測重現)。
簽名宣告 `BaseException` 也一直是錯的。改用同一支 `describe_refusal`。

### 真實驗收(2026-08-17,stg):主要未知結案,風險模型錯一處

完整紀錄在 `docs/acceptance-v0.9.16-findings.md`。**核心那條成立**:`generate_slide_deck`
撞配額是**同步 `raise RateLimitError`**、0.63 / 0.75 秒(audio 是 1.35 / 1.43,同量級),
而最關鍵的 `artifact_list` 拒絕前後差集**三次量測全空** —— 零副作用拒絕在 slides 上成立,
failover 掛在對的分支。耗盡終態、權限不 rotate、真實並行下兩個稽核面零交叉、身分跟著
client 走到下載(兩腿 failover 後 16.4MB / 17 頁 PDF)全部得證。

**但抓到兩件文件說錯的事**:

1. **`REVISE_SLIDE` 是另一個配額桶。** 本版註解寫「改版也燒配額,**也會被同步拒絕**」——
   後半句量不到:在 slide_deck 配額已完全耗盡的槽位上連送 9 次 revise,9 次全部受理且
   fork 都真的完成(兩支走不同 RPC,伺服器按 RPC 分桶)。所以 revise 的 failover 對限流
   **沒有已知觸發條件**,而「被拒時會不會 fork 出孤兒 `(2)`」維持**待確認** —— 9 次沒打到
   不等於打不到。註解與 ADR-0011 都已更正。
2. **配額不是一個布林值,是 per-kind**,而且此前沒寫在任何地方(ADR-0010 讀起來像
   「一個帳號用完就換下一個」)。最硬的證據:slides 被拒的**同一帳號、同一 process、相隔
   2 分鐘**,audio dispatch 被受理。免費帳號 slide_deck 實測每日 3 次(n=2 槽)。
   已寫進 `docs/gotchas-pool.md` §三之〇,連同兩個誠實邊界(樣本小、未驗重置週期;
   「拒絕不消耗配額」是推論不是觀測)。

**還有一條沒被觸發**:`attachment_acceptance_unknown` 這一輪完全沒有自然發生,所以那條分支
**只有離線測試背書** —— 它的測試要一直維持突變驗證。

驗收自己也修正了一次過度樂觀:原本列了四條「該補成離線測試」的候選,逐條比對既有測試後
**只有一條是真缺口** —— 權限被拒走 `refused` 還是 `acceptance_unknown` 從來沒有斷言過
(既有那條只看 `_failovers == []`,而那在**兩種**終態下都成立)。補了
`test_permission_denied_is_a_clean_refusal_not_acceptance_unknown`,兩個突變點各驗過:
改成 `PHASE_ACCEPTANCE_UNKNOWN` → 新測試紅而**舊測試照樣綠**(缺口的直接證據);
改拿原始 `exc` 寫稽核 → 紅(順帶鎖住「稽核要寫在 `access_denied_error` 之後」的順序)。
其餘三條已被既有測試完整涵蓋,誠實記下來免得下一輪又「補」一次已經存在的鎖。

## v0.9.15 — v0.9.14 真實驗收抓到的五條(全部碰 runtime code)

v0.9.14 的驗收(Phase 0–10、35/35 支工具、FINDINGS 全文見
`docs/acceptance-v0.9.14-findings.md`)抓到七條,其中**五條碰到執行路徑**,所以獨立發一版
—— `v0.9.14` 那個 tag 指的是**沒有**這五條修正的 commit,不能讓它們搭便車。
另外兩條是驗收腳本自身的洞(§2a 掃描寫死 `range(1, 8)` 而 stg 有 9 槽;
§3b 要求驗 `source_add_text` 的冪等,但那支從未宣稱冪等),已就地修在驗收工作區。

五條的共同形狀值得先講:**四條的症狀都是「輸出看起來完全正常」** ——
少一段程式碼但句子通順、`**粗體**` 原樣進 RSS、等滿 30 分鐘才知道帳號傳錯、
9 槽任一槽死掉就整台起不來卻不說是哪一槽。這正是離線測試結構上照不到的那一類。

- **`chat_ask(strip_citations=True)` 沒清掉 inline 的 `**粗體**`**(FINDING-D)。
  `answer_document.render()` 只拿掉 **block 級**標記(`###` 標題、`*` 條列),inline 的原樣留著,
  而 skill `tool-reference.md` 明文說「不會出現」。實測含 `**` 的字串**原樣寫進 manifest**,
  直達公開 RSS `<description>`。新增 `_text.strip_inline_emphasis`,`chat_ask` 與
  `episode_set_description` 兩處共用(後者是進 manifest 的唯一正門,呼叫端也可能自己組 show notes)。
  **底線刻意不清**:`_斜體_` 與 `NOTEBOOKLM_AUTH_JSON` / `source_id` / `snake_case` 同形,
  清掉會毀掉正文 —— skill 那句「`_斜體_` 之類」跟著改成精確描述,不是反過來讓實作追文件。
- **`render()` 把解不出 spans 的 block 整段靜默丟棄,連 U+FFFC 都不留**(FINDING-E)。
  上游文件說 `.text` 會用 U+FFFC 填補未解碼位置,**實測 spans 為空時 `render()` 與 `.text`
  兩邊都不填**,整段就是不見了 —— 而剩下的句子讀起來完全通順(「以下是一段示範程式碼:」
  後面直接接下一段),呼叫端沒有任何訊號。判別實驗:同一個 conversation 用
  `strip_citations=False` 追問,程式碼完整回來。`_assert_no_dropped_blocks` 在
  `strip_citations=True` 這條路 fail-loud。**判準是有沒有解出文字,不是 block 的 kind** ——
  `CODE_BLOCK` 帶 spans 時 `render()` 照樣輸出它,那種情況不該擋;而「整份都沒文字」也不擋
  (那是 fallback 的地盤,不是部分丟失)。
  已知取捨:`HORIZONTAL_RULE` 之外的空 block 一律擋,所以回答含**圖片**時也會 raise ——
  圖片在純文字 show notes 裡消失是必然而非解碼失敗,訊息在那個情境下措辭不夠精確。
  保守方向刻意選擇「寧可擋,也不要靜默少一段」。
- **`notebook_get` 撞權限不足時裸拋上游 `ClientError`**(FINDING-F)。上游那句講的是
  `authuser` account-routing、指向 SDK issue #114/#294,**對 pool 情境無用且沒有修復指引**,
  而 `_list_sources` 早就有正確的那段。skill 正是引導「生成前先 `notebook_get` 確認目標對不對」,
  所以那是實務上最先撞到的一支。訊息抽成 `_errors.raise_if_access_denied`,兩處共用一份。
- **`research_wait` 傳「pool 內但非發起者」的帳號會等滿 timeout(預設 1800 秒)才死**
  (FINDING-G),訊息只有 `last status: no_research`。傳 pool **外**的帳號則當場 raise 並列出
  可用帳號(v0.9.13 的修正完好)—— 正確的診斷文字本來就寫在同一個檔案裡,只差沒帶到這條路徑上。
  `_explain_no_research` 把它接上去。判準用訊息裡的 `no_research` 而不是例外型別:
  SDK 對逾時用的是內建 `TimeoutError`,型別分不出「等太久」與「這個帳號根本看不到它」。
  `_handle_client` **刻意留在 `try` 外面** —— 它自己的 `ValueError` 訊息裡也含 `no_research`,
  包進去會被二次包裝成 `RuntimeError`(既有測試守著)。
- **憑證結構合法但 cookie 已死時,`from_storage` 的 `_LoginRedirectError` 原樣穿透**(FINDING-B),
  訊息只有「Authentication expired or invalid. Run 'notebooklm login'」。9 槽 pool 裡任一槽過期
  就整台起不來,而人看不出要去重登**哪一個帳號**(`_write_credential_file` 那條結構不合法的
  本來就會指名)。重拋時帶槽位名,原因留在 `__cause__`。

新增 13 條離線測試,五條逐一突變驗證(把修正改回舊行為 → 對應測試變紅 → 還原),
每條另配「判別力那一半」證明沒有誤傷。全套 12,938 passed。

驗收期間改壞三個既有測試,**三個都是判別力發揮作用**:把 `_handle_client` 誤包進 `try`、
把「整份空文件」誤判成部分丟失、teardown 測試斷言舊例外型別 —— 都已修正並保留原測試意圖。

## v0.9.14 — 升到 notebooklm-py 0.8.1

pin 從 `>=0.8,<0.9` 收到 `>=0.8.1,<0.9`。**下界不是形式**:這一輪把好幾個 0.8.1-only 的
符號變成硬相依(`Notebook.role`、`AskResult.answer_document`、`normalize_rpc_code`、
以及一組 `_auth` 私有函式),而 `uv tool install git+…` 不讀 `uv.lock` ——
解析到 0.8.0 的消費端安裝會在 `import notebooklm_mcp.app` 就 ImportError。

### 0.8.1 悄悄開了一條新的 cookie 重鑄路,而我們的護欄照不到它

**這是這一版最重要的一條。** 0.8.1 把 `from_storage(path=…)` 的載入路徑換成
`HealPolicy.HEAL_THEN_NAME_ONLY`,它的 routing preflight 是舊 gate 的**超集**:
`__Secure-1PSIDTS` 存在、值非空,但**已過期**或 **scope 打不到 `accounts.google.com`**
時也算失敗 → 呼叫 `_recover_psidts_inline` → 在本 process 內發一次 `RotateCookies` POST。

實測(0.8.0 / 0.8.1 同一份憑證,`_attempt_rotation` 換成記錄器、零網路):

| PSIDTS 狀態 | 0.8.0 | 0.8.1 |
|---|---|---|
| 過期 | 不發 | **發** |
| scope 錯 | 不發 | **發** |

`_recover_psidts_inline` 的 docstring 說「`NOTEBOOKLM_AUTH_JSON` 設了就 decline」,
**那個保護對 pool 不適用**:`_resolve_recovery_path` 是「明確 `path` 優先,env 根本不看」,
而多帳號 pool 每個槽位正是 `from_storage(path=str(path))`。
(單帳號模式走 `from_storage()` 無 path,env 分支生效,本來就 decline。)

後果就是 ADR-0010 v0.9.0 amendment 描述的那個事故:3 VM 共用的 Doppler cookie 被本
process 重鑄,新值寫進 lifespan 結束就刪掉的臨時槽位檔,Doppler 完全不知道,而伺服器端
舊 cookie 已作廢 —— 另外兩台下次啟動就掛,**本機自己看起來是正常啟動**。

**修法走了兩次彎路,兩次都被審查擋下來,值得記下來:**

1. 第一版是 **fail-loud**:預驗證加 routability gate,不 routable 就拒絕落檔。
   被推翻的理由是上游自己講死的 —— `load_with_recovery` 的 docstring:routing condition
   「asks whether `__Secure-1PSIDTS` can be **refreshed**, not whether it can be **used**」。
   拿它當接受條件會誤拒還能用的憑證,而 PSIDTS 過期是它的正常生命週期。
2. 第二版是**把本機 `expires` 挪成 session cookie**(`-1`)。實測有效、零行為改變,
   但**只擋得住「過期」,擋不住「scope 錯」**。
3. 最終版是**持有 rotation flock**:`_recover_psidts_inline` 的第 4 個前提是「跨 process
   rotation flock 搶得到」,`app._lifespan` 在建 client 前對每個槽位檔持有它到程序結束,
   heal 就發不出 POST,而載入照常走完(`load_with_recovery` 在 heal 回 False 之後仍會
   name-only retry,那條不檢查 routable)。三種情況下 jar 的 cookie 數/名稱/值/domain
   完全一致 —— 精準只擋 POST。

語義上也對:那把鎖的意思是「有別的 process 正在 rotate」,而我們正是宣告
**「這個槽位檔的 rotation 由外部(Doppler)負責,本 process 永遠不做」**。

routability 判準留下來,但**降級成 warning** —— 它指出「這個槽位該換憑證了」,不再是
接受條件。這同時解掉一個與 `_account_label` 政策自相矛盾的地方:一個壞槽位不該讓整台
server 起不來。

**驗收時發現的真實憑證問題**:prd 槽位 1 的 `__Secure-1PSIDTS` scope 在 `.youtube.com`,
到不了 `notebooklm.google.com` 也到不了 `accounts.google.com` —— 那個槽位一直靠 SID 等
其他 cookie 在撐,而它正是 0.8.1 下每次啟動都會觸發 heal 的那一列。flock 擋住了重鑄,
但那份憑證該重 mint(`dev` config 的唯一槽位同形)。

### 吃下 0.8.1 的新能力

- **`sharing.set_users()` 取代逐個 `add_user`**(`_share_each`)。N 次 RPC → 1 次,而且是
  伺服器端的批次 upsert(不是覆寫;`add_user` 現在自己就是它的 wrapper),所以原本
  「分享到一半留下孤兒」的失敗形狀消失了。錯誤訊息因此從「已分享到哪裡」改寫成
  「請重跑 `notebook_share_with_pool` 對帳」——**取消/逾時仍可能發生在伺服器已受理之後**,
  所以 `except (Exception, asyncio.CancelledError)` 的形狀原樣保留。
- **`chat_ask(strip_citations=True)` 改用 `answer_document.render()`**。上游把三種 rendering
  的分工寫死了:`.text` 是 **offset-faithful layout**「inserts no separators at all …
  fills undecodable positions with `￼`, so blocks run together and filler shows」,
  `render()` 才是「the only one built for reading」。第一版指名了 `.text`,實測多段落回答
  會變成 `'重點一重點二重點三'` —— 而那串文字一路流到公開的 Apple Podcast `<description>`。
  兩個獨立複審各自抓到同一條,那個收斂才是信號。
  `_text._CITATION_RE` 保留:`episode_set_description` 清的是 caller 傳進來的文字,
  根本沒有 `AskResult`,而且它是空文件時的 fallback。
- **`_errors.is_permission_denied` 改用上游的 `normalize_rpc_code` / `GrpcStatusCode`**,
  拿掉硬編的 `7` 與 str/int 雙比對。數字 7 的定義從此只有上游一處。
- **`notebook_get` 誠實轉發 `role`**。0.8.1 修好了 `is_owner`(從 `meta[1]`「有沒有共享者」
  搬到 `meta[0]` userRole),但**只在 `role` 解得出值時才同步**:`role is None` 時
  `is_owner` 停在欄位預設 `True`。**升版前這個欄位錯的方向是「恆為 False」(保守),
  現在 schema drift 時錯的方向變成「宣稱自己是 owner」(樂觀)** —— 所以必須同時轉發
  `role`,呼叫端才分得出「真的是 owner」與「role 未知的樂觀預設」。
- `artifact_list` 帶出 `Artifact.source_ids`(**純觀測面,沒當 gate**:還沒在生產資料上
  驗收過它有沒有值)。
- `from_storage(allow_headless=False)` 顯式傳:0.8.1 把 L3 headless re-auth 從「只看 env」
  升級成建構參數(預設就是 False),顯式傳等於把意圖寫進 code。
  `NOTEBOOKLM_REFRESH_CMD_MIDSESSION` 一併加進 `_INLINE_AUTH_ENV_OVERRIDES` ——
  **這條是防禦深度不是修 bug**:`_midsession_refresh_cmd_enabled()` 要求 `NOTEBOOKLM_REFRESH_CMD`
  也有值,而那個我們早就刪了。

### 上游修掉的、我們零改動受惠的

- **`import_sources_with_verification`**:FAILED_PRECONDITION 不再盲目重投同一個 task_id、
  每次 attempt 的 read timeout 被 `max_elapsed` 夾住、baseline 改 `strict=False`
  (一個重複 id 不再讓整個 idempotency baseline 失效)。這是 `research_import` 最脆的一段。
- **ArtifactStatus 1/2 轉置修正**:0.8.0 的 `is_pending` / `is_processing` 互相答錯了對方的
  問題。我們只依賴 `is_completed` / `is_failed` / `is_removed`,剛好避開,只影響
  `status_str` 的顯示文字。
- `NotebookNotFoundError` 現在帶 `rpc_code` / `found_ids` / `detail`,status-5 會講明
  「可能屬於另一個登入帳號」→ pool failover 少一類誤判。
- 上傳失敗時保留已註冊的 source;`get_or_none` not-found 契約;source status unknown 解碼。

### tripwire 收緊(existence-only → 語義鎖)

這一輪加的鎖第一版有好幾條只鎖「名字存在」,鎖不住語義漂移,被獨立複審逐條指出失敗情境:
- `_psidts_routes_to_rotate` 從簽名鎖改成**六列行為斷言**(scope 錯 / session cookie /
  重複身分這三種正是 routability 判準會誤判的形狀,原本完全沒覆蓋)。
- `SharingAPI.set_users` 補**回傳型別**鎖 —— 回傳的 `ShareStatus` 是 `_share_each` 唯一的
  「分享有沒有生效」後檢;上游哪天拿掉尾端的 `return await self.get_status(…)`,
  **每一次成功分享都會 raise「呼叫成功但未生效」**。
- `StructuredDocument` 從 `hasattr(…, "text")` 改成鎖 `render()` 的 marker-free 與
  block 分隔(並用 `.text` 沒有分隔當對照)。
- `Artifact.source_ids` / `Notebook.role` 補型別與行為鎖。
- `_errors` 的 `isinstance(exc, ClientError)` guard 原本**零覆蓋**(刪掉全套 12908 條照樣綠,
  因為那條測試用的例外根本沒有 `rpc_code` 屬性,測到的是別的分支)。

### 對帳

`mcp[cli]>=1.27,<2` 的上界正在生效地擋著已經上架 PyPI 的 **mcp 2.0.0**;
lock / tool venv 目前都是 1.29.0,一致。

### 真實環境驗收結果(FINDINGS 全文見 `docs/acceptance-v0.9.14-findings.md`)

2026-08-15 於 `-c stg`(9 槽 pool)跑完 Phase 0–10,**35/35 支工具全部碰過**,
`local-checks.sh` 18/18。

**核心目標達成**:flock 擋 inline heal 在**真實憑證 + 真實網路**下得證。stg 天生 9 槽全
routable(沒有現成素材),所以在**本機 env 層**造了一個 scope 錯的槽位(`.youtube.com`,
值不動,Doppler 沒碰),把 `_attempt_rotation` 換成記錄器當安全網兼觀測點:
**不持鎖的對照組 1 次、持鎖的真 `_lifespan` 0 次**,warning 指名槽位且不含 cookie 值,
該槽位真 RPC 仍成功,事後 Doppler 9 槽逐欄未變。

**三種帳號狀態的分流全部走對路**:認證失效 → fail-loud、零 rotate;結構不合法 → 啟動即擋
且指名槽位;權限不足 → `observed_state="notebook_access_denied"`、`attempt_count=0`(零配額)、
游標與 `_COOLING` 都沒動,**照著 `safe_next_action` 跑 `notebook_share_with_pool` 真的解得開**
—— v0.9.0 唯一的功能性 FAIL 複驗通過。

**抓到 7 條**(依可行動性):

- **`chat_ask(strip_citations=True)` 沒有清掉 `**粗體**` / `_斜體_`**,而 skill
  `tool-reference.md:588` 明文說「不會出現」。`render()` 只拿掉 block 級標記(`###`、`*`),
  inline 的不管;`episode_set_description` 也只清 `[n]` —— 實測含 `**` 的字串**原樣寫進 manifest**,
  會直達公開 RSS `<description>`。
- **`render()` 把 CODE_BLOCK 整段靜默丟棄,連 U+FFFC 都不留**(上游只說「不解碼」,
  `.text` 才是 U+FFFC)。判別實驗:同一個 conversation 用 `strip_citations=False` 追問,
  程式碼完整回來。危險在**輸出讀起來完全通順**(「以下是程式碼:」後面直接接下一段)。
- **`notebook_get` 撞權限不足時拋原生 `ClientError`**,沒翻成 `NotebookAccessDenied`、
  沒有修復指引(`_list_sources` 有)。skill 引導「生成前先 `notebook_get` 確認」,這是最先撞到的一支。
- **`research_wait` 傳 pool 內但非發起者的帳號 → 等滿 timeout(預設 30 分鐘)才死**,
  訊息只有 `no_research`。傳 pool 外帳號則當場 raise 並列出可用帳號(v0.9.13 的修正完好)
  —— 正確的診斷文字已經寫在那條路徑上,只差沒帶進 timeout 訊息。
- **憑證結構合法但 cookie 已死時,`from_storage` 的 `_LoginRedirectError` 原樣穿透,不指名槽位**
  (`_write_credential_file` 那條有指名)。9 槽任一槽過期就整台起不來,而人看不出是哪一槽。
- 驗收腳本自身兩個洞:README §2a 的掃描寫死 `range(1, 8)` 但 stg 有 **9** 槽;
  §3b 要求驗 `source_add_text` 的「idempotent」,但它**不冪等**(工具也沒宣稱過)。

**從未知變成已知**:

- **`Artifact.source_ids` 在生產資料上有值**,而且是**生成當下的歷史快照**(含已刪除的
  source id)→ 可從「純觀測」升格,但**不可拿來反查現存 source**。
- 0.8.1 的預設 host `notebook.google.com` 與既有 Doppler 憑證**通用**(原本只是推導)。
- `Notebook.role` 在真實 notebook 上解得出來且分得出 OWNER / EDITOR。
- `set_users` 的 **upsert 不踢人**在真實伺服器上成立 —— 第一次跑因為 `already_shared` 全命中
  而沒有判別力,補做「先 `remove_user` 踢掉一個槽位再跑」才真的送出 `set_users`,
  pool 外的 VIEWER 原封不動。
- 發布路徑的 `_embed_cover` 確實把 MP4/AAC(`ftypdash`)轉成真 MP3(`ID3` v2.3.0);
  **重新發布不下架舊產物**得證(舊 URL 仍 HTTP 206);附帶發現**換單集封面會連帶換掉
  mp3 的 enclosure URL**(封面嵌進 ID3 → 位元組變 → content-hash 變)。

**未觀測 / 未結案**(如實標記,不猜成通過):配額耗盡(9 槽全新鮮沒撞到)、
`artifact_retry_failed`(全程沒有 failed artifact)、`podcast_attempt_adopt` 的成功路徑、
`role is None` 時 `is_owner` 樂觀回 `true` 的分支、`generate_report` 的識別碼**引用正確性**
(講義零識別碼,只證得了「沒編造」)。

**下架比想像中難(收尾時撞到,對正式節目有實質意義)**:`uploader` 只有 `do_GET`/`do_PUT`,
沒有 `do_DELETE`,所以下架只能上 NAS 刪檔;而 `Caddyfile` 給 mp3 的是
`max-age=31536000, immutable`(一年)—— **刪掉 NAS 上的檔案之後,mp3 的公開 URL 仍然
`cf-cache-status: HIT` 回 206**(feed.xml 是 `no-cache`,所以它立刻 404)。
要真的下架一集,刪檔之外必須同時 purge Cloudflare 快取。

**環境異常一則**:一顆 `slide_deck` 卡在 `in_progress` **85 分鐘**未轉終態,兩次
`artifact_download_slides` 救援等待都 timeout;同 notebook 同帳號重送一份 **12 分鐘**完成
(正向對照組成立 → 是那顆 artifact 在遠端卡死,不是工具/帳號/來源問題)。
全程**沒有為了趕進度關掉 `require_slides`**。

## v0.9.13

### 真實驗收抓到的四條(FINDINGS 全文見 `docs/acceptance-v0.9.13-findings.md`)

- **卡在 ingest 的回錄對清理義務隱形**(中):上傳成功但 NotebookLM 端 ingest 卡死,
  那筆 source 逾 13 小時停在 `kind=unknown` / `ready=false`。`unresolved_upload_candidates()`
  的 kind 條件只認 `media`,而它是**唯一一份候選判準** —— 於是同一筆對 finalize 對帳
  (→ `acceptance_unknown`,resume 續不下去)與 retract 後的清理義務對帳(→ 零候選、
  義務誤結案放行)同時隱形,孤兒永遠留在 notebook 裡被之後每一集讀進生成 context,
  而工具全程回報成功。實測三項對照:retract 當下 `source_cleanup_unresolved: true`
  → 重呼 `podcast_series` 後變 `null`,孤兒還在。kind 放寬成「`media` 或尚未分類
  (`unknown`/`None` 且 not ready)」;對 finalize 那側的效果是**把身分綁回來、不是放行**
  (postcondition 仍要 `_source_ready`),retract 這時走身分確定的 `stale_source_ids` 路徑。
- **research 的輪詢 handle 綁帳號**(中):同一個 `task_id`、同一本已分享給全 pool 的
  notebook —— 發起它的 server 立刻回 `completed`,另一個帳號的 server 輪詢 900 秒只拿到
  `no_research`。research session 不跟著 notebook 分享走,而 start / wait / import 是三次
  獨立呼叫,中間一次配額 failover 就換人,「斷線救援用 `research_wait`、不要重新 start」
  那條指引因此永遠走不通。`research_start` 改回傳 `account`(`runtime.snapshot()` 取,
  記帳與送出同源),`research_wait` / `research_import` 收 optional `account` 反查 client
  (**游標不動**);指名的帳號不在這個 server 的 pool 裡就當場 raise 並列出可用的。
- `strip_citations` 清掉標記後留下標點前的空格(「…時間 。」),而它的用途正是產
  show notes。`_CITATION_RE` 前面加 `[ \t]*`:只吃前面(否則 `see [1] and` 會黏起來)、
  不吃換行(否則行首引用會把 markdown 結構拉平)。
- streamable-http 被 Ctrl-C(只有子 process 收到 SIGINT)時,憑證目錄在 rmtree 之後會被
  filelock 的 `parent.mkdir()` 用 umask **重建**成 0775 空目錄。非外洩(憑證檔一律 0600),
  但「正常結束會自己刪乾淨」只對 stdio 成立 —— 只改註解,行為不動。

- Deep Research 後續輪詢改用 SDK 的 `report_id`;fast 仍用 SDK `task_id`。MCP 對外
  保持原本必填的 `task_id` 欄位,但值改為可輪詢 handle,不再把 deep 的不可輪詢
  sessionId 交給呼叫端。deep 缺少或只回空白 `report_id` 時直接拋 `DecodingError`,
  且不建議可能重複消耗配額的 retry。
- 多步驟工具在入口固定同一個 account client,避免並行 quota failover 讓一次呼叫
  跨帳號;series 對帳沿用該 client,重送前先驗認證、失效時保留已完成結果且不先 rearm /
  supersede manifest,並回 `observed_state="auth_expired"` 讓 caller 重登後安全續跑。
- auth probe 改用 SDK 的 `is_auth_error`,涵蓋 `AuthError`、RPC auth code、HTTP 401/403 與
  mapper 保留的 cause;網路、限流、server 與其他錯誤仍原型別拋出。
- account label 讀取失敗時會保留原 RPC 並輸出警告，不再讓觀測資訊中斷操作。
- 移除已不需要、依賴 SDK 私有 API 的登入替代腳本；原生 `notebooklm login` 已支援
  `notebook.google.com`，`sync-auth.sh` 改由公開 `get_storage_path()` 解析 profile 路徑。

### `podcast_series` 不再把指名來源的 attempt supersede 成不指名的(內容錯置)

**症狀**:`podcast_episode(source_ids=["src-1"])` 遠端生成失敗(`dispatch=accepted` +
`remote=failed`)之後重呼 `podcast_series`,兩次 dispatch 送出 `[["src-1"], None]` ——
第二次靜默改讀**整本筆記本**(含後面各集的回錄音檔),而工具回報 `complete=True`。
這是 v0.9.5 花整輪在防的內容錯置形狀,從任何成功訊號都看不出來。**v0.9.12 與更早皆可重現**。

**根因是「補一半」的又一例**:接手守門 `_assert_series_owns_attempt` 只掛在
`prepared`/`not_accepted` 兩格,而會產生新 dispatch 的狀態有**三種** —— `failed`/`removed`
的 supersede 分支整條漏掉,`_create_audio_attempt` 在那裡不帶 `source_ids`。
`_attempt_next_step()` 的 `settled` 分支註解早就點名過這個形狀、也刻意不教人重呼 series,
但**只防了訊息、沒防程式路徑**。判準收斂成 `_series_will_redispatch()`,兩道守門(接手歸屬 +
來源筆數)共用同一個上游條件,不再各自列狀態字面值。

**訊息也一併從紅線的反面救回來**:舊 `owner_hint` 無條件說「原樣重呼 `podcast_episode`
就會沿用同一顆重送」,但那只對 `_NEVER_DISPATCHED` 成立 —— `accepted`+`failed` 的
`can_resend` 是 False,照做會撞 `already has durable active attempt`。改由
`_attempt_capabilities()` / `_attempt_next_step()` 產生(docs/gotchas-attempt.md 紅線),
那一格給的是「先 retract,再用 `podcast_episode` 帶回原本的 `source_ids` 重生」,
並由測試實走一遍證明走得出去。

### 認證停點的 `safe_next_action` 不再寫死 `podcast_series`

同一輪新加的每集 auth probe,停點硬寫 `ACTION_SERIES`。對指名來源／frozen bundle 的
attempt 而言,照著這個欄位重呼**正是上面那條內容錯置的觸發路徑**。改由
`_series_handoff_caps()` 判斷「series 重新進來接不接得住這一集」,接不住就交棒給
`_attempt_capabilities()` 算出的工具並附上同源的 `next_step`。

判準的**順序**是安全性質的一部分:先問「series 會不會在這顆上重新 dispatch」,再問歸屬。
反過來的話,一集已完成的 output attempt 也帶著 `source_ids`,會被交棒指引拖去
`podcast_attempt_retract` —— 叫人作廢一集已經做好的正式輸出(突變驗證確認)。
參數漂移(形狀是 series 的、只有 language/format/length 不同)刻意**不算交棒**:
那一格 caps 會算出 `podcast_series`,貼上去等於把呼叫端送回剛剛拒絕它的工具;
它的附帶條件(要用原本那組參數)由 `_SERIES_ARGUMENT_DRIFT_HINT` 一份講,守門的例外
訊息與認證停點的 `next_step` 共用 —— **只指對工具不夠**,照這次的參數重呼仍會裸拋。

### 復原路徑的回傳要自足:`regeneration_source_ids`

上面兩條把呼叫端導向「retract → `podcast_episode` 重生」,而那條路自己會把來源弄丟:
retract 的回傳說「重生時必須帶回原本那組 `source_ids`」,**卻沒有給那組** ——
`podcast_episode` 的 `source_ids` 預設是 `None`,省略就直接讀整本筆記本。整條官方
復原路徑因此仍會靜默擴大生成輸入(認證失效 → 停點指向 retract → retract 指向
`podcast_episode` → 兩次 dispatch 送出 `[['src-1'], None]`)。

`_attempt_capabilities()` 因此多算一個 `regeneration_source_ids`,由
`podcast_attempt_retract` 與 series 的認證停點一起公開;`_regeneration_hint()` 也把
id **逐字列出來**(同 `_attempt_next_step()` 清理義務分支早就立下的「列得出 id,不能
只報欄位名」)。這是 docs/gotchas-attempt.md 自足性紅線的又一次現形 —— 而它能撐到
這一輪,是因為原本那條「照指引走得通」的 E2E 測試在執行下一步時**從 fixture 硬寫**
`source_ids=["src-1"]`,繞過了公開回傳,所以測了等於沒測(那正是同一份文件裡
「只准用公開回傳裡的值」那條紅線在講的事,現已改用回傳值)。

### 兩道守門的順序:筆數在前、歸屬在後

歸屬是純本機判斷,擺在要打一趟 RPC 的筆數守門之前看起來更划算,但它把一條原本回
結構化 `too_many_sources` 停點的路改成裸拋(外來 attempt 撞上超標筆記本時,整批
`run_results` 隨 exception 消失)。兩道守門都不改 manifest,順序只決定「先講哪個
理由」,那就讓保住進度的那個先講。

同理,這一集**已經有完成的輸出**時,歸屬也不是該講的那件事:`_create_audio_attempt`
的「拒絕靜默覆蓋既有輸出」更根本,而歸屬訊息教的 retract-then-regenerate 對一集
已發布的節目是破壞性建議。終態那條路因此讓位給它(`prepared`/`not_accepted` 不經過
`_create_audio_attempt`,歸屬仍在原處驗)。

## v0.9.12

**這一版修的全是「補一半」。** 上一版為了不 tombstone 一顆還在飛的 attempt,把 retract 整個
關掉;這一版把那個決定倒過來 —— **記下義務,而不是擋住操作** —— 並收掉隨之而來的四個並行/
身分缺口。三輪獨立審查(兩輪盲審 + Codex)各自抓到不同層,列在下面。

### 回錄 upload 未結案時的 retract:從「擋住」改成「留下義務」

`dispatching` 不是短暫的 commit-point 窗口,是**耐久 checkpoint**:`claim_upload` 先寫進
manifest 才 `await add_file`,而收尾的 `except Exception` **收不到 `CancelledError`**。
MCP 長 request 被 client 取消是常態,取消或 process 被 kill 都會把狀態永久留在那裡。而唯一
出口 `_reconcile_source_upload` 只從 `finalize_attempt` 進得去 —— 等於「要作廢一顆輸入本來
就錯的 attempt,得先把它完整 finalize、上傳、promote」,比 `abandon_in_flight` 本來要避免的
後果還多一輪遠端副作用;notebook 已刪／share 撤掉時更是永久死鎖。

改成:retract 放行,tombstone 記 `source_cleanup_unresolved`,由生成前的 gate 用候選窗對帳
結案。涵蓋三種 unresolved 狀態(`dispatching` / `acceptance_unknown` /
`reconciliation_ambiguous`)—— 三者的後果相同,只修一種等於另外兩種的孤兒照樣沒人記得。
候選判準抽成 `unresolved_upload_candidates()`,finalize 對帳與清理義務**共用同一份**。
`add_file` 周圍顯式收 `CancelledError`(不用 `except BaseException`)。
代價寫在 [gotchas-attempt](docs/gotchas-attempt.md):作廢這種 attempt 之後最多 12 分鐘不能
重生,**即使孤兒已經刪掉也一樣**(窗還開著時晚到的 upload 仍可能冒出來)。

### 清理義務自帶 notebook 身分

`manifest_store` 明文允許 tombstone 保留建立時綁的舊 notebook,所以「attempt 在 nb-old、
episode 現在指向 nb-new」是合法狀態。舊版拿 episode 當下的 canonical 去查 —— 新本裡當然沒有
那筆 source,於是義務被判成已結案,而它還躺在舊本裡污染那邊每一集的 context。
`pending_source_cleanup` 每筆升級成 `{"source_id", "notebook_id"}`(舊字串仍可讀),身分在
**寫入當下**決定;讀寫收斂成三支共用 helper,四個站點全走它們。連 canonical 都沒有的 legacy
義務改成 fail-closed(舊版是直接放行)。

### 對帳寫回一律 CAS,衝突時保留已觀測候選

**孤兒靜默消失**:A、B 同讀 revision R,A 的 `sources.list` 回零候選、B 撈到 orphan;A 無條件
寫入清掉旗標,B 下一輪連這筆記錄都不會收集 → 不算候選、不寫任何東西、**成功返回**。全程零
錯誤零告警,下一次生成直接放行。上一版只在「有新發現」時才 CAS,那個論證只看了 manifest 側
的競態,漏了**兩個 process 對遠端的觀測不同**。

而只補 CAS 不夠(誰先 commit 誰贏),所以衝突時的處置是不對稱的:**保留「加義務」**(重讀
manifest、重驗 ownership、再條件寫入,有界重試)、**丟掉「清旗標」**(下一輪重算)、
**保留 `checked_absent`**(它逐筆比對自己查過的 id,併發追加的是別的 id,碰不到)。
另外 blocked/waiting 這種什麼都沒改的路徑不再寫盤 —— `ManifestStore` 的 revision 無條件 +1,
無效寫盤會平白撞掉別人正在做的 discovery CAS。

### 回傳自足:`source_cleanup_obligations`

`safe_next_action == "source_delete"` 時,舊版真實 id 只藏在 `next_step` 散文裡,而
`stale_source_ids` 在「gate 對帳後才撈到候選」的情形下是空的(tombstone 不回寫)。改成回
`[{notebook_id, source_id}]`(`source_delete` 兩個參數都在),**retract 與 adopt 兩個入口都補**
—— 只補一個又是補一半。指引也改成列出完整可執行的呼叫,而身分不明時不印假的
`source_delete(notebook_id=None, ...)`。

### `publish_series` 支援 `itunes:type`

兩個題庫節目(SAA 51 集 / SAP 36 集)在 Apple 裡的集序跟題號對不起來。根因:feed 從來沒輸出
過 `<itunes:type>`,Apple 因此當 `episodic` —— 照 `pubDate` 由新到舊排、`itunes:episode` 基本
被忽略。`itunes_type` 加成第 12 欄季級設定(serial/episodic,預設 episodic = Apple 的隱含值),
channel 層一律輸出。**item 的文件順序刻意不反轉**:Apple 對 serial 用 `itunes:episode` 排、
不看文件順序,而照文件順序顯示的播放器,現行的遞增正好是連載要的順序。既有節目重跑一次即可,
不動 GUID／音檔 URL,不需重生音檔。

### 其他

- `source_delete` 查無 id 時不發 destructive RPC 也不 raise,回 `was_present=False`
  —— 安全性質留在「不打 RPC」、冪等留在「不 raise」,清理迴圈可以重放。
- 原子換檔收回 `_atomic.prepared_replacement` 一份共用(紅線寫下之後又被違反兩次);
  必要-cookie 判準抽成 `_cookies`,pool 落檔與 auth CLI 共用同一份。
- cover `--skip-existing` 撞到壞檔改成重畫而不是整批 abort;Chrome lookup 延到真的要 render。
- `check_skill_sync` 現在**也掃 `SKILL.md`** 的核心契約詞。v0.9.12 教訓:
  `source_cleanup_obligations` 只補進 tool-reference、SKILL.md 仍教 `stale_source_ids`,
  而 checker 只掃 tool-reference —— 整條從那個洞掉出去,CI 照樣綠。

## v0.9.11

**「回傳要自足」的三層,一版收完。** v0.9.10 立的紅線是「指引一律由
`_attempt_capabilities()` 產生」——那條後來被徹底遵守了,而同一根因還是連續現形三次,
因為它管的只是**訊息從哪裡來**,不管**回傳裡有沒有照做需要的東西**。三次的形狀一路變窄:

| # | 症狀 | 缺什麼 |
|---|---|---|
| 第十 | retract 後的指引在它自己產生的狀態下 raise | 分支沒看同一集還在飛的 sibling attempt |
| 第十一 | 動作交棒給 sibling,身分沒交棒 | 回傳沒有那顆 attempt 的 id |
| 第十二 | 動作是 `podcast_attempt_adopt`,但它必填候選身分 | 回傳沒有候選的 source id |

### 第十次:post_retract 分支看不到已經在飛的替代 attempt

`_attempt_capabilities` 的 post_retract 分支是無條件 early return,只有三種答案,
**從不看 `episode["active_attempt_id"]`**。純工具呼叫就能走到:retract(A) → source_delete
→ 重生但 response lost(B 成為 active + `acceptance_unknown`)→ **再次 retract(A)**
(MCP request 被取消後的正常重試,而 retract 明文冪等)。v0.9.10 回 `podcast_series`,
照做撞 `already has durable active attempt`;**v0.9.9 回的 `source_delete` 反而可執行**
—— 是 v0.9.10 自己造成的 regression,而且長在為了防止這件事而加的分支上。
修法:發現 `active_attempt_id` 指向另一顆還在飛的 attempt 時,遞迴算**那顆自己的**
capabilities 並交棒。遞迴有界(遞迴呼叫必然 `post_retract=False`)。
⚠️ 副作用要記:`_attempt_capabilities` 從此**不再是「一次呼叫一顆 attempt」**,它會側看
同一集的兄弟;若再出現第三種跨 attempt 查詢,那是該重新設計而不是再加一支。

同一份**不給任何 findings、只看 diff** 的獨立盲審(AGENTS.md 紀律④)另外五條:
`candidate_selection_required` 在 caps 與 next_step 的優先序相反(同一份回傳兩個欄位教
不同工具)、`resend_possible` 的 guard 突變掉全套全綠(零覆蓋)、建構上不可達的 dead
guard、`safe_next_action: null` 與 `blocking_attempt_ids` 沒文件、`blocking_attempt_ids`
只在單候選那條路回傳(多候選那條會讓 host KeyError)。兩條只記不修(F7 單 process 不可達、
F8 `abandon_in_flight` 的永久代價)寫進 `docs/gotchas-attempt.md`。

### 第十一次:動作交棒了,身分沒有

上一輪的交棒只換動作名,回傳的 `attempt_id` 仍是被 retract 的 A(tombstone),
B 的 id 根本沒出現在回傳裡。照做 → `podcast_episode_reconcile(A)` 撞 tombstone;
B 是 `failed` 時更硬 —— 回 `podcast_attempt_retract` + `attempt_id=A`,照做冪等 retract A
**得到一模一樣的回傳 → 無限迴圈**。修法是 `_safe_next_target()` 把已算好的 action 字面值
翻成 `safe_next_attempt_id` / `safe_next_artifact_id`,**刻意不重覆優先序 if/else**。

**根因比它重要**:上一輪為此加的 E2E 註解寫著「照著回傳的下一步做真的走得通」,
實際卻從 manifest 私下讀 `active_attempt_id` 來驅動下一步 —— 外部審查把公開回傳的 target
突變成不存在的字串,**全套 12389 仍全綠**。掃全部 tests、4 支 identity-driven 工具的
131 個呼叫點,找到 4 條同型全部改成只用公開回傳驅動(改完仍綠 —— 值本來是對的,
錯的是驗證方式沒驗到自足性)。紅線立進 gotchas:**凡是宣稱「照著回傳做走得通」的 E2E,
執行下一步時只准用公開回傳裡的值**;raise 型停點沒有結構化 dict,是明文例外。

### 第十二次:`podcast_attempt_adopt` 必填候選,回傳沒有候選

`podcast_series` 在回錄 source 上傳撞歧義時停在 `reconciliation_ambiguous` +
`safe_next_action="podcast_attempt_adopt"`,而那支工具必填 `feedback_source_id` 或
`artifact_id` **之一** —— 只讀公開回傳執行不了(外部獨立審查實跑驗證)。候選本來就在
manifest 的 `finalize.feedback_source_upload.candidate_source_ids`,跟 artifact 對帳歧義的
`candidate_artifact_ids` 是同一個家族,只是沒被帶出來。修法:停點一併回
`candidate_source_ids`,兩個測試改成只從公開回傳取候選。
同輪修掉 `podcast_attempt_retract` docstring 把「沒有委派時 `safe_next_attempt_id` 就等於
`attempt_id`」**寫反**的那句(`safe_next_action` 是全新呼叫入口或認別種身分時它是 `null`),
並把另一處私讀值換成公開回傳(那個值恰好相同,曾讓該回傳點被突變成假字串時測試完全看不見)。

**這一版新增的判斷準則**(寫進 gotchas,避免下一輪反向補過頭):另外兩個 `ACTION_ADOPT`
停點 `continuity_unverified` / `legacy_output_unverified` **刻意不帶候選,不是漏補** ——
它們的出路是呼叫端自己 `source_list` 找出正確 id,**不得以唯一同名來源推定 identity**,
server 塞候選就是在幫它推定。分界:候選是 server 自己對帳算出、呼叫端無法重建的(時間窗)
→ 必須帶出;身分本來就要人為指名的 → 不准帶。

`REQUIRED_CONTRACT_TERMS` 從 13 → 16(`blocking_attempt_ids`、`safe_next_attempt_id`、
`safe_next_artifact_id`、`candidate_source_ids`),skill repo 的 `tool-reference.md` /
`troubleshooting.md` 同步。離線全套 12391 passed / 597 skipped。

## v0.9.10

**五輪連續「修好原問題、同時引入新問題」的收束。** 每一輪都是離線測試全綠、突變驗證做過、
多個獨立視角審過才發版,而下一輪的外部 review 每次都抓到東西 —— 其中多數長在**上一輪新加的
防護**上。這一版把那個循環的根因處理掉了,而它不在任何一條個別缺陷裡。

### 循環長什麼樣(照時間順序)

| 為了修什麼 | 那個修正自己造成什麼 |
|---|---|
| 不要 tombstone 還在飛的 attempt(v0.9.8) | 加的安全窗可被呼叫端縮小到同樣後果 |
| 終止性不依賴時鐘(v0.9.9) | 加的 tried guard 會漏試仍可用的帳號 |
| 窗不能被 retry 參數縮小 | 合併兩個窗 → 關閉窗丟 floor、候選窗誤綁外來 artifact |
| 兩個窗拆回來、方向相反 | 窗變大 → 跨 attempt 誤綁更容易觸發 |
| 加 guard 擋跨 attempt 誤綁 | guard 有 TOCTOU;停點手寫 action 繞過紅線;tombstone 停點永遠不解除 |

### 根因一:測試全打在 helper 上,看不見公開回傳

`tests/test_attempt_capabilities.py` 的笛卡爾積很漂亮 —— 8000 多個案例、每一格驗不變式。
但它斷言的是 `_attempt_capabilities()` 的**回傳值**,而工具的公開回傳裡手寫
`safe_next_action` 時,那套測試**完全看不見**。所以「指引一律由 capabilities 產生」這條紅線
才寫進 `docs/gotchas-attempt.md`,下一個 commit 新加的 guard 停點就手寫了 `ACTION_ADOPT`,
而全套照樣綠 —— 紅線有了、文件有了、笛卡爾積有了,就是沒有任何東西在檢查工具真正回什麼。

新增一層**針對公開回傳的契約測試**:實際呼叫 `podcast_episode_reconcile` /
`podcast_attempt_adopt` / `podcast_attempt_retract`,拿它們真正回傳的 `safe_next_action`
與 `next_step`,斷言與該狀態下的 capabilities 相容。它上線後**立刻抓到三個既有的手寫偏離**。
真正需要偏離的出口列進帶理由的例外清單(目前只有一項:`podcast_attempt_adopt` 的
feedback-source 模式,它問的不是 audio attempt 的下一步,legacy 甚至沒有 `attempt_id`)。
**偏離從此是刻意且看得見的,不是靜默的手寫值。** 八個公開出口現在全部讀
`caps["safe_next_action"]`。

### 根因二:時間窗本質上判定不了 artifact 歸屬

前四輪都在調同一個旋鈕的大小 —— 而**窗大 → 誤綁別的 attempt 的產物,窗小 → 撈不到真的**,
兩邊都是輸。真正的判準是所有權(誰 claim 了它),不是時間。

`_claimed_artifact_ids` 只排除 `remote.artifact_id` 非 None 的 attempt,而一顆 response lost
的 attempt 它的 artifact_id 就是 None —— 於是它的產物對別人來說是「無主的窗內候選」。
實跑:EP1 承諾 7200 秒後 response lost,EP2 在 T0+4000 建出 artifact 也 response lost,
T0+4100 對帳 EP1 就把 EP2 的音檔綁走、改名、下載、回錄、發布成 EP1。
**這個缺陷 v0.9.8 / v0.9.9 也有**,只是那兩版候選窗較小(用本次 `wait_timeout`,預設 1200 秒),
觸發條件較窄。

完整的所有權導向重設計**刻意留給獨立一輪**(會碰遠端行為、需要真實驗收)。這一版先把失敗
模式從「靜默綁錯並發布」收斂成「停下來問人,而且問完有路可走」:同一本 notebook 底下還有
其他未解決的 attempt 時不自動綁定唯一候選,改走 ambiguous 停點要求明確指名。

而那個 guard 自己又踩了兩個坑,都已修:①**TOCTOU** —— 它原本掃 `artifacts.list` await
之前的舊 snapshot,並行 session 在那個縫裡 dispatch 就看不到;現在 unresolved 重驗移進
`_bind_reconciled_artifact` 的同一個原子 update,與 claimed 重驗共用那次區段,RPC 前那次
降級成便宜的早退。②**tombstone 永久停點** —— 以 `abandon_in_flight` retract 的 attempt
永久保留 `acceptance_unknown` + `artifact_id=None`,所以永遠算 unresolved;它的遲到 orphan
落進新 attempt 的候選窗時,每次對帳都回同一個 adopt 停點,而確認「這顆屬於 tombstone」之後
adopt tombstone 被 default-deny、adopt 自己會錯綁、重跑回到原點 —— **無事可做**。
(直接排除 retracted 不可採用:那會讓 orphan 自動綁給新 attempt。)現在停點的指引涵蓋
否定答案,端到端測試實際走完「adopt tombstone 被拒 → retract 自己 → 重生完成」。

### 其餘修正

- **安全窗被 retry 參數改寫**:`window_end` 吃的是**本次** reconcile 呼叫的 `wait_timeout`,
  而原始 dispatch 承諾等多久沒有被持久化。傳 `wait_timeout=1` 約 61 秒後就給 retract 指引。
  改成 dispatch 時把承諾存進 `dispatch.wait_timeout`,對帳時
  `promised = max(這次呼叫, 持久化值)` —— 取 max 而不是「持久化值贏」,因為呼叫端顯式傳大值
  是撈遲到 artifact 的救援路徑。
- **兩個窗的保守方向相反,不能共用判準**:關閉判斷窗大才保守(不提前 tombstone),候選篩選窗
  小才保守(不誤綁)。曾經合併成一顆值,兩邊各壞一半。現在候選窗用 `promised` 裸值、關閉窗
  在呼叫點套 `max(promised, _RECONCILIATION_MIN_WINDOW)`,推導寫進常數註解 ——
  下一個人想合併它們的動機會再出現。
- **failover 漏試可用帳號**:`tried` 的排除發生在 `rotate_client()` **掃描之後**,碰到第一個
  已試過的候選就整批放棄,而游標後面可能還有沒試過也沒冷卻的帳號(實跑:pool A/B/C/D、
  已試 A/B、並行把游標推到 D、A 冷卻剛好到期 → 放棄時 C 既沒試過也沒冷卻)。改成把 `skip`
  傳進掃描裡。
- **關窗指引講錯窗、還否定自己提供的救援路徑**:文案說「候選窗還沒關」而依據是關閉窗;
  窗關分支寫死「重呼必然一模一樣」,而放大 `wait_timeout` 重呼就會不一樣 —— 照它做會作廢
  一顆救得回來的 attempt。
- **`safe_next_action` 漏掉 `active≠output`**:查 output 回 retract 但實際會被拒(要先處理
  active)、查 active 回 resume 但實際會因既有 output 被拒 —— 正確出口是先 retract active,
  兩個建議都走不通。笛卡爾積的 `ROLES` 從三格擴成四格。**這一格內部複審報過、被判成 low
  沒修**,理由是「行為面已被端到端測試覆蓋」——覆蓋的是 retract 的行為,不是指引。
- **`wait_timeout` 從「本次等待」升級成持久化安全參數,入口卻沒驗證**:`float('nan')` 存進
  manifest 之後每次對帳都在 `timedelta(seconds=nan)` 爆掉,那顆 attempt 永久對帳不了。
  抽成 `_validate_wait_timeout` 接上三個信任邊界(順帶補掉 reconcile 自己的漏洞:
  `nan <= 0` 是 `False`,舊的 inline 檢查擋不住 nan)。
- **re-arm 沒清掉新增的持久化欄位**:`_reset_attempt_for_resend` 清了 `account` 卻漏
  `wait_timeout`,留下「沒有當前 dispatch 卻帶著上一輪承諾」的形狀。同函式清 `account` 的
  註解理由逐字適用:「缺漏至少看得出來,錯的值看不出來」。

### 方法論(這幾輪最該留下來的東西)

- **AGENTS.md 紀律④**:「修正有沒有修好原問題」與「修正自己有沒有引入新問題」是兩個不同的
  審查,要分開問。v0.9.8 派了三個 opus 視角、每條突變驗證、全套綠、tag 也發了,外部 review
  仍抓到四條 —— 根因不在審查者,在 prompt:他們拿到的是「這幾條修正對不對」,於是逐條對照
  原缺陷驗證,沒有人對成品重新問「這裡面有什麼是新的、而且沒被任何測試守住的」。
- **不變式測試有盲區**:拿掉 `_attempt_next_step` 的 `output_owner` 分支時,純不變式測試
  **0 failed**,只有顯式測試抓到(135 failed)。不變式抓自相矛盾,抓不到語意選錯。
- **事後切 patch 做不到「commit 內容 = 驗證過的內容」**:用
  `git apply --cached --unidiff-zero` 分批提交時,`-U0` 沒有 context 行,連續套用的行號偏移
  把一整個 helper 插進了另一個函式的註解中間,三個 commit 全錯 —— **而 pytest 跑的是
  working tree,所以照樣 8779 綠**。要一修復一 commit,得在實作階段逐條做完逐條提交。

8785 passed + 597 skipped。每條修正各自突變驗證過;主迴圈另外獨立驗過三個最關鍵的突變
(atomic 重驗、公開契約測試、guard 本體),不是只讀 agent 的回報。

## v0.9.9

外部 Codex 對 v0.9.8 做獨立 review,抓到四條 —— **其中兩條是 v0.9.8 自己引入的,
而且都長在「修正動作」上**:v0.9.8 為了修 A 而加的東西,自己變成了 B 的攻擊面。
離線測試全綠、三個 opus 視角都審過,仍然沒抓到。這是這一版最該記住的事。

### 修「不要 tombstone 還在飛的 attempt」的那道窗,自己可以被呼叫端縮小

v0.9.8 把零候選的 `safe_next_action` 從「指回自己」改成「窗關了才給 retract」,
理由是候選窗還開著時 retract 會 tombstone 一顆其實還在生成的 attempt(ADR-0009)。
但窗是這樣算的:

```python
window_end = dispatched_at + wait_timeout + skew   # wait_timeout 是**本次呼叫**傳進來的
```

原始 dispatch 當時承諾等多久**沒有被持久化**。於是原生成允許 3600 秒、30 分鐘後
reconcile 用預設 1200 秒,就會誤判「窗已關」而建議 retract;傳 `wait_timeout=1` 的話
**約 61 秒後就給 retract 指引**。host 照做就 tombstone 一顆還在生成的 attempt,
再生成會出現第二個 artifact —— **正好是那道窗被加進來要防的那件事**。

修法把一個 `window_end` 拆成兩個各有名字的值:`candidate_window_end`(篩選候選
artifact,仍用本次 `wait_timeout`,既有行為不動)與 `reconciliation_window_end`
(關閉判斷,用 `max(wait_timeout, _RECONCILIATION_MIN_WINDOW)`)。呼叫端只能**放大**
這個窗、不能縮小,因為丟錯成本(tombstone 一顆還在飛的 attempt)遠高於試錯成本
(多等一輪再對帳一次)。

Codex 建議的是在 dispatch 前持久化不可變的 `reconciliation_window_end`,那更正確 ——
但要改 manifest schema、處理既有 attempt 沒有該欄位的向後相容,而且動到 dispatch
寫 manifest 的路徑。這一輪選零 schema 改動的版本,取捨標成 `ponytail:` 註解留在
常數旁邊。

### 修「無限重送」的那道 tried guard,自己會漏試可用帳號

v0.9.8 為了讓終止性不依賴時鐘,在 failover 迴圈加了 `tried` set:`rotate_client()`
回來的帳號已經試過就停。但 `rotate_client()` **不知道 `tried` 是誰** —— 它只回
「游標後方第一個不在冷卻中的槽位」,而那個槽位剛好試過的話,呼叫端就整批放棄了,
**游標後面可能還有完全沒試過、也沒在冷卻中的帳號**。

實跑復現(pool A/B/C/D,本次已試 A、B,並行 request 把全域游標推到 D,A 的冷卻剛好
到期):rotate 回 A → 在 tried 裡 → 放棄,而 **C 既沒試過也沒冷卻**。結果錯誤標成
`not_accepted`,違反「每個已知帳號最多、也應該試一次」的承諾。v0.9.8 新增的雙帳號
bouncing 測試涵蓋不到 —— 兩個帳號時「第一個候選在 tried」等價於「全試過了」。

修法把排除搬進掃描本身:`rotate_client(refused=…, skip=frozenset(tried))`,迴圈裡
`skip` 與冷卻是兩個獨立條件、都要滿足才是候選。冷卻副作用不受 `skip` 影響,永遠做。

### 零候選分支繞過了這一版自己剛立的紅線

v0.9.8 在 `docs/gotchas-attempt.md` 寫下「狀態能力與指引一律由 `_attempt_capabilities()`
/ `_attempt_next_step()` 產生,不准手寫 if/else」,而它自己的零候選分支就是用時間
if/else 手寫 `ACTION_RETRACT` / `ACTION_RECONCILE` 加兩段 `next_step` —— 兩個 return
還重複了大半結構。上一輪只把 hint 抽成 `_retract_hint`,**action 的決策仍留在呼叫點**。

修法採納 Codex 的方向,把時間性狀態納入能力:`_attempt_capabilities(...,
reconciliation_window_closed: bool = False)`,語意很直接 —— **窗關了就是「不能再對帳」**
→ `can_reconcile` 為 False。`_attempt_next_step()` 既有的分支順序就會自動走到 retract
那一支,`safe_next_action` 也從 caps 推導,呼叫點的第二個決策來源消失、兩個 return
收成一個。函式仍是純的(窗關了沒由呼叫端算好傳進來)。笛卡爾積測試加這個 bool 維度。

### frozen bundle 的重生指引,少了唯一那句讓它可執行的話

`_attempt_next_step()` 的 settled 分支叫人 retract 後呼叫 `podcast_episode`,但沒帶
`caps["regeneration_hint"]` —— 而 `_regeneration_hint()` 對 `input_bundle` 的 attempt
明明算好了正確那句:「重生要用一份**新的、尚未綁定的** frozen bundle,舊 bundle 的
`attempt-binding.json` 還綁著這顆已作廢的 attempt,沿用它會被 tombstone 擋下來」。

照現況做:沿用原 bundle → dispatch 前被 tombstone binding 拒絕;完全不傳 bundle →
失去 frozen-input 的確定性承諾。**兩條路都不通**,又一次「指引在它自己產生的狀態下
做不到」(第八次)。三個提到 `regeneration_entry` 的分支現在一律附上這句提示
(它對不需要提醒的形狀回空字串)。

### 駁回一條

Codex 另指 ADR-0010 新增的 v0.9.7 amendment 是英文、違反「全程繁體中文」。**不採納**:
整份 ADR-0010 從標題到 v0.9.0 amendment 都是英文,在一份英文 ADR 裡插一段中文才是破壞
體例。那條規範約束的是新寫的註解與中文文件。

3494 passed + 12 skipped(笛卡爾積多一個維度,比 v0.9.8 的 3041 多)。四條各自突變驗證過。

## v0.9.8

兩份**互相獨立**的 v0.9.7 事後審查(本迴圈的三視角 opus 複審、外部 Codex 複審)各自跑完,
在兩條上收斂到同一個結論 —— 那個獨立收斂才是信號。加上各自獨有的發現,共七條。

### 最重的一條不是缺陷,是**護欄本身是假的**

v0.9.7 的主角是「配額冷卻」,而它的核心修正(冷卻**真正被拒**的那個槽位)**沒有任何測試**:
把 `refused` 反查整段刪掉,全套 **2588 全綠**。上一版的 red proof 只是
`TypeError: rotate_client() got an unexpected keyword argument 'refused'` —— 那只證明
「參數以前不存在」,不證明「冷卻目標對了」。

原因很細:缺陷版燒掉 a1 之後,搜尋起點也跟著推到 slot1,**下一格照樣是 a2** ——
唯一的斷言 `second == "a2@x"` 對缺陷版與修復版同時成立。這與 v0.9.7 自己修掉的
「名字寫 concurrent、內容全同步」是**同一個病灶換皮**:斷言看的是回傳值,而缺陷只
表現在 `_COOLING` 的內容上。現在兩條斷言一條查狀態(`1 not in _COOLING`)、一條查行為
(再轉一次,a1 仍該是候選;缺陷版此時三格全冷卻會回 `None`)。

**這一輪三個「修正沒有測試」都是突變驗證抓到的,審查者讀 code 讀不出來**(讀起來都對)。
另外兩個:tried guard 的第二條 dispatch 路徑(只改第二處回舊形狀,全套照樣綠)、
零候選的 `retract_instruction` 分支(兩支都含 `abandon_in_flight` 字串,而斷言是子字串比對,
硬寫成錯的那一支全套照樣綠)。**紀律:守門測試寫完要突變一次再收,不然它只是註解。**

### cooldown 冷卻的是游標,不是真正被拒的帳號

`rotate_client()` 無條件 `_COOLING[_ACTIVE] = now`,但呼叫端是拿早先 `snapshot()` 取到的
`(label, client)` 去送出的,中間隔著至少一次 await。並行下 pool `[a0,a1,a2]` 兩個呼叫都
snapshot 到 a0、都被拒:A 先 rotate(冷卻 slot0、游標推到 1);B 接著 rotate 時
**把正在替 A 服務、從沒拒絕過任何人的 a1 打進 600 秒冷卻**。v0.9.7 的 docstring 逐字宣稱
「現在只有真的被拒的槽位進冷卻,跳過去的那個下次照樣是候選」—— 那句在並行下是假的。

`_rotate_for_quota` 手上一直有 `from_account`(它自己的 docstring 還寫著「必須是這次
dispatch 實際用過的那一個」),只是沒往下傳。改成 `rotate_client(refused=label)` 用 label
反查槽位;反查不到(pool 重裝過)才保守退回舊行為。**參數是 label 不是 index**:呼叫端
手上只有 `snapshot()` 給的 label,index 是 runtime 的內部表示。

### cooldown 順手把「繞完一圈就停」從無條件降級成依賴時鐘

舊版終止性是結構性的(`_ACTIVE` 只增不減,一輪必然用完)。加了時間過期之後,**若繞一圈
的耗時超過冷卻期,第一格已經解凍、`rotate_client()` 永遠回得出帳號** —— 而
`_dispatch_audio_with_failover` 是 `while True`,那就是無限重送、無限燒配額,CI 全綠。
SDK backoff + 429 慢回之下 600 秒不是不可達。

修法不動 cooldown 語意:failover 迴圈自己記 `tried` set,終止性回到**無條件**。
`rotate_client` 的 docstring 也照實改口 —— 它自己不再保證這件事,由呼叫端保證。

### 而那道 tried guard 第一版**擋在副作用之後**(三個審查視角獨立抓到同一條)

`_rotate_for_quota` 不是純查詢:它在回傳前已經 ①動了全域游標與冷卻 ②`_record_dispatch_failover`
把 `dispatch.account` 改寫成 to_account、往 **append-only 的 `errors[]`** 寫一筆。
guard 放在回傳之後,命中時只丟掉回傳值,**兩個副作用一個都沒撤** —— manifest 留下一次
從未發生的換帳號,而且 `dispatch.account` 指向一個這次根本沒送過東西的帳號。實跑復現:
實際送出 `[a@x, b@x]`,落盤 `dispatch.account: a@x`、failover 紀錄兩筆而第二筆沒發生過。

這正面違反 **ADR-0010 紀律③「記帳與送出必須同源」—— 而那條紀律正是這一輪 P1 存在的理由**。
`errors[]` 是 repo 自己宣告永不清除的稽核正本,假紀錄會活到永遠。guard 移進
`_rotate_for_quota`、擋在 `_record_dispatch_failover` **之前**;冷卻副作用保留(帳號真的被拒過)。
兩處呼叫端因此變回同形的 `if rotated is not None:`,不會再有「只補一處」的機會。

### 零候選的出路:把時間性判斷寫進條件,不是寫進散文

v0.9.7 為 FINDING-1 加的 `next_step` 前半說「重呼本工具會得到一模一樣的回傳」、後半接
`_attempt_next_step(caps)` 而它在這一格恆定吐出「先 `podcast_episode_reconcile` 對帳」——
**照做又回到同一支工具**;`safe_next_action` 更是一個字沒改,仍指回自己。

但把它一路改成 `ACTION_RETRACT` 也錯,而且錯得更貴:零候選有兩種成因(同一句話裡自己就
寫了),候選窗還沒關的時候晚幾分鐘再對帳**是會撈到的**。更關鍵的是,新指引要求的「外部
知識」是 `artifact_list(kind="audio")` —— 那正是 reconcile 自己上一秒剛打過的同一支 RPC,
回傳必然同樣是空,`abandon_in_flight` 那道「我知道 manifest 推導不出來的事」的門就被降級成
**同義反覆**。照著做的下場:伺服器其實受理了、artifact 還在生成 → retract + 重生 →
幾分鐘後第一顆出現變成雲端孤兒 → 下次 reconcile 撞 `reconciliation_ambiguous`、配額白燒,
**正是 ADR-0009 要擋的「把還在飛的因果紀錄提前寫成墓碑」**。

改成用已經算好的 `window_end`:窗關了才給 `ACTION_RETRACT`,窗還開著維持 `ACTION_RECONCILE`
並明說要等 —— 回到 v0.9.6 驗收 FINDING-1 的原始建議(「值域不必改」)。

### `podcast_series` 重包 reconcile 停點時把 `next_step` 丟掉(外部 Codex 抓到)

零候選的出路只存在於 `next_step` 那一欄,而 series 內部拿到 reconcile 的回傳後重包 `partial()`
時**只轉傳 `candidate_artifact_ids`**。於是走 series 這條路的呼叫端拿到的仍是一個沒有出路的
停點。`partial(**extra)` 本來就吃額外欄位,純漏傳 —— 又一次 AGENTS.md 逐字點名的
「`podcast_series` 有兩條路徑」。

### `_reuse_frozen_input_attempt` 是單一事實來源唯一漏收的出口(第七次現形)

v0.9.6 宣稱把「這顆 attempt 能做什麼」收斂成 `_attempt_capabilities`,但這支仍自己讀
`dispatch.status`、手寫 `frozen attempt is {status!r}; reconcile or resume it instead`,
只放行 `prepared`/`not_accepted`。可達狀態:frozen bundle 送出成功(`accepted`)、遠端回
終態失敗、attempt 仍 active → 呼叫端照冪等契約原樣重呼就撞那句 → 照它說的做,reconcile 被
`ValueError` 擋、resume 拋 `TerminalGenerationError`,**兩條建議都走不通**。而同一顆 attempt
餵進 `_attempt_capabilities` 得到的是正確答案(免旗標 retract → 重生)。

**而換過去之後才看見更深的一層**:`_attempt_next_step` 的 settled 分支結尾寫死「整季流程也
可以直接重呼 `podcast_series` 讓它自動 supersede」,**沒有跟著同一顆 caps 的
`regeneration_entry` 走**。frozen bundle 這條路上 `regeneration_entry` 必定是 `podcast_episode`,
於是同一句話前半教 episode、後半教 series —— 而後者正是 `_regeneration_entry_point()` 整顆
docstring 存在的理由(series 生不出帶 `source_ids`／bundle 的 settings)。照後半句做,
這一集會改讀整本筆記本(含後面各集的回錄),**正是 v0.9.5 花整輪在防的那個內容錯置形狀**。
那句改成條件式之後,笛卡爾積測試新加的不變式一次紅了 **32 個組合**。

### 紀律終於寫進常駐文件

這個根因已經現形**七次**,而每次的修法都是「補那一格」。`docs/gotchas-attempt.md` 補上紅線:
**任何 `safe_next_action` 與狀態相關的指引訊息一律由 `_attempt_capabilities()` /
`_attempt_next_step()` 產生,不准手寫 if/else**,並列出七次的形狀。AGENTS.md 的
`tools_podcast.py` 那一列指過去。**這條沒寫進常駐文件正是第七次現形的直接原因**;
零候選那段手寫的 `retract_instruction` 抽成共用的 `_retract_hint(caps)`,不再平行維護第二份。

### 其餘

- **`authorization_basis` / `remote_status_at_retraction` 補上落盤斷言測試**:v0.9.6 加這兩欄
  是為了取代「只記呼叫端傳了什麼」,但把它們改回 v0.9.5 語義**測試全綠** —— 唯一守著它的
  檢查在驗收工作區的 `local-checks.sh`,跟工作區一起丟了。
- **ADR-0010 補 v0.9.7 amendment**::15「never walks back into a spent account」與
  :27「rotation walks front to back」兩句與 code 相反,而 AGENTS.md 規定動 dispatch 前必讀它。
- **`generate_audio` docstring 補「這支沒有配額 failover」**(v0.9.3 FINDING-4 只修了 skill 那半)。
- **README 的 HTTP 範例從 `0.0.0.0` 改回 `127.0.0.1`** —— 該 transport 無認證,與 AGENTS.md
  的 loopback-only 紅線牴觸。`docs/mcp-setup.md` 的 tag precheck 還停在 `v0.4.2`(下一行卻裝
  `v0.9.7`);`docs/release-checklist.md` 的六處 pin 指向一個不存在的「本檔 §Commands」。
- v0.9.7 CHANGELOG 寫的「6 settings 形狀」實際是 5(450 格,不是 540)。

3041 passed + 12 skipped(連跑三次無間歇紅)。七條修正各自做過突變驗證。

---

## v0.9.7

v0.9.6 真實環境驗收(full,35 工具全覆蓋)的四個 FINDING,加上驗收順手推翻的一個
**既有認知**。核心機制全部 PASS —— 重生入口白名單四種形狀全對、逐字稿隔離拿到
自家 6/6 對外部 0/6 兩個獨立資料點(其中一集**標題裡就有「碰撞」兩字**,逐字稿仍 0 命中)、
守門回歸全綠、`_ensure_resume_attempt` 兩個分支都走到。

### `RateLimitError` 不等於「今天已耗盡」——而游標把兩者當同一件事

**實測:同一個帳號被拒後 26 分鐘,在另一個 process 又被受理。** 那是瞬時限流。而
`rotate_client()` 舊版只有 `_ACTIVE += 1`、**只增不減**,於是一次限流就讓那個帳號在該
process **餘生**退場;那一輪三個帳號因此提早出局,pool 的有效容量被白白吃掉,
**而 manifest 看起來一切正常**(每次都成功 failover 了)。走到底之後 `rotate_client()`
永遠回 `None`,**只有重啟 server 才會回到第一個**(`set_clients()` 重設游標)。

舊 docstring 把它留成已知取捨,理由是「代價只是那一輪少試一個帳號、下一次呼叫照樣會從頭
輪」—— 26 分鐘那個觀測推翻了前提:代價是整個 process 的餘生。

改成**環狀游標 + 每槽位獨立冷卻**(`time.monotonic()`,10 分鐘,取得比實測值保守 ——
試錯成本是一次 RPC,丟錯成本是少一個帳號)。終止性不變:被拒的一定進冷卻,繞完一圈自然
回 `None`。順帶修掉舊 docstring 記載的並行缺陷:**只有真的被拒的槽位進冷卻**,被並行
rotate 推格跳過的那個下次照樣是候選(舊版會跟著一起燒掉)。

### 四個 FINDING 的共同根因:v0.9.6 的收斂只做了一半

v0.9.6 建了 `_attempt_capabilities` / `_attempt_next_step` 當單一事實來源,但**只把
「拒絕訊息」那一族出口接上去,沒接「停點回傳」那一族**。四條裡有三條就長在沒接的那些出口:

- **FINDING-4(第六次現形,v0.9.6 自己種的)**:retract 的 `next_step` 是手寫 if/else,
  對 `settings={"origin": "explicit_resume"}` 的 attempt 說「**必須帶回原本那組
  `source_ids`**」—— 而那顆 manifest 裡**根本沒有** source_ids。根因是入口判斷用白名單
  (認不出來就保守導向 `podcast_episode`),而那句話另外用 if/else 猜,猜錯的正好是白名單
  **特意涵蓋**的那一類。`safe_next_action` 對了,附帶的話還是假的。
  修法:`_regeneration_hint()` 與 `_regeneration_entry_point()` 同源,三種形狀各有正確的
  話,認不出來的**明說認不出來**、要呼叫端自己指名。
- **FINDING-1(中)**:`podcast_episode_reconcile` 零候選時 `safe_next_action` 指回它自己,
  連呼兩次的回傳**逐欄位相同** —— 照著做就是無限迴圈。而 skill 與 `partial()` 都說
  「原地打轉」的唯一依據是 `attempt_count` / `superseded_attempt_count` 持續增加,可是
  reconcile 不建 attempt,這兩個數字**恆為 1 / 0**,偵測條件從不成立。出路只存在於 skill
  散文,不在工具回傳裡。修法:那個分支也接上 `_attempt_capabilities`,回傳帶 `next_step`
  (先 `artifact_list` 看雲端 → `abandon_in_flight=true` retract),並明說重呼本工具會得到
  一模一樣的結果。
- **FINDING-2(低)**:守門叫你 retract,retract 完卻叫你回去撞同一道牆。retract **不打
  RPC**(設計如此),看不到筆記本此刻已超標。修法照驗收建議:講在**擋下它的那個工具**的
  `error` 裡 —— 我們知道超標,retract 不知道。
- **FINDING-3(文件)**:skill 的 `continuity_unverified` 一行寫成
  `podcast_attempt_adopt(feedback_source_id=...)`,漏掉必填的位置參數,逐字照做會失敗。

### 窮舉測試也補上一層

`test_attempt_capabilities.py` 新增一條:**警告句宣稱「必須帶回原本那組 `source_ids`」時,
那顆 attempt 必須真的有**。它把「話」與「事實」綁在一起,而且是窮舉的 —— 突變驗證時
這一條紅了 **180 個組合**。之後任何新的 settings 形狀進來,都不可能再靠猜。

2583 passed + 12 skipped;四道修正各做過突變驗證。

## v0.9.6

**第五次現形,而且是 v0.9.5 自己種的。** 那一版的 commit 訊息寫著「這次連根拔」——
獨立複審把那個宣稱推翻了,窮盡掃描(318 個 `raise`、20 個 `partial()`、9 個直接
safe-action 出口、16 個含動作序列的 docstring)找出 10 條,3 條 High。所以這一版改的
不只是那 10 條,是**產生它們的結構**。

### 第五次現形長什麼樣

`_outcome_is_settled()` 回答的是「retract 需不需要旗標」,v0.9.5 卻拿它當「可不可以原樣
重送」的判準。而 `failed`/`removed` 這兩個問題的答案**相反**:結果已定所以免旗標 retract
可以,但重送走的是 supersede 建新 attempt,`_is_resendable_same_request()` 只收
`prepared`/`not_accepted`。於是訊息教「參數完全相同就原樣重呼」,照做必然撞
`already has durable active attempt` —— 又一次「指引在它自己產生的狀態下不可執行」。

另外兩條 High 同型:`origin="explicit_resume"` 的 attempt 沒有來源 provenance,卻被
`_regeneration_entry_point()` 武斷判成 series,又生出 `[["src-1"], None]`(同一個內容錯置
形狀,換一條路徑);frozen bundle 的 retract 指引說「用同一份 bundle」,而 binding 刻意
只能建立一次,照做必撞 tombstone、新 dispatch 數 = 0。

### 為什麼要改結構而不是再補一輪

五次的形狀完全一樣,而每次的修法都是「補那一格」。判斷分散在三個各自為政的布林
(`_outcome_is_settled` / `_is_resendable_same_request` / `abandons_unauthorized_candidate`)
加十幾處手寫訊息裡 —— **只要保持那個結構,修第 N 條時就會在新訊息裡種下第 N+1 條**,
而這已經連續發生三輪。

`_attempt_capabilities()` 把「這顆 attempt 現在能做什麼」變成單一純函式:輸入
(dispatch.status, remote.status, 是否 active/output, settings 形狀),輸出每個動作可不可以
(`can_resend` / `can_resume` / `can_reconcile` / `authorization_basis` / `regeneration_entry`)。
`_attempt_next_step()` 把結論翻成一句可執行的話。retract 的准入與拒絕訊息、
`_create_audio_attempt` 與 `_ensure_resume_attempt`(**兩個分支**,v0.9.5 只修了一個)的
拒絕訊息,全部改讀它。

**`_regeneration_entry_point()` 的判準從黑名單改成白名單**:只有「認得出是 series 自己
建的 settings 形狀」才回 series,其餘一律回 `podcast_episode`(它會要求明示來源,不可能
靜默擴大)。黑名單版本漏過 `explicit_resume` 與 `source_ids=[]` / `input_bundle={}` 這類
falsy-but-present 的舊 manifest。fail-safe 的方向是「不確定就要求明示」。

### 驗證方法也換了 —— 這比那 10 條重要

前幾輪都做過突變驗證卻照樣漏,因為**突變只打在我想得到的那幾處**。獨立複審用兩個突變
證明了這個盲區:把 `input_bundle` 判斷整個拿掉、把 `removed` 從 settled 集合刪掉,既有
測試都全綠(fixture 只造了另一半)。

`tests/test_attempt_capabilities.py` 改走**狀態組合的笛卡爾積**(6 dispatch × 5 remote ×
3 role × 6 settings 形狀),對每一格驗不變式:訊息教的每個動作在該狀態下都必須真的做得到。
它上線後**立刻抓出 75 個我沒想到的組合** —— 已 promote 的 output attempt 不需要旗標,
而 next_step 在 reconcile/resume 分支無條件教「帶 abandon_in_flight=true」。

**但不變式測試也有盲區**,而且是複審實測出來的:把 `removed` 從 settled 拿掉時所有不變式
仍然自洽(它只是變成「需要旗標」,訊息跟著改口)。不變式抓「自相矛盾」,抓不到「語意
選擇錯了」。所以另加一條顯式的單向包含斷言:**來源守門會停在 retract 的每一個狀態,
retract 都必須免旗標收下它** —— 那兩份程式碼分處兩地,動一邊沒動另一邊就是死路。

### 其餘

- 稽核多存 `authorization_basis`(`settled` / `output_owner` / `legacy_evidence` /
  `abandon_in_flight`)與 `remote_status_at_retraction`。`abandon_in_flight` 只是「傳了
  什麼」,結果已定的 attempt 就算傳 `true` 也是白傳,兩種語意不同的 retract 原本存成
  一模一樣的紀錄。
- 修掉 v0.9.5 那句過度宣稱:守門條件與免旗標條件是**單向包含**不是「完全等價」——
  後者還涵蓋 `output_owner` 與 `legacy_evidence`。安全性只需要單向。
- `_classify_not_accepted_stop` 的 annotation 補成三元組(實作 v0.9.5 就改了,契約描述沒跟上)。
- 兩支公開 docstring(它們會成為 MCP tool description)修正:retract 的狀態表補齊五種
  情況;`podcast_series` 的 too-many-sources 說明改成「`safe_next_action` 分兩種,照回傳
  的那個做」。
- **`uv.lock` 修正**:v0.9.5 的 tag 裡 lock 還停在 `0.9.4`(bump 之後沒重跑 `uv run` 就
  commit),任何人 clone 那個 tag 跑一次就立刻髒掉。

2129 passed + 12 skipped。

## v0.9.5

**同一個根因的第四次現形,這次連根拔**:`safe_next_action` 與錯誤訊息**沒有跟著 attempt
的實際狀態走**。v0.9.1 FAIL-1(停點叫人跑一支在該狀態下自己也 permission denied 的工具)、
v0.9.3(停點指向會拒收它的工具)、v0.9.4 FINDING-2(拒絕訊息那句在該狀態下是假的)都是它,
而每一次的修法都是「補那一格」—— 然後在新寫的訊息裡又種一個。Codex 獨立複審抓出 6 條,
**其中三條是 v0.9.4 自己新種的**。

### 最重的一條:retract 之後的指引會靜默改掉生成輸入

`podcast_episode(source_ids=["src-1"])` 撞配額停在 `not_accepted` → retract(沒有 stale
source)→ 回 `safe_next_action="podcast_series"` → 呼叫端照做 → **series 建的新 attempt
不帶 `source_ids`**。實測兩次 dispatch 送出 `[["src-1"], null]`:第二次改讀整本筆記本,
而那正是 v0.9.3 花整輪在防的內容錯置形狀。skill 也明寫「帶 `source_ids` 的 attempt 只有
`podcast_episode` 續得下去」——工具自己的指引卻反著教。

修法是 `_regeneration_entry_point()`:重生入口跟著**被作廢那顆的生成輸入**走,
`settings.source_ids` 或 `input_bundle` 存在就回 `podcast_episode`。回傳另加 `next_step`
一句話寫明「必須帶回原本那組 source_ids」。

### `failed`/`removed` 的停點指引照做會被拒

筆數守門的條件涵蓋 `remote_state in ("failed","removed")` —— 而那時 `dispatch.status` 是
`accepted`。v0.9.4 寫的停點訊息卻**無條件**說「它從未 dispatch,不需要旗標」,照著
`podcast_attempt_retract` 做必然被拒。

修法把「manifest 自己就知道結果」抽成 `_outcome_is_settled()`:除了 `prepared`/
`not_accepted`(沒建出 task),**遠端已回報終態(`failed`/`removed`)也算** —— 那不是
「可能還在飛」,不存在需要外部知識的 in-flight 狀態。這讓守門的觸發條件與 retract 的
免旗標條件**完全等價**,所以停點指引一定走得通;等價關係寫進兩邊註解,免得日後只改一邊。

### 對歷史 attempt 教一個永遠無效的旗標

`abandon_in_flight` 只放行 `active_attempt_id` 那一顆。A 被 supersede、B 接手之後,拿 A 的
id 來 retract,傳 `True` 與傳 `False` 得到**同一句**話 —— 而 v0.9.4 新寫的訊息無條件教它
「帶旗標重呼」。修死路的那一版自己又給了一條死路。現在先分 ownership:歷史 attempt 直接
說清楚它已被取代,並指出現在真正的 active/output 是哪顆。

### 權限停點的 `safe_next_action` 指向必然重複失敗的工具

`notebook_access_denied` 的 `error` 說去跑 `notebook_share_with_pool`,而 `safe_next_action`
回 `podcast_series` —— 同一份回傳的兩個欄位互相矛盾,而 skill 教呼叫端「拿不準就直接照
`safe_next_action` 做」,只讀那個欄位的自動化會原地重試同一個沒權限的帳號。

程式碼註解承認這個矛盾,理由是「白名單只放真工具名,不想為此新增字面值」。**但那個理由
在 v0.9.3 就被自己推翻了** —— 那一版為了完全相同的道理加過 `ACTION_EPISODE` 與
`ACTION_RETRACT`,而 `notebook_share_with_pool` 本來就是公開 MCP 工具。新增
`ACTION_SHARE_WITH_POOL`,`_classify_not_accepted_stop` 改回三元組。

### 其餘兩條

- **新出口沒寫進舊訊息**:`podcast_episode` 的「already has durable active attempt」與
  `_ensure_resume_attempt` 的「has active attempt」都無條件教「reconcile or resume」——
  對 `not_accepted` 那三條全是死的(reconcile 明說該狀態不可對帳、resume 要 artifact_id
  而它是 null、identical arguments 在 brief 產生器改過後重現不了)。兩處改成按
  `_outcome_is_settled` 分岔,把 v0.9.3 開的免旗標 retract 寫進去。
- **稽核欄位的理由寫錯了**:v0.9.4 說「`dispatch.status` 在 retract 之後還會被後續操作
  改動,所以連當時的值一起存」—— 與 tombstone 的 default-deny 直接矛盾,`_attempt_record`
  擋掉所有 attempt 級 writer。真正的理由是「讓這筆稽核自我完整、不受手改 manifest 影響」。
  同時講明兩個欄位要一起讀:`abandon_in_flight` 是**傳了什麼**而非「特權有沒有生效」,
  結果已定的 attempt 就算傳 `true` 也是白傳;文件的狀態表也限縮成「active、未 promote」。

### 沒改的一條

Codex 同意 FINDING-4(低階 `generate_audio` 沒有配額 failover)維持不修:風險是中等可用性,
不會改寫 manifest、重複 claim 或毀損輸出。它也指出真要修不能只包一層 rotate —— 得先有
durable dispatch receipt 才能滿足 ADR-0010「記帳與送出同源」,否則 response 一遺失稽核也跟著
消失。**完整修法其實是把低階救援升成 manifest-backed 能力**,在那之前保留現狀比較安全。

四道修正各做過突變驗證。610 passed。

## v0.9.4

v0.9.3 真實驗收抓到的四個 FINDING,三個修在這裡(第四個的 runtime 半留作單獨決策)。
**都不在核心機制上** —— 核心的驗收結果寫在下面 v0.9.3 節。

### 拒絕訊息自己是死路(FINDING-2)

`podcast_attempt_retract` 對 `acceptance_unknown` / `accepted` 不傳旗標時,訊息逐字是
`only a promoted output attempt can be retracted` —— **那句話在那個狀態下是假的**,
傳 `abandon_in_flight=True` 就 retract 得掉(同一輪驗收下一步就實測了)。只讀工具回傳的
呼叫端會判定「這條路關著」。

**這是 v0.9.1 FAIL-1 的同型**:指引在它自己產生的狀態下不可執行。而諷刺的是 v0.9.3
整輪都在修這個形狀 —— 它修了 `abandon_in_flight` 的 **docstring**,漏了 **runtime 訊息**,
等於修好給人讀的那份、留著給機器讀的那份。真正的呼叫端讀的是後者。

訊息現在帶三件事:那顆 attempt 當下的 `dispatch.status`(呼叫端才知道自己落在哪一格)、
先去 `artifact_list` 查雲端、然後帶 `abandon_in_flight=True` 重呼;並註明 `prepared` /
`not_accepted` 不需要旗標。

### 稽核紀錄分不出兩種 retract(FINDING-3)

一次是 `prepared` / `not_accepted`(純本機、零遠端後果、manifest 自己就知道),一次是
呼叫端**顯式宣告**了推導不出來的外部知識、而遠端可能真的有東西在燒 —— 兩者的
`retraction` 區塊欄位**完全相同**,事後只能去讀 `dispatch.status` 反推,而那個欄位在
retract 之後還會被後續操作改動。

`reason` 必填的理由是「retract 是審計事件」(ADR-0009);同一個理由要求記下**這次動用了
哪一種權限**。`retraction` 現在多存 `abandon_in_flight` 與 `dispatch_status_at_retraction`
(當時的值,不事後反推)。ADR-0010 §Transparency:manifest 是唯一的稽核憑據。

### 文件:兩處「沒限定範圍」的說法(FINDING-4 / FINDING-1)

- **SKILL.md §Auth 說配額耗盡「呼叫端不會看到失敗」,沒說那只涵蓋 podcast 家族。**
  `_rotate_for_quota` 只在 `tools_podcast`;低階 `generate_audio` / `generate_slides` /
  `generate_report` 撞到配額直接拋。這在救援時最容易咬人 —— 配額耗盡正是需要救援的時候,
  而 troubleshooting 對 `acceptance_unknown` 死結建議的備援路徑(低階 `generate_audio` +
  `podcast_attempt_adopt`)正好建在沒有 failover 的那一支上。v0.9.3 驗收就因此卡住了
  `podcast_attempt_adopt` 的正向驗證。兩邊都標註了。
  **要不要給低階入口也接上 failover 是獨立的設計決策,這一版沒做。**
- **驗收工作區範本的清理指令會誤刪活著的 server 的憑證目錄。**
  `fuser "$d" || rm -rf "$d"` 的前提是「活著的 server 持有 open FD」—— 而 server 寫完
  slot 檔就 `close`,執行期不持有任何 FD,`/proc/<pid>/cwd` 也不在那裡。判別實驗確認
  `fuser -v` 與 `lsof +D` 對活著的 server 都是空的,所以那條指令對**每一個**活著的
  server 都判成「沒人持有」。這台當下常有 3 個 server 在跑,其中一個是 `-c prd` 的正式
  帳號 pool。範本改成「有活的就不自動刪,印 `lstart` 對照表讓人判斷」。
  ⚠️ **那份範本在 `.claude/skills/acceptance-workspace/`,而 `.claude/` 被 gitignore ——
  它從來不在版控裡**(`aa81890` 那個「把驗收工作區的做法固化成 skill」的 commit 實際只
  收了 `tests/` 與 `uv.lock`)。所以這個修正**只存在原作者那台機器**,clone 下來的人
  既拿不到範本也拿不到修正。要不要把它納入版控是獨立決策,這一版沒動。

### 回收成離線測試

`test_the_refusal_message_points_at_the_way_out`(訊息要說得出正門與當下狀態)、
`test_the_audit_record_says_whether_the_flag_was_used`(兩種 retract 的稽核紀錄要分得出來,
回傳值與落盤都驗)。606 passed。

## v0.9.3

**一個 host 跑了 14 集,13 集裡 9 集內容錯置,而工具全程回報成功**(2026-08-10)。
這一版把那件事變成呼叫不到的組合,並修掉一路挖出來的四個相鄰缺陷。

### 症狀為什麼查不出來

模型講不出某一項時,會**從 context 撈一個前面集數講過的內容填進那個位置**。聽起來很順,
證據還全部找得到出處(某集的四項捏造分別來自 EP01/EP02/EP12 的考點,一字不差)。而
**任何成功訊號都看不出來** —— 音檔存在、sha 相符、時長正常、`ok=true`;只有拿
`source_fulltext` 對逐字稿提問「該篇獨有的事實」才驗得出來。

實測分界(同一節目、同一 brief):11–15 筆 → 6 教錯 + 4 捏造;6 筆 → 0;1 筆 → 0。
所以量到的是「≤9 乾淨、≥11 出事」——**10 這一格從來沒量過**,從 10 起拒絕是 fail-closed
的政策選擇,不是實驗結論。程式碼與訊息都照這樣寫,免得下一個調閾值的人把政策當量測。

### 守門:判準是「帶進去幾筆」,不是「有沒有指名」

第一版把守門掛在 `source_ids is None` 上,而那只是 proxy —— 指名 12 筆與不指名 12 筆
對模型是同一件事。host 讀了 skill 知道要指名、卻把「本集 + 最近 5 集」做成「本集 +
全部前集」時,掛在 proxy 上的守門會全程沉默而事故照樣發生。改成一律算 effective count:
指名時是純本地的 `len()`(超標連對帳 RPC 都不打),沒指名才去問筆記本。

**三個公開入口各一道**,都在該路徑最早的零副作用位置:`tools_basic.generate_audio`
(低階救援入口,但失效模式一模一樣 —— 只守 podcast 家族的話它就是公開後門)、
`_run_episode`(建 attempt 之前)、`podcast_series` 的 `refuse_if_too_many_sources`
(**任何 manifest mutation 之前**)。

`prior_mp3_path` 會在守門**之後**、生成之前再上傳一筆,所以現在預先計入 —— 否則 9 筆
放行、上傳完以 10 筆送出,剛好落在拒絕線上。那是同一個函式內的確定性順序,不是競態。

### series 停在**結構化停點**,而且那個停點走得出去

守門一開始是裸拋 `ValueError`。而 series 的 except 分支收的是 `RuntimeError` /
`_REFUSED_WITHOUT_DISPATCH` / `(TimeoutError, ConnectionError)` —— 裸拋會直接冒泡,
**把前面幾集已經跑完的 `run_results` 整份丟掉**。F-4 修過同一個形狀,這次用專屬型別
(`TooManySourcesError`)擋在再犯之前。

觸發時序是常態不是邊角:逐集加原文時 EP_n 開始前有 `2n-1` 筆,`2n-1 >= 10` 解出 **n=6**
—— 正好是 skill 說的「EP06 起改用單集入口」。

**停點位置與 `safe_next_action` 都改過一輪**,因為第一版兩者都是錯的:

- 守門原本擺在 dispatch 前,但 `not_accepted` 已經先被 re-arm 成 `prepared`、
  `failed/removed` 已經先建好 superseding attempt(`attempt_count` 白長一格)。
  等於「先把狀態改成準備送出、再拒絕送出」。已移到 re-arm/supersede 的共同上游。
- `safe_next_action` 原本一律指 `podcast_episode`。但既有 attempt 的 settings 是
  「不指名來源」,呼叫端拿指名版進來會被 `_is_resendable_same_request` 拒絕
  (`already has durable active attempt`),不指名又撞回守門 —— **死路**,正是 v0.9.1
  修過的「指引在它自己產生的狀態下不可執行」。改成:沒有 attempt → `podcast_episode`;
  有 attempt → `podcast_attempt_retract`(新增進 `SAFE_NEXT_ACTIONS`)。
  測試現在**真的照著 `safe_next_action` 走一遍**,不只斷言字串。

### `podcast_attempt_retract` 對「從沒送出去」的 attempt 不再要求外部宣告

上一條的出路要能走,retract 得先能收下那顆 attempt —— 而它拒收。准入判準問的是
「**這一集有沒有其他 output 證據**」,該問的是「**這顆有沒有可能在遠端留下東西**」。
後者 manifest 自己就記著:`dispatch.status` 是 `prepared` / `not_accepted` 時,契約
保證伺服器沒建出 task(`_REFUSED_WITHOUT_DISPATCH` 與 ADR-0010 紀律④整條立論都建在
這上面),作廢它是純本機、零遠端後果。

**又是把 proxy 當判準**,而真實後果撞過三次:一次 dispatch 被配額或 502 拒絕後 attempt
停在 `not_accepted`,retract 拒收它,而 `podcast_episode` 給的唯一出路「用 identical
arguments 重送」要求 brief **逐字相同** —— 中途改過 brief 產生器就重現不了。呼叫端只剩
「低階 `generate_audio` + `podcast_attempt_adopt`」這條繞路,還多燒一次生成配額。

放寬**只涵蓋那兩種狀態**。`acceptance_unknown` / `dispatching` / `accepted` 仍然只能走
`abandon_in_flight=True` —— 那個是真的不知道有沒有受理,需要外部知識。同時補上 docstring:
原本只寫「伺服器已經受理生成(dispatch=accepted)」,讀起來像不適用於 `acceptance_unknown`,
於是撞上死結的人不知道正門就在那個旗標後面。

### preflight 前移不可以繞掉權限分類

守門的 `sources.list` 現在是整條路徑上**第一個**遠端呼叫,而權限分類原本只長在
`_dispatch_audio_with_failover` 裡。pool 剛 rotate 到看不到 notebook 的帳號時,真實的
`ClientError(rpc_code=7)` 會在 helper 外裸拋 —— 前面幾集的 `run_results` 一起丟掉,
而且拿不到「去 `notebook_share_with_pool`」那條指引(v0.9.0/v0.9.1 花了兩輪才做出來的)。
`_list_sources` 統一翻譯,只轉權限那一種:網路錯誤、認證過期一路吞下去只會把根因埋掉。

### 仍未守的一處(刻意)

`artifact_retry_failed` 對 failed AUDIO 的原地重跑**沒有**筆數守門。RETRY_ARTIFACT 只送
artifact_id,伺服器**應該**沿用該 artifact 原本的來源集合而不是重抓筆記本當下全部 ——
但那是推測、沒有實測前提,所以既不加守門(會廢掉一條救援路)也不宣稱安全。已寫進該工具
docstring,列入 v0.9.3 驗收。

### 真實驗收(2026-08-10,stg 9 帳號池):守門與停點全部成立,四個 FINDING 都在外圍

完整記錄:[acceptance-v0.9.3-findings.md](docs/acceptance-v0.9.3-findings.md)。full 一輪,
35 支工具全部呼叫過(`podcast_attempt_adopt` 只走到拒絕路徑、`publish_series` 依使用者
指示只跑 preflight),13 次真實生成。

**三個驗收問題的答案**:守門擋得住(八個邊界格全中,含「判準是筆數不是指名」與
`prior_mp3_path` 預先計入)、擋下來照著 `safe_next_action` 走得出去(兩種停點各走完整
一輪到重生成功,並先刻意照錯的做一次撞出 `already has durable active attempt`)、
沒有把原本能跑的路關掉(9 筆整季流程照生、**只等 finalize 的 attempt 不被攔**、
`generate_slides`/`generate_report` 在 15 筆筆記本上照生)。

`prepared` / `acceptance_unknown` / `accepted` 三種既有 attempt 用**可控的 client
cancellation** 造出來(3.2s / 7s / 45s 三個時點,RPC 全真)。

**`artifact_retry_failed` 從「推測」變成一次實測**:failed AUDIO 之後才加 4 篇全新領域
來源(筆記本 12 筆、已超標),retry 完逐字稿對 16 個獨有指紋 **0/16**;同一批來源指名
生成的正向對照 **16/16**。伺服器確實沿用原來源集合,守門維持不加。限制(n=1、只測
「新增」沒測「刪除」)已寫進該工具 docstring。

**v0.9.2 的 `notebook_share_with_pool` 一併結案**:非 owner 作用中帳號對只有 owner 看得到
的 notebook 呼叫 → `shared_by` 是真 owner、事後 `OWNER×1 + EDITOR×8`、輪替游標不動;
單帳號模式 **0 趟** `get_status` 安靜 no-op 且 schema 一致;不存在的 id **1 趟**就原樣
重拋 `rpc_code=5`。

**四個 FINDING(都不在核心機制上)**:

1. 驗收工作區 CLAUDE.md 的 `fuser` 清理指令偵測不到活著的 server(server 寫完憑證就關
   FD),照抄會刪掉正在跑的 server 的憑證目錄。
2. `podcast_attempt_retract` 對 `acceptance_unknown` 不傳旗標時,拒絕訊息是
   `only a promoted output attempt can be retracted` —— **沒提 `abandon_in_flight`**,
   而那句話在該狀態下是假的。這一版修了 docstring,沒修 runtime 訊息。
3. manifest 的 `retraction` 區塊**沒有記下 `abandon_in_flight`**:純本機的 `prepared`
   retract 與顯式宣告的 in-flight retract 事後在稽核紀錄上分不出來。
4. 低階 `generate_audio` **沒有配額 failover**(`_rotate_for_quota` 只在 `tools_podcast`),
   而 SKILL.md §Auth 說「呼叫端不會看到失敗」沒有限定範圍 —— troubleshooting 對
   `acceptance_unknown` 死結建議的備援路徑正好建在它上面。

**回收成離線測試**:`test_the_guard_does_not_block_an_attempt_that_only_needs_finalizing`
—— 既有 26 條守門測試只驗「該擋的擋住」,沒有一條驗「不該擋的放過」,把守門條件改成
無條件擋時全部照樣綠。已做突變驗證(改壞→紅、還原→綠),全套 604 passed。

### 這一輪的分工

離線 review + **Codex 獨立複審**(沒有先餵它我方 findings,兩邊各自收斂)。Codex 抓到的
三條是我方沒看到的:低階入口的公開後門、`prior_mp3_path` 的確定性順序、以及那條死路。
它也做了判別實驗——把權限錯誤掛在 `sources.list` 上,證明既有測試因為只把錯誤掛在
`generate_audio` 而全綠。四道新守門各做過突變驗證。

## v0.9.2

**v0.9.1 的 `notebook_share_with_pool` 修法本身帶進三個缺陷**(2026-08-10,離線 review
+ Codex 獨立複審,兩邊各自指出同一組問題)。都在同一支工具上,根因是**新的 executor
掃描被擺在錯的位置、而挑選條件只驗了一半**。

**「看得到」不等於「分享得動」。** 掃描挑第一個 `get_status` 成功的帳號 —— 但作用中
帳號在 pool 裡通常正是一個 EDITOR(owner 早就把 notebook 分享給全 pool,所以它看得到),
於是 `add_user` 由一個很可能無權改分享設定的帳號發出,而 pool 裡真正的 owner **從沒被
試過**,症狀還偽裝成「這個 notebook 沒救」。修法不必逐槽掃:`get_status` 回的
`shared_users` 含 owner 那一列(v0.9.0 真實驗收實測的形狀),第一趟成功的查詢就足以
定位 owner,命中 pool 就換它的 client,總成本仍是 1 趟 RPC(`_owner_slot`)。owner 不在
pool 時退回「看得到的那個」去試 —— 那可能被伺服器拒絕,但那是遠端的答案,不該預先替它
判死。副作用:「owner 落進 peers」這條路因此不可達,`_has_sufficient_permission` 的
OWNER 豁免退成最後防線(兩者用同一個 `SharePermission` 比對,上游改形狀是一起失效的)。

**單帳號模式不再是零 RPC,還多長出一個失敗模式。** 掃描被擺在 peers 計算之前,於是
單帳號對一個**不屬於自己**的 notebook 呼叫這支工具,會從「安靜回 no-op」變成拋
`NotebookAccessDenied`,而訊息還叫人「分享給 pool 成員」——單帳號根本沒有 pool。
沒有 peers 就沒有事情可做,這個判斷是純本機的,不該用一趟遠端呼叫換答案:順序改回
「先算有沒有 peers」。

**為什麼 591 個測試全綠。** `test_..._single_account_is_a_noop` 斷言的是
`sharing.calls == []`,而 fake 只在 `add_user` 裡記錄 —— 對一支「只打 `get_status`」
的實作永遠是綠的。fake 現在另記 `status_calls`(**刻意不併進 `calls`**:既有測試用
`calls == []` 表達「沒打 add_user」,混在一起那個意思會消失)。同型的一課第 N 次:
**觀測面沒有涵蓋到的行為,測試寫了也是綠的**。

**回傳 schema 不一致**:no-op 路徑少了 `shared_by`,呼叫端統一讀它時 `KeyError`,
而那條路徑(單帳號)最常見。所有成功路徑收斂到 `_nothing_to_share`。

順帶:`tools_podcast.py` 在 `_errors.py` 抽取後留下的 `ClientError` dead import。

## v0.9.1

v0.9.0 真實驗收(2026-08-09/10)抓到的兩個 FAIL。**都不在核心機制上** ——
核心改動的驗收結果寫在下面 v0.9.0 節的〈真實驗收〉。


驗收本身的結論在下方「驗收結果」節;這裡是**照著結論改的兩件事**。兩個 FAIL 都不在
核心機制上,但都是「照著文件做會走進死路或拿到錯東西」。

**`notebook_access_denied` 的指引在它自己產生的狀態下不可執行。** 停點完全正確,
但 `error` 叫呼叫端跑 `notebook_share_with_pool`,而那支工具用**作用中帳號**執行 ——
正是那個看不到 notebook 的帳號(配額 failover 剛換過去)。於是指引自己也 permission
denied,呼叫端只是從一個死路換到另一個。而這正是文件描述的典型情境:既有 notebook 的
owner 通常是 slot 1,pool 卻會 rotate 走。

修法是讓那支工具**自己找得動手的帳號**:先試作用中的(絕大多數就是它,零額外成本),
permission denied 才依槽位順序試其餘的,回傳多一個 `shared_by`。**只對 permission
denied 往下試** —— 網路錯誤、認證過期每個槽位都會遇到,一路吞下去只會把根因埋掉。
掃描全是唯讀 `get_status`,而且**不動輪替游標**(`_ACTIVE` 的語意是「配額走到哪」,
借去做別的事會讓 failover 的帳號記帳失去意義),所以 runtime 新增的是
`all_clients()` 而**不是**「切到某個槽位」的 API。全員都看不到時 fail-loud,訊息說出
唯一出路(用真正的擁有者帳號在網頁上分享)。順帶把 `NotebookAccessDenied` /
`is_permission_denied` 抽到 `_errors.py`:兩個模組要判同一件事,各寫一份等於埋一顆
「上游改了 `rpc_code` 只會有一處被改到」的地雷。

**`artifact_revise_slide` 的「artifact 不變、就地改版」是錯的。** 實測遠端會 fork 出
一顆 `<原標題> (2)`,傳進去那顆原封不動還在。**實作本來就是對的** —— 它一律用回傳的
id 而不假設相等,那個「不自己假設」的寫法救了這支工具:當初若照 docstring 寫死用輸入
id,下載到的會是**沒改過的舊那份**,而且看起來完全成功。修的是文件,外加回傳
`superseded_artifact_id` 讓呼叫端知道 id 換了、舊的還在(**刻意不自動刪**:遠端破壞性
動作,而且新的萬一有問題,舊的是唯一退路)。連續 revise 會堆出 `(2)`、`(3)`… 而標題
只差一個序號,正好放大文件自己承認的「`artifact_list` 分不出哪一集」風險。

### 那條「未結案」的事實 —— 已結案(2026-08-10,離線)

`source_delete` 之後 `source_list` 確實看不到那筆,但 **`source_fulltext` 用同一個
`source_id` 在 55 分鐘後仍讀得回完整內容**。所以「從筆記本移除」與「後端不再持有」
不是同一件事,而 ADR-0009 那條「重生前必須刪掉舊回錄 source」的清理義務,**效果因此
沒有被證明**(判 INCONCLUSIVE,不寫成 PASS)。它擋得住可觀測的那一半(同名 source
出現兩筆)是確定的,所以那條 precondition 不鬆;要結案得用兩個內容互斥的來源做一次
生成對照。

### 真實驗收(2026-08-10,stg 9 帳號池):FAIL-1 **已修好**

targeted 一輪,只驗 FAIL-1 —— 核心機制沿用 v0.9.0 的結論(v0.9.1 沒動到)。重現前提
(作用中帳號 rotate 到非 owner 槽位)在 MCP 工具面挪不動輪替游標,所以由測試腳本走
`app._lifespan` 起 pool 驅動,**RPC 全是真的**。

用 slot 1 的憑證直接走 SDK 建一顆**刻意不分享**的 notebook,rotate 到 slot 2
(唯讀 `notebooks.get` 實測 `rpc_code=7`),然後照 v0.9.0 的死路走一遍:

| 步驟 | 實測 |
|---|---|
| `podcast_series` | 2.1s 停在 `observed_state="notebook_access_denied"`,`error` 指名 `notebook_share_with_pool` 並寫出被拒帳號,`attempt_count=1` |
| `notebook_share_with_pool`(v0.9.0 死在這) | **12.9s 成功**。`shared_by` = slot 1 owner ≠ 作用中的 slot 2;`shared_with` = 其餘 8 個;事後 `get_status` 後檢 `OWNER×1 + EDITOR×8` |
| 掃描成本 | 只花 2 趟 `get_status` 就命中 owner(先試作用中、再依槽位順序);沒權限那個 **0 趟 `add_user`**;12.9s 幾乎全在 8 個 `add_user`,單趟 sharing RPC 0.33–0.61s,**掃描期間沒撞到 rate limit** |
| 輪替游標 | 掃描前後同一個帳號 —— 沒被借走 |
| 再跑 `podcast_series` | `observed_state` 變 `pending`,dispatch `accepted`;而且這次撞配額 failover **連走 4 個不同帳號**對同一個 notebook 送出、一次 permission denied 都沒有 —— 分享對 pool 全員生效,不只對掃描碰到的那一個 |

兩個邊界:pool 全員都看不到時 raise `NotebookAccessDenied`,訊息列出被拒的 8 個帳號並
說出兩條出路(掃完整個 pool 8 趟唯讀 `get_status` = 4.3s,≈0.54s/帳號)。**非權限錯誤
不被吞掉這條在真實環境測到了**(原以為做不出來):不存在的 notebook id 回的是
`rpc_code=5`(not found)而非 7,9 帳號的 pool **只打 1 趟 `get_status`** 就原樣重拋。

那個真實形狀原本沒有離線測試鎖著 ——
`test_share_reraises_non_permission_errors_instead_of_walking_the_pool` 用的是
`RuntimeError`,連 `isinstance(exc, ClientError)` 都不過,**走不到 `rpc_code` 的比較**。
補上
`test_share_reraises_a_client_error_whose_rpc_code_is_not_permission_denied`,並做過
突變驗證:判準退化成「只看型別」時只有新那條紅,舊那條仍綠。



**結案方式不是再燒配額,是把 SDK 的三環釘住(全部離線可驗)**:
①`generate_audio(source_ids=None)` 在 **client 端**呼叫 `notebooks.get_source_ids()` 拿
清單、再把明確 id 列表送進 RPC —— **不是讓伺服器自己挑**;②`get_source_ids` 走 `get_raw()`
→ `GET_NOTEBOOK`,而 `sources.list`(我方 `source_list`)走的**也是** `GET_NOTEBOOK`,
同一支 RPC、同一份資料 ⇒ **`source_list` 看不到 ⟺ 生成的清單裡也沒有它**;
③`get_fulltext` 的 params 是 `[[source_id]]`、**不帶 notebook_id**,直接查 source 物件、
繞過 notebook。

**所以「刪掉還讀得回」是預期的,不是清理失敗** —— 那是兩個不同層級的查詢,而 ADR-0009
的清理義務有效。三環由 `test_generation_takes_its_source_list_from_the_notebook_not_the_server`
釘住:任一環被上游改掉就會紅,提醒重新論證。**它證明的是推導的前提,不是端到端行為**
——真要端到端仍得用兩個內容互斥的來源做一次生成對照,但在三環成立的前提下那只是加強,
不是必要條件。
## v0.9.0

一輪多 agent 深度審查的產出。**主軸是一件架構級的事:帳號身分不再放在 process 全域**
——其餘都是同一輪審查在 pool / failover / sharing / retract 四條路上找到的實際 bug。
minor bump 而非 patch,是因為憑證的存放方式變了(見下),即使對外 API 完全相容。

### 身分跟著 client 走,不再是 process 全域(ADR-0010 補記)

**根因是一個從一開始就站不住的前提。** ADR-0010 當初寫「`NOTEBOOKLM_AUTH_JSON` 必須
隨時等於作用中帳號」,而 v0.8.0 驗收 F-1 的修法(`_sync_auth_env`)也照著做。但
**MCP 是並行的**:`mcp/server/lowlevel/server.py` 對每則 incoming message 跑
`tg.start_soon(self._handle_message, …)`,而本 package 全域零鎖。SDK 的媒體下載又在
**下載當下**重讀憑證(`_artifact/downloads.py` 的 `self._cookie_loader(self._storage_path)`,
`_storage_path is None` 才回頭讀 env)。於是 EP05 正在 finalize(client 已 pin 住)、
EP06 撞配額 rotate,EP05 的下載就以別人的身分發出——**一個全域槽不可能同時是兩個值,
加鎖也救不了**,「隨時等於」這個要求本身無法達成。

改成每個槽位各自寫一份 **0600 storage_state 檔**(0700 `mkdtemp` 目錄,lifespan 每條退出
路徑都刪),用 `from_storage(path=…)` 建 client,SDK 會把路徑注進 download service
(`_client_assembly.py` → `ArtifactsAPI(storage_path=…)`)。`_sync_auth_env` 整支刪除,
**server 從此完全不寫這個 env**。

兩個代價寫在這裡免得日後有人以為是疏漏:①**憑證會落檔**,推翻了 ADR-0010 列的「不落檔」
優點——取捨是「能讀那個 0700 目錄的 user 本來就讀得到 `/proc/<pid>/environ`」;
②給了真路徑會**重新武裝 SDK 的 L2 inline PSIDTS `RotateCookies`**,而它**不受
`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**。它唯一入口是 strict cookie loader 的
`ValueError`,所以落檔前先用 SDK 自己的 `extract_cookies_from_storage` 驗一次,那條路
就永遠到不了——「不在本 process 內重鑄 cookie」的紀律(3 VM 共用一份唯讀 Doppler 憑證)
維持不變,缺憑證仍然是啟動時的大聲失敗。

**單帳號路徑逐字保持舊碼**:沒有輪替就沒有 race,不必平白多一份憑證副本與一組新的 SDK 行為。

### 記帳與送出同源(同一個並行問題的第二面)

`_claim_prepared_dispatch(account=…)` 與真正的 `generate(client)` 原本是兩次獨立的全域讀取,
而 `_rotate_for_quota` 更是在 dispatch 的 await **之後**才讀 `from_account`——那一整趟 RPC
期間,另一個工具呼叫完全可能已經 rotate 過。結果是 manifest 記下一個**根本沒參與這次
dispatch 的帳號**,而且兩個帳號都成功,事後無從發現。ADR-0010 §Transparency 說 manifest
是唯一的稽核憑據,那份憑據於是失真。

新增 `runtime.snapshot()` 一次取 `(label, client)`;`_dispatch_audio_with_failover` 改吃
`account=`/`client=`,failover 換帳號時兩者一起換,全程不回頭讀全域。
`tests/test_pool_gaps.py::test_failover_credits_the_account_that_actually_dispatched`
用「generate 期間並行 rotate」重現,突變驗證下舊碼記成 `b@x`(旁觀者)而非 `a@x`(被拒者)。

### 配額 failover:`ensure_started` 拋了 ≠ 沒建出 task

紀律②的判準原本實作成「`ensure_started` 拋了就 rotate」,但它的條件是
`is_failed or not task_id`——**只有後半是零副作用契約**。SDK
`_artifact/generation.py` 明寫「有 artifact_id 就回 `GenerationStatus(task_id=artifact_id, …)`」,
而 `_ARTIFACT_STATUS_MAP` 含 `FAILED`,所以「有 id + failed」在上游路徑上可達。那種形狀
rotate 重送會建出**第二個 artifact**:manifest 只綁得到後者,前者不在 `artifact_ids_before`
基線裡,日後 reconcile 撞成 `reconciliation_ambiguous`,還多燒一次配額。現在只有真的
沒有 id 才 rotate,有 id 卻 failed 標成 `acceptance_unknown`(先對帳,不盲目重試)。

### `podcast_series` 的 client 陳舊了一整季

`_run_episode` 早有 `client = runtime.get_client()` 並附註「failover 可能已經換過帳號」,
series 的 inline 重送分支卻沒有——**同一個要求補了一條路、漏了另一條,正是它要修的 F-4
的同一個形狀**。生產上被 v0.8.1 的自動分享遮住(pool 全員看得到同一個 notebook),
但那是巧合不是設計:同一個陳舊 client 還被用在 source cleanup 複驗、drift 複驗、baseline
`artifacts.list`,以及 **`probe_auth`**——那個「長跑前 fail-fast」預檢因此驗的是**沒在用的
那個帳號的 cookie**,正好在最需要它的時候驗錯對象。

### 權限被拒的指引在 series 路徑整個蒸發

兩層:`_mark_not_accepted` 收到的是原始 `ClientError`,manifest 的 `remote.error` 只留
`"permission denied"`;而 `NotebookAccessDenied` 繼承 `RuntimeError`,被 series 收成結構化
partial,**而 partial 不帶訊息欄位**。加上游標對權限問題刻意不 rotate,呼叫端拿到
`safe_next_action="podcast_series"` → 原樣重呼 → 再撞同一個沒權限的帳號 → 回一模一樣的
partial,`attempt_count` 恆為 1(而註解正說那個數字是「看得出自己在原地打轉」的唯一依據)
——無限重試。現在例外在標 manifest **之前**就建好、訊息指名 `notebook_share_with_pool`,
series 的安全停點回 `observed_state="notebook_access_denied"` + `error`,讓呼叫端分得出
「等配額」與「要人去補分享」。

### `podcast_attempt_retract`:兩種「無路可走」的狀態(ADR-0009 補記)

真實事故:某集的 attempt **已被伺服器受理,但送進去的 brief 是失真版**。五條路全被擋死
(retract 撞「只有 promoted output 才能 retract」、乾淨 brief 撞「already has durable active
attempt」、原樣重呼被 `_is_resendable_same_request` 擋、`supersedes_attempt_id` 撞
「is not terminal」、adopt/reconcile 都要求已 finalize),唯一出路是讓錯的版本跑完 →
promote → retract → `source_delete` → 重生。

現在 `abandons_unauthorized_candidate` 多涵蓋兩種:①**有 legacy 硬證據但沒有
`output_attempt_id`**(v0.5.0 前的 manifest),且同一輪要把那些 legacy 欄位一起清掉——
否則只開了 retract 這道門、下一次生成改撞 `has_hard_output_evidence`,等於換個地方繼續
擋死(補一半的又一例);②新參數 **`abandon_in_flight=True`**。

②**刻意做成顯式旗標而不是狀態判斷**:「這顆的輸入是錯的」是 manifest 推導不出來的外部
知識——它與「第一次 dispatch、還在飛」逐欄位相同,而後者屬於 reconcile/resume。放寬
*狀態* 規則等於靜默併吞 reconcile 的守備範圍。旗標只放行 `active_attempt_id` 那一顆,
其餘 ADR-0009 不變式全部原封不動。**它省不了配額**(生成已經在燒,retract 是純本機、
取消不了遠端);省的是整輪 finalize 與那筆會污染後續 context 的回錄 source。遠端那個
artifact 成為孤兒,但 `_claimed_artifact_ids` 掃所有 attempt(含 retracted),不會被誤 claim。

### 分享:VIEWER 被當成「已分享」

`notebook_share_with_pool` 的 dedup key 只取 email、丟掉 `SharedUser.permission`,而
`add_user` 的**預設就是 VIEWER**——使用者在 NotebookLM 網頁手動分享過的既有 notebook
極可能正是 VIEWER。於是工具回「已分享」、`add_user` 呼叫數 0,而 pool 其實還是壞的,
症狀要等 failover 換過去才爆:**這支工具存在的唯一理由沒被滿足**。現在只認 EDITOR/OWNER。
把 OWNER 也算「已足夠」順帶擋掉另一件事:failover 之後 peers 會含 notebook 的 owner,
而 SDK 只擋 `permission == OWNER` 這個**參數**、不擋「對象就是 owner」,對他呼叫
`add_user(EDITOR)` 等於降權。

同一輪的其餘四條:`add_user` 回傳的 `ShareStatus` 原本被丟掉,而它是**零成本的後檢**
(那趟 `get_status` RPC 本來就打了),workspace 政策擋外部分享 / email 打錯字都不 raise
——現在未生效就 raise;`notebook_create` 改成**驗證先於變更**(`_pool_peers` 是純本機檢查
卻放在 `create()` 之後,而 label 退成 `#N` 是決定性失敗,呼叫端每重試一次就多一個雲端
孤兒 notebook);`except Exception` 補上 `CancelledError`(8 趟分享 RPC 被外層 60s timeout
砍掉時,舊碼讓 notebook id 隨著裸例外消失);pool 帳號去重。

### 啟動期的四個 loader guard

①**base 槽位缺失**(`_2`/`_3` 在、不帶後綴的那個不在)舊碼直接 `return []`,而 `inline_auth`
又是用「base 在不在」判斷的——於是連 inline 模式一起關掉:`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE`
沒設(冷啟動那顆 RotateCookies poke 會作廢 3 VM 共用的 cookie)、`NOTEBOOKLM_HEADLESS_REAUTH`
保持生效、pool 等於關掉,而在有本機 storage_state 的登入機上還會**啟動成功**、用舊身分跑。
最糟的失敗形狀:看起來好好的。②**重複帳號**(ADR-0010 自己點名的 `dev_alt` branch-config
繼承陷阱)此前沒有任何機器檢查,`rotate_client()` 照樣回報「換到第二格」、failover 照樣
寫一筆謊報的 rotation。③空字串檢查原本只做在 `_2..`,第 1 槽沒做。④`_account_label` 吞掉
`get_account_email()` 的例外——那是啟動時唯一的真 RPC,而 `_pool_peers` 之後看到 `#N` 會
要求人工介入,訊息裡卻說不出原因。改成 `logging.warning`,**刻意仍不 raise**(一個死憑證
讓整台 server 起不來更糟)。

### 第二輪:codex 對整個 diff 做對抗性審查後的修正

上面那些改完之後,把整份 diff 交給 codex 做深度審查。它抓到的第一條就是**這一輪自己新種的**:

**身分只釘到 dispatch,finalize 又鬆開。** `_dispatch_audio_with_failover` 內部把
`(account, client)` 換對了,卻**只回傳 `artifact_id`**,那一份直接丟掉;兩個呼叫端只好
回頭 `runtime.get_client()` —— 又變回「此刻游標指到誰」。而**風險視窗幾乎全落在沒修的
那一半**:dispatch 只有幾秒,finalize(等生成 → 下載 → 回錄上傳 → rename)是數十分鐘。
一處是既有的,另一處是這輪新種的 —— 一個以「殺掉分開讀全域」為目的的 changeset,
自己在 series inline 路徑上又種了一顆同型的。現在回 `(artifact_id, account, client)`。

**而且核心那一半原本沒有測試鎖住。** 把 `generate(client)` 改回 `generate(runtime.get_client())`,
全套照樣綠 —— 因為那條測試的 fake 在 `generate_audio` **內部**才 rotate,那個時序只驗得到
「記帳讀了誰」,永遠驗不到「generate 收到哪個 client」。真正的縫在 `snapshot()` 之後、
dispatch 之前那幾個 await(baseline `artifacts.list`、`_claim_prepared_dispatch`)。新測試
把 rotate 注進那個視窗,一條同時鎖住三個突變(dispatch 讀全域 / 記帳讀全域 / finalize 讀全域)。

**PSIDTS 空值繞過預驗證,`RotateCookies` 仍打得出去(實跑重現)。** 上面那段「先驗過 ⇒
L2 那條路永遠到不了」的論證,建立在兩顆驗證器條件相同 —— 而它們對**空值**的判定相反:
`extract_cookies_from_storage` 只看 name(空字串照樣算存在),strict loader 連 value 也要
非空。於是 `__Secure-1PSIDTS: ""` 預驗證通過 → 開檔 raise → recovery → **真的發出
RotateCookies POST**,而 server 這邊正常啟動、只有 debug 級訊息。預驗證改成 strict 同語義,
並補一條絆線測試同時釘住「SDK strict loader 的判定」與「兩者等價」——那個假設是整段安全
論證的地基,此前沒有任何東西守著。同時把 `NOTEBOOKLM_REFRESH_CMD` 加進 inline 模式的
env override:它會走到帶 recovery 的 loader,而 `docs/superpowers/specs/` 的設計文件正把
它列為「Doppler 過期自癒」方案,誰照做就打開這條路(該文件已加過時標註)。

**`notebook_create` / `notebook_share_with_pool` 三次分開讀全域。** `_pool_peers` 與
`create()` 之間沒有 await(乾淨),裂縫在下一步:`create` 是 await,之後 `_share_each` 才
第三次讀全域。並行呼叫在那個 await 裡 rotate A→B,`_share_each` 就拿 B 去分享一個 **B 還
看不到的 notebook** → 失敗,雲端留下只有 A 看得到的孤兒;而 `rotate_client()` 不回頭,
事後在同一個 process 補跑一樣是 B,只能重啟 server。

**其餘**:`abandon_in_flight` 讓「並行 retract 掉在飛的 attempt」變成**受支援操作**,
於是 except handler 裡的 `store.read()`/`_attempt_record` 多了一條會 raise 的路 —— 在
except handler 裡再拋會蓋掉真正該讀的錯誤,兩處都包起來(讀不到就把原例外原樣交出去)。
inline 重送分支的契約改成三態:同一個 `acceptance_unknown` 原本只因例外型別是不是
`RuntimeError` 就分岔,一邊裸拋(**前面幾集跑完的 `run_results` 整份丟掉**)、一邊回
partial,而 series 的契約正是「預期內的停止用回傳值表達」。email 比對加 `.casefold()`
(大小寫不同會讓後檢恆為 False → 每次都 raise 且留孤兒)。

**一條無法離線結案、已標記進程式碼的**:`_has_sufficient_permission` 把 OWNER 算進
「已足夠」,用來擋掉「failover 之後對 notebook owner 送 `add_user(EDITOR)` 等於降權」。
但前提 —— `get_share_status` 的 `shared_users` **會不會包含 owner 那一列** —— fake 證不了
(它吐的就是測試自己 seed 的東西)。若不包含,那是整個 diff 唯一一條會**靜默改掉使用者
既有權限**的路徑,而且後檢會判定「已生效」而放行。一次唯讀 `sharing.get_status` 對真帳號
即可結案,列進下一輪 stg 驗收。

### 測試

551 → 585。除了對應上述每條的迴歸測試,補掉三個結構性盲點:**pool 測試原本每個槽位裝的是
同一個 fake client 物件**,所以「這通 RPC 實際發給哪個 client」完全不可見(series 陳舊
client 那條就是從這個縫溜過去的);**`FakeSharing` 少了 `permission` 欄位**,正是 VIEWER
那個 P0 沒有任何測試會紅的直接原因(與 `Source.created_at` tz 是同一種「陷阱在 fake」);
**sharing API 零 contract 測試**,而 `SharePermission` 是 module-level import(上游搬家 =
server 起不來)、`permission` 用位置參數傳(0.7.0 對 source add 做過 keyword-only 那次的
同型風險)。另補「正常關閉時 N 個 client 都被關掉」——此前在 `set_clients` 前插一行
`stack.pop_all()`,38 條測試照樣全綠。

### 真實驗收(v0.9.0,2026-08-09,`stg` 9 帳號 pool,35/35 工具覆蓋)

劇本 [docs/acceptance-v0.9.0.md](docs/acceptance-v0.9.0.md)。**核心改動全綠,而且是用
「新舊行為的判別實驗」證的,不只是「沒壞」。**

- **身分跟著 client 走 —— 決定性證明(不燒配額)**:同一個 process、同一顆 audio artifact,
  兩個不同 storage_state 檔各建一個 client;把其中一個帳號 `remove_user` 出共享名單後,
  它的下載失敗、另一個成功。**再把 process 全域 `NOTEBOOKLM_AUTH_JSON` 換成沒權限那份憑證,
  用有權限的 path 建 client 仍下載成功** —— 舊機制(env 當身分)在這裡必敗。
- **並行時序真的排出來了**(以往每輪都是單線):A 集 `accepted` 在 slot 4 進入 finalize
  期間,B 集撞配額把全域游標推到 slot 5;A 的 `dispatch.account` 沒有被改寫,兩集各自
  指向真的送出它的帳號。三支寫 manifest 的附件工具同時在飛,沒有一次覆寫遺失。
- **failover 記帳鏈式接龍**:`(pool-account-1→pool-account-2)`、`(pool-account-2→pool-account-3)`、`(pool-account-3→pool-account-4)`,
  每筆 `from_account` 都是**該次實際被拒**的帳號,`attempts` 全程 1。配額拒絕是同步
  `RateLimitError`,每次 rotate 約 0.7 秒(實測每帳號每天 3 次生成)。
- **五條 dispatch 路徑全部真的走到**,含 v0.8.0 漏補過的 series inline 重送;
  supersede 用**真的遠端 `artifacts.delete()`** 製造 `removed`,舊 attempt 完整保留。
- **reconcile 的三態全部走到**(零候選 / 唯一候選補綁 / `reconciliation_ambiguous` → adopt),
  dispatch 視窗判準與 artifact claim 唯一性都實測有效。`acceptance_unknown` 是用 MCP 協定層
  `notifications/cancelled` 逼出來的。
- **`abandon_in_flight` 含反向**:不帶旗標照舊拒絕;帶旗標成功,retraction 與理由留存,
  標題不變,取代版落在 `attempts/<attempt_id>/`。三支續跑工具對已 retract 的 attempt
  都回同一句可讀的 tombstone 拒絕。
- **啟動期 guard 6 種情境全部當場 raise 並指名槽位**;9 槽位 = 9 個 `0600` 檔於 `0700` 目錄,
  正常結束(stdin EOF)後目錄消失;`mcp` 1.29.0 的 pydantic 警告只在 stderr,
  stdout 非 JSON 行數 **0**。

**⛔ 抓到的兩個 FAIL(都是契約/指引與行為不符,不在核心機制上):**

1. **`notebook_access_denied` 的指引在它自己產生的狀態下不可執行。** 停點與 `error`
   都正確(指名 `notebook_share_with_pool`、寫出被拒帳號、`attempt_count=1`),但那支工具
   用**作用中帳號**執行 `sharing.get_status()`,而作用中帳號正是看不到這個 notebook 的那個
   → 同樣 permission denied。只有 owner 分享得動,而 MCP 沒有指定槽位的方式。
   文件描述的典型情境(既有 notebook + pool 已 rotate)**就是這個死路的常態**。
   至少要在錯誤訊息補一句「請用 owner 帳號分享(重啟 server 讓游標回 slot 1,或在網頁上手動
   分享)」;進階做法是 `get_status` 吃到 permission denied 時自動換槽位試一輪。
2. **`artifact_revise_slide` 的「artifact 不變」是錯的。** 實測回傳的 `artifact_id` 與輸入
   不同,遠端多出一顆 `System Latency Realities (2)`,舊的還在。實裝碼 docstring 與 skill
   `tool-reference.md` 都這樣寫,所以不是快照過期。它放大了文件自己承認的「artifact ↔ episode
   binding 未驗證、分不出是哪一集的 deck 就別猜」風險 —— 順手把 `slides_artifact_id`
   回寫 manifest 就能解掉(工具已經知道新 id)。

**兩個 INCONCLUSIVE(不寫成通過)**:① `artifact_retry_failed` 的成功路徑 —— 這一輪 11 次
生成沒有自然產生 `failed` artifact,而遠端刪除得到的是 `removed` 不是 `failed`,無法可靠製造;
只驗到負向 fail-loud。② `source_delete` 之後那筆 source 對**生成端 context** 的影響 ——
`source_list` 已看不到它,但 `source_fulltext` 55 分鐘後仍讀得回全文,所以 retract 流程
「清掉 stale 回錄 source 以免污染後續各集」的效果未被證明。

**`publish_series` 的成功路徑依使用者要求不測**(uploader 不刪檔、上傳即永久)。
已驗三層 preflight 全部 fail-closed 在第一個 PUT 之前(`cover_path` → `description` →
`slides_pdf_path`),並用 feed URL `HTTP 404` 證明沒有任何 blob 上傳。

**附帶觀察**:`serverInfo.version` 回的是 `mcp` SDK 版本(`1.29.0`)而非 `notebooklm-mcp`
版本 —— 本 repo 最痛的失敗模式正是「uv git cache 壞掉靜默裝成舊版」,而呼叫端從 handshake
看不出來;建議 `FastMCP(version=importlib.metadata.version("notebooklm-mcp"))`。
沒權限的帳號下載 artifact 時,SDK 回的是 `ArtifactNotReadyError`(「還沒生好」)而非權限錯誤
—— 呼叫端很容易誤判成「再等一下」而無限重試。

---


## v0.8.2

v0.8.1 的真實驗收(stg 四個命題全綠 + prd 端到端四集回歸)之後的補完。

### 新增

- **`notebook_share_with_pool`** —— 把**既有** notebook 補分享給 pool 其餘帳號(EDITOR)。
  v0.8.1 的自動分享只對 `notebook_create` 生效,而正在跑的專案 notebook 都是既有的;
  沒有這個前置狀態,配額耗盡 failover 換帳號時會 `NotebookAccessDenied`,而在這支工具
  之前唯一的補法是自己寫 SDK 腳本。**冪等**(已有權限的帳號跳過),單帳號是 no-op。

### 修正

- `notebook_get` 的 docstring 講明 **`is_owner` 在 notebook 有共享者時一律回 `False`**
  (驗收 G-1:同一份 owner 憑證,移除共享者後同一欄位才變 `True`)。多帳號 pool 下自動
  分享是常態,這個欄位實務上恆為 `False`,**不能拿來判斷歸屬**。行為來自上游 SDK,
  沒有任何生產邏輯依賴它,所以照實轉發 + 文件說清楚,不悄悄拿掉欄位。
- 補上 v0.8.1 漏 commit 的 `uv.lock` 版本號。

### 驗收結果(v0.8.1,四個命題全綠)

- **重送/supersede 路徑也 failover、也記帳號**(F-4 的修正成立):同一 attempt 重呼後
  `errors[]` 3→6,新增兩筆新時間戳的 `dispatch_failover`,`attempts` 仍是 1、無 supersede。
- **下載用作用中帳號的身分**(F-1):把舊 notebook 的共享移除到只剩 owner,下載仍成功。
- **自動分享**(F-2)在 pool=5 下也成立(`shared_with` 回 4 個帳號)。
- **permission denied → `not_accepted`**,訊息指名要分享;不再往下 rotate。
- 走錯路兩條都安全:對 `not_accepted` 跑 reconcile 是純本機拒絕、manifest 一個位元沒動。

### 仍待確認(不在這一版)

F-3(finalize 失敗原因不進 `errors[]`)、F-5(暫時性失敗的 `remote.error` 只有一句
`failed`)。兩者都是既有行為,影響的是出事後的可查性,不是成功路徑。

## v0.8.1

v0.8.0 的真實驗收(stg 三個免費帳號,三個帳號全部打爆)抓到的三個 P0 + 一個設計缺口。
**核心命題成立**:EP04 撞到 A 的配額 → 1.43 秒同步拒絕 → 換到 B → 4.4 秒後受理。
notebook 的 owner 是 A,以 EDITOR 身分的 B 送出就過了 ⇒ **配額算發起者不算 owner**,
ADR-0010 標成「操作者拍板但未實測」的前提現在是事實,這一版不作廢。
兩次 failover(A→B、B→C)都拿到,per-process 輪替不回頭撞舊帳號也實測成立。

### 修正

- **下載身分綁死在 pool 最後一個槽位**(F-1,P0)。pool 建構把 `NOTEBOOKLM_AUTH_JSON`
  當暫存槽,迴圈結束後 env 停在最後一個憑證,而還原寫在 lifespan 最外層的 `finally`
  —— 那是 **server 關閉**才跑。而 notebooklm-py 的媒體下載**在下載當下重讀那個 env**,
  不是用 client 自己的 session(驗收已隔離重現:同一個 client 只要換掉 env,下載身分
  就跟著換)。結果:不管作用中的是哪個帳號,**下載永遠以最後一個槽位的身分發出**,
  notebook 沒分享給它就一律 401,而症狀出現在十幾分鐘後的 finalize。
  修法:憑證跟 client 一起存進 pool,`set_clients()`/`rotate_client()` 同步 env。
  只「建完還原成第一個」不夠 —— failover 換到 B 之後 artifact 屬於 B。
  **測試盲區一併修**:原測試在 `async with` **退出後**才斷言 env,那時最外層 finally
  已經還原過,bug 正是從這個縫溜過去的。
- **重送/supersede 路徑沒 failover 也沒記帳號**(F-4,P0)。`podcast_series` 有兩條
  dispatch 路徑,v0.8.0 只補了「全新一集」那條。於是配額耗盡後隔天原樣重呼
  (**工具自己給的 `safe_next_action`**)不會換帳號,pool 對「重試」這條最需要它的路
  完全無效;`dispatch.account` 落地是 null,那一集永久答不出誰生的。
  修法:兩條路徑共用 `_dispatch_audio_with_failover`,只把例外翻成 series 的結構化
  安全停點。`_reset_attempt_for_resend` 也 pop 掉 `account`(留著會變成**過期值**,
  比缺漏危險)。續跑指引從 helper 拉回呼叫端 —— `_mark_not_accepted` 會把 `str(exc)`
  寫進 `remote.error`,寫死一種指引等於讓**錯的**工具名落進 manifest。
- **換到的帳號沒權限被誤標成 `acceptance_unknown`**(F-2,P0)。permission denied
  (`ClientError` rpc_code=7)掉進泛用 except → 「先對帳、禁止直接重生」,把一個
  **確定沒發出去**的請求叫去跑註定撈不到東西的 reconcile,正是 v0.7.1 那類死鎖的形狀。
  新增 `NotebookAccessDenied`,**刻意繼承 `RuntimeError`** —— 兩個 dispatch 呼叫端
  本來就把它當「沒建出 task 的乾淨終態」,分類自動正確,不必兩處各加分支。
  **不放進 `_REFUSED_WITHOUT_DISPATCH`**(那個集合的契約是配額/限流),**也不 rotate**
  (權限是設定問題,逐一試過去只會掩蓋根因)。

### 新增

- **`notebook_create` 在 pool 模式自動分享給其餘帳號**(EDITOR,`notify=False`)。
  failover 的前置狀態先前沒有任何機制建立 —— 驗收時是人工用 SDK 補上才走得動。
  回傳值新增 `shared_with`。**單帳號時完全不打 RPC**,現行機器行為不變。
  既有的 notebook(v0.8.1 之前建的、或手動建的)仍需自行分享一次。

### 待確認(不在這一版)

- finalize 階段的失敗原因沒有落進 `errors[]`(F-3):`download` 失敗時 manifest 只記
  `status="failed"`,「為什麼」只存在於工具回傳的例外訊息裡,session 一結束就沒了。
- 暫時性生成失敗的 `remote.error` 只有一句 `failed`(F-5)。配額拒絕那筆是完整的。

兩者都是既有行為、不是 v0.8.0 引入的回歸。

---

## v0.8.0

多帳號配額 pool。Google One 家庭方案下有 5 個付費帳號,但一個 process 只綁一份
`NOTEBOOKLM_AUTH_JSON`,某帳號當日配額用完整條生成線就停住 —— 即使 pool 裡還有 4 個
活的帳號。決策與已實測的前提見
[ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)。

### 新增

- **lifespan 支援 N 個帳號**。Doppler 同一個 config 注入 `NOTEBOOKLM_AUTH_JSON` +
  `_2`/`_3`…,各建一個長駐 client。**`get_client()` 的語意刻意不變**(「當前作用中的
  那一個」)→ 46 個呼叫點、安裝指令、MCP 註冊、client 端一行都不用動,`dev` 這種
  單憑證 config 的行為完全照舊(遷移因此可以逐台進行,漏改的機器照樣能跑)。
  憑證輪流覆寫 env 再 `from_storage()`:SDK 只認不帶後綴的那個 key
  (`_auth/cookies.py`),而 `AuthTokens` 沒有 `from_json`。啟動期單線程、建完還原,
  憑證不必落到檔案系統。
- **編號缺號 fail-loud**:`_2` 沒設但 `_4` 有 = Doppler 打錯一個字的形狀。靜默跳過會讓
  那個付費帳號永遠不進 pool,而症狀只是「配額比預期早用完」,幾乎查不回根因。
- **配額被拒就換帳號,原地重送同一個 attempt**。實測配額耗盡是**同步拒絕**
  (1.35 秒、`dispatch.status=not_accepted`、`remote.artifact_id=null`),落在
  `_REFUSED_WITHOUT_DISPATCH` 這條「契約保證沒建出 task」的路上 → 重送是冪等的:
  **同一個 `attempt_id`,不 supersede、不新建 attempt、不多燒一次配額紀錄**。
  兩種形狀(0.8.0 起 raise、0.7.x 回 `task_id=""` 由 `ensure_started` 判定)收進
  同一個 `_dispatch_audio_with_failover`,分兩處各補一次正是本 repo 反覆出事的「補一半」。
- **`dispatch.account`**(單帳號也記)+ **`errors[]` 的 `dispatch_failover`**
  (from/to/理由)。對 client 透明可以,對稽核紀錄不行 —— 沒有它「EP35 是誰生的」
  事後答不出來。**選 `errors[]` 不是隨便選的**:`_reset_attempt_for_resend` 會把
  dispatch/remote 清回 prepared,診斷放那裡會被抹掉。

### 邊界(刻意不做)

- **已 dispatch 之後的失敗不換帳號**。`status="removed"` 看起來像配額問題,但那時
  task 已存在,換帳號等於 retract + 取代版 —— 讓 MCP 在背後改寫 manifest 的因果紀錄,
  正是 ADR-0009 禁止的事。維持既有的結構化安全停點,`podcast_series` 那圈因此**一行未改**
  (它的判準是 `dispatch.status != "not_accepted"`,failover 對它完全透明)。
- **認證失效不 failover,大聲停住**。靜默吸收過期憑證的 pool 會一路吸到沒帳號可用。
- **`store is None`(standalone 無 manifest)不換帳號**:沒有地方寫稽核紀錄。
- **輪替是 per-process 持續,不是 per-attempt 重設**:今天耗盡的帳號今天就是耗盡了,
  每集都先撞一次等於每集多燒一趟 RPC、多一筆假的 failover 紀錄。

### 尚未驗證

**真實的 failover 一次都沒跑過** —— rotate 只在 mock client 上測過。`stg` 的三個
免費帳號就是為此存在(免費配額低,打得爆才測得到)。它同時會驗證「配額算**發起者**
不算 notebook owner」這個**操作者拍板但未實測**的假設 —— 若配額算 owner,換呼叫端
帳號毫無作用,這一版的功能全部作廢。

---

## v0.7.2

真實驗收(v0.7.1,測試帳號)抓到的三個 bug。**觸發條件都是「配額拒絕」**,而那正是
v0.7.0 換過契約的那條路徑 —— 離線測試證明得了分類,證明不了整個情境走得通。

### 修正

- **`podcast_episode` 的 attempt 撞到配額拒絕後不再死鎖**(驗收 F-8,中到高)。
  QA 拒收重生(`podcast_episode` + `source_ids`)剛好撞到配額時,該集**五條出路全被擋死**:
  重呼被 durable-attempt guard 擋、`reconcile` 拒收 `not_accepted`、`resume` 必填的
  `artifact_id` 是 null、`podcast_series` 因 settings 不符永久迴圈、`retract` 只認已
  promote 的 output。唯一脫出是手改 manifest —— ADR-0009 明令禁止。
  根因是 `supersedes_attempt_id` 只有 `podcast_series` 會傳,所以
  `tools_podcast.py` 的 guard 必定先 raise,底下的 `failed`/`removed` 白名單**從公開
  介面根本到不了**;而 `retract` 的 `abandons_unauthorized_candidate` 又要求該集已有
  promoted output,retract 流程剛好把它清掉了。
  **修法**:`_is_resendable_same_request` —— 從未 dispatch 的 attempt,只要這次請求
  (notebook / 標題 / brief 雜湊 / settings / frozen bundle)**逐字相同**就沿用它重送。
  選「讓建立它的那支工具自己重送」而不是新增作廢工具:attempt 是 `podcast_episode`
  建的就該由它推進,呼叫端的動作是**原樣重跑同一個呼叫**,與 series 的續跑語意一致。
  同 pattern 已存在於 `_reuse_frozen_input_attempt`。
- **失敗的 `podcast_series` 呼叫不再抹掉拒絕證據**(驗收 F-7,中)。
  `_rearm_not_accepted_attempt` 跑在 settings 驗證**之前**,於是一個註定失敗的呼叫
  仍會先把 `remote.error` / `error_code` / `dispatched_at` 清成 null。抹完的 manifest
  長得正好像「分類從未生效過」。改成**驗證先於變更**(`_assert_series_owns_attempt`
  提前到任何 mutation 之前)。
- **拒絕分支不再裸拋**(驗收 F-9,低)。`podcast_episode` 的 `not_accepted` 分支現在
  比照 8 行外的 `acceptance_unknown` 姊妹分支,把 `attempt_id` 與確切的續跑指令寫進
  例外訊息。docstring 承諾「依錯誤中的 `attempt_id` 續跑」,先前只有一個分支兌現。
- **`podcast_series` 接不了的 attempt 會指名誰接得了**(驗收 O-6)。訊息從「settings
  changed」改成明講「這個 attempt 帶 per-episode `source_ids`,請用 `podcast_episode`
  原樣重呼」。舊訊息把人往死路指。

### 內部

- 新增 `_reset_attempt_for_resend`:`_rearm_not_accepted_attempt` 與同請求沿用分支共用。
  **dispatch 與 remote 必須一起清** —— 只清一半會讓 series 看到 `remote.status="failed"`
  而誤判該 supersede。診斷靠 `errors[]`(只 append)保存,不靠 `remote`。

---

## v0.7.1

- **認證失效訊息不再指向壞掉的登入指令**。`probe_auth` 的 `RELOGIN_HINT` 原本叫人跑
  `notebooklm login`,但 Google 已把登入流程搬到 `notebook.google.com`,SDK(含 0.8.0)
  偵測還沒跟上,照著跑會卡滿 5 分鐘。**這段訊息出現的時機正是使用者最會照著做的時候**,
  指錯等於把三十秒的修復變成五分鐘的困惑,而且會把人誤導到「NotebookLM 搬家了」。
  改指向 `scripts/login_notebooklm.py`,並有逐行 tripwire 鎖著。

---

## v0.7.0 — 依賴升到 notebooklm-py 0.8.0(**breaking ×2**)

完整推導見 [升級筆記](docs/notebooklm-py-0.8-upgrade.md)。

### Breaking

- **server 命令 `notebooklm-mcp` → `nblm-mcp`**。`notebooklm-py` 0.8.0 自己也宣告了一支
  同名 console script,同一個 uv tool venv 只留最後寫入的那份 —— 實測**全新安裝 3/3 拿到
  上游那支**(缺 `fastmcp` 直接 ModuleNotFoundError),等於裝完就是壞的。
  消費端必須改呼叫名並重下 `claude mcp add-json` 註冊。
  **這件事讀 changelog 讀不出來、`uv tool list` 也看不出來,只有真的跑一次 `--help` 才會發現。**
- **依賴 `notebooklm-py>=0.8,<0.9`**(ADR-0019 錯誤契約:缺席與拒絕改 raise)。

### 行為修正

- 生成 kickoff 的配額/限流拒絕在 0.8.0 改成 raise,attempt 終態不再被誤標成
  `acceptance_unknown`;`podcast_series` 也不再把原始例外拋給呼叫端(先前會繞過
  `except RuntimeError` 的安全網,「預期內的停止用回傳值表達」的契約整個破掉)。
- `rename(return_object=False)` 不再短路,新增的 raise 路徑已在 `audio_finalize` 吸收。
- lifespan 在 inline auth 模式額外壓掉 0.8.0 新增的 **L3 headless re-auth**
  (與 keepalive 同一類災難:在本 process 重鑄 cookie、寫不回 Doppler)。
- `Source.created_at` 由 naive 翻回 aware UTC;正規化器兩種都吃,但 **fake 必須跟著
  實裝版本走**,否則重演「測試綠、production 濾光」。

---

## v0.6.0

- **`published_at` 是「首發時間」不是「產製時間」**。retract 必須把它 pop 進
  `retraction.retracted_output`,promote 補回時走 `_first_published_at()` 沿 attempts
  建立順序找第一筆非空值。舊行為用 `setdefault` 補成重生當下的 wall clock,GUID 不變
  但 pubDate 漂 → episodic feed 按 pubDate 倒序,重生集跳到最前(saa-drill EP05/EP09 實際事故)。
  **改 code 不回溯既有 manifest**,用 `scripts/backfill_published_at.py`。
- 真實驗收再抓到**同一個 bug 只修一半**:`manifest` 與「回傳給呼叫端的那份 dict」是兩個
  出口,第一版只蓋掉前者(實測 manifest 12:07:20、回傳值 12:16:39)。修在
  `_promote_attempt_output` 這個匯流點,四條路徑一起正確。**測試要同時斷言兩者** ——
  只驗 manifest 正是它溜過去的原因。
- 冪等、資料完整性與前置驗證的一輪硬化;發布路徑的內容防護與原子性。

---

## v0.5.0 及更早

見 git log。`source_ids`(v0.5.0)、發布路徑硬化、generation-input bundle 等。
