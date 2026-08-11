"""Headless-friendly helper to write storage_state.json from pasted JSON."""
from __future__ import annotations

import argparse
import json
import os
import sys

from ._atomic import prepared_replacement
from ._cookies import assert_usable_storage_state


def main() -> None:
    parser = argparse.ArgumentParser(description="Write storage_state.json from pasted JSON")
    parser.add_argument("--out", default=os.path.expanduser("~/.notebooklm/storage_state.json"))
    args = parser.parse_args()
    print("Paste storage_state JSON, then Ctrl-D:", file=sys.stderr)
    raw = sys.stdin.read().strip()
    # Validate BEFORE writing anything (no partial/garbage file on bad input).
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid storage_state JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        raise SystemExit("Invalid storage_state: must be an object with a 'cookies' list")
    # 必要-cookie 判準與 pool 落檔前的預驗證**共用同一支**(`_cookies`):各寫一份的話,
    # 上游改語義時只有其中一邊會被改到,而 tripwire 只守著 app 那一邊。
    try:
        assert_usable_storage_state(data)
    except Exception as exc:
        raise SystemExit(f"Invalid storage_state: {exc}") from None

    out = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    # 原子換檔走 `_atomic` 那一份(gotchas-files 的紅線:不准再自己寫一份)。
    # `mode=0o600` 是顯式的:憑證不繼承既有檔案的 mode,也不吃 `_NEW_FILE_MODE` 的 0644。
    with prepared_replacement(out, mode=0o600) as temporary_path:
        with open(temporary_path, "wb") as handle:
            handle.write(json.dumps(data).encode("utf-8"))
    print(f"Wrote {out} ({len(data['cookies'])} cookies)", file=sys.stderr)


if __name__ == "__main__":
    main()
