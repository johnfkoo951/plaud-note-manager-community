#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
APP_NAME="Plaud Note Manager Community"
EXPECTED_IDENTIFIER="com.cmdspace.PlaudNoteManagerCommunity"
EXPECTED_EXECUTABLE="PlaudNoteApp"
EXPECTED_PROFILE="community"
APP_PATH="${1:-$ROOT_DIR/dist/$APP_NAME.app}"
EXPECTED_ARCH="${2:-${EXPECTED_ARCH:-arm64}}"

if [[ $# -gt 2 ]]; then
    echo "Usage: $0 [path-to-app] [arm64|x86_64]" >&2
    exit 64
fi

case "$EXPECTED_ARCH" in
    arm64|x86_64) ;;
    *)
        echo "Usage: $0 [path-to-app] [arm64|x86_64]" >&2
        exit 64
        ;;
esac

die() {
    printf '[FAIL] %s\n' "$1" >&2
    exit 1
}

pass() {
    printf '[PASS] %s\n' "$1"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command is unavailable: $1"
}

for command_name in /usr/bin/grep /usr/bin/codesign /usr/bin/file /usr/bin/lipo /usr/libexec/PlistBuddy; do
    require_command "$command_name"
done

[[ -d "$APP_PATH" ]] || die "App bundle does not exist: $APP_PATH"
APP_PATH="$(cd "$(dirname "$APP_PATH")" && pwd -P)/$(basename "$APP_PATH")"

CONTENTS="$APP_PATH/Contents"
RESOURCES="$CONTENTS/Resources"
RUNTIME="$RESOURCES/runtime"
PYTHON_HOME="$RESOURCES/python"
INFO_PLIST="$CONTENTS/Info.plist"
MAIN_EXECUTABLE="$CONTENTS/MacOS/$EXPECTED_EXECUTABLE"
EMBEDDED_PYTHON="$PYTHON_HOME/bin/python3"
PYTHON_STDLIB="$(find "$PYTHON_HOME/lib" -mindepth 1 -maxdepth 1 -type d -name 'python3.*' -print | LC_ALL=C sort | tail -1)"
[[ -n "$PYTHON_STDLIB" ]] || die "Embedded Python standard library is missing."
SITE_PACKAGES="$PYTHON_STDLIB/site-packages"

echo "Auditing macOS release: $APP_PATH"

[[ -f "$INFO_PLIST" ]] || die "Info.plist is missing."
[[ -x "$MAIN_EXECUTABLE" ]] || die "Main executable is missing or not executable."
[[ -x "$EMBEDDED_PYTHON" ]] || die "Embedded Python is missing or not executable."
[[ -d "$SITE_PACKAGES/core" ]] || die "Embedded core Python package is missing."
[[ -d "$SITE_PACKAGES/cli" ]] || die "Embedded CLI Python package is missing."
[[ -f "$RUNTIME/LICENSE" ]] || die "Bundled LICENSE is missing."
[[ -f "$RUNTIME/THIRD_PARTY_NOTICES.md" ]] || die "Bundled third-party notices are missing."
[[ -f "$RUNTIME/templates/default.md" ]] || die "Default template is missing."
[[ -f "$RUNTIME/templates/meeting.md" ]] || die "Meeting template is missing."
[[ -f "$RUNTIME/templates/lecture.md" ]] || die "Lecture template is missing."
pass "Required bundle resources are present."

