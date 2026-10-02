#!/usr/bin/env python3
"""比對兩個 notebooklm-py 版本**我們實際依賴的那塊表面**,升級時用。

為什麼要有這支:上游的 CHANGELOG 講的是「他們改了什麼」,不是「我們會不會壞」。
0.8.2 那次(見 CHANGELOG v0.9.24)兩者差很多——上游敘事的頭條是 Android backend,
而對我們真正的變化是「公開 facade 變成 ABC、web 實作搬到 `_web.*`」,以及一筆**跟
那個版本無關**的舊帳(`ReportFormat.CONCEPT_EXPLANATION` 白名單漏了)。後者只有逐項
比對 enum 值才看得到,讀 CHANGELOG 一萬次也不會出現。

用法(兩版各開一個獨立 venv,照 docs/release-checklist.md §依賴版本對帳 的紀律
——**不要**在共用 venv 上 `uv pip install X==版本`,`uv run` 會把它拉回 lock 版):

    uv venv --python 3.12 /tmp/v081 && uv pip install --python /tmp/v081/bin/python -q notebooklm-py==0.8.1
    uv venv --python 3.12 /tmp/v082 && uv pip install --python /tmp/v082/bin/python -q notebooklm-py==0.8.2
    /tmp/v081/bin/python scripts/compare_sdk_surface.py > /tmp/a.txt
    /tmp/v082/bin/python scripts/compare_sdk_surface.py > /tmp/b.txt
    diff -u /tmp/a.txt /tmp/b.txt

輸出**刻意是穩定排序的純文字**,讓 `diff` 就是報告。四個區塊各對應一種會咬人的漂移:

  ① enum 成員與**整數值** —— `enums.py` 的 map 是靜態白名單,值變了會靜默送錯參數,
     成員多了則是「上游長出能力、呼叫端看不到」。
  ② dataclass 欄位 —— 我們有幾處直取屬性(`nb.role`、`ft.char_count`)沒有 getattr 防護。
  ③ 我們實際呼叫的 method 簽名 —— 參數順序/預設值變了會靜默換語意。
  ④ 行為函式的**原始碼 hash** —— 簽名沒變但實作換掉的那種。hash 不同**不等於**行為
     不同(0.8.2 的 `_normalize_import_verification_url` 只是換成 alias),但它會逼你
     去看;真要下結論就針對那一支寫實測輸入對照,像那次用 19 個 URL 對出來一樣。

exception 階層沒有做成獨立區塊,因為它已經在 ③ 之後由 `tests/test_contracts.py` 釘住;
這裡只列名字與父類別,足夠讓 diff 顯示「有沒有人被移除或改父類別」。
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json

_ENUMS = [
    # 一律走**兩版都在**的匯出點:`ArtifactStatus` 0.8.2 搬到 `_types.enums`,但
    # `rpc.types` 兩版都 re-export —— 挑會搬家的那個路徑只會產生「模組不存在」的假訊號。
    (
        "notebooklm.rpc.types",
        [
            "AudioFormat",
            "AudioLength",
            "SharePermission",
            "GrpcStatusCode",
            "ShareViewLevel",
            "ArtifactStatus",
        ],
    ),
    (
        "notebooklm.types",
        [
            "SlideDeckFormat",
            "SlideDeckLength",
            "ReportFormat",
            "ArtifactType",
            "SourceType",
            "VideoFormat",
        ],
    ),
]

_TYPES = [
    (
        "notebooklm.types",
        [
            "Artifact",
            "Notebook",
            "Source",
            "SourceFulltext",
            "ShareStatus",
            "SharedUser",
            "AskResult",
            "GenerationStatus",
            "RelevantChunk",
        ],
    ),
    (
        "notebooklm._types.research",
        ["ResearchSource", "ResearchStart", "ResearchStatus", "ResearchTask"],
    ),
]

#: 我們真的會呼叫的東西。新增呼叫點時**請一起加進來** —— 這份清單就是「依賴表面」的定義。
_CALLS = {
    "notebooklm._artifacts:ArtifactsAPI": [
        "download_audio",
        "download_report",
        "download_slide_deck",
        "generate_audio",
        "generate_report",
        "generate_slide_deck",
        "generate_study_guide",
        "get_or_none",
        "list",
        "rename",
        "retry_failed",
        "revise_slide",
        "wait_for_completion",
    ],
    "notebooklm._sources:SourcesAPI": [
        "add_file",
        "add_url",
        "add_text",
        "delete",
        "get_fulltext",
        "list",
        "rename",
        "search",
    ],
    "notebooklm._notebooks:NotebooksAPI": ["create", "list", "get", "get_source_ids", "get_raw"],
    "notebooklm._chat:ChatAPI": ["ask"],
    "notebooklm._research:BaseResearchAPI": [
        "start",
        "poll",
        "wait_for_completion",
        "import_sources_with_verification",
    ],
    "notebooklm._sharing:SharingAPI": ["get_status", "set_users", "add_user"],
    "notebooklm.client:NotebookLMClient": ["from_storage"],
}

#: `(模組, 屬性)`;property 會自動取 fget。production code 直接 import 的私有符號都在這。
_BEHAVIOUR = [
    ("notebooklm._types.documents", "StructuredDocument.render"),
    ("notebooklm._types.documents", "StructuredDocument.text"),
    ("notebooklm._types.documents", "StructuredDocument.slice"),
    ("notebooklm.types", "SourceFulltext.find_citation_context"),
    ("notebooklm.research", "extract_report_urls"),
    ("notebooklm.research", "normalize_citation_url"),
    ("notebooklm.rpc.types", "normalize_rpc_code"),
    ("notebooklm._runtime", "is_auth_error"),
    ("notebooklm._auth.cookies", "_sanitized_auth_entries"),
    ("notebooklm._auth.psidts_recovery", "_rotation_lock_path"),
    ("notebooklm._auth.psidts_recovery", "_recover_psidts_inline"),
    ("notebooklm._auth.psidts_recovery", "_iter_routable_psidts_cookies"),
    ("notebooklm._auth.keepalive", "_file_lock_try_exclusive"),
    ("notebooklm._research", "_normalize_import_verification_url"),
    ("notebooklm._sources", "validate_search"),
]


def _resolve(module: str, dotted: str):
    obj = importlib.import_module(module)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj.fget if isinstance(obj, property) else obj


def main() -> int:
    import importlib.metadata as md

    print("### VERSION")
    print("notebooklm-py", md.version("notebooklm-py"))

    print("\n### ENUM MEMBERS + VALUES")
    for module, names in _ENUMS:
        for name in names:
            try:
                enum = getattr(importlib.import_module(module), name)
                members = {m.name: m.value for m in enum}
            except Exception as exc:  # 成員整個消失也是要看見的漂移
                print(f"{module}.{name}: UNAVAILABLE {exc!r}")
                continue
            print(f"{module}.{name}: " + json.dumps(members, sort_keys=True, ensure_ascii=False))

    print("\n### DATACLASS FIELDS / PROPS")
    for module, names in _TYPES:
        for name in names:
            try:
                t = getattr(importlib.import_module(module), name)
            except Exception as exc:
                print(f"{module}.{name}: UNAVAILABLE {exc!r}")
                continue
            if dataclasses.is_dataclass(t):
                print(
                    f"{module}.{name} fields: "
                    + json.dumps(sorted(f.name for f in dataclasses.fields(t)), ensure_ascii=False)
                )
            props = sorted(k for k, v in vars(t).items() if isinstance(v, property))
            print(f"{module}.{name} props: {props}")

    print("\n### CALL SIGNATURES")
    for path, names in _CALLS.items():
        module, cls = path.split(":")
        try:
            owner = getattr(importlib.import_module(module), cls)
        except Exception as exc:
            print(f"{path}: UNAVAILABLE {exc!r}")
            continue
        for name in names:
            func = getattr(owner, name, None)
            if func is None:
                print(f"{cls}.{name}: MISSING")
                continue
            try:
                print(f"{cls}.{name}{inspect.signature(func)}")
            except Exception as exc:
                print(f"{cls}.{name}: SIGNATURE UNAVAILABLE {exc!r}")

    print("\n### BEHAVIOUR SOURCE HASHES")
    for module, dotted in _BEHAVIOUR:
        try:
            src = inspect.getsource(_resolve(module, dotted))
        except Exception as exc:
            print(f"{module}.{dotted}: UNAVAILABLE {exc!r}")
            continue
        digest = hashlib.sha256(src.encode()).hexdigest()[:16]
        print(f"{module}.{dotted}: {digest} ({len(src.splitlines())} lines)")

    print("\n### EXCEPTION MRO")
    exceptions = importlib.import_module("notebooklm.exceptions")
    for name in sorted(dir(exceptions)):
        obj = getattr(exceptions, name)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            bases = " <- ".join(b.__name__ for b in obj.__mro__[1:] if b is not object)
            print(f"{name}: {bases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
