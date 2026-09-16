#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SOURCE_ROOT="${1:-$DEFAULT_ROOT}"

if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [source-root]" >&2
    exit 64
fi

if [[ ! -d "$SOURCE_ROOT" ]]; then
    echo "[FAIL] Source root does not exist: $SOURCE_ROOT" >&2
    exit 1
fi

SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd -P)"

if ! command -v rg >/dev/null 2>&1; then
    echo "[FAIL] ripgrep (rg) is required for the source privacy audit." >&2
    exit 1
fi

failures=0

pass() {
    printf '[PASS] %s\n' "$1"
}

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    failures=$((failures + 1))
}

report_paths() {
    local heading="$1"
    local paths="$2"
    fail "$heading"
    while IFS= read -r path; do
        [[ -n "$path" ]] && printf '       %s\n' "$path" >&2
    done <<< "$paths"
    return 0
}

echo "Auditing shareable source: $SOURCE_ROOT"

required_files=(
    "LICENSE"
    "README.md"
    "THIRD_PARTY_NOTICES.md"
    "pyproject.toml"
    "requirements-runtime.txt"
    "requirements-windows.in"
    "requirements-windows.txt"
    "core/distribution.py"
    "core/curl_auth.py"
    "core/_windows_job_runner.py"
    "core/community_models.py"
    "core/community_router.py"
    "core/provider_secrets.py"
    "core/transcribe.py"
    "scripts/package-macos-app.sh"
    "scripts/package-macos-intel.sh"
    "scripts/package-windows-portable.py"
    "scripts/audit-release.sh"
    "scripts/audit-windows-release.py"
    "scripts/install-local.sh"
    "windows_app/launcher.py"
    "windows_app/server.py"
    "windows_app/service.py"
)

missing_files=""
for relative_path in "${required_files[@]}"; do
    if [[ ! -f "$SOURCE_ROOT/$relative_path" ]]; then
        missing_files+="$relative_path"$'\n'
    fi
done
if [[ -n "$missing_files" ]]; then
    report_paths "Required release files are missing." "$missing_files"
else
    pass "Required source and release files are present."
fi

for forbidden_dir in .claude .venv .uv data downloads; do
    if [[ -e "$SOURCE_ROOT/$forbidden_dir" ]]; then
        fail "Private or mutable directory must not ship: $forbidden_dir"
    fi
done

generated_dirs=""
while IFS= read -r -d '' candidate; do
    generated_dirs+="${candidate#"$SOURCE_ROOT"/}"$'\n'
done < <(
    find "$SOURCE_ROOT" \
        -path "$SOURCE_ROOT/.git" -prune -o \
        -path "$SOURCE_ROOT/dist" -prune -o \
        -type d \( \
            -name .build -o \
            -name .pytest_cache -o \
            -name .ruff_cache -o \
            -name .mypy_cache -o \
            -name .swiftpm -o \
            -name __pycache__ \
        \) -print0 -prune
)
if [[ -n "$generated_dirs" ]]; then
    report_paths "Generated build or test cache directories must not ship." "$generated_dirs"
else
    pass "No generated build or test cache directories are present."
fi

unsafe_links=""
while IFS= read -r -d '' candidate; do
    relative="${candidate#"$SOURCE_ROOT"/}"
    raw_target="$(/usr/bin/readlink "$candidate" 2>/dev/null || true)"
    resolved_target="$(/bin/realpath "$candidate" 2>/dev/null || true)"
    if [[ "$raw_target" == /* || -z "$resolved_target" ]]; then
        unsafe_links+="$relative"$'\n'
        continue
    fi
    case "$resolved_target" in
        "$SOURCE_ROOT"|"$SOURCE_ROOT"/*)
            ;;
        *)
            unsafe_links+="$relative"$'\n'
            ;;
    esac
done < <(
    find "$SOURCE_ROOT" \
        -path "$SOURCE_ROOT/.git" -prune -o \
        -path "$SOURCE_ROOT/dist" -prune -o \
        -type d \( -name .build -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache -o -name __pycache__ \) -prune -o \
        -type l -print0
)
if [[ -n "$unsafe_links" ]]; then
    report_paths "Absolute, broken, or source-escaping symbolic links are present." "$unsafe_links"
else
    pass "All source symbolic links are relative and remain inside the project."
fi

state_files=""
while IFS= read -r -d '' candidate; do
    relative="${candidate#"$SOURCE_ROOT"/}"
    basename="${candidate##*/}"
    case "$basename" in
        .env.example)
            ;;
        .env|.env.*|*.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.sqlite-wal|*.sqlite-shm|cookies.txt|cookie.txt|*.keychain|*.keychain-db|*.pyc|*.pyo|.coverage|.DS_Store)
            state_files+="$relative"$'\n'
            ;;
        *.mp3|*.m4a|*.wav|*.aac|*.opus|*.flac|*.mp4|*.mov)
            state_files+="$relative"$'\n'
            ;;
    esac