unsafe_links=""
while IFS= read -r -d '' candidate; do
    relative="${candidate#"$APP_PATH"/}"
    raw_target="$(/usr/bin/readlink "$candidate" 2>/dev/null || true)"
    resolved_target="$(/bin/realpath "$candidate" 2>/dev/null || true)"
    if [[ "$raw_target" == /* || -z "$resolved_target" ]]; then
        unsafe_links+="$relative"$'\n'
        continue
    fi
    case "$resolved_target" in
        "$APP_PATH"|"$APP_PATH"/*)
            ;;
        *)
            unsafe_links+="$relative"$'\n'
            ;;
    esac
done < <(find "$CONTENTS" -type l -print0)
if [[ -n "$unsafe_links" ]]; then
    printf '%s' "$unsafe_links" | /usr/bin/sed 's/^/       /' >&2
    die "Absolute, broken, or bundle-escaping symbolic links are present."
fi
pass "All bundle symbolic links are relative and remain inside the app."

plist_value() {
    /usr/libexec/PlistBuddy -c "Print :$1" "$INFO_PLIST" 2>/dev/null || true
}

assert_plist() {
    local key="$1"
    local expected="$2"
    local actual
    actual="$(plist_value "$key")"
    [[ "$actual" == "$expected" ]] || die "Info.plist $key is '$actual'; expected '$expected'."
}

assert_plist "CFBundleIdentifier" "$EXPECTED_IDENTIFIER"
assert_plist "CFBundleName" "$APP_NAME"
assert_plist "CFBundleDisplayName" "$APP_NAME"
assert_plist "CFBundleExecutable" "$EXPECTED_EXECUTABLE"
assert_plist "CFBundlePackageType" "APPL"
assert_plist "PlaudDistributionProfile" "$EXPECTED_PROFILE"
assert_plist "LSMinimumSystemVersion" "14.0"
assert_plist "LSArchitecturePriority:0" "$EXPECTED_ARCH"

bundle_version="$(plist_value "CFBundleVersion")"
short_version="$(plist_value "CFBundleShortVersionString")"
git_sha="$(plist_value "PlaudGitSHA")"
[[ "$bundle_version" =~ ^[0-9]+$ ]] || die "CFBundleVersion must contain only digits."
[[ "$short_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z]+)*$ ]] || die "CFBundleShortVersionString is not a release version: $short_version"
[[ -n "$git_sha" ]] || die "PlaudGitSHA is empty."
pass "Info.plist identity, profile, version, and architecture declarations are valid."

if ! /usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH" >/dev/null 2>&1; then
    /usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH" >&2 || true
    die "Code signature verification failed."
fi

signature_details="$(/usr/bin/codesign -dv --verbose=4 "$APP_PATH" 2>&1)"
signature_identifier="$(printf '%s\n' "$signature_details" | /usr/bin/sed -n 's/^Identifier=//p' | /usr/bin/head -n 1)"
[[ "$signature_identifier" == "$EXPECTED_IDENTIFIER" ]] || die "Signed identifier is '$signature_identifier'; expected '$EXPECTED_IDENTIFIER'."

if printf '%s\n' "$signature_details" | /usr/bin/grep -q '^Signature=adhoc$'; then
    signature_kind="ad-hoc"
    if [[ "${REQUIRE_DEVELOPER_ID:-0}" == "1" ]]; then
        die "A Developer ID signature is required, but this bundle is ad-hoc signed."
    fi
else
    signature_kind="identity-backed"
fi
pass "Code signatures are internally valid ($signature_kind signature)."

mach_o_count=0
while IFS= read -r -d '' candidate; do
    if /usr/bin/file -b "$candidate" | /usr/bin/grep -q 'Mach-O'; then
        mach_o_count=$((mach_o_count + 1))
        architectures="$(/usr/bin/lipo -archs "$candidate" 2>/dev/null || true)"
        [[ "$architectures" == "$EXPECTED_ARCH" ]] || \
            die "Mach-O file is not exclusively $EXPECTED_ARCH: ${candidate#"$APP_PATH"/} ($architectures)"
        if ! /usr/bin/codesign --verify --strict "$candidate" >/dev/null 2>&1; then
            die "Nested Mach-O signature is invalid: ${candidate#"$APP_PATH"/}"
        fi
    fi
done < <(find "$CONTENTS" -type f -print0)
(( mach_o_count > 0 )) || die "No Mach-O executables were found in the bundle."
pass "All $mach_o_count Mach-O files are $EXPECTED_ARCH and have valid signatures."

for forbidden_path in \
    "$RUNTIME/.env" \
    "$RUNTIME/data" \
    "$RUNTIME/downloads" \
    "$RUNTIME/.git" \
    "$RUNTIME/.claude"; do
    [[ ! -e "$forbidden_path" ]] || die "Private or mutable path is bundled: ${forbidden_path#"$APP_PATH"/}"
done

forbidden_files=""
while IFS= read -r -d '' candidate; do
    relative="${candidate#"$APP_PATH"/}"
    basename="${candidate##*/}"
    case "$basename" in
        .env|.env.*|*.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.sqlite-wal|*.sqlite-shm|cookies.txt|cookie.txt|*.keychain|*.keychain-db|*.pyc|*.pyo|*.pth|direct_url.json|.DS_Store)
            forbidden_files+="$relative"$'\n'
            ;;
    esac
