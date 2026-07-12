"""Thin MCP server over notebooklm-py."""

from importlib.metadata import PackageNotFoundError, version

# 單一版本來源 = pyproject(裝好後的套件 metadata)。硬編字串已 stale 過
# (0.2.7 掛到 0.2.9),靠人記得同步 bump 不可靠——直接查 metadata 根治。
try:
    __version__ = version("notebooklm-mcp")
except PackageNotFoundError:  # 未安裝、直接 import 原始碼樹的邊角情況
    __version__ = "0.0.0.dev0"
