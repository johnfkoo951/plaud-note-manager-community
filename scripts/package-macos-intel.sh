#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ARCH=x86_64 exec "$ROOT_DIR/scripts/package-macos-app.sh" "$@"
