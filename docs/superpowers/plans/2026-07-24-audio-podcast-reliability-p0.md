# Audio Podcast Reliability P0 Implementation Plan

**日期**：2026-07-24
**狀態**：Ready for implementation
**依據**：[研究報告](../2026-07-24-notebooklm-podcast-reliability-research.md)、[ADR-0001](../../adr/0001-persist-attempt-before-remote-side-effects.md)、[ADR-0002](../../adr/0002-separate-attempt-facts-from-publication.md)、[ADR-0007](../../adr/0007-mcp-provides-capabilities-workflows-live-in-the-host.md)

**Goal:** 修正 audio podcast 路徑在 timeout、斷線、重試與多 process manifest 寫入時的資料遺失、錯 artifact、重複 Feedback Source 與 series 無法可靠續跑問題，同時維持現有 MCP 呼叫與 `podcast-lab` manifest 相容。

**Architecture:** 建立一個深 `ManifestStore` module，把 flock、schema validation、revision/CAS、原子 replace 與 crash-safe fsync 藏在兩個操作後面；audio attempt lifecycle 留在 podcast module，現有 MCP tools 只當薄 adapter。P0 不建立泛用 artifact workflow framework，也不加入任何強制人工 QA。

**Tech Stack:** Python 3.12、stdlib `fcntl`／`tempfile`／`os`／`uuid`／`hashlib`、`notebooklm-py>=0.7.3,<0.8`、FastMCP、pytest。

## P0 成功條件

- `generate_audio()` 呼叫前已持久化本地 `attempt_id` 與 `dispatching`。
- client timeout／取消／process restart 後，不會把未知結果當成未受理而盲目重生。
- 同一 artifact 重複 resume，只會產生一筆 Feedback Source。
- 新 attempt 不會沿用或覆寫成舊 attempt 的 `artifact_id`。
- `podcast_series()` 重新呼叫時能自動 wait、finalize、skip 或停止；在本次 candidate range 內，只有前集所有必要 postconditions 已確認，才可 submit 下一集。
- 長 MCP request 被取消時，durable checkpoints 足以讓下一次呼叫續跑；仍連線但到達安全停止點時，回傳結構化 partial result。
- 任一 manifest writer 都不再直接 `open(..., "w") + json.dump(...)`。
- corrupt manifest、CAS conflict、ambiguous reconciliation 全部 fail-closed。
- 現有 tool 參數仍可照舊呼叫；現有 manifest 的平面欄位仍可被 publisher 與 `podcast-lab` 讀取。

## 明確不做

- 泛用 audio／slides／report workflow engine。
- event sourcing、資料庫、daemon 或背景 queue。
- MCP 強制人耳 QA、protected facts、逐集核准或發布審批。
- slides／report 的通用 artifact resume。
- Feed Deployment 重寫。
- Notebook capacity cleanup 與 archive。
- 自動刪除舊 artifact／source。
- 保存每個舊 attempt 的本機音檔版本；P0 保留遠端 identity 與狀態歷史，平面 `mp3_path` 仍指向最近成功輸出。

## 相容性契約

### MCP tools

- `podcast_episode(...)`：既有參數不移除、不改位置；新增回傳欄位可以，但既有欄位保留。
- `podcast_episode_resume(...)`：既有 `artifact_id` 路徑繼續可用；傳 `manifest_path` 時提供強 durability，未傳時維持 best-effort 相容行為。
- `podcast_series(...)`：保留 `start`，且 `start=N` 永遠是 execution lower bound，不是 regenerate flag。只檢查 `N..end`，執行點是其中第一個 unsafe／incomplete episode；已完成集一律 skip。P0 不提供隱含重生。
- 新增 `podcast_episode_reconcile(manifest_path, episode_n, attempt_id, wait_timeout=...)`，處理尚無 `artifact_id` 的 `acceptance_unknown`。
- 所有原例外型別保持；只附加 attempt/recovery context，不用新例外包掉 SDK 例外。

### Legacy manifest

現有 `podcast-lab/output/series_manifest.json` 已進版控，且 publisher 直接讀：

```yaml
episode: 34
artifact_id: ...
mp3_path: ...
notebook_id: ...
description: ...
cover_path: ...
slides_pdf_path: ...
report_md_path: ...
```

P0 採增量 schema，不移除這些欄位：

