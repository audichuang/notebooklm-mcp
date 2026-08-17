# Gotchas — 發布、附件與封面

動 `publish_series` / `generate_slides` / `generate_report` / `notebooklm-cover` 之前讀。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

- **(v0.9.16)附件三支生成有配額 failover 了,而稽核面是 episode 級、不是 attempt。**
  `generate_slides` / `generate_report` / `artifact_revise_slide` 現在與音檔共用
  `_failover.dispatch_with_failover`:配額被拒就換 pool 下一個帳號原地重送,全部被拒才原樣拋。
  **改這一塊之前先讀 [ADR-0011](adr/0011-attachment-failover-buys-audit-with-an-episode-field-not-an-attempt.md)。**
  三件事動之前要知道:
  ① **provenance 只在成品落地時寫,而且與路徑同一次 `update`。** `slides_account` +
    `slides_artifact_id`(report 同形)講的是「**現在磁碟上這份**是誰、哪一顆生的」。
    救援下載(`artifact_download_slides`)寫 `account: None` —— 它真的不知道。
    ⚠️ **不要「優化」成受理時就先落憑據** —— 那個版本寫過又退掉了:失敗的生成會留下
    「檔案舊、manifest 新」的錯配(實測),而配套的並行 fence 自己是 TOCTOU、在下載的
    `await` 裡被接手時**兩個呼叫都回成功**,比沒有 fence 更糟(它讓人以為競態處理過了)。
    推導與正確做法(獨立的 pending 欄位 + 提交時 CAS)寫在 ADR-0011。
  ② **`attachment_errors` 只 append,永不清除,而且三種 phase 都要寫**
    (`attachment_dispatch_failover` / `..._refused` / `..._acceptance_unknown`)。
    只寫換帳號那一種的話,**帳號耗盡時最後一腿一筆都不留**(單帳號 pool 則完全沒紀錄)
    ——「沒有狀態要標」不等於「沒有紀錄要留」。
  ③ **只有真的碰過遠端才准寫這張表。** `resolve_language` / `to_slide_format` 這類純本地
    轉換一律擋在 dispatch closure **外面** —— 放進去的話它們拋的 `ValueError` 會落進共用
    迴圈的泛用 except,被記成一筆「遠端受理不明」(實測 SDK 呼叫次數 0),而 append-only
    的表清不掉,事後查配額的人會被帶去查一個從未發生的遠端狀態。
  ④ **同一集同一 kind 的並行是 last-writer-wins,沒有修。** 輸出檔名固定、manifest 回寫是
    blind update,慢的那個最後落地會蓋掉快的。這在 v0.9.16 之前就是這樣,沒有被放大也沒被
    修掉。要關掉得把 `download_atomically` 拆成「抓 temp + 驗證」與「持鎖 CAS 後 replace」。
  ⑤ **生成前的 `assert_sources_exist` / `_require_completed_slide_deck` 用的是起始帳號**,
    failover 換過去之後不重驗。notebook 層級是安全的(換過去看不到就 `NotebookAccessDenied`
    並**停止**輪替);**但 source 層級的論證不完整** —— B 看得到 notebook、而某個 `source_id`
    在 A 驗過之後被刪掉的話,B 的生成會拿到聚焦錯誤的成品而不報錯。要在 dispatch 的幾秒內
    被刪才會撞到,所以留著沒修,但別把它當成已證明安全。
  ⚠️ **`generate_slide_deck` 撞配額的實際形狀還沒量過**(ADR-0010 的 1.35s 同步拒絕是對
    **audio** 量的)。推導在 ADR-0011,真實驗收未跑。
- **發布用的 HTML guard 是標籤/屬性允許清單,不是關鍵字黑名單**(`publish/notes_html.py`):
  黑名單會把「設定 online=1」「JavaScript:動態語言的起點」這種普通中文散文誤殺(誤判成本 =
  整季 publish raise),又漏掉 `<svg><image href>`、`<input type=image>` 等。允許清單走
  stdlib `HTMLParser`,但**要一併擋「被 parser 吞掉的區段」**(comment / decl / CDATA / PI):
  `<![CDATA[ > <img …> ]]>` 在 HTMLParser 眼中是一個 `unknown_decl`,瀏覽器卻當 bogus comment
  在第一個 `>` 結束、`<img>` 變真元素(chrome --dump-dom 驗過)。惰性行內標籤
  (`b`/`i`/`span`/`details`…)刻意放行:report 來自 NotebookLM,夾帶它們不罕見,而屬性另有
  逐一過濾,放行不擴大攻擊面。
