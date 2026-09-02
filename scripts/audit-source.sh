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
    "core/distribution.py"
    "scripts/package-macos-app.sh"
    "scripts/audit-release.sh"
    "scripts/install-local.sh"
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

state_files=""
while IFS= read -r -d '' candidate; do
    relative="${candidate#"$SOURCE_ROOT"/}"
    basename="${candidate##*/}"
    case "$basename" in
        .env.example)
            ;;
        .env|.env.*|*.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.sqlite-wal|*.sqlite-shm|cookies.txt|cookie.txt|*.keychain|*.keychain-db)
            state_files+="$relative"$'\n'
            ;;
        *.mp3|*.m4a|*.wav|*.aac|*.opus|*.flac|*.mp4|*.mov)
            state_files+="$relative"$'\n'
            ;;
    esac
done < <(
    find "$SOURCE_ROOT" \
        -type d \( -name .git -o -name .build -o -name dist -o -name __pycache__ \) -prune -o \
        -type f -print0
)

if [[ -n "$state_files" ]]; then
    report_paths "Mutable state, credentials, database, or recording files are present." "$state_files"
else
    pass "No mutable state, credential files, databases, or recordings are present."
fi

rg_common=(
    --hidden
    --glob '!.git/**'
    --glob '!.build/**'
    --glob '!dist/**'
    --glob '!**/__pycache__/**'
    --glob '!scripts/audit-source.sh'
    --glob '!scripts/audit-release.sh'
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
