# Podcast Series Example

Use this shape as the `episodes` argument for `podcast_series` after the user
has reviewed and approved the season outline. Every episode needs BOTH a
non-empty `title` (names the Studio artifact + source as `EP{n:02d} {title}`,
e.g. `EP01 開場篇`) and a `brief`; a missing/blank title is rejected up front.

```json
[
  {
    "title": "開場篇",
    "brief": "介紹節目名稱、兩位主持人與本季核心問題。用繁體中文，deep-dive 風格，先建立共同語彙，不要急著下結論。"
  },
  {
    "title": "案例篇",
    "brief": "承接第一集：先回顧上一集兩個具體觀點，再進入主要案例。主持人要明確指出案例如何改變第一集的假設。"
  },
  {
    "title": "轉折篇",
    "brief": "先讀取前集音檔來源，引用前集討論中的一個張力，接著引入反例與限制條件。保持主持人人設一致。"
  },
  {
    "title": "收束篇",
    "brief": "回顧前三集的推進，整理三個可行結論與兩個仍未解的問題。最後自然預告下一季可能方向。"
  }
]
```

Example MCP call:

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"title": "開場篇", "brief": "EP01 ..."},
    {"title": "案例篇", "brief": "EP02 ..."},
    {"title": "轉折篇", "brief": "EP03 ..."},
    {"title": "收束篇", "brief": "EP04 ..."}
  ],
  "output_dir": "/tmp/notebooklm/my-series",
  "start": 1,
  "language": "zh_Hant",
  "audio_format": "deep-dive",
  "audio_length": "long",
  "wait_timeout": 1200
}
```

Resume from episode 3 (pass the FULL episodes list — titles and all — and set `start`):

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"title": "開場篇", "brief": "EP01 ..."},
    {"title": "案例篇", "brief": "EP02 ..."},
    {"title": "轉折篇", "brief": "EP03 ..."},
    {"title": "收束篇", "brief": "EP04 ..."}
  ],
  "output_dir": "/tmp/notebooklm/my-series",
  "start": 3
}
```

`start=3` assumes the same NotebookLM notebook already contains the prior source
`EP02 案例篇` (left there when episode 2 self-uploaded on a prior run).
`podcast_series` does not read the local `ep02.mp3`; local disk is only the
download / manifest location, and the season manifest is merged (episodes 1–2 are
preserved).