- **發布的 preflight 是硬契約,且一定在第一個 PUT 之前**(v0.3.3):`require_slides` /
  `require_report` 預設 True——manifest 沒回寫附件路徑就 raise。理由是三個生成是獨立背景
  呼叫、完成訊號分散,manifest 是唯一匯流點,舊行為「缺路徑靜默不附」讓「還在生成」與
  「使用者不要」無從區分(EP36 發布早於交付完成)。兩個獨立旗標,對齊 skill「能略過的只有
  簡報/研讀講義」。同一輪把附件檔案存在性、**每集 mp3 的 `_ensure_local_mp3` resolve**、
  **講義 HTML 預渲染**全部提前——舊版都在上傳迴圈內,後面某集失敗會讓前面幾集的
  mp3/封面已經落在 NAS 上(違反該迴圈上方註解自己宣告的不變式)。**只驗 `artifact_id` 不夠**:
  resolve 還要 `notebook_id`,且遠端下載本身可能失敗。**這叫 required-deliverable preflight
  gate,不是 await barrier**(分不出「manifest 有舊路徑、新版正在重生」),也**不保證零 orphan
  blob**(網路/ffmpeg/uploader 階段失敗仍會留未引用的 immutable blob,那是 media-first 發布
  的已知代價)。
- **(v0.9.18)`publication_state: "deferred"` 是 host↔`publish_series` 的契約:那一集留在
  manifest 當 audit、但不進 feed。** 這不是假想功能 —— `podcast-lab/output/series_manifest.json`
  的 EP46 就是這個狀態(音檔五次 semantic QA 拒收、全部 attempt 已撤回、`mp3_path: null`、
  沒有 `artifact_id`)。少了這道過濾,**50 集的整季 republish 會在 `tools_publish.py` 的
  `_ensure_local_mp3` resolve 迴圈整批 raise**(`episode 46: mp3_path missing and no
  artifact_id to re-download`)—— 不是只掉那一集,是換封面重發之類的事完全做不了。
  四件動之前要知道的事:
  ⓿ **寫入的唯一正門是 `episode_set_publication_state`**(`tools_artifacts.py`),白名單正本在
    [`publish/state.py`](../notebooklm_mcp/publish/state.py)。**常數不能放回 `tools_publish`**:
    那條路在「先 import `tools_publish`」時炸循環 import(`tools_publish` → `app` → 註冊
    `tools_artifacts` → `tools_publish` 只初始化到一半),實測被 `test_publish_tools.py` 的
    module-level import 抓到。第一版**只做了讀取端**,而 manifest 只由工具寫入的紀律
    不允許手改 JSON —— 於是「解除」在受支持的路徑上是死路(EP46 之後生出可用音檔也解不開)。
    ⚠️ 「沒變更就不寫盤」必須在 mutator 內用 `_NoChange` 中止(`ManifestStore.update` 的
    revision 無條件 +1,無效寫盤會撞掉別人的 CAS);**不能先 `read()` 再決定要不要 `update()`**
    —— 兩次呼叫之間別的 writer 插進來,判斷就過期了。
  ① **只管發布層,不代表禁止重生。** `podcast_series`、attempt/artifact/cleanup 掃描
    (`_claimed_artifact_ids`、`_unresolved_attempt_ids`、`_settle_cleanup_state`)**刻意不看
    這個欄位** —— deferred 的理由正是「待有受支持的生成入口後再修復」,拿它擋重生會把
    暫緩變成永久除名;而 attempt 級掃描要看全 manifest 才算得出正確的清理義務與 artifact 歸屬。
  ② **判準是「欄位在不在」,而且值只認明列的那幾個 —— 其餘一律 raise。** 拼錯(`defered`)、
    空字串、**顯式 `null`**、未來新增的狀態,任何一個靜默公開都是在這個欄位該生效的時候
    失效,而 **feed host 永不刪檔,送出去的 mp3 收不回來**。所以「缺席」才是照發,
    `.get() is None` 不行(`null` 與缺席同形,而 `null` 的意圖無從得知);unhashable 值
    (list/dict)也要先驗型別,不然 `in frozenset` 會漏 `TypeError` 出去。
    要加狀態就改 `publish/state.py` 的 `WITHHELD_PUBLICATION_STATES`,並同步這一條。
  ③ **deferred 集整集跳過 preflight,不是只跳過 mp3 那一項。** 理由是它不進 feed,驗它的
    檔案沒有意義。⚠️ **別把「剛好缺哪個檔」寫進過濾條件** —— 生產上的 EP46 其實
    description / cover / 簡報 / 講義**全部齊全且檔案存在**,只缺 `mp3_path` 與 `artifact_id`;
    順手多看一個欄位(`… and not ep.get("cover_path")`)對極簡 fixture 全綠,對真的 EP46
    卻會再次擋掉整季。兩種形狀各有一條測試(`_deferred_ep_production_shape` 那條就是為此存在)。
  ④ **被扣下的集號一定要回報**(`deferred_episodes`):50 集的 manifest 回 `episode_count=49`
    卻不說是哪一集不見了,讀起來就是「發布漏集」。全季都被扣下時 raise,不發空 feed
    (那會把既有 show.json 的集數整批清掉)。
