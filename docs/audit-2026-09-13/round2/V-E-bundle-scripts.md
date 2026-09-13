# 驗證 E:generation_input / _atomic / scripts 八條(R6)—— 兩條 P1 都降 P2;1 REFUTED;2 降 P4

R6-P1-1 隱藏目錄繞過容器偵測 → **CONFIRMED(三子論點只成立 hidden 一個),P1→P2**。
  V1 hidden ACCEPTED brief LEAKED;CTL 可見 archive/ REFUSED(證明 hidden-specific);V2 symlink REFUSED(執行端 O_NOFOLLOW 擋,兩端效果對稱 → REFUTED);V3 不可讀 0111 REFUSED(walk 與 loader 都需 r → REFUTED)。
  非刻意取捨:ADR-0012 決定 #2 與 :219 docstring 括號寫 (.venv/.git),意圖是除噪非邊界決策。
  觸發度:今天 podcast-lab 12 份 manifest 無一在隱藏路徑;但 ADR-0013 退役是搬進可見 archive/,一次 `mv shows/old shows/.old` 就靜默失效 —— 差一步的近失。
  修法落點:generation_input.py:220 刪 hidden-skip 一行(shows/ 底下隱藏目錄只有 .venv/.venv-whisper,無套件 ship series_manifest.json)。測試:test_generation_input_bundle.py:895 _multi_show_container 參數化隱藏 sibling 餵 :903。

R6-P1-2 換手 tripwire 恆真 → **CONFIRMED(測試債),P1→P2**。
  Path.is_dir calls: [];MUT-A(全拔 O_NOFOLLOW+inode)→ :171 PASS 但 :653 test_frozen_input_rejects_symlink_ancestor FAIL → 目錄段 O_NOFOLLOW 有守;MUT-B(只拔成員檔 O_NOFOLLOW+inode 重驗)→ 兩支全綠 → 真正無人守的只有成員檔 :51 與 inode 重驗 :62-68。
  第二個恆真缺陷(審查者沒講):`except ValueError: return` 在 with 內,任何 ValueError 都算過,不驗拒絕理由。
  生產碼正確:換手掛在第一次 os.fstat 上 → REFUSED "changed or became a symlink"。
  修法落點:test_generation_input_bundle.py:171-200 改掛 os.fstat 或 patch _read_frozen_bundle_files,`except ValueError: return` 換 pytest.raises(match="changed or became a symlink")。

R6-P2-3 sidecar 不套 containment → **CONFIRMED,P2→P3**。
  prepared 無 pinned inode(:351-364);窗口屬實(:2846 load → :2860 probe_auth 網路 → 三趟 RPC → :2539/2542 sidecar),數秒級。對手面邊際價值≈0(能在窗內換目錄者可在 load 前換整顆自洽 bundle,校驗自我指涉);殘留是意外重整。harm 有界:binding 落錯目錄 → 重複燒配額+稽核說謊,不會 dispatch 錯內容(brief 已從 pinned inode 讀出)。
  修法落點:_read_frozen_bundle_files 回傳 (st_dev, st_ino) 進 prepared,write_attempt_binding 開頭 os.lstat(bundle) 比對。測試:test_generation_input_bundle.py :171 旁。

R6-P3-4 bytes=true → **半 REFUTED,P3→P4 cosmetic**。多位元組 brief + bytes=true REFUSED(mismatch);只有 1-byte brief 才 ACCEPTED 留 json true;另兩成員必為合法 JSON 不可能 1 byte。修法:generation_input.py:326 `type(...) is not int`;測試 :139-141 parametrize 加 bytes。

R6-P3-5 backfill 鎖外閘/零變更寫入 → CONFIRMED 如實,**P3→P4 hygiene**。A 後果 publish preflight 擋(loud);B 需競爭 writer。修法:下次動到時搬 :93 進 mutator + 抄 reorder 的 _NoChange。不開測試檔。

R6-P3-6 sed 行號腐化 → **CONFIRMED P3 維持**。實跑 --help 尾巴吐 17/18/19/20 四行程式碼;--from(:26)未在 :10-12 說明出現。修法:setup-test-config.sh:27 換 sync-auth.sh:51 的 awk 寫法,補 --from 說明。

R6-P3-7 _atomic 路徑版 chmod → **REFUTED,不列**。prepared_replacement 契約是 :64 close(fd) 後把路徑交呼叫端寫(cover_cli.py:95 渲染 PDF、auth_cli.py:37),結構上拿不到 fd;留 fd 反而更糟(內容寫到別處時 chmod/fsync 作用在空 inode)。可利用性:需目錄寫入權,有權者可直接寫最終檔,無提升。頂多 gotchas-files 加一句一致性註解。

R6-P3-8 eval_harness → A **CONFIRMED P3**:ln -sfn 直通活 skill working tree,重現 agent 寫穿 symlink 蓋 SKILL.md、建 references/new.md,rm -rf box 救不回。修法:run.sh:30-33 ln -sfn → cp -r。
  B **CONFIRMED 且比估的強**:README:16-18 推薦的平行迴圈 10 併發搶同一 $SRC,重現 `fatal: already exists` ×9,job2..10 exit 1 樣本消失;results-v0.9.26.json 20/20 完整是因 README:10-13 單題示範先建了 worktree;冷啟動下必然發生。grade.py:47-58 glob 加總不查兩臂 id 對齊 → 半靜默(git 吼、評分層不吼),是 README「三個坑」的第四個。修法:run.sh:22 冪等建法或 flock;grade.py:58 印各臂樣本數。
