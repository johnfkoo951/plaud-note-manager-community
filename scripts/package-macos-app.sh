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
ICON_ICNS_SOURCE="${ICON_ICNS_SOURCE:-$ROOT_DIR/app/Resources/AppIcon.icns}"
SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
TARGET_ARCH="${TARGET_ARCH:-arm64}"
case "$TARGET_ARCH" in
  arm64)
    SWIFT_TRIPLE="arm64-apple-macosx14.0"
    PYTHON_REQUEST="${UV_PYTHON_REQUEST:-cpython-3.12.13-macos-aarch64-none}"
    ;;
  x86_64)
    SWIFT_TRIPLE="x86_64-apple-macosx14.0"
    PYTHON_REQUEST="${UV_PYTHON_REQUEST:-cpython-3.12.13-macos-x86_64-none}"
    ;;
  *)
    echo "error: TARGET_ARCH must be arm64 or x86_64" >&2
    exit 64
    ;;
esac
DIST_DIR="$ROOT_DIR/dist"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/plaud-community-package.XXXXXX")"
STAGE_APP="$TMP_DIR/$APP_NAME.app"
STAGE_ZIP="$TMP_DIR/$APP_NAME-$VERSION-macOS-$TARGET_ARCH.zip"
FINAL_APP="$DIST_DIR/$APP_NAME.app"
FINAL_ZIP="$DIST_DIR/$APP_NAME-$VERSION-macOS-$TARGET_ARCH.zip"
SWIFT_SCRATCH="$TMP_DIR/swift-build"

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

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS packages must be built on macOS"
[[ -f "$ICON_SOURCE" ]] || fail "missing icon source: $ICON_SOURCE"
command -v uv >/dev/null || fail "uv is required to build the release"
command -v swift >/dev/null || fail "Swift is required to build the release"

if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  DIRTY="$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all -- . ':!dist')"
  [[ -z "$DIRTY" ]] || fail "commit or stash source changes before packaging"
fi

UV_CACHE_DIR="${UV_CACHE_DIR:-$TMP_DIR/uv-cache}"
UV_PYTHON_INSTALL_DIR="$TMP_DIR/uv-python"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
uv python install --no-bin "$PYTHON_REQUEST"
PYTHON_BIN="$(uv python find --managed-python "$PYTHON_REQUEST")"
PYTHON_HOME="$(cd "$(dirname "$PYTHON_BIN")/.." && pwd -P)"
[[ -x "$PYTHON_HOME/bin/python3" ]] || fail "uv-managed Python 3.12 was not found"
[[ "$(/usr/bin/lipo -archs "$PYTHON_BIN")" == "$TARGET_ARCH" ]] || \
  fail "Python architecture does not match $TARGET_ARCH: $PYTHON_BIN"

mkdir -p "$STAGE_APP/Contents/MacOS" "$STAGE_APP/Contents/Resources/runtime/templates"

echo "Building Swift application..."
swift build \
  --package-path "$ROOT_DIR/app" \
  --scratch-path "$SWIFT_SCRATCH" \
  --disable-sandbox \
  --disable-automatic-resolution \
  --triple "$SWIFT_TRIPLE" \
  -c release \
  -Xswiftc -gnone \
  -Xswiftc -file-prefix-map \
  -Xswiftc "$ROOT_DIR=/SOURCE/plaud-note-manager-community" \
  -Xswiftc -debug-prefix-map \
  -Xswiftc "$ROOT_DIR=/SOURCE/plaud-note-manager-community" \
  -Xswiftc -file-prefix-map \
  -Xswiftc "$TMP_DIR=/BUILD" \
  -Xswiftc -debug-prefix-map \
  -Xswiftc "$TMP_DIR=/BUILD"

BIN_DIR="$(swift build --package-path "$ROOT_DIR/app" --scratch-path "$SWIFT_SCRATCH" \
  --disable-sandbox --disable-automatic-resolution --triple "$SWIFT_TRIPLE" \
  -c release --show-bin-path)"
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
EMBEDDED_PYTHON_ROOT="$STAGE_APP/Contents/Resources/python"

