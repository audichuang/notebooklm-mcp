# 附件 failover 用 episode 欄位買到稽核,不是用 attempt

ADR-0010 讓配額 failover 掛在「零副作用拒絕」上,但它只接了音檔。`generate_slides` /
`generate_report` / `artifact_revise_slide` 一直走 `runtime.get_client()`(此刻游標指到的
那一個),撞 `RateLimitError` 直接拋。**這不是遺漏,是 v0.9.3 驗收 FINDING-4 明確決定
不修的一條**(v0.9.5 CHANGELOG「沒改的一條」):當時的判斷是「真要修不能只包一層 rotate
—— 得先有 durable dispatch receipt 才能滿足 ADR-0010『記帳與送出同源』,否則 response 一
遺失稽核也跟著消失;完整修法是把低階救援升成 manifest-backed 能力」。

那個判斷擋住的是**低階 `tools_basic.generate_audio`** —— 它連 `manifest_path` 參數都沒有,
真的無處可寫。**但附件三支從一開始就吃 `manifest_path` + `episode_n`**,而這一版重新檢查
之後結論是:ADR-0010 §Transparency 要的不是「attempt 結構」,是兩個問題答得出來 ——
「這集簡報是哪個帳號生的」與「哪個帳號被拒過」。兩者都寫得進 episode 記錄,不需要 attempt。

所以這一版接的是 failover,**不是** durable attempt。

## 決定

三支附件生成工具與音檔**共用同一個 failover 迴圈**(`_failover.dispatch_with_failover`),
稽核面換成 episode 級的兩樣東西:

- `slides_account` / `report_account` + `slides_artifact_id` / `report_artifact_id` ——
  「**現在磁碟上這份成品**是哪個帳號、哪一顆 artifact 生的」。**寫入時機 = 成品真的落地時,
  而且與 `slides_pdf_path` 同一次 `update`。** 救援下載(`artifact_download_slides`)寫
  `account: None` —— 它知道成品是哪一顆,但不知道誰生的,寫一個明確的「不知道」比留著上一次
  的帳號好(那個值看起來是權威的,而它講的是**別的**成品)。
- `attachment_errors[]` —— append-only,**三種 phase**:`attachment_dispatch_failover`
  (換帳號,帶 `from_account` / `to_account`)、`attachment_dispatch_refused`(最後一腿
  被拒,帶 `account`)、`attachment_acceptance_unknown`(受理不明,帶 `account`)。
  三種都寫才答得出「哪個帳號被拒過」——第一版只寫換帳號那一種,於是帳號耗盡時**最後
  一個**被拒的帳號一筆都不留,單帳號 pool 則是完全沒有紀錄。**而且只有真的碰過遠端才准寫**:
  `language` / enum 這類純本地轉換一律擋在 dispatch closure 外面,否則它們拋的 `ValueError`
  會落進共用迴圈的泛用 except、被記成一筆憑空的「遠端受理不明」,而這張表清不掉。

### 試過又退掉的:受理憑據 + 並行 fence

中途實作過「受理成功之後、開始等生成之前就先落 account + artifact_id」的憑據,想擋
「等待途中被砍 → 重跑多燒一顆 artifact」;並用它當並行 fence(下載前檢查憑據還是不是自己
那一顆)。**兩者連同配套測試一起退掉了**,理由是獨立複審(第二輪)實測重現的三件事:

1. 那組欄位的語意是「**現在磁碟上這份**是誰生的」。受理時就寫 = 把「在飛的」與「已交付的」
   塞進同一組欄位 → 舊成品 X 還在磁碟上、重生 Y 受理後 `wait` 回 failed 時,檔案是
   `%PDF-OLD` 而 manifest 已經聲稱 `slides_artifact_id=Y` / `slides_account=B`。
2. fence 自己是 TOCTOU:檢查通過後在下載的 `await` 裡被接手,最終「檔案 X、manifest Y」
   而**兩個工具呼叫都回成功** —— 那正是 fence 的 docstring 聲稱它換掉的東西
   (「把安靜地服務錯的 PDF 換成大聲失敗」)。加了它比沒加更糟:它讓人以為競態被處理了。
3. 救援下載同形:先讀 provenance、`await` 下載、再盲寫回去,中間被更新的生成接手就整組倒退。

