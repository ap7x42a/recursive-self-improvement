#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET_PARENT=${1:-"$HOME/.local/share/skills"}
TARGET="$TARGET_PARENT/recursive-self-improvement"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$TARGET_PARENT"
if [[ -e "$TARGET" ]]; then
  mv "$TARGET" "$TARGET.backup-$STAMP"
fi
cp -a "$SOURCE_DIR" "$TARGET"
find "$TARGET" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
python3 "$TARGET/scripts/self_test.py"
VERSION=$(python3 "$TARGET/scripts/recursive_self_improve.py" --version)
printf 'Installed recursive-self-improvement v%s at %s\n' "$VERSION" "$TARGET"