```yaml
schema_version: 2
revision: 17
episodes:
  - episode: 34
    title: ...

    # compatibility projection：最近一次成功 finalize 的輸出
    output_attempt_id: attempt-completed
    artifact_id: artifact-completed
    mp3_path: /.../ep34.mp3
    notebook_id: nb-...

    # durable work-in-progress
    active_attempt_id: attempt-pending
    attempts:
      - attempt_id: attempt-completed
        ...
      - attempt_id: attempt-pending
        ...
```

規則：

- `active_attempt_id` 是目前生成／恢復工作，不必等於平面 `artifact_id`。
- `output_attempt_id` 指向平面 compatibility projection 的來源。
- 建立 pending attempt 時，不提早覆寫上一份可用的 `artifact_id`／`mp3_path`。
- 新 attempt finalize 全部成功後，才原子切換 `output_attempt_id` 與平面 projection。
- podcast resume/reconciliation 永遠讀 attempt record，不再把平面 `artifact_id` 當執行 cursor。
- v1 manifest 在第一次 mutation 時於同一把 lock 內增量升級；不另做 bulk migration CLI。
- 未知 top-level／episode 欄位全部保留。

## Audio attempt schema

```yaml
attempt_id: "uuid-local"
created_at: "UTC ISO-8601"
notebook_id: "nb-..."
episode: 34
title: "..."
brief_sha256: "..."
settings:
  language: zh_Hant
  audio_format: deep-dive
  audio_length: long

dispatch:
  status: prepared | dispatching | accepted | acceptance_unknown | not_accepted
  artifact_ids_before: []
  dispatched_at: null
  accepted_at: null

remote:
  artifact_id: null
  status: unknown | pending | in_progress | completed | failed | removed
  status_origin: remote | sdk_heuristic
  observed_at: null
  error: null
  error_code: null

finalize:
  artifact_rename:
    status: not_started | dispatching | completed | outcome_unknown | failed
  download:
    status: not_started | dispatching | completed | outcome_unknown | failed
    path: null
    bytes: null
    sha256: null
  feedback_source_upload:
    status: not_started | dispatching | accepted | acceptance_unknown | completed | failed
    source_ids_before: []
    source_id: null
  feedback_source_rename:
    status: not_started | dispatching | completed | outcome_unknown | failed
    source_name: null
    verified_at: null

errors: []
```

這些是正交事實；P0 不另存單一 `overall_status`。

P0 統一使用 `not_accepted` 表示 SDK 明確證明 generation 未受理；不再使用同義的 `failed_before_send`。只有能明確證明沒有遠端 generation side effect 時才可進此狀態。

## Module seams

### `ManifestStore` module

**File:** `notebooklm_mcp/manifest_store.py`

小 interface：

```python
store = ManifestStore(manifest_path)
snapshot = store.read()
snapshot, result = store.update(mutator, expected_revision=None)
```

`ManifestStore` implementation 隱藏：

- `<manifest>.lock` 的 per-path exclusive `flock`。
- 讀取最新 bytes 後才執行 read-modify-write。
- root／episodes／attempt referential validation。
- v1 → v2 增量 migration。
- `revision` CAS 與每次成功 mutation 自增。
- 同目錄 temp file。
- write → flush → file fsync → `os.replace` → directory fsync。
- 任何 validation／write／replace 失敗都保留舊完整 manifest。
- mutator 必須同步、不可在 lock 內 await 或打網路。

不新增 adapter interface：目前只有本機 JSON 一個實作，抽象 storage backend 是 YAGNI。

### Audio attempt implementation

**Files:** `notebooklm_mcp/tools_podcast.py`，必要時新增 private `notebooklm_mcp/audio_attempt.py`

- 公開 MCP tools 是 interface。
- attempt state transitions、reconciliation 與 finalize postconditions 集中在一處。
- `tools_podcast.py` 不再自行開檔或拼 JSON。
- 若抽出 `audio_attempt.py`，它保持 package-private，不新增第二套公共 interface。

---

## PR 1 — ManifestStore 與所有 manifest writers 收斂

**目的:** 先消滅截斷 JSON 與 lost update；這是後續 attempt state 可相信的前提。

**Files:**

