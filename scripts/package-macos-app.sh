#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Plaud Note Manager Community"
EXECUTABLE_NAME="PlaudNoteApp"
IDENTIFIER="com.cmdspace.PlaudNoteManagerCommunity"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -1)"
VERSION="${VERSION:-0.0.0}"
BUILD="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || date +%y%m%d%H%M)"
GIT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo source-archive)"
ICON_SOURCE="${ICON_SOURCE:-$ROOT_DIR/app/Resources/AppIcon.png}"
SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
DIST_DIR="$ROOT_DIR/dist"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/plaud-community-package.XXXXXX")"
STAGE_APP="$TMP_DIR/$APP_NAME.app"
STAGE_ZIP="$TMP_DIR/$APP_NAME-$VERSION-macOS-arm64.zip"
FINAL_APP="$DIST_DIR/$APP_NAME.app"
FINAL_ZIP="$DIST_DIR/$APP_NAME-$VERSION-macOS-arm64.zip"

cleanup() {
  if [[ -n "${TMP_DIR:-}" && "$TMP_DIR" == *plaud-community-package.* ]]; then
    /bin/rm -R "$TMP_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT

fail() {
  echo "error: $*" >&2
  exit 1
}

[[ "$(uname -m)" == "arm64" ]] || fail "this workshop build currently supports Apple silicon only"
[[ -f "$ICON_SOURCE" ]] || fail "missing icon source: $ICON_SOURCE"
command -v uv >/dev/null || fail "uv is required to build the release"
command -v swift >/dev/null || fail "Swift is required to build the release"

if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  DIRTY="$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all -- . ':!dist')"
  [[ -z "$DIRTY" ]] || fail "commit or stash source changes before packaging"
fi

PYTHON_BIN="$(uv python find 3.12)"
PYTHON_HOME="$(cd "$(dirname "$PYTHON_BIN")/.." && pwd)"
[[ -x "$PYTHON_HOME/bin/python3" ]] || fail "uv-managed Python 3.12 was not found"

mkdir -p "$STAGE_APP/Contents/MacOS" "$STAGE_APP/Contents/Resources/runtime/templates"

echo "Building Swift application..."
swift build \
  --package-path "$ROOT_DIR/app" \
  -c release \
  -Xswiftc -gnone \
  -Xswiftc -file-prefix-map \
  -Xswiftc "$ROOT_DIR=/SOURCE/plaud-note-manager-community" \
  -Xswiftc -debug-prefix-map \
  -Xswiftc "$ROOT_DIR=/SOURCE/plaud-note-manager-community"

BIN_DIR="$(swift build --package-path "$ROOT_DIR/app" -c release --show-bin-path)"
BUILD_BINARY="$BIN_DIR/$EXECUTABLE_NAME"
[[ -x "$BUILD_BINARY" ]] || fail "build binary not found: $BUILD_BINARY"
/usr/bin/ditto "$BUILD_BINARY" "$STAGE_APP/Contents/MacOS/$EXECUTABLE_NAME"
/usr/bin/strip -S "$STAGE_APP/Contents/MacOS/$EXECUTABLE_NAME"

while IFS= read -r bundle; do
  /usr/bin/ditto "$bundle" "$STAGE_APP/Contents/Resources/$(basename "$bundle")"
done < <(find "$BIN_DIR" -maxdepth 1 -type d -name '*.bundle' -print | sort)

echo "Embedding an isolated Python runtime..."
/usr/bin/ditto "$PYTHON_HOME" "$STAGE_APP/Contents/Resources/python"
EMBEDDED_PYTHON="$STAGE_APP/Contents/Resources/python/bin/python3"

mkdir -p "$TMP_DIR/wheels"
UV_CACHE_DIR="${UV_CACHE_DIR:-$TMP_DIR/uv-cache}" uv build \
  --project "$ROOT_DIR" --wheel --out-dir "$TMP_DIR/wheels"
WHEEL="$(find "$TMP_DIR/wheels" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -f "$WHEEL" ]] || fail "community wheel was not produced"

"$EMBEDDED_PYTHON" -I -B -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --no-cache-dir \
  --requirement "$ROOT_DIR/requirements-runtime.txt"
"$EMBEDDED_PYTHON" -I -B -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --no-cache-dir \
  --no-deps "$WHEEL"

find "$STAGE_APP/Contents/Resources/python" -type f \
  \( -name '*.pyc' -o -name 'direct_url.json' \) -delete
find "$STAGE_APP/Contents/Resources/python" -type d -name '__pycache__' -empty -delete
find "$STAGE_APP/Contents/Resources/python" -type f \
  \( -name '*.pth' -o -name 'sitecustomize.py' -o -name 'usercustomize.py' \) -delete

for template in default.md meeting.md lecture.md; do
  /usr/bin/ditto "$ROOT_DIR/templates/$template" \
    "$STAGE_APP/Contents/Resources/runtime/templates/$template"
done
/usr/bin/ditto "$ROOT_DIR/LICENSE" "$STAGE_APP/Contents/Resources/runtime/LICENSE"
/usr/bin/ditto "$ROOT_DIR/THIRD_PARTY_NOTICES.md" \
  "$STAGE_APP/Contents/Resources/runtime/THIRD_PARTY_NOTICES.md"
if [[ -f "$PYTHON_HOME/LICENSE" ]]; then
  /usr/bin/ditto "$PYTHON_HOME/LICENSE" \
    "$STAGE_APP/Contents/Resources/runtime/PYTHON_LICENSE"
fi
GRDB_LICENSE="$ROOT_DIR/app/.build/checkouts/GRDB.swift/LICENSE"
if [[ -f "$GRDB_LICENSE" ]]; then
  /usr/bin/ditto "$GRDB_LICENSE" \
    "$STAGE_APP/Contents/Resources/runtime/GRDB_LICENSE"
fi

ICONSET="$TMP_DIR/AppIcon.iconset"
mkdir -p "$ICONSET"
sips -z 16 16 "$ICON_SOURCE" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$ICON_SOURCE" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ICON_SOURCE" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$ICON_SOURCE" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$ICONSET" -o "$STAGE_APP/Contents/Resources/AppIcon.icns"

/usr/libexec/PlistBuddy -c 'Clear dict' "$STAGE_APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleName string $APP_NAME" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $APP_NAME" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleExecutable string $EXECUTABLE_NAME" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $IDENTIFIER" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $BUILD" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :PlaudGitSHA string $GIT_SHA" "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :PlaudDistributionProfile string community' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundlePackageType string APPL' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleIconFile string AppIcon' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :LSMinimumSystemVersion string 14.0' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :LSArchitecturePriority array' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :LSArchitecturePriority:0 string arm64' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :LSApplicationCategoryType string public.app-category.productivity' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :NSHighResolutionCapable bool true' "$STAGE_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :NSQuitAlwaysKeepsWindows bool false' "$STAGE_APP/Contents/Info.plist"

echo "Signing nested code and application..."
while IFS= read -r -d '' candidate; do
  if /usr/bin/file -b "$candidate" | grep -q 'Mach-O'; then
    /usr/bin/codesign --force --sign "$SIGN_IDENTITY" --timestamp=none "$candidate"
  fi
done < <(find "$STAGE_APP/Contents" -type f -print0)
/usr/bin/codesign --force --sign "$SIGN_IDENTITY" --timestamp=none "$STAGE_APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGE_APP"

echo "Creating release archive..."
(cd "$TMP_DIR" && /usr/bin/ditto -c -k --sequesterRsrc --keepParent \
  "$APP_NAME.app" "$(basename "$STAGE_ZIP")")
mkdir -p "$DIST_DIR"
if [[ -e "$FINAL_APP" || -e "$FINAL_ZIP" ]]; then
  BACKUP_DIR="$DIST_DIR/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  [[ ! -e "$FINAL_APP" ]] || mv "$FINAL_APP" "$BACKUP_DIR/"
  [[ ! -e "$FINAL_ZIP" ]] || mv "$FINAL_ZIP" "$BACKUP_DIR/"
fi
mv "$STAGE_APP" "$FINAL_APP"
mv "$STAGE_ZIP" "$FINAL_ZIP"

(cd "$DIST_DIR" && shasum -a 256 "$(basename "$FINAL_ZIP")" > SHA256SUMS)
"$ROOT_DIR/scripts/audit-release.sh" "$FINAL_APP"

echo
echo "Release ready:"
echo "  $FINAL_APP"
echo "  $FINAL_ZIP"
echo "  $DIST_DIR/SHA256SUMS"
if [[ "$SIGN_IDENTITY" == "-" ]]; then
  echo "  signature: ad hoc (not notarized; use Control-click > Open on first launch)"
else
  echo "  signature: $SIGN_IDENTITY (notarization is a separate release step)"
fi