done < <(find "$CONTENTS" \( -type f -o -type l \) -print0)
if [[ -n "$forbidden_files" ]]; then
    printf '%s' "$forbidden_files" | /usr/bin/sed 's/^/       /' >&2
    die "Private state or non-relocatable Python artifacts are bundled."
fi

recording_files="$(find "$RUNTIME" -type f \( -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.wav' -o -iname '*.aac' -o -iname '*.opus' -o -iname '*.flac' -o -iname '*.mp4' -o -iname '*.mov' \) -print)"
[[ -z "$recording_files" ]] || {
    printf '%s\n' "$recording_files" >&2
    die "Recording or video files are bundled in the runtime resources."
}
pass "No mutable databases, credential files, recordings, caches, or non-relocatable .pth files are bundled."

private_pattern='/Users/[^/]+/(DEV|Projects|src)/plaud-note-manager|file:///Users/|yohankoo|johnfkoo951|Yohan.s|구요한'
private_hits=""
while IFS= read -r -d '' candidate; do
    if /usr/bin/grep -aEq "$private_pattern" "$candidate" 2>/dev/null; then
        private_hits+="$candidate"$'\n'
    fi
done < <(find "$CONTENTS" -type f -print0)
if [[ -n "$private_hits" ]]; then
    printf '%s\n' "$private_hits" | /usr/bin/sed 's/^/       /' >&2
    die "Personal identifiers or local build paths remain in the app bundle."
fi

scan_targets=("$INFO_PLIST" "$MAIN_EXECUTABLE" "$RUNTIME" "$SITE_PACKAGES/core" "$SITE_PACKAGES/cli")
secret_pattern='-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'
secret_hits=""
for scan_target in "${scan_targets[@]}"; do
    if [[ -d "$scan_target" ]]; then
        while IFS= read -r -d '' candidate; do
            if /usr/bin/grep -aEq "$secret_pattern" "$candidate" 2>/dev/null; then
                secret_hits+="$candidate"$'\n'
            fi
        done < <(find "$scan_target" -type f -print0)
    elif /usr/bin/grep -aEq "$secret_pattern" "$scan_target" 2>/dev/null; then
        secret_hits+="$scan_target"$'\n'
    fi
done
if [[ -n "$secret_hits" ]]; then
    printf '%s\n' "$secret_hits" | /usr/bin/sed 's/^/       /' >&2
    die "A bundled application file matches a high-confidence credential format (contents withheld)."
fi
pass "No personal build paths or high-confidence plaintext credentials were detected."

plaud_literal_pattern='PLAUD_(AUTHORIZATION|COOKIE|X_DEVICE_ID|X_PLD_USER)[^:=]{0,4}[:=][[:space:]]*["](([Bb]earer[[:space:]]+[A-Za-z0-9][A-Za-z0-9._~+/=-]{11,})|[a-z0-9][A-Za-z0-9._~+/=-]{11,})|([Aa]uthorization|[Cc]ookie|x-device-id|x-pld-user)[^:=]{0,6}:[[:space:]]*["](([Bb]earer[[:space:]]+[A-Za-z0-9][A-Za-z0-9._~+/=-]{11,})|[a-z0-9][A-Za-z0-9._~+/=-]{11,})'
plaud_literal_hits=""
for scan_target in "${scan_targets[@]}"; do
    if [[ -d "$scan_target" ]]; then
        while IFS= read -r -d '' candidate; do
            if /usr/bin/grep -aEq "$plaud_literal_pattern" "$candidate" 2>/dev/null; then
                plaud_literal_hits+="$candidate"$'\n'
            fi
        done < <(find "$scan_target" -type f -print0)
    elif /usr/bin/grep -aEq "$plaud_literal_pattern" "$scan_target" 2>/dev/null; then
        plaud_literal_hits+="$scan_target"$'\n'
    fi