- Create: `notebooklm_mcp/manifest_store.py`
- Create: `tests/test_manifest_store.py`
- Create: `tests/fixtures/legacy_series_manifest.json`
- Modify: `notebooklm_mcp/tools_podcast.py`
- Modify: `notebooklm_mcp/tools_artifacts.py`
- Modify: `notebooklm_mcp/tools_publish.py`
- Modify: `notebooklm_mcp/cover_cli.py`
- Modify: `tests/test_tools_podcast.py`
- Modify: `tests/test_tools_artifacts.py`
- Modify: `tests/test_publish_tools.py`
- Modify: `tests/test_cover_cli.py`

### Red tests

- [ ] legacy manifest 第一次 update 後新增 `schema_version=2`／`revision=1`，但 show、description、cover、attachments 與未知欄位完全保留。
- [ ] corrupt／非 object／episodes 非 list 時 `read` 與 `update` 都 raise 明確 `ValueError`，且不覆寫原檔。
- [ ] `expected_revision` 不符時 raise `ManifestConflictError`，不執行 mutator。
- [ ] monkeypatch `os.replace`／file fsync 失敗時，舊 manifest 仍是完整可讀 JSON。
- [ ] replace 成功後檔案只能是完整新 JSON。
- [ ] 兩個 process 對不同 episode 各更新 50 次，最後 revision 與兩邊欄位都保留。
- [ ] `tools_artifacts`、`tools_publish`、`cover_cli` 與 podcast writers 併發更新不同欄位時不互蓋。

### Minimal implementation

- [ ] `ManifestStore.read()` 取得 lock 後讀取與 validate；檔案不存在時回受控的空 schema。
- [ ] `ManifestStore.update()` 在單一 lock scope 內重新讀取、migrate、mutate、validate、增加 revision、原子寫入。
- [ ] schema validation 用少量 stdlib 函式，不加 JSON Schema／Pydantic dependency。
- [ ] 將 `_write_manifest`、`_upsert_manifest_stub`、`_update_episode_manifest`、publish show config write、cover batch write 全部改走 store。
- [ ] 移除 `cover_cli.py::_atomic_write_json`，或把它縮成只呼叫 `ManifestStore` 的 compatibility wrapper；不得保留第二套 atomic JSON 實作。
- [ ] `auth_cli.py` 不是 series manifest writer，不納入本 PR。
- [ ] lock 只保護本地 mutation；不得跨任何 SDK／HTTP await 持有。
- [ ] 文件明示 `flock` 是 advisory：`podcast-lab` 或其他外部 maintenance script 若直接修改同一 manifest，必須改用 MCP tool 或同一個 `ManifestStore`，否則 P0 只保證 MCP 內部 writers。

### Verification

```bash
uv run pytest tests/test_manifest_store.py tests/test_tools_artifacts.py \
  tests/test_cover_cli.py tests/test_publish_tools.py tests/test_tools_podcast.py -q
```

Expected: PASS。

**Suggested commit:** `fix(manifest): centralize atomic locked read-modify-write`

---

## PR 2 — Audio attempt history 與 legacy projection

**目的:** 修正 `_upsert_manifest_stub(setdefault)` 把新 generation 與舊 artifact 混在同一 episode row。

**Files:**

- Modify: `notebooklm_mcp/tools_podcast.py`
- Optional Create: `notebooklm_mcp/audio_attempt.py`
- Modify: `tests/test_tools_podcast.py`
- Modify: `tests/test_publish_tools.py`
- Modify: `tests/fixtures/legacy_series_manifest.json`

### Red tests

- [ ] 同 episode 建三次 attempt，三筆 identity／status 都保留，`active_attempt_id` 永遠指向最新一次。
- [ ] 新 attempt 在尚未 finalize 時，不覆寫上一份 `output_attempt_id`、平面 `artifact_id`／`mp3_path` 與人工欄位。
- [ ] 新 attempt finalize 成功後，平面 projection 才一次切換到新 artifact。
- [ ] failed 舊 attempt 不會被新 attempt 的欄位覆蓋，也不會繼續充當 active cursor。
- [ ] v1 episode 的平面 artifact 可被讀成 legacy output，但不捏造不存在的歷史 attempt。
- [ ] publisher 對 v1 fixture 與 v2 projection 產生相同既有行為。

### Minimal implementation