**教訓不是「憑據沒用」,是「憑據不能借用 provenance 的欄位」。** 真要做,受理憑據必須是
獨立的 pending 欄位,而提交必須是條件式的 —— 把 `download_atomically` 拆成「抓到 temp +
驗證」與「持 manifest 鎖做 artifact-id CAS 後才 replace」兩段。那是下面延後的 attachment
attempt 的第一步,不是一個檢查擋得住的。

### 誠實留著的兩件事(不假裝修好了)

- `download_atomically` 的 `os.replace` 與 manifest 的 `update` 是**兩個資源**:兩者之間
  crash 或 directory fsync 失敗會留下「新檔配舊 provenance」。要跨資源原子得有 journal。
  (原文曾聲稱「同一次原子回寫,所以『路徑存在但不知道誰生的』不會出現」——**那是假的**,
  已改掉。)
- 同一集、同一 kind 的並行仍是 last-writer-wins(兩個並行的 `generate_slides(episode_n=1)`
  之中慢的那個最後落地)。這在這一版之前就是這樣,這一版沒有放大它,也沒有修掉它。

**append-only 是承重的,不是風格。** 重生會覆寫 `slides_pdf_path` 與 `slides_account`;
把診斷放在會被覆寫的欄位裡等於「下一次成功就把『這個帳號今天被拒過』抹掉」,而那正是唯一
能看出「pool 裡有個 cookie 快死了」的訊號 —— ADR-0010 講得很直接:一個靜默吸收過期憑證的
pool 會一路吸到沒帳號可用。音檔的 `attempt["errors"]` 也是同一個理由才不隨 reset 清掉。

## 明確**不**做的:durable attachment attempt

slides/report 家族的重入架構債留著:外層在 mutation 成功之後、回寫 manifest 之前斷線,
重跑會再 mutate 一次(`generate_slides` 與 `artifact_revise_slide` 逐字同形)。修它要做成
涵蓋 generate 與 revise 的 attachment attempt,那是 ADR-0009 那一整套不變式的第二份實例。
(`slides_artifact_id` 這一版有了,但它是 **provenance** —— 「磁碟上這份是哪一顆生的」——
不是 attempt 需要的 in-flight claim,兩者不能用同一個欄位,理由見上面退掉憑據那段。)

**這一版刻意不做,而且不做的理由與「接不接 failover」無關** —— failover 的正確性只需要
「伺服器拒絕時什麼都沒建出來」這個前提(ADR-0010 已量測),不需要 attempt;重入的正確性
需要 attempt,而它在**沒有** failover 的時候就已經壞了。每次呼叫仍然**最多只建出一顆**
artifact(被拒的那幾腿什麼都沒建),所以 failover 沒有放大重複 artifact 的風險。兩件事
綁在一起做只會讓一個本來就成立的修正等一個大重構。留這一支就是為了不讓「附件家族缺
attempt」再次隱形(research namespace 曾因為砍掉 scope 沒留 ADR 而隱形 38 集)。

> **複審對這一點的反駁,以及最後的結論。** 第一輪主張重入債不是旁支:既然這一版拿
> 「可稽核」當作允許換帳號的前提,那麼「受理之後沒有任何 receipt」就讓那個前提不成立。
> **駁回。** ADR-0010 授權 rotate 的前提是「**被拒的**那一腿沒建出 task」——那是 refusal
> 的性質,與事後記不記得受理無關;而重複 artifact 的視窗(受理後、回寫前)寬度在這一版
> 前後完全相同,failover 沒有放大它。
>
> 不過當時採納了它的次要論點(「兩個稽核問題在 crash 面前答不出來」)並加了受理憑據 ——
> **然後第二輪證明那個憑據本身製造了更糟的錯配,又退掉了**(見上面「試過又退掉的」)。
> 所以最終的承諾寫成它實際的強度:**provenance 只保證「manifest 說的那一顆,就是最後一次
> 成功落地的那一顆」**;成品落地與 manifest 寫入之間 crash 的那一瞬答不出來,而受理成功
> 但生成失敗時 manifest 完全不動(這是對的 —— 磁碟上還是舊那份)。這條路上沒有
> crash-durable 的保證,要有就得做 attempt。

## 為什麼是「抽出共用迴圈」而不是在附件端再寫一份