done < <(
    find "$SOURCE_ROOT" \
        -type d \( -name .git -o -name .build -o -name dist -o -name __pycache__ \) -prune -o \
        \( -type f -o -type l \) -print0
)

if [[ -n "$state_files" ]]; then
    report_paths "Mutable state, credentials, database, or recording files are present." "$state_files"
else
    pass "No mutable state, credential files, databases, or recordings are present."
fi

rg_common=(
    --hidden
    --no-ignore
    --glob '!.git/**'
    --glob '!**/.build/**'
    --glob '!dist/**'
    --glob '!**/__pycache__/**'
    --glob '!scripts/audit-source.sh'
    --glob '!scripts/audit-release.sh'
    --glob '!scripts/audit-windows-release.py'
)

# These are deliberately high-confidence token formats. Variable names and
# short placeholders are allowed; plausible live credential values are not.
secret_pattern='-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'
secret_hits="$(cd "$SOURCE_ROOT" && rg -l -e "$secret_pattern" "${rg_common[@]}" . 2>/dev/null || true)"
if [[ -n "$secret_hits" ]]; then
    report_paths "Files contain values matching high-confidence credential formats (contents withheld)." "$secret_hits"
else
    pass "No high-confidence plaintext credentials were detected."
fi

# Plaud credentials can also be opaque values that do not resemble common API
# token formats. Scan production source and documentation for literal values;
# dynamic expressions and test fixtures remain allowed.
plaud_literal_pattern='PLAUD_(AUTHORIZATION|COOKIE|X_DEVICE_ID|X_PLD_USER)[^:=]{0,4}[:=][[:space:]]*["](([Bb]earer[[:space:]]+[A-Za-z0-9][A-Za-z0-9._~+/=-]{11,})|[a-z0-9][A-Za-z0-9._~+/=-]{11,})|([Aa]uthorization|[Cc]ookie|x-device-id|x-pld-user)[^:=]{0,6}:[[:space:]]*["](([Bb]earer[[:space:]]+[A-Za-z0-9][A-Za-z0-9._~+/=-]{11,})|[a-z0-9][A-Za-z0-9._~+/=-]{11,})'
plaud_literal_hits="$(
    cd "$SOURCE_ROOT" && \
    rg -l -e "$plaud_literal_pattern" \
        "${rg_common[@]}" \
        --glob '!**/tests/**' \
        . 2>/dev/null || true
)"
if [[ -n "$plaud_literal_hits" ]]; then
    report_paths "Production files contain plausible literal Plaud credentials (contents withheld)." "$plaud_literal_hits"
else
    pass "No plausible literal Plaud credential assignments were detected in production files."
fi

# Keep the audit source itself out of this scan so the deny-list does not flag
# its own documentation. The strings are split to avoid accidental self-hits.
owner_account="yo""hankoo"
owner_handle="johnfkoo""951"
owner_possessive="Yo""han's"
owner_korean="구""요한"
private_pattern="/Users/[A-Za-z0-9._-]+/(DEV|Projects|src)/plaud-note-manager|file:///Users/|$owner_account|$owner_handle|$owner_possessive|$owner_korean"
private_hits="$(cd "$SOURCE_ROOT" && rg -l -e "$private_pattern" "${rg_common[@]}" . 2>/dev/null || true)"
if [[ -n "$private_hits" ]]; then
    report_paths "Files contain personal identifiers or local build paths." "$private_hits"
else
    pass "No personal identifiers or local build paths were detected."
fi

if [[ -f "$SOURCE_ROOT/core/distribution.py" ]] && \
   rg -q '^COMMUNITY_EDITION[[:space:]]*=[[:space:]]*True([[:space:]]*#.*)?$' "$SOURCE_ROOT/core/distribution.py"; then
    pass "Community distribution guard is enabled."
else
    fail "core/distribution.py must set COMMUNITY_EDITION = True."
fi

quarantine_mutation_hits=""
for script in "$SOURCE_ROOT/scripts/package-macos-app.sh" "$SOURCE_ROOT/scripts/install-local.sh"; do
    if [[ -f "$script" ]] && rg -q 'xattr[^\n]*(--clear|-c|-d)[^\n]*com\.apple\.quarantine|xattr[^\n]*(--clear|-c)' "$script"; then
        quarantine_mutation_hits+="${script#"$SOURCE_ROOT"/}"$'\n'
    fi
done
if [[ -n "$quarantine_mutation_hits" ]]; then
    report_paths "Release scripts must not clear or delete quarantine metadata." "$quarantine_mutation_hits"
else
    pass "Release scripts do not clear quarantine metadata."
fi

if (( failures > 0 )); then
    printf '\nSource audit failed with %d issue(s).\n' "$failures" >&2
    exit 1
fi

printf '\nSource audit passed.\n'