- [ ] local `attempt_id = str(uuid.uuid4())`。
- [ ] 新增集中 mutation helpers：create attempt、bind artifact、record remote status、record finalize result、promote output。
- [ ] attempt 建立時保存 notebook、episode、title、brief hash 與生成 settings snapshot。
- [ ] 每個 helper 經 `ManifestStore.update()` 驗證 episode／attempt pointer。
- [ ] 保留 tool response 的 `artifact_id`、`mp3_path` 等既有欄位，新增 `attempt_id`。

### Verification

```bash
uv run pytest tests/test_tools_podcast.py tests/test_publish_tools.py -q
```

Expected: PASS。

**Suggested commit:** `fix(podcast): separate active attempts from legacy output projection`

---

## PR 3 — Submit intent、acceptance_unknown 與 reconciliation

**目的:** 關閉「遠端可能已受理，但本機尚未保存 artifact identity」的 crash gap。

**Files:**

- Modify: `notebooklm_mcp/tools_podcast.py`
- Modify/Create: `notebooklm_mcp/audio_attempt.py`
- Modify: `notebooklm_mcp/app.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_tools_podcast.py`
- Modify: `tests/test_resume.py`

### Submit ordering

```text
local validation
→ auth probe
→ persist prepared attempt
→ list audio artifact baseline
→ persist baseline
→ persist dispatching
→ generate_audio()
→ persist accepted + artifact_id
```

### Exception semantics

- SDK 回傳有效 task id：`accepted`。
- SDK 明確回傳 failed／empty task id：`not_accepted`，保存原 error/error_code。
- timeout、disconnect、transport exception、`CancelledError`：`acceptance_unknown`。
- process 在 `dispatching` 死亡：下次讀取同樣視為 `acceptance_unknown`。
- 未知結果不建立替代 attempt，也不自動再呼叫 `generate_audio()`。

### Reconciliation

候選條件：

- notebook 相同。
- kind 是 audio。
- artifact id 不在 submit 前 baseline。
- created_at 落在 dispatch window。
- 尚未被其他 attempt claim。

結果：

- 1 筆：adopt，綁定原 attempt。
- 0 筆：保持 unknown，回 `safe_next_action=wait_and_reconcile`。
- 多筆：回 `reconciliation_ambiguous` 與候選 ids，不選 latest。

### Red tests

- [ ] fake 在 `generate_audio` 入口讀 manifest，必須已看到 `dispatching`。
- [ ] fake 建立遠端 artifact 後丟 timeout；本地 attempt 變 unknown，且完全沒有第二次 generate。
- [ ] process-restart fixture 留在 `dispatching`，重新呼叫先 reconcile。
- [ ] 唯一新 artifact 被 adopt。
- [ ] 0 candidate 保持 unknown。
- [ ] 2 candidates fail-closed，不猜最新。
- [ ] `CancelledError` 仍先 checkpoint 再原樣 re-raise。
- [ ] returned failed status 保存 terminal detail，不標成 transport timeout。

### Tool interface

- [ ] 新增 `podcast_episode_reconcile(...)` MCP tool。
- [ ] `podcast_episode` 的 post-submit exception message 附 `attempt_id`；有 artifact 時仍附 `artifact_id` 與既有 resume 指引。
- [ ] `podcast_series` 內部使用同一 reconciliation implementation。

### Verification

```bash
uv run pytest tests/test_tools_podcast.py tests/test_resume.py tests/test_errors.py -q
```

Expected: PASS。

**Suggested commit:** `fix(podcast): persist submit intent and reconcile unknown acceptance`

---

## PR 4 — 冪等 audio finalize 與 Feedback Source upload

**目的:** 同一 attempt 無論在哪一步斷線，重跑都只補未完成 side effect。

**Files:**

- Modify: `notebooklm_mcp/tools_podcast.py`
- Modify/Create: `notebooklm_mcp/audio_attempt.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_tools_podcast.py`
- Modify: `tests/test_resume.py`

### Postcondition-driven finalize

1. **Remote wait**
   - pending／in_progress：繼續 wait。
   - completed：進 finalize。
   - failed／removed：保存精確 origin/detail，停止。

2. **Artifact rename**
   - 先查 artifact title；已等於 label 就跳過。
   - 呼叫前寫 `dispatching`。
   - response lost 時查遠端 postcondition，成立就 adopt。