那個迴圈的每個分支都是一次真實事故的疤:集合刻意不長大、「有 id + failed」不准 rotate、
權限不 rotate、終止性不靠冷卻時鐘、`tried` 要擋在稽核寫入之前。各寫一份等於下一次修正
只有一處被改到 —— 正是本 repo 反覆出事的「補一半」,而且這一條有前例:v0.8.0 的 failover
只補了 `_run_episode`、漏了 `podcast_series` 的重送分支,於是 pool 對「重試」這條最需要它
的路完全無效(驗收 F-4)。

所以 `_failover.py` 現在是那五條紅線的**唯一**產地,家族差異只用 callback 表達:

- `record_failover` —— 稽核往哪寫。傳 `None` 就一律不換帳號(沒有稽核面時的退化行為,
  低階 `generate_audio` 與 standalone 沒 manifest 的路徑都走這條)。
- `on_clean_refusal` / `on_acceptance_unknown` —— 終態處理,兩者都拿到**這一腿實際用的
  帳號**。音檔用它們標 attempt 的 `not_accepted` / `acceptance_unknown`(讓呼叫端知道
  要不要先對帳);附件用它們補上 `attachment_errors` 的最後一筆 —— 附件沒有狀態可標,
  但**紀錄還是要留**:`record_failover` 只在找得到下一個帳號時才寫,所以耗盡時最後那一腿
  (單帳號 pool 則是唯一那一腿)完全不會進紀錄。第一版兩個都傳 `None`,理由寫成「附件沒有
  attempt,沒有東西要對帳」—— **「沒有狀態要標」被誤當成「沒有紀錄要留」**,而那兩件事
  不一樣。獨立複審 Review B #5 抓的就是這個。

同一輪把 `access_denied_error` 也收成一支:那段「補分享」的指引文字原本有兩份(一份在
`_errors.raise_if_access_denied` 指名 notebook、一份在音檔 failover 迴圈裡指名帳號),
內容一樣連結尾都差一句。合併後兩個都指名得出來。

## 真實驗收:v0.9.16 已跑,主要未知已結案(2026-08-17,stg)

原文寫的是「還沒跑,而且有一條前提推導不出來、只能量」。**量過了**,完整紀錄在
[acceptance-v0.9.16-findings.md](../acceptance-v0.9.16-findings.md)。

> ⭐ **「零副作用拒絕」在 slides 上成立。** 同步 `raise RateLimitError`(**不是**回
> `task_id=""` 的 status)、**0.63s / 0.75s**(SDK 自報 `RPC CREATE_ARTIFACT failed after
> 0.677s`)—— 與 ADR-0010 在 audio 上量到的 1.35 / 1.43s 同一量級;而最關鍵的那一項,
> **`artifact_list(kind="slide_deck")` 的拒絕前後差集三次量測全部為空**。
> 所以 failover 掛在 `REFUSED_WITHOUT_DISPATCH` 這條分支上是對的,重送是冪等的。

同一輪一併得證(都不必再重測):耗盡終態原樣拋 + `refused` 記最後一腿 + 零假 failover 紀錄;
權限被拒走乾淨終態、pool 尚有候選仍不 rotate、指引訊息落地;兩個稽核面在**真實並行**下
零交叉污染;身分跟著 client 走到 finalize(兩腿 failover 後 16.4MB / 17 頁 PDF 下載成功,
沒有 v0.9.0 那個「十幾分鐘後爆 401」)。

### 但風險模型錯了一處:`REVISE_SLIDE` 是**另一個配額桶**

本 ADR 與 `tools_artifacts` 的註解都把 revise 當「generate 家族的第三支,也會被同步拒絕」。
**前半句對(它確實建 artifact),後半句量不到**:在 slide_deck 生成配額**已完全耗盡**的槽位上
連送 **9 次** `revise_slide`,**9 次全部受理**且 fork 都真的 `completed`。根因是兩支走不同
RPC method(`REVISE_SLIDE` vs 生成的 `CREATE_ARTIFACT`),伺服器按 RPC 分桶。

**所以 revise 那條 failover 分支,對「限流」沒有已知觸發條件** —— 它仍然可能由
`REFUSED_WITHOUT_DISPATCH` 的另一個成員 `ArtifactFeatureUnavailableError`(SDK 解不出
artifact id 時自己拋)觸發,那條沒被排除。**Q3(被拒時會不會 fork)維持「待確認」**:
9 次沒打到不等於打不到,更不等於「被拒也不會 fork」。不要因為這一輪沒出事就把 revise 的
重送當成已證明冪等。

### 三個 phase 只驗到兩個

