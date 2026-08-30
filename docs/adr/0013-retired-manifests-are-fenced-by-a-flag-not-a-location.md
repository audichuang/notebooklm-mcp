# 退役 manifest 由 machine-readable 旗標圍住,不靠位置

podcast-lab 的 graphify 節目換過一次 `show_id`(`no-vector-map` → `no-vector-map-v2`),留下兩份
退役 manifest。2026-08-19 它們被搬進 `shows/graphify-deep-dive/archive/`、配一份 README 說明
「一律不得作為 `manifest_path`」。2026-08-30 Codex 獨立審查指出:**那只擋得住讀過 README 的人**。
兩份檔仍通過 `ManifestStore` schema,`show_id=no-vector-map` 仍能打到第一季的舊 feed(仍公開可讀);
其中 `s1-published` 那份的 8 個 `mp3_path` 指向 Audicast 搬家前的根層 `output/epNN.mp3`。今天
publisher 在 resolve 就 fail-closed 是因為那些檔**恰好不存在**——而 legacy 集(無 attempts 記錄)
publisher 不比 sha(`_mp3_provenance_gap` 放行並列進 `sha_unverified_episodes`),所以只要有人
「修好」路徑,Audicast 的音檔就會以「沒有向量的地圖 EP01–08」的名義送上公網。

這與 ADR-0012 是同一個形狀:**目錄結構不是安全邊界**——那裡是深度、這裡是位置。

## 決定

1. manifest 頂層新增 `retired: true`。`publish_series` 讀完 manifest 的**第一步**就
   `publish/state.assert_not_retired`,在 auth probe 與任何 PUT 之前 raise。
2. 判準同 `publication_state` / `published_at`:**欄位在不在**。缺席 = 照發;`true` = 退役;
   其他任何值(`false`、`null`、字串、`1`)raise,不 fall through。`is True`,不是 `== True`。
3. **只管發布層。** 生成、attempt 掃描、retract、`published_at` 兩支腳本刻意不看這個欄位:
   退役是「不得再公開」,不是「不得再讀」——它們是換 feed 前後的稽核紀錄,`EPISODES.md`
   靠它們對帳。
4. **不加寫入工具。** 標記是一次性的稽核動作,走 `ManifestStore.update`(手改 JSON 仍是
   ADR-0009 禁止的路徑):
   ```
   python -c "from notebooklm_mcp.manifest_store import ManifestStore as S; \
     S('<archive>/x.json').update(lambda m: m.__setitem__('retired', True))"
   ```
   哪天要退役的 manifest 多到需要工具再加,那時再加也不會比現在難。

## 為什麼不做另外兩種

- **publisher 加 canonical-path allowlist**(只接受 `<show>/series_manifest.json` 之類的路徑):
  又是一條寫死佈局的規則,ADR-0012 剛把一條同型的拆掉。而且它擋的是「檔在哪」,不是
  「這份能不能發」——把退役檔搬回正確位置就繞過了。
- **給 legacy 集補 `finalize.download.sha256` 讓 provenance 閘接手**:那是另一個問題(Audicast
  EP01–34 同名檔冒充的風險),使用者 2026-08-30 決定接受現狀。就算補了,退役檔的 8 集 sha
  仍會與 Audicast 的檔不符而被擋——但那是「剛好不符」,不是「這份不該發」;兩個問題各有
  自己的閘。

## 後果

- 兩份 graphify archive manifest 由 `ManifestStore.update` 標上 `retired: true`(revision +1),
  README 的「publisher 沒有 canonical-path allowlist」改寫成指向這道閘。
- `retired` 進 `check_skill_sync.REQUIRED_CONTRACT_TERMS`:它與 `publication_state` 一樣是
  「工具名與參數都沒變、純 manifest 欄位」的契約,正是那支 checker 最容易漏的一類。
- 要「復活」一份退役 manifest 必須**移除欄位**(同樣走 `ManifestStore.update`),沒有
  `retired: false` 這條捷徑——復活是罕見且該被看見的動作。
