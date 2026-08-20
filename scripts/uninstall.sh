#!/usr/bin/env bash
#
# Undo an installation. Use this when a trial goes wrong.
#
#   sudo ./scripts/uninstall.sh            remove the software, keep the logs
#   sudo ./scripts/uninstall.sh --purge    remove everything, including logs,
#                                          config, and the monit user and group
#   ./scripts/uninstall.sh --dry-run       print what would be removed
#
# Safe to run against a half-finished or already-removed install: every step
# is skipped if there is nothing to do, and a failure in one does not stop
# the rest. Nothing here can fail in a way that leaves the system worse off.
#
set -euo pipefail

# shellcheck source=scripts/config.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

PURGE=0
DRY_RUN=0

REMOVED=0
KEPT=0

# --- helpers ----------------------------------------------------------------

act() {
    if [[ "$DRY_RUN" == "1" ]]; then
        monit::info "would run: $*"
        return 0
    fi

    "$@"
}

drop_path() {
    local path="$1"
    local label="${2:-$path}"

    if [[ ! -e "$path" && ! -L "$path" ]]; then
        return 0
    fi

    if act rm -rf "$path"; then
        monit::info "removed $label"
        REMOVED=$((REMOVED + 1))
    else
        monit::warn "could not remove $path"
    fi
}

# --- steps ------------------------------------------------------------------

stop_services() {
    monit::step "Services"

    if ! command -v systemctl >/dev/null 2>&1; then
        monit::info "no systemd here"
        return 0
    fi

    if monit::is_root; then
        # "|| true": the unit may never have been enabled.
        act systemctl disable --now "$MONIT_SERVICE_NAME" 2>/dev/null || true
        monit::info "stopped and disabled $MONIT_SERVICE_NAME (if it was running)"
    else
        monit::warn "not root: skipping systemctl disable"
    fi

    # The per-user copy is enabled per account, so it has to be undone by the
    # account that enabled it; root cannot reach another user's session.
    if [[ "${MONIT_ADMIN_USER:-}" == "$(id -un)" ]] || ! monit::is_root; then
        act systemctl --user disable --now "$MONIT_SERVICE_NAME" 2>/dev/null || true
        monit::info "stopped the per-user service for $(id -un) (if it was running)"
    else
        monit::info "if you enabled the user service, run as $MONIT_ADMIN_USER:
    systemctl --user disable --now $MONIT_SERVICE_NAME"
    fi
}

remove_files() {
    monit::step "Files"

    drop_path "$MONIT_SYSTEMD_DIR/$MONIT_SERVICE_NAME" "the system unit"
    drop_path "$MONIT_USER_UNIT_DIR/$MONIT_SERVICE_NAME" "the user unit"
    drop_path "$MONIT_LOGROTATE_DIR/monit" "the logrotate config"
    drop_path "$MONIT_BIN_DIR/monit" "$MONIT_BIN_DIR/monit"
    drop_path "$MONIT_PREFIX" "$MONIT_PREFIX"

    if command -v systemctl >/dev/null 2>&1 && monit::is_root; then
        act systemctl daemon-reload || true
    fi
}

remove_data() {
    monit::step "Logs and configuration"

    if [[ "$PURGE" != "1" ]]; then
        monit::info "keeping $MONIT_LOG_DIR"
        monit::info "keeping $MONIT_ENV_FILE"
        monit::info "pass --purge to delete them too"
        KEPT=2
        return 0
    fi

    drop_path "$MONIT_LOG_DIR" "$MONIT_LOG_DIR (logs)"
    drop_path "$MONIT_CONFIG_DIR" "$MONIT_CONFIG_DIR (configuration)"
}

remove_accounts() {
    monit::step "Account and group"

    if [[ "$PURGE" != "1" ]]; then
        monit::info "keeping the $MONIT_USER account and $MONIT_GROUP group"
        monit::info "pass --purge to remove them too"
        return 0
    fi

    if ! monit::is_root; then
        monit::warn "not root: cannot remove the account or group"
        return 0
    fi

    # Take the human back out of the group first, so nobody is left holding
    # membership of a group that no longer exists.
    local admin="${MONIT_ADMIN_USER:-}"

    if [[ -n "$admin" && "$admin" != "root" ]] &&
        id -nG "$admin" 2>/dev/null | tr ' ' '\n' | grep -qx "$MONIT_GROUP"; then
        act gpasswd -d "$admin" "$MONIT_GROUP" >/dev/null 2>&1 || true
        monit::info "removed $admin from the $MONIT_GROUP group"
    fi

    if id "$MONIT_USER" >/dev/null 2>&1; then
        act userdel "$MONIT_USER" 2>/dev/null ||
            monit::warn "could not delete the $MONIT_USER account"
        monit::info "deleted the $MONIT_USER account"
    fi

    # userdel removes the primary group automatically; only act if it survived.
    if getent group "$MONIT_GROUP" >/dev/null 2>&1; then
        act groupdel "$MONIT_GROUP" 2>/dev/null ||
            monit::warn "could not delete the $MONIT_GROUP group (still in use?)"
        monit::info "deleted the $MONIT_GROUP group"
    fi
}

report() {
    printf '\n'

    if [[ "$DRY_RUN" == "1" ]]; then
        printf 'Dry run: nothing was changed.\n'
        return 0
    fi

    if [[ "$PURGE" == "1" ]]; then
        printf 'monit has been completely removed.\n'
    else
        printf 'monit has been removed. Your logs and configuration were kept.\n'
        printf 'Run again with --purge to delete those as well.\n'
    fi

    if [[ "$KEPT" -gt 0 ]]; then
        printf '\n  %s\n  %s\n' "$MONIT_LOG_DIR" "$MONIT_ENV_FILE"
    fi
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge) PURGE=1 ;;
            --dry-run) DRY_RUN=1 ;;
            -h | --help)
                sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) monit::die "unknown option: $1" ;;
        esac
        shift
    done

    monit::step "Target"
    monit::show_settings

    stop_services
    remove_files
    remove_data
    remove_accounts
    report
}

main "$@"