`attachment_acceptance_unknown` 這一輪**完全沒有自然觸發**(它要「非
`REFUSED_WITHOUT_DISPATCH` 的例外」或「有 id + failed」)。不值得為它燒真配額,但這代表
**那條分支只有離線測試背書**,所以它的測試要一直維持突變驗證。

## 這一輪的審查分工(三輪,值得完整記下來)

離線實作 → **codex 獨立複審 ×2(`--effort max`)** → 主迴圈裁決。

**第一輪**(問「行為有沒有漂移」+「成品裡有什麼沒被測試守住」,沒有先餵我方 findings):
回 1 LOW + 3 HIGH + 2 MEDIUM。裁決:LOW 與 F3(假 provenance)、F4(幽靈稽核紀錄)、
F5(最後一腿沒紀錄)全採納;2 條 HIGH(重複 artifact、並行覆寫)駁回「是本輪引入」的定性
—— 每次呼叫仍最多只建出一顆 artifact,視窗寬度前後相同 —— 但採納它們指出的稽核承諾過強。

**第二輪**(**只看成品、明確不給第一輪 findings**,問「這裡面有什麼是新的、而且沒被任何
測試守住的」):回 3 HIGH + 2 MEDIUM,**其中三條長在第一輪修正新加的東西上**。這正是
AGENTS.md 記著的那個病灶:v0.9.8 派了三個視角、每條修正都做過突變驗證、全套綠,發版後
外部 review 仍抓到四條,兩條長在那一輪新加的東西上。根因是 prompt —— 審查者拿到的是
「這幾條修正對不對」,沒有人對成品重新問一次。**這一輪特意分開問,而它立刻兌現。**

**兩邊獨立收斂**也值得記:主迴圈自己那份 findings(寫在讀第一輪結果之前)點到 F4、F5、
F3 的同一區、以及「report / revise_slide 的下載身分沒測試」。複審多抓到的是**主迴圈聲稱過
的事實其實錯了** ——「音檔訊息逐字不變」實際少一個空格還刪掉結尾,而當時沒有任何測試看得
出來(`test_permission_denied_message_is_stable_and_names_both` 現在釘住)。

順帶修掉一條主迴圈自己發現、與附件無關的既有缺陷:`_mark_acceptance_unknown` 收到
**status 物件**時把稽核寫成 `type: "GenerationStatus"` / `message: "<... object at 0x7f...>"`,
真正的原因整條蒸發(實測)。改用同一支 `describe_refusal`。

### 事後的一次結構整理:做了,複審之後退掉

驗收之後跑了一輪四角度的品質複審(`/simplify`),它提了幾項結構整理,其中最大的一項是
**把 `dispatch_with_failover` 的三個各自可為 `None` 的 callback 收成單一
`audit(phase, reason, **fields)`**,理由是「三個之中只有一個在把關准不准 rotate,所以
『rotation 稽核接了、終態沒接』是簽名允許的合法狀態,而第一版正好落在那裡」。

**做完之後由 codex 獨立複審,而它把那個理由推翻了**:收成一個參數並沒有讓半接線變不可能
—— 傳一個「只處理 `PHASE_FAILOVER`、其他 phase 直接 return」的合法 callable,同樣會在
帳號耗盡時漏掉終態紀錄(它用 probe 證明 dispatcher 接受這種 callback)。理由不成立,
那項就只剩「比較整齊」,而它動的是有 v0.9.0 / v0.9.7 / v0.9.8 三輪事故史的核心迴圈、
**在真實驗收已經對舊形狀跑完之後**。連同它依附的兩項(post-commit 容忍收成一份、
`refusal_fields` 抽成唯一的上游欄位讀取點 —— 後者的收益也不成立,`_status.py` 仍直接讀
那兩個欄位)一起退掉。

**留下的教訓寫在這裡而不只是 commit 訊息裡**:`_failover` 的三個 callback 之所以危險,
不是因為它們是三個參數,而是因為**沒有任何機制保證三個 phase 都被處理**。要真的關掉那個
形狀,得驗證 callback 的完整性(例如註冊三個具名 handler、缺一個就啟動時拋),不是把三個
參數併成一個。**不要再重做參數合併** —— 那條路走過了,換不到那個保證。

Status: implemented, offline-verified,**真實驗收已跑**(2026-08-17,stg —— 見上方
「真實驗收」一節與 [findings](../acceptance-v0.9.16-findings.md))。