- **附加簡報/講義**:`generate_slides`/`generate_report` 只吃**傳入的 `source_ids`**才聚焦原文;
  不傳則 SDK 用全部來源(v1 不自動排除音檔來源)。附件缺檔時 `publish_series` **fail-fast**。
  **順序鐵律**:uploader 白名單放寬 `.pdf`/`.html` 後**要先重部署 NAS**,再跑帶附件的發布,否則附件 PUT 404。
  講義是 Markdown(`download_report`),`notes_html` 渲染成 HTML 才 host;`.md` 只留本機。
- **未暴露的 artifact 型別是產品決策**:video / cinematic_video / infographic / quiz /
  flashcards / data_table / mind_map 的 `generate_*` 都**刻意不做**成 MCP tool(不出 YouTube 版;
  封面走 `notebooklm-cover` 的 HTML+Chrome 決定性管線,不能換成 infographic——發布端拿封面
  bytes 做 content-hash)。`artifact_list(kind=…)` 仍可列出它們,那只是讀取面。
- **封面圖 = 凍結的 HTML template + headless Chrome 光柵化**(`notebooklm-cover`,實作
  `cover_cli.py`)。設計固化在 `notebooklm_mcp/assets/cover_episode.html` / `cover_show.html`
  (由 **agy/Gemini 設計、產出自包含 HTML**,已 commit 進 repo);**生封面時不叫 agy**——工具只做
  「填佔位符 `__SHOW__`/`__EPNUM__`/`__TITLE__`/`__BYLINE__`/`__HUE__` → Chrome 截 3000² → RGB JPEG
  → `validate_artwork`」,離線、決定性、無 LLM。要換整體設計才再叫一次 agy 重生 template(手動、很少)。
  agy 這類 coding agent **仍不能直接吐點陣圖**,但**擅長出 HTML/CSS**,交給 Chrome 光柵化質感高一截、
  改版只改 template。**需系統有 headless Chrome**(`google-chrome`/`chromium`;`--chrome` 或
  `NOTEBOOKLM_COVER_CHROME` 指定)——只有「產封面的那台」需要,3 個認證 VM 不用。NotebookLM 下載的
  音檔是 **fragmented-MP4 / DASH**(AAC),雖常用 `.mp3` 副檔名卻不是 MP3；`_embed_cover`
  發布前會正向辨識並轉成 256 kbps true MP3，再寫 ID3/APIC(見下方 gotcha)。
- **單集封面**:`notebooklm-cover --manifest <json> --show-name Audicast --byline audichuang [--output-dir <dir>]`
  批次讀 episodes 逐集填 episode template(集號決定色相 `(n*77)%360`、集標當大標、EP 徽章)、
  把絕對 `cover_path` 寫回 manifest,供 `publish_series` 吃(該集 `<item>` 掛 `itunes:image`,
  缺 `cover_path` 直接 fail,不 fallback 節目封面)。**節目封面**:`--show --output assets/cover.jpg
  --show-name Audicast --tagline "…" --byline audichuang`。單集一次性:`--output --episode EP0n --title "…"`。
  **決定性鐵律**:發布端用封面 bytes 做 content-hash,同一集必須永遠生同一張——所以色相只能是集號的
  函式(不可隨機);但 bytes 也吃 **Chrome/CJK 字型版本**,換版本會漂 → 固定在同一台機器產、產出的
  JPEG 即事實來源(可版控;等同舊 PIL 的字型 caveat)。`.jpg`/`.png` uploader 白名單本來就放行,
  **不用重部署 NAS**(不像加 `.pdf`/`.html` 那次)。
- **單集封面「app 讀不到」的真根因 = 音檔沒內嵌圖 + 格式假標**:feed 的 `<item>`
  itunes:image 我方掛得對、URL 也公網可達(實測 200),但 Apple/Spotify 常優先吃音檔內嵌圖。
  NotebookLM raw 檔是 fragmented MP4/AAC、常偽裝成 `.mp3`;若仍以 `audio/mpeg` 發布，副檔名/MIME/
  container/codec 四者矛盾。故 `publish_series` 的 `_embed_cover` seam 先用 ffprobe **正向辨識**:
  MP4/AAC → ffmpeg 轉 256 kbps、44.1 kHz stereo true MP3；既有 true MP3 不重編音訊；其他格式
  fail-closed。最後用 mutagen 寫單一 authoritative front-cover ID3/APIC。這讓公開 enclosure 的
  `.mp3` + `audio/mpeg` 與實際 MP3 完全一致且可 HTTP range seek。**代價**:首次啟用正規化會讓
  MP4 來源各集換一次 content-hash URL(舊 URL 因 uploader 不刪仍可用,訂閱者可能重抓)。
  exact output bytes/hash 只在相同 ffmpeg/libmp3lame 工具鏈穩定；升級會再次 churn enclosure URL，
  故產製環境要固定版本，計畫升級時要預期重驗。測試用假 audio bytes 由 autouse fixture 把
  `_embed_cover` 換成 no-op；真媒體 regression 鎖住 MP4→MP3、
  MP3 保留、ADTS 拒絕、端到端 uploaded bytes/副檔名/RSS MIME 一致與決定性。

