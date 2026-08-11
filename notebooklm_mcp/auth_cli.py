"""Headless-friendly helper to write storage_state.json from pasted JSON."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from notebooklm.auth import MINIMUM_REQUIRED_COOKIES, extract_cookies_from_storage


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
    try:
        cookies = extract_cookies_from_storage(data)
        if missing := sorted(name for name in MINIMUM_REQUIRED_COOKIES if not cookies.get(name)):
            raise ValueError(f"missing or empty required cookies: {missing}")
    except Exception as exc:
        raise SystemExit(f"Invalid storage_state: {exc}") from None

    out = os.path.expanduser(args.out)
    parent = os.path.dirname(out) or "."
    os.makedirs(parent, exist_ok=True)
    encoded = json.dumps(data).encode("utf-8")
    fd, temporary_path = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(out)}.")
    replaced = False
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(encoded)
            temporary_file.flush()
            if os.name != "nt":
                os.fchmod(temporary_file.fileno(), 0o600)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, out)
        replaced = True
    finally:
        if not replaced:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    print(f"Wrote {out} ({len(data['cookies'])} cookies)", file=sys.stderr)


if __name__ == "__main__":
    main()
