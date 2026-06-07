"""Headless-friendly helper to write storage_state.json from pasted JSON."""
from __future__ import annotations

import argparse
import json
import os
import sys


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

    out = os.path.expanduser(args.out)
    parent = os.path.dirname(out) or "."
    os.makedirs(parent, exist_ok=True)
    # Create with 0600 from the start (no world-readable window before chmod).
    if os.name == "nt":
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f)
    else:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
    print(f"Wrote {out} ({len(data['cookies'])} cookies)", file=sys.stderr)


if __name__ == "__main__":
    main()
