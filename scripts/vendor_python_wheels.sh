#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$ROOT_DIR/.tmp_wheels"
REQ_FILE="$ROOT_DIR/requirements-native.txt"
PY_MODULES_DIR="$ROOT_DIR/py_modules"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "Missing $REQ_FILE" >&2
  exit 1
fi

mkdir -p "$TMP_DIR"

get_pinned_version() {
  local package_name="$1"
  local line
  line="$(grep -E "^${package_name}==" "$REQ_FILE" || true)"
  if [[ -z "$line" ]]; then
    echo "Missing ${package_name} pin in $REQ_FILE" >&2
    exit 1
  fi
  echo "${line#*==}"
}

download_wheel() {
  local package_spec="$1"
  local py_version="$2"
  local abi="$3"

  python3 -m pip download \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version "$py_version" \
    --abi "$abi" \
    -d "$TMP_DIR" \
    "$package_spec"
}

copy_compiled_module() {
  local module_glob="$1"
  local source_dir="$2"
  local destination_dir="$3"

  local module_file
  module_file="$(find "$source_dir" -maxdepth 5 -type f -name "$module_glob" | head -n 1)"

  if [[ -z "$module_file" ]]; then
    echo "Could not find module matching $module_glob under $source_dir" >&2
    exit 1
  fi

  cp -f "$module_file" "$destination_dir/"
}

extract_and_copy_pydantic_core() {
  local py_version="$1"
  local abi="$2"
  local version="$3"

  local wheel_glob="pydantic_core-${version}-cp${py_version}-${abi}-*.whl"
  local wheel_path
  wheel_path="$(find "$TMP_DIR" -maxdepth 1 -type f -name "$wheel_glob" | head -n 1)"

  if [[ -z "$wheel_path" ]]; then
    echo "Could not find wheel matching $wheel_glob" >&2
    exit 1
  fi

  local extract_dir="$TMP_DIR/pydantic_core_${py_version}_extract"
  rm -rf "$extract_dir"
  mkdir -p "$extract_dir"
  python3 -m zipfile -e "$wheel_path" "$extract_dir"

  mkdir -p "$PY_MODULES_DIR/pydantic_core"
  copy_compiled_module "_pydantic_core.cpython-${py_version}-x86_64-linux-gnu.so" "$extract_dir" "$PY_MODULES_DIR/pydantic_core"
}

extract_and_copy_cffi_backend() {
  local py_version="$1"
  local abi="$2"
  local version="$3"

  local wheel_glob="cffi-${version}-cp${py_version}-${abi}-*.whl"
  local wheel_path
  wheel_path="$(find "$TMP_DIR" -maxdepth 1 -type f -name "$wheel_glob" | head -n 1)"

  if [[ -z "$wheel_path" ]]; then
    echo "Could not find wheel matching $wheel_glob" >&2
    exit 1
  fi

  local extract_dir="$TMP_DIR/cffi_${py_version}_extract"
  rm -rf "$extract_dir"
  mkdir -p "$extract_dir"
  python3 -m zipfile -e "$wheel_path" "$extract_dir"

  mkdir -p "$PY_MODULES_DIR"
  copy_compiled_module "_cffi_backend.cpython-${py_version}-x86_64-linux-gnu.so" "$extract_dir" "$PY_MODULES_DIR"
}

PYDANTIC_CORE_VERSION="$(get_pinned_version "pydantic-core")"
CFFI_VERSION="$(get_pinned_version "cffi")"

# Keep binaries for both currently-targeted Python ABIs used by Decky environments.
for py_version in 311 314; do
  abi="cp${py_version}"

  download_wheel "pydantic-core==${PYDANTIC_CORE_VERSION}" "$py_version" "$abi"
  extract_and_copy_pydantic_core "$py_version" "$abi" "$PYDANTIC_CORE_VERSION"

  download_wheel "cffi==${CFFI_VERSION}" "$py_version" "$abi"
  extract_and_copy_cffi_backend "$py_version" "$abi" "$CFFI_VERSION"
done

echo "Native wheels vendored successfully."