3. **Download**
   - 下載到同目錄 temp path。
   - 非空並計算 size／SHA-256 後 `os.replace` 到既有 `epNN.mp3`。
   - manifest 已記 hash 且本機 bytes 相符就跳過。
   - partial／hash 不符不當成功。

4. **Feedback Source upload**
   - 呼叫前保存 source id baseline、dispatch start/end window、預期原始 filename/title 與 `dispatching`。
   - 收到 source id 後立即保存，再 rename。
   - response lost 時，候選必須同時滿足：不在 baseline、`kind == media`、`created_at` 落在 dispatch window、符合 SDK 當下可見的預期 filename/title identity，且尚未被其他 attempt claim。
   - 目前 `notebooklm-py 0.7.3` 的 Source list 提供 `id`、`title`、`kind`、`created_at`，但不提供 MIME 或 upload id；不得假裝 `kind == media` 能單獨證明它是目標 audio。若未來 SDK 提供 MIME／upload id，才把它們加入必要比對。
   - 恰好一筆充分符合條件的候選才 adopt；0 筆保持 unknown；多筆或 metadata 不足以唯一辨識時標為 ambiguous 並 fail-closed。
   - 已保存 source id 且 `source_list` 可驗證，就不重傳。
   - 此流程是 best-effort identity recovery，不承諾在所有併發上傳情境都能自動找回。

5. **Source rename**
   - 以 source id 驗證 title；已正確就跳過。
   - 不以「同名最新來源」代替 identity。

6. **Promote output**
   - 全部 finalize postconditions 完成後，才原子更新 `output_attempt_id` 與平面 compatibility projection。

### Red tests

- [ ] 同 artifact、同 attempt 連續 resume 兩次，第二次沒有 rename/download/add_file side effect。
- [ ] `add_file` 遠端已建立 source 但 response 遺失；reconcile 後 source 總數仍為 1。
- [ ] response loss 產生多個 source candidate 時列 ids 並停止，不自動 delete。
- [ ] baseline 後同時出現非 media source、window 外 media source 與唯一符合預期 filename/title 的 window 內 media source；只 adopt 後者。
- [ ] 多筆 window 內 media source 或缺少足以唯一辨識的 metadata 時標為 ambiguous，不選最新。
- [ ] source id 已保存但遠端消失時不盲目重傳。
- [ ] download 中斷只留下 temp，不覆寫上一份成功 `epNN.mp3`。
- [ ] manifest checkpoint 寫入後 process restart，只執行下一個未完成步驟。
- [ ] legacy `podcast_episode_resume(..., manifest_path=None)` 仍可呼叫，但回傳 durability warning；強冪等測試使用 manifest。

### Verification

```bash
uv run pytest tests/test_tools_podcast.py tests/test_resume.py \
  tests/test_reject_delete.py -q
```

Expected: PASS。

**Suggested commit:** `fix(podcast): make audio finalize and source upload reentrant`

---

## PR 5 — `podcast_series` 自動狀態恢復

**目的:** 重新呼叫整季工具即可從 durable attempt state 推進，不再以人工 `start=N` 作唯一 cursor。

**Files:**

- Modify: `notebooklm_mcp/tools_podcast.py`
- Modify/Create: `notebooklm_mcp/audio_attempt.py`
- Modify: `tests/test_resume.py`
- Modify: `tests/test_tools_podcast.py`

### Action derivation

`start=N` 的語意固定為 execution lower bound：

```text
candidate range = N..end
execution episode = candidate range 中第一個 unsafe／incomplete episode
```

- `start=1`：檢查全季；EP1／EP2 complete 時 skip，從 EP3 開始。
- `start=3`：不檢查、也不主張 EP1／EP2 已完成；只處理 EP3 起。
- `start` 永遠不表示重新生成已完成集。若未來要重生，另設明確 `force_regenerate`；P0 不做。
- `start>1` 是明確的 compatibility trust boundary：caller 對 N 之前的 continuity prerequisites 負責；MCP 不驗證也不修改它們，但從 EPn 進入 candidate range 後，仍必須完成 EPn postconditions 才能 submit EPn+1。

對 candidate range 逐集推導：

