# Podcast Series Example

Use this shape as the `episodes` argument for `podcast_series` after the user
has reviewed and approved the season outline.

```json
[
  {
    "brief": "EP01 開場：介紹節目名稱、兩位主持人與本季核心問題。用繁體中文，deep-dive 風格，先建立共同語彙，不要急著下結論。"
  },
  {
    "brief": "EP02 承接第一集：先回顧上一集兩個具體觀點，再進入主要案例。主持人要明確指出案例如何改變第一集的假設。"
  },
  {
    "brief": "EP03 轉折：先讀取前集音檔來源，引用前集討論中的一個張力，接著引入反例與限制條件。保持主持人人設一致。"
  },
  {
    "brief": "EP04 收束：回顧前三集的推進，整理三個可行結論與兩個仍未解的問題。最後自然預告下一季可能方向。"
  }
]
```

Example MCP call:

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"brief": "EP01 ..."},
    {"brief": "EP02 ..."},
    {"brief": "EP03 ..."},
    {"brief": "EP04 ..."}
  ],
  "output_dir": "/tmp/notebooklm/my-series",
  "start": 1,
  "language": "zh_Hant",
  "audio_format": "deep-dive",
  "audio_length": "long",
  "wait_timeout": 1200
}
```

Resume from episode 3:

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"brief": "EP01 ..."},
    {"brief": "EP02 ..."},
    {"brief": "EP03 ..."},
    {"brief": "EP04 ..."}
  ],
  "output_dir": "/tmp/notebooklm/my-series",
  "start": 3
}
```

`start=3` uses `/tmp/notebooklm/my-series/ep02.mp3` as the prior episode if it
exists.
