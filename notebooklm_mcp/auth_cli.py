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
    data = json.loads(raw)
    if "cookies" not in data:
        raise SystemExit("Invalid storage_state: missing 'cookies' key")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    if os.name != "nt":
        os.chmod(args.out, 0o600)
    print(f"Wrote {args.out} ({len(data['cookies'])} cookies)", file=sys.stderr)


if __name__ == "__main__":
    main()