# The uv-managed CPython build is relocatable at runtime, but its dylib ID and
# sysconfig build metadata remember the build user's installation prefix.
# Rewrite those values in the private bundle copy before code signing.
/usr/bin/install_name_tool -id '@rpath/libpython3.12.dylib' \
  "$EMBEDDED_PYTHON_ROOT/lib/libpython3.12.dylib"
SYSCONFIG_DATA="$EMBEDDED_PYTHON_ROOT/lib/python3.12/_sysconfigdata__darwin_darwin.py"
if [[ -f "$SYSCONFIG_DATA" ]]; then
  /usr/bin/sed -i '' "s#$PYTHON_HOME#/opt/plaud-community-python#g" "$SYSCONFIG_DATA"
fi

mkdir -p "$TMP_DIR/wheels"
UV_CACHE_DIR="${UV_CACHE_DIR:-$TMP_DIR/uv-cache}" uv build \
  --project "$ROOT_DIR" --wheel --out-dir "$TMP_DIR/wheels"
WHEEL="$(find "$TMP_DIR/wheels" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -f "$WHEEL" ]] || fail "community wheel was not produced"

"$EMBEDDED_PYTHON" -I -B -m pip install \
  --disable-pip-version-check \
  --break-system-packages \
  --no-compile \
  --no-cache-dir \
  --requirement "$ROOT_DIR/requirements-runtime.txt"
"$EMBEDDED_PYTHON" -I -B -m pip install \
  --disable-pip-version-check \
  --break-system-packages \
  --no-compile \
  --no-cache-dir \
  --no-deps "$WHEEL"

find "$STAGE_APP/Contents/Resources/python" -type f \
  \( -name '*.pyc' -o -name 'direct_url.json' \) -delete
find "$STAGE_APP/Contents/Resources/python" -type d -name '__pycache__' -empty -delete
find "$STAGE_APP/Contents/Resources/python" -type f \
  \( -name '*.pth' -o -name 'sitecustomize.py' -o -name 'usercustomize.py' \) -delete
while IFS= read -r -d '' metadata_dir; do
  /bin/rm -R "$metadata_dir"
done < <(find "$EMBEDDED_PYTHON_ROOT/lib/python3.12/site-packages" \
  -type d -name sboms -print0)
find "$EMBEDDED_PYTHON_ROOT/bin" -mindepth 1 -maxdepth 1 \
  ! -name 'python3.12' ! -name 'python3' ! -name 'python' -delete

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
GRDB_LICENSE="$SWIFT_SCRATCH/checkouts/GRDB.swift/LICENSE"
if [[ -f "$GRDB_LICENSE" ]]; then
  /usr/bin/ditto "$GRDB_LICENSE" \
    "$STAGE_APP/Contents/Resources/runtime/GRDB_LICENSE"
fi

if [[ -f "$ICON_ICNS_SOURCE" ]]; then
  /usr/bin/ditto "$ICON_ICNS_SOURCE" "$STAGE_APP/Contents/Resources/AppIcon.icns"
else
  ICON_PNGS="$TMP_DIR/AppIcon.pngs"
  mkdir -p "$ICON_PNGS"
  for size in 16 32 64 128 256 512 1024; do
    sips -z "$size" "$size" "$ICON_SOURCE" --out "$ICON_PNGS/icon_$size.png" >/dev/null
  done
  /usr/bin/python3 "$ROOT_DIR/scripts/make-icns.py" \
    "$ICON_PNGS" "$STAGE_APP/Contents/Resources/AppIcon.icns"
fi

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
/usr/libexec/PlistBuddy -c "Add :LSArchitecturePriority:0 string $TARGET_ARCH" "$STAGE_APP/Contents/Info.plist"
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

/usr/bin/python3 "$ROOT_DIR/scripts/update-checksums.py" "$DIST_DIR"
"$ROOT_DIR/scripts/audit-release.sh" "$FINAL_APP" "$TARGET_ARCH"

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
