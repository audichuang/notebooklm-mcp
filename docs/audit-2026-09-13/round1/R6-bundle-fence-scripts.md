# R6 審查者:generation_input / _atomic / scripts(8 條;兩條 P1 已在 scratchpad 實測重現)

## P1-1 generation_input.py:219-225 多節目容器偵測的排除集 ≠ 圍籬執行端的接受集,ADR-0012 原始事故可重現
偵測端 _other_series_manifests 排除隱藏目錄(:220)、symlink(followlinks=False)、os.walk onerror=None 吞不可讀目錄;執行端 _read_frozen_bundle_files(:39-80)只拒 symlink,隱藏目錄照走。
實測(scratchpad fence/):base/showA/series_manifest.json + base/.archive/showB/series_manifest.json → others detected: [] → ACCEPTED brief='showB brief' → showA 吃下 showB 的 bundle(ADR-0012 開頭 2026-08-30 事故同形)。
路徑:load_frozen_generation_input:263-273 → :277 → :279
測試:test_generation_input_bundle.py:903 只用可見 sibling
嚴重度 P1。信心:機制高;真實觸發中等(需 .bak/.archive/權限被收的舊季目錄)。

## P1-2 tests/test_generation_input_bundle.py:171-200 bundle 換手 tripwire 打不到,恆真
靠 patch.object(Path, "is_dir", swap_after_check)(:181-189),但 loader 全程 os.open/fstat/lstat,一次都沒呼叫 Path.is_dir。
實測(scratchpad hook/):Path.is_dir calls during load: [] → swapped 永遠 False。拿掉 :41 O_NOFOLLOW 與 :62-68 inode 重驗,測試照綠。
嚴重度 P1(安全迴歸守衛靜默失效)。信心高。

## P2-3 generation_input.py:389-392, 408, 446-451, 511 sidecar 讀/寫/rollback 不套用 load 那套 containment
write_attempt_binding :390 mkstemp(dir=bundle)、:408 os.link;read_attempt_binding :446-451 只查最後一段;rollback :511 同。
窗口:load(:2846)到 write_attempt_binding(:2542)之間隔 probe_auth + 三趟 RPC;窗內把 bundle 換 symlink → sidecar 落在目標 bundle → 原 bundle 下次建第二個 attempt_id(破「一個 bundle 只綁一次」)
嚴重度 P2。信心:路徑高;觸發需窗內動目錄。

## P3-4 generation_input.py:326-327, 332 files[*].bytes 收 true,bool 防護三處補兩處
:326 `not isinstance(record.get("bytes"), int)`;True 過;:332 `1 != True` 為假。實測 bytes=true ACCEPTED,record runtime_brief_bytes = True → manifest 稽核欄位 json true。sha 仍須符,無內容替換風險。
測試:無(:141 只參數化 schema_version)。P3。信心高。

## P3-5 scripts/backfill_published_at.py:93 vs :110-121 安全閘鎖外算、變更鎖內算
A:_breaks_ordering 在 :93 鎖外 snapshot(且與 :82 不同的第二次 read),鎖內重算 applied 可能非單調(下次 publish preflight 擋,非靜默)。B:零變更仍 revision+1(reorder 有 _NoChange,backfill 沒有)。
測試:兩支腳本零測試。P3。信心:路徑高,機率低。

## P3-6 scripts/setup-test-config.sh:27 sed -n '2,20p' 行號腐化(sync-auth.sh:47-51 已改 awk)
現在就錯:多印 :17 set -euo、:19 PROJECT、:20 SOURCE_CONFIG;--from 參數未在說明出現。P3。信心高。

## P3-7 _atomic.py:75-77 prepared_replacement 用路徑版 chmod/open,sibling(generation_input:401 fchmod、manifest_store:103 fchmod)已改 fd 版
:64 先 close(fd) 交路徑;:75 os.chmod(temp_path)、:76 open(temp_path,"rb")。audio_finalize.py:658-663 亦路徑版(有結構理由)。P3。信心中(可利用性低)。

## P3-8 scripts/eval_harness/run.sh:30-33 + :22
A:受測 box 以 ln -sfn 直通活的 skill repo(~/research/audi-skill/notebooklm),agent 帶 --dangerously-skip-permissions,寫穿 symlink 改到正式 skill working tree。信心高。
B:平行迴圈對同一 arm 起多個 run.sh,:22 `[ -d ] || git worktree add` 競態,輸的 exit 1 → 樣本悄悄消失或 import 半建好的樹。信心中低。
測試:無。P3。

## 乾淨的
無 writer 繞過 ManifestStore/write_attempt_binding(全 package 掃過);temp 與目標同 fs;post-commit dir fsync 四處一致;ADR-0001 順序成立;sidecar link 成功但 manifest 寫入前 crash 可復原(同 id 重建不多燒);兩支 published_at 腳本 dry-run 預設、走 ManifestStore;sync-auth.sh set -euo、--raw、非互動只警告是明文刻意;check_skill_sync 現況綠;eval_harness 真 dry-run、兩臂無污染。
scratchpad 實驗檔:fence/、boolbytes/、hook/
