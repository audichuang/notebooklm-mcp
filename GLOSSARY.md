# NotebookLM Podcast Tools

Audicast 使用 MCP 工具提交 NotebookLM 生成、恢復長流程、處理媒體並發布 feed 的工具領域；具體人工工作流程由 skill 或 host 編排。

## Language

**產製可靠性（Production Reliability）**:
MCP 操作可恢復、可追溯、不重複且不誤報狀態；不表示 NotebookLM 永不失敗，也不規定使用者的人工流程。
_Avoid_: 零失敗、強制審批流程

**生成嘗試（Generation Attempt）**:
一次單集生成意圖的持久化生命週期；它在任何 NotebookLM 遠端副作用前誕生，可能尚未受理、受理不明，或綁定一個遠端產物。
_Avoid_: 重試、目前 artifact

**嘗試識別碼（Attempt ID）**:
由 MCP 在任何遠端副作用前建立的生成嘗試身分，並在整個生命週期保持不變。
_Avoid_: Artifact ID、Task ID

**產物識別碼（Artifact ID）**:
NotebookLM 為遠端產物提供、並綁定至一個生成嘗試的身分；它不是生成嘗試主鍵。
_Avoid_: Attempt ID

**受理不明（Acceptance Unknown）**:
無法證明 NotebookLM 已受理或未受理生成請求的結果；在完成對帳前，不得以新生成取代。
_Avoid_: 失敗、離線、未受理

**對帳（Reconciliation）**:
比對已記錄的操作意圖與 NotebookLM 遠端狀態，唯一吻合時補綁，無法唯一判定時回報候選而不猜測。
_Avoid_: 選最新、直接重生

**Feedback Source**:
為後續集數提供 continuity 而上傳到 NotebookLM Sources 的單集音檔；工具保存並驗證其 source identity，讓重跑不會重複上傳。
_Avoid_: 暫存檔、最新同名 source

**Canonical Enclosure**:
完成媒體辨識、必要轉碼、封面與 metadata 內嵌及解碼驗證後的穩定 MP3 bytes，可供發布工具原樣使用。
_Avoid_: NotebookLM 原始下載、偽 MP3

**Enclosure Revision**:
同一生成嘗試下由完整 SHA-256 識別的一份不可變 Canonical Enclosure；任何 bytes 改變都建立新 revision。
_Avoid_: 覆寫 epNN.mp3、改 metadata

**Feed Deployment**:
把一份完整 feed 快照及其公開資產送往託管端並驗證的工具生命週期；它是邏輯上獨立的 aggregate。
_Avoid_: 單集發布、feed 增量

**Deployment Snapshot**:
一次 Feed Deployment 預定公開的完整 channel 與全部 episode items，不以未列出的項目暗示沿用前版。
_Avoid_: 變更集、增量清單

**公網驗證（Public Verification）**:
從公開端讀回並保存 feed、頁面與 enclosure 實際內容及協定屬性的驗證證據。
_Avoid_: PUT 成功、上傳完成
