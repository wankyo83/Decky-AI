#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_NAME="$(basename "$ROOT_DIR")"
PARENT_DIR="$(dirname "$ROOT_DIR")"
OUTPUT_DIR="$ROOT_DIR/out"
OUTPUT_ZIP="$OUTPUT_DIR/deck-muse-plugin.zip"

mkdir -p "$OUTPUT_DIR"
rm -f "$OUTPUT_ZIP"

cd "$PARENT_DIR"

zip -r "$OUTPUT_ZIP" "$ROOT_NAME" \
  -x "$ROOT_NAME/*.zip" \
  -x "$ROOT_NAME/.git/*" \
  -x "$ROOT_NAME/out/*" \
  -x "$ROOT_NAME/.tmp_wheels/*" \
  -x "$ROOT_NAME/__pycache__/*" \
  -x "$ROOT_NAME/.venv/*"

echo "Created $OUTPUT_ZIP"