| Manifest／remote state | Action |
|---|---|
| 無 attempt | create + submit |
| `dispatching`／`acceptance_unknown` | reconcile；不 generate |
| accepted + pending/in_progress | wait |
| remote completed + finalize incomplete | resume finalize |
| finalize complete 且 Feedback Source identity 已驗證 | skip，才可考慮下一集 |
| failed／removed | stop；要求 caller 明確建立新 attempt |
| ambiguous | stop；回候選與 recovery action |

### Safe advancement contract

- 系列每一步都先 checkpoint，再執行一個遠端 side effect，最後驗證該步 postcondition。
- EPn accepted／pending 時，只能 wait EPn；不得 submit EPn+1。
- EPn remote completed 但 download／Feedback Source upload／rename 未完成時，只能 finalize EPn。
- EPn Feedback Source 的 `source_id`、預期 label 與 READY/可用狀態尚未確認時，必須停止；不得 submit EPn+1。
- 只有 EPn finalize 全部完成且 Feedback Source identity 已驗證，才可 create／submit EPn+1。
- 一次仍連線的 call 可以連續跨越數個已驗證安全邊界；這不代表 client 斷線後原 task 會繼續。`CancelledError` 只能保證已完成 checkpoint，不能保證回傳。
- 若 call 仍可回應但遇到 pending timeout、unknown、ambiguous 或其他安全停止點，回傳結構化 partial result，至少包含 `complete=false`、`stopped_at_episode`、`attempt_id`、`observed_state` 與 `safe_next_action`，讓 caller 重新呼叫同一 series。

### Plan drift

- [ ] manifest 保存 title、brief hash 與 settings。
- [ ] 同 episode 已有未完成 attempt，但新的 `episodes[]` title／brief hash 不同時 fail-closed。
- [ ] 已完成 episode 可被相同 plan skip。
- [ ] P0 不自動改寫既有 episode brief／title。

### Red tests

- [ ] EP2 在 accepted/pending 中斷；重新呼叫 series 不 generate EP2 或 EP3，只 wait/finalize EP2；所有 EP2 postconditions 驗證後才允許 generate EP3。
- [ ] EP2 completed 但下載前中斷；重新呼叫先且只 finalize EP2，不得在 finalize postconditions 完成前 generate EP3。
- [ ] EP2 source upload response lost；重新呼叫先 reconcile 成同一 source，source identity 未確認前不得 generate EP3。
- [ ] EP1/EP2 complete 時 default `start=1` 自動 skip，生成 EP3。
- [ ] 顯式 `start=3` 舊用法仍只處理第 3 集起。
- [ ] `start=2` 且 EP2 finalize complete、EP3 incomplete 時，skip EP2 並 generate EP3。
- [ ] `start=3` 不讀取或驗證 EP1／EP2，也不把它們標為 complete。
- [ ] pending timeout／unknown／ambiguous 在 caller 仍連線時回結構化 partial result；process cancellation 後重呼可從 checkpoint 繼續。
- [ ] failed／removed 不自動建立新 attempt。
- [ ] acceptance ambiguous 不生成下一集。
- [ ] manifest 的早期 episodes、show config、description、covers、attachments 全部保留。
- [ ] auth probe 在每次新的昂貴 generation 前執行；resume 純 wait/finalize 不誤建新 generation。

### Verification

```bash
uv run pytest tests/test_resume.py tests/test_tools_podcast.py -q
```

Expected: PASS。

**Suggested commit:** `fix(series): resume episodes from durable attempt state`

---

## PR 6 — Tool docs、cross-repo sync 與 release gate

**目的:** 讓 host 正確使用新能力，但不把 optional QA workflow 寫成 MCP 強制流程。

**Files:**