done
if [[ -n "$plaud_literal_hits" ]]; then
    printf '%s\n' "$plaud_literal_hits" | /usr/bin/sed 's/^/       /' >&2
    die "A bundled production file contains a plausible literal Plaud credential (contents withheld)."
fi
pass "No opaque Plaud authorization, cookie, device, or user credential literals were detected."

template_inventory="$(find "$RUNTIME/templates" -maxdepth 1 -type f -name '*.md' -exec basename {} \; | LC_ALL=C sort)"
expected_templates=$'default.md\nlecture.md\nmeeting.md'
[[ "$template_inventory" == "$expected_templates" ]] || {
    printf '       Found templates:\n%s\n' "$template_inventory" >&2
    die "Template inventory differs from the approved community allow-list."
}
pass "Only the three approved generic templates are bundled."

if [[ "${SKIP_EMBEDDED_CLI:-0}" == "1" ]]; then
    pass "Embedded CLI execution was skipped for installation-time Gatekeeper compatibility."
else
    audit_tmp="$(mktemp -d "${TMPDIR:-/tmp}/plaud-community-release-audit.XXXXXX")"
    cleanup() {
        local status=$?
        if [[ -n "${audit_tmp:-}" && -d "$audit_tmp" && "$audit_tmp" == */plaud-community-release-audit.* ]]; then
            /bin/rm -R "$audit_tmp"
        fi
        return "$status"
    }
    trap cleanup EXIT

    mkdir -p "$audit_tmp/home" "$audit_tmp/tmp" "$audit_tmp/work" "$audit_tmp/data"
    help_output="$audit_tmp/cli-help.txt"
    cert_file="$SITE_PACKAGES/certifi/cacert.pem"
    [[ -f "$cert_file" ]] || die "Bundled certifi CA file is missing."

    if ! (
        cd "$audit_tmp/work"
        /usr/bin/env -i \
            HOME="$audit_tmp/home" \
            TMPDIR="$audit_tmp/tmp" \
            PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            LANG="${LANG:-en_US.UTF-8}" \
            LC_CTYPE="${LC_CTYPE:-UTF-8}" \
            TERM="dumb" \
            NO_COLOR="1" \
            PLAUD_DISTRIBUTION_PROFILE="community" \
            PLAUD_DATA_DIR="$audit_tmp/data" \
            PLAUD_RESOURCE_ROOT="$RUNTIME" \
            PLAUD_TEMPLATES_DIR="$RUNTIME/templates" \
            PLAUD_ENV_FILE="$audit_tmp/settings.env" \
            PLAUD_KEYCHAIN_SERVICE="com.cmdspace.PlaudNoteManagerCommunity.audit" \
            PLAUD_APP_SUPPORT_ID="com.cmdspace.PlaudNoteManagerCommunity.audit" \
            SSL_CERT_FILE="$cert_file" \
            "$EMBEDDED_PYTHON" -I -B -m cli.main --help
    ) >"$help_output" 2>&1; then
        /usr/bin/sed -n '1,80p' "$help_output" >&2
        die "Embedded CLI help failed outside the source repository."
    fi

    /usr/bin/grep -Eq '(Usage|Commands|Options)' "$help_output" || die "Embedded CLI help did not contain a recognizable help page."

    if ! (
        cd "$audit_tmp/work"
        /usr/bin/env -i \
            HOME="$audit_tmp/home" \
            TMPDIR="$audit_tmp/tmp" \
            PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            PLAUD_DISTRIBUTION_PROFILE="community" \
            "$EMBEDDED_PYTHON" -I -B -c 'from core.distribution import COMMUNITY_EDITION; assert COMMUNITY_EDITION is True'
    ); then
        die "Embedded Python package is not locked to the community profile."
    fi
    pass "Embedded Python and CLI run in isolated mode without the source repository or uv."
fi

if [[ "${REQUIRE_GATEKEEPER:-0}" == "1" ]]; then
    if ! /usr/sbin/spctl --assess --type execute --verbose=2 "$APP_PATH"; then
        die "Gatekeeper assessment failed. A notarized Developer ID build may be required."
    fi
    pass "Gatekeeper assessment passed."
fi

printf '\nRelease audit passed: %s (%s, %s signature)\n' "$short_version" "$EXPECTED_ARCH" "$signature_kind"
