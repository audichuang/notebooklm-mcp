#!/usr/bin/env bash
# Point the moving `latest` tag at the highest vMAJOR.MINOR.PATCH release.
#
# `latest` is an operational pointer, not a release. Immutable history stays on
# vX.Y.Z. Pre-releases (v0.9.20-rc1) and non-semver tags are ignored, so a
# mistaken older tag push cannot walk latest backwards.
#
# Usage:
#   bash scripts/retag-latest.sh          # update local tag only
#   bash scripts/retag-latest.sh --push   # also force-push to origin
#   bash scripts/retag-latest.sh --push vX.Y.Z CHECKED_COMMIT  # CI 驗過的固定目標
set -euo pipefail

push=0
if [[ "${1:-}" == "--push" ]]; then
  push=1
  shift
fi
if [[ $# -ne 0 && $# -ne 2 ]]; then
  echo "usage: $0 [--push] [vX.Y.Z checked-commit]" >&2
  exit 2
fi
newest="${1:-}"
expected_sha="${2:-}"

if git remote get-url origin >/dev/null 2>&1; then
  git fetch --tags --force origin
fi

if [[ -z "$newest" ]]; then
  newest=$(git tag -l 'v*.*.*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
fi
if [[ -z "$newest" ]]; then
  echo "no vMAJOR.MINOR.PATCH tags" >&2
  exit 1
fi
if [[ ! "$newest" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "invalid release tag: $newest" >&2
  exit 2
fi

target=$(git rev-list -n 1 "$newest")
if [[ -n "$expected_sha" && "$target" != "$expected_sha" ]]; then
  echo "release tag $newest moved: checked $expected_sha, now $target" >&2
  exit 1
fi

if git rev-parse -q --verify refs/tags/latest >/dev/null; then
  current=$(git rev-list -n 1 latest)
  if [[ "$current" == "$target" ]]; then
    echo "latest already at $newest ($target)"
    if [[ "$push" -eq 1 ]]; then
      git push --force origin refs/tags/latest
    fi
    exit 0
  fi
fi

git tag -fa latest -m "latest release: $newest" "$target"
echo "latest -> $newest ($target)"
if [[ "$push" -eq 1 ]]; then
  git push --force origin refs/tags/latest
fi
