#!/usr/bin/env bash

set -euo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
APP_NAME="Plaud Note Manager Community"
DEFAULT_SOURCE="$ROOT_DIR/dist/$APP_NAME.app"
SOURCE="${1:-$DEFAULT_SOURCE}"
AUDIT_SCRIPT="$SCRIPT_DIR/audit-release.sh"
INSTALL_DIR="$HOME/Applications"
DESTINATION="$INSTALL_DIR/$APP_NAME.app"
BACKUP_ROOT="$INSTALL_DIR/$APP_NAME Backups"

if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [path-to-app-or-zip]" >&2
    exit 64
fi

die() {
    printf '[FAIL] %s\n' "$1" >&2
    exit 1
}

[[ -e "$SOURCE" ]] || die "Release artifact does not exist: $SOURCE"
[[ -f "$AUDIT_SCRIPT" ]] || die "Release audit script is missing: $AUDIT_SCRIPT"

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
    die "Install location exists but is not a directory: $INSTALL_DIR"
fi
mkdir -p "$INSTALL_DIR"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/plaud-community-install.XXXXXX")"
stage_dir="$(mktemp -d "$INSTALL_DIR/.plaud-community-staging.XXXXXX")"
staged_app="$stage_dir/$APP_NAME.app"
backup_app=""
backup_moved=0

cleanup() {
    local status=$?
    trap - EXIT

    if (( status != 0 )) && (( backup_moved == 1 )) && \
       [[ ! -e "$DESTINATION" && -n "$backup_app" && -e "$backup_app" ]]; then
        if /bin/mv "$backup_app" "$DESTINATION"; then
            printf '[ROLLBACK] Restored the previous app to %s\n' "$DESTINATION" >&2
        else
            printf '[ROLLBACK FAILED] Previous app remains at %s\n' "$backup_app" >&2
        fi
    fi

    if [[ -d "$work_dir" && "$work_dir" == */plaud-community-install.* ]]; then
        /bin/rm -R "$work_dir"
    fi
    if [[ -d "$stage_dir" && "$stage_dir" == "$INSTALL_DIR"/.plaud-community-staging.* ]]; then
        /bin/rm -R "$stage_dir"
    fi

    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

case "$SOURCE" in
    *.app)
        [[ -d "$SOURCE" ]] || die "The .app artifact is not a directory: $SOURCE"
        source_app="$SOURCE"
        ;;
    *.zip)
        [[ -f "$SOURCE" ]] || die "The zip artifact is not a file: $SOURCE"
        extract_dir="$work_dir/extracted"
        mkdir -p "$extract_dir"
        /usr/bin/ditto -x -k "$SOURCE" "$extract_dir"
        source_app="$extract_dir/$APP_NAME.app"
        [[ -d "$source_app" ]] || die "Zip does not contain '$APP_NAME.app' at its top level."
        ;;
    *)
        die "Expected a .app bundle or .zip release artifact: $SOURCE"
        ;;
esac

# ditto preserves bundle metadata and extended attributes. In particular, this
# installer never removes com.apple.quarantine or bypasses Gatekeeper.
/usr/bin/ditto "$source_app" "$staged_app"

echo "Validating the exact staged copy..."
# Do not execute a quarantined nested binary before Finder has completed the
# user's first-open consent. The build-time release audit still runs CLI help.
SKIP_EMBEDDED_CLI=1 /bin/bash "$AUDIT_SCRIPT" "$staged_app"

if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
    timestamp="$(/bin/date '+%Y%m%d-%H%M%S')"
    backup_dir="$BACKUP_ROOT/$timestamp"
    suffix=1
    while [[ -e "$backup_dir" ]]; do
        backup_dir="$BACKUP_ROOT/$timestamp-$suffix"
        suffix=$((suffix + 1))
    done
    mkdir -p "$backup_dir"
    backup_app="$backup_dir/$APP_NAME.app"
    /bin/mv "$DESTINATION" "$backup_app"
    backup_moved=1
    printf 'Previous installation backed up to:\n  %s\n' "$backup_app"
fi

if ! /bin/mv "$staged_app" "$DESTINATION"; then
    die "Could not move the staged app into $DESTINATION"
fi

backup_moved=0

printf '\nInstalled successfully:\n  %s\n' "$DESTINATION"
if [[ -n "$backup_app" ]]; then
    printf 'Recoverable backup:\n  %s\n' "$backup_app"
fi

if quarantine_value="$(/usr/bin/xattr -p com.apple.quarantine "$DESTINATION" 2>/dev/null)" && \
   [[ -n "$quarantine_value" ]]; then
    printf 'Gatekeeper quarantine is preserved. On first launch, use Finder Control-click > Open if prompted.\n'
else
    printf 'No quarantine attribute was present on the supplied artifact; the installer did not alter quarantine state.\n'
fi

printf 'No administrator privileges or system-wide installation were used.\n'
