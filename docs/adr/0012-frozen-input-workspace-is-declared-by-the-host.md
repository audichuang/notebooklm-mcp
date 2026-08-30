# 凍結輸入的圍籬由 host 宣告,不從目錄深度推

`load_frozen_generation_input` 把「manifest 的祖父目錄」當成 bundle 的唯一圍籬。這條規則的
前提是佈局 `<show>/<any>/series_manifest.json`——祖父恰好是節目目錄。設計時已刻意不限定
父目錄的**名字**(`manifest/`、`output/`、`season-01/output/` 都要能用),但仍隱含限定了
**深度**:manifest 一定在節目目錄下**兩層**。

podcast-lab 十二個節目裡有五個把 manifest 直接放 show root(`shows/<show>/series_manifest.json`)。
對它們,祖父目錄是**整個 `shows/`**。2026-08-30 獨立審查實測:用 graphify 的 manifest 傳
`input_bundle_path="audicast/episodes/EP46-…/attempt-005"`,loader 照吃、`read_attempt_binding`
回 `None`——下一步就會把 Audicast 的 brief 綁進 graphify 的 attempt、燒配額、留下一筆
「哪份輸入產出哪一集」說謊的 binding。圍籬沒破,是圍籬畫得太大。

## 決定

1. `podcast_episode` / `load_frozen_generation_input` 新增 `workspace_root`(節目目錄的路徑)。
   **有傳就以它為圍籬**,並要求 manifest 在它底下(宣告與 manifest 各說各話直接拒)。
   `input_bundle_path` 相對於它。
2. **沒傳時仍推祖父目錄,但推導 fail-closed**:祖父目錄裡若還有**別的** `series_manifest.json`
   (不限深度、不跟 symlink、跳過隱藏目錄),就是多節目容器,拒絕並在錯誤訊息裡指名要傳
   `workspace_root`。單節目佈局(Audicast、data-structure-lab、network-breakdown 的形狀)行為不變。
3. binding 的 `manifest_workspace_sha256` **語義不變**(仍是 manifest 絕對路徑的 sha):
   它回答的是「在哪一個 manifest workspace 產出」,圍籬怎麼畫不改變這個事實。

## 為什麼不做另外兩種

- **把五個 show-root manifest 搬進 `<show>/manifest/`**:五次 ManifestStore 前綴改寫去遷就
  一個 hardcode,而下一個把 manifest 放 show root 的人會再踩一次。深度是 MCP 自己的假設,
  該在 MCP 修。
- **推導時自動縮到「manifest 的父目錄」**:對 `output/series_manifest.json` 這種佈局會把
  `episodes/` 排除在圍籬外,現有所有 Audicast bundle 立刻失效;而且「有沒有別的 manifest」
  本來就是可以確定性判斷的事,不需要猜。

## 後果

- graphify 這類 show-root 佈局的節目**從此必傳 `workspace_root`**,而且 bundle 路徑不再帶
  節目前綴(`attempt-inputs/ep09`,不是 `graphify-deep-dive/attempt-inputs/ep09`)。
  舊 binding 記的 `path` 是舊形狀——但那些 bundle 早已被 `manifest_workspace_sha256`
  擋在搬家前的路徑上,不受影響。
- `_other_series_manifests` 是一次 `os.walk`;workspace 是節目目錄時幾百個檔,是容器時
  幾千個,都在毫秒級。它只在**沒傳** `workspace_root` 時跑。
