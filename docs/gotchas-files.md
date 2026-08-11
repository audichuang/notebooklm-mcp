# Gotchas — 檔案落地:上傳、原子寫入、fsync

新增任何「寫檔」或「上傳來源」的路徑之前讀 —— 這幾條各自對應一次真實事故,而且都是**補一半就等於沒補**的形狀。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

- **upload endpoint 的副檔名地雷**:`.json`/`.ts`/`.py`/`.yaml` 直接 400 Bad Request(上游只
  提前擋 HTML family:`_source/upload.py` 的 `_HTML_UPLOAD_SUFFIXES`)。v0.3.3 起
  `source_add_file` 自動把讀得開的小 UTF-8 文字檔複製成 `<原檔名>.md` 上傳並回
  `converted_from`(EP36 的 fixture-output.json 事故:每次新檔案副檔名都不同,pitfall 文件
  救不了)。`_NO_AUTO_WRAP_SUFFIXES` **不是** endpoint support allowlist——我們無法從外部
  證明那件事,它的語意只有「這些格式不該用改副檔名來處理」(文件/表格/圖片/媒體會丟掉
  原生語意;HTML 要保留上游的 ValidationError)。**刻意不用 `mimetypes.guess_type()`**:它讀
  `/etc/mime.types`,同一支 `.ts` 在有/無該檔的機器上分類不同,3 VM + podcast-lab 會不決定性。
  另外 **NUL byte 是合法 UTF-8**,`read_text()` 只擋掉無效序列的那一半 → 需要獨立的
  `"\x00" in text` guard。顯式 `mime_type` 一律優先(呼叫端比我們清楚那是什麼)。
  注意 `source_add_file` 只是 caller-facing 通用入口;podcast 流程的已知 mp3 是**直接打
  SDK 的 `sources.add_file`**、不經過它(三個呼叫點:`grep -n 'sources.add_file'
  notebooklm_mcp/`)。
- **任何新的原子寫入一律重用 `_atomic.prepared_replacement`,別自己再寫一份**
  (v0.9.12 起是一個 context manager:`with prepared_replacement(path) as tmp:` 把內容寫進
  `tmp`,離開區塊時原子換上去;`mode=` 顯式指定用於憑證那種不能繼承既有 mode 的檔)。
  這條紅線寫下之後**又被違反兩次** —— `auth_cli`(憑證)與 `cover_cli`(封面)各自手寫
  temp/fsync/replace,兩處都漏了 `os.replace` 之後的 parent-directory fsync、又各自重寫
  了一份 0644 常數。已改成共用,`tests/test_atomic.py` 逐條釘住那幾件事。`generation_input` 的 sidecar
  曾經自帶一個 `_fsync_directory`,結果把 v0.3.3 學過的三件事全漏了:mkstemp 的 0600 被帶到
  最終檔、commit point 之後的 fsync 放在 try 裡(一拋就把剛建立的綁定刪掉)、沒容忍
  `_DIR_FSYNC_UNSUPPORTED`。三件事都有測試鎖著——但鎖在簡報/講義那條路上,新路徑照樣漏。
  `fsync_parent` / `_NEW_FILE_MODE` / `_DIR_FSYNC_UNSUPPORTED` 是 package 內共用的。
  **注意 sidecar 用 `os.link` 而不是 `os.replace`**:它要的是「只在不存在時建立」
  (原本的 `O_EXCL` 語義,一個 bundle 只綁一次),`os.replace` 會靜默蓋掉既有綁定。
- **固定檔名的下載一律原子換檔**(`_atomic.download_atomically`):`ep{n:02d}-slides.pdf` /
  `-report.md` 原本直接寫最終路徑,重生中斷會讓 partial file 頂替上一版完整產物,而 manifest
  仍指向同一路徑、`publish_series` 的「存在且非空」檢查也抓不到。temp → 驗(非空 + PDF
  magic / UTF-8 可讀)→ fsync → `os.replace` → fsync parent,與音檔 finalize 同一 pattern
  (`fsync_parent` 已抽到 `_atomic.py`,兩邊共用)。
- **`os.replace` 之後的 directory fsync 一律移出 try 並容忍 `_DIR_FSYNC_UNSUPPORTED`**:
  replace 是 commit point,dir fsync 只是額外的 crash-durability。留在 try 內會讓 NAS/overlay
  mount(回 EINVAL/ENOTSUP)上「已經寫成功」被回報成整個失敗——呼叫端據此 rollback
  (如 `generation_input` 的 binding),變成「manifest 有新 attempt、binding 卻被刪」。
  `_atomic.download_atomically` 一開始就對,`manifest_store._write` 與 `audio_finalize` 的
  mp3 下載都是後來才補上的**同一個坑**(補一半的又一例)。