- Modify: `notebooklm_mcp/app.py` server instructions
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/notebooklm-mcp-findings.md`
- Modify: `/home/user/research/audi-skill/notebooklm/SKILL.md`
- Modify: `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`
- Modify: `/home/user/research/audi-skill/notebooklm/references/troubleshooting.md`
- Modify: sync tests/scripts as required

### Documentation

- [ ] 記錄 `attempt_id`／`artifact_id` 差異。
- [ ] 記錄 timeout／unknown／failed／removed 的誠實語意。
- [ ] 說明 default series auto-resume 與 `start` 相容行為。
- [ ] 新增 `podcast_episode_reconcile` 範例。
- [ ] 把「同 artifact 只能 resume 一次」舊警告改成 manifest-backed 冪等語意，並說明 series 只推進到下一個安全 side-effect boundary。
- [ ] 更新 `app.py` server instructions：有 manifest-backed attempt 時優先用 reconciliation／`podcast_series` auto-resume；`podcast_episode_resume(artifact_id=...)` 僅是 standalone／legacy fallback。
- [ ] 明示長 MCP request 可能被 client cancellation 終止；可靠性來自 checkpoints 與重呼，不是背景 task 保證。
- [ ] 明示未傳 `manifest_path` 的 standalone call 是 best-effort。
- [ ] QA／protected facts 維持可選 host workflow，不是 MCP lifecycle。
- [ ] 明示外部 manifest writer 也必須使用 MCP tool 或相同 `ManifestStore`；advisory lock 無法保護不合作的直接覆寫。

### Release verification

```bash
uv run pytest -q
uv run python scripts/check_skill_sync.py
```

Expected: 全部 PASS。

- [ ] 在測試帳號做一次 fault-injection live contract：accepted 後 client 中斷 → reconcile → finalize；不得再 generate。
- [ ] 同 attempt resume 兩次，NotebookLM Sources 只留一筆該 attempt 的 source。
- [ ] 複製真實 legacy manifest 到 temp，跑一次新 episode，publisher 仍可讀。
- [ ] 若要發布版本，這是 schema/tool capability 增量，建議 bump `0.2.9 → 0.3.0`。
- [ ] 依 repo 規則先同步／推 audi-skill，再推 MCP；commit、push、tag、deploy 都另取使用者明確授權。

**Suggested commit:** `docs: document durable podcast attempts and recovery`

---

## Final acceptance matrix

| Scenario | Expected |
|---|---|
| Crash before `generate_audio` | attempt=`prepared/dispatching`；重啟先 reconcile |
| Remote accepted, response lost | 不重生；唯一候選綁回原 attempt |
| Reconcile finds 0 | 保持 unknown，回 wait/reconcile |
| Reconcile finds >1 | ambiguous，列 ids，禁止猜 latest |
| Wait timeout with artifact id | 保留 accepted/pending，resume 同 artifact |
| Remote failed | terminal detail 保存；不標 timeout、不自動新 attempt |
| SDK `removed` heuristic | `status_origin=sdk_heuristic`，不宣稱唯一 root cause |
| Rename response lost | 以 artifact id/title postcondition adopt |
| Download interrupted | 舊成功檔不被 partial 覆寫 |
| Source upload response lost | baseline + media kind + dispatch window + 可用 identity metadata 唯一候選才 adopt；否則 unknown/ambiguous |
| Resume twice | 第二次零新增 Feedback Source |
| New attempt for same episode | history 保留；active 指新 attempt；舊欄位不混入 |
| Previous episode postconditions incomplete | 只恢復該集；禁止 submit 下一集 |
| Series process restart | 從 checkpoint 自動 wait/finalize/skip；不假設原 request 還活著 |
| Series reaches safe stop while connected | 回 structured partial result 與 `safe_next_action` |
| `start=N` | 只檢查 N..end；skip 已完成集；不代表 regenerate |
| Concurrent manifest writers | 無截斷、無 lost update、revision 單調增加 |
| Legacy podcast-lab manifest | 原欄位與 publisher 行為維持 |

## Implementation order and stop conditions

嚴格依 PR 1 → 6；每個 PR targeted tests 綠才進下一個。遇到以下任一條件停止，不用補丁掩蓋：

- SDK list model 無法提供 reconciliation 所需的 artifact identity／created_at。
- Source list 無法以 id、media kind、created_at 與可用 title metadata 將 upload 前後候選縮到唯一一筆。
- legacy manifest migration 會清掉未知或人工欄位。
- 現有 dirty `tools_publish.py` 修改與 ManifestStore migration 發生衝突。
- 發現 scope 內的外部 writer 仍直接覆寫同一 manifest，且無法在 P0 內改走 MCP tool／`ManifestStore`；此時不得宣稱跨 process 完整保護。
- full suite 出現與本 P0 無關但會讓 release 不安全的失敗。

實作開始前先重新跑 worktree guard，並保留目前使用者在 `AGENTS.md`、`tools_publish.py`、`pyproject.toml`、`tests/test_publish_tools.py` 的既有修改；不得 reset、checkout 或混進 resilience commit。
