#!/usr/bin/env bash
#
# Shared settings and first-time setup for a monit installation.
#
#   ./scripts/config.sh          create the service user, directories and env file
#   ./scripts/config.sh --force  rewrite the env file even if one already exists
#   ./scripts/config.sh --show   print the resolved settings and exit
#   source scripts/config.sh     load the settings only; changes nothing
#
# Every path can be overridden from the environment. That is also how these
# scripts are tested without touching a real system:
#
#   MONIT_PREFIX=/tmp/x MONIT_LOG_DIR=/tmp/x/log ./scripts/config.sh
#
set -euo pipefail

# --- settings ---------------------------------------------------------------

# Account the service runs as. It owns the log directory and the env file.
MONIT_USER="${MONIT_USER:-monit}"
MONIT_GROUP="${MONIT_GROUP:-$MONIT_USER}"

# Where the application and its virtualenv live.
MONIT_PREFIX="${MONIT_PREFIX:-/opt/monit}"

# Where the "monit" command is linked. /usr/local/bin, not /usr/bin, because
# /usr/bin belongs to the distribution's package manager.
MONIT_BIN_DIR="${MONIT_BIN_DIR:-/usr/local/bin}"

# Must match DEFAULT_LOG_DIRECTORY in src/monit/logging/logger.py.
MONIT_LOG_DIR="${MONIT_LOG_DIR:-/var/log/monit}"

# Configuration read by the service at startup via systemd's EnvironmentFile.
MONIT_CONFIG_DIR="${MONIT_CONFIG_DIR:-/etc/monit}"
MONIT_ENV_FILE="${MONIT_ENV_FILE:-$MONIT_CONFIG_DIR/monit.env}"

MONIT_SYSTEMD_DIR="${MONIT_SYSTEMD_DIR:-/etc/systemd/system}"

# System-wide location for the per-user unit, so any user can run
# "systemctl --user enable monit" to get desktop notifications.
MONIT_USER_UNIT_DIR="${MONIT_USER_UNIT_DIR:-/etc/systemd/user}"

MONIT_LOGROTATE_DIR="${MONIT_LOGROTATE_DIR:-/etc/logrotate.d}"

MONIT_SERVICE_NAME="${MONIT_SERVICE_NAME:-monit.service}"

# Set to 0 to install without creating a dedicated account.
MONIT_CREATE_USER="${MONIT_CREATE_USER:-1}"

# Human account added to the monit group so it can read and write the logs.
# Under sudo this is the account that invoked sudo, not root.
MONIT_ADMIN_USER="${MONIT_ADMIN_USER:-${SUDO_USER:-$(id -un)}}"
MONIT_ADD_ADMIN_TO_GROUP="${MONIT_ADD_ADMIN_TO_GROUP:-1}"

MONIT_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- output helpers ---------------------------------------------------------

monit::info() { printf '  %s\n' "$*"; }
monit::step() { printf '\n== %s\n' "$*"; }
monit::warn() { printf 'warning: %s\n' "$*" >&2; }
monit::die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# --- checks -----------------------------------------------------------------

monit::is_root() { [[ ${EUID} -eq 0 ]]; }

# Fail early and clearly rather than part way through with a bare EACCES.
monit::require_writable() {
    local target="$1"
    local probe="$target"

    # Walk up to the nearest existing ancestor: that is what must be writable
    # for the target to be created.
    while [[ ! -e "$probe" && "$probe" != "/" ]]; do
        probe="$(dirname "$probe")"
    done

    if [[ ! -w "$probe" ]]; then
        monit::die "cannot write to $probe (needed for $target).
  Re-run with sudo, or override the path, e.g. MONIT_PREFIX=\$HOME/.local/monit"
    fi
}

# --- setup ------------------------------------------------------------------

monit::ensure_group() {
    if [[ "$MONIT_CREATE_USER" != "1" ]]; then
        monit::info "skipping group (MONIT_CREATE_USER=0)"
        return 0
    fi

    if getent group "$MONIT_GROUP" >/dev/null 2>&1; then
        monit::info "group $MONIT_GROUP already exists"
        return 0
    fi

    if ! monit::is_root; then
        monit::warn "not root: cannot create the $MONIT_GROUP group.
  Re-run with sudo, or set MONIT_CREATE_USER=0."
        return 0
    fi

    groupadd --system "$MONIT_GROUP"

    monit::info "created system group $MONIT_GROUP"
}

monit::ensure_user() {
    if [[ "$MONIT_CREATE_USER" != "1" ]]; then
        monit::info "skipping service account (MONIT_CREATE_USER=0)"
        return 0
    fi

    if id "$MONIT_USER" >/dev/null 2>&1; then
        monit::info "service account $MONIT_USER already exists"
        return 0
    fi

    if ! monit::is_root; then
        monit::warn "not root: cannot create the $MONIT_USER account.
  Re-run with sudo, or set MONIT_CREATE_USER=0 to run as an existing user."
        return 0
    fi

    # A system account with no login shell and no home: it only runs a daemon.
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --gid "$MONIT_GROUP" "$MONIT_USER"

    monit::info "created system account $MONIT_USER (group $MONIT_GROUP)"
}

# The service writes the logs as monit:monit. Putting the human account in the
# monit group is what lets it read and manage those files without sudo.
monit::add_admin_to_group() {
    if [[ "$MONIT_ADD_ADMIN_TO_GROUP" != "1" ]]; then
        monit::info "skipping group membership (MONIT_ADD_ADMIN_TO_GROUP=0)"
        return 0
    fi

    local target="$MONIT_ADMIN_USER"

    if [[ -z "$target" || "$target" == "root" ]]; then
        monit::info "no non-root account to add (MONIT_ADMIN_USER is '$target')"
        return 0
    fi

    if ! id "$target" >/dev/null 2>&1; then
        monit::warn "account $target does not exist, not adding it to $MONIT_GROUP"
        return 0
    fi

    # usermod would fail outright if the group was never created.
    if ! getent group "$MONIT_GROUP" >/dev/null 2>&1; then
        monit::info "group $MONIT_GROUP does not exist, nothing to add $target to"
        return 0
    fi

    if id -nG "$target" 2>/dev/null | tr ' ' '\n' | grep -qx "$MONIT_GROUP"; then
        monit::info "$target is already in the $MONIT_GROUP group"
        return 0
    fi

    if ! monit::is_root; then
        monit::warn "not root: cannot add $target to $MONIT_GROUP.
  Re-run with sudo, or run: sudo usermod -aG $MONIT_GROUP $target"
        return 0
    fi

    usermod -aG "$MONIT_GROUP" "$target"

    monit::info "added $target to the $MONIT_GROUP group"

    # Group membership is read at login, so the current shell does not have it.
    monit::warn "$target must log out and back in before the new group applies.
  To use it in the current shell right now: newgrp $MONIT_GROUP"
}

# Ownership is best-effort: it only applies when running as root and the
# account exists, so unprivileged test installs still work.
monit::own() {
    local path="$1"
    local mode="$2"

    chmod "$mode" "$path"

    if monit::is_root && id "$MONIT_USER" >/dev/null 2>&1; then
        chown "$MONIT_USER:$MONIT_GROUP" "$path"
    fi
}

monit::ensure_directories() {
    local directory

    for directory in "$MONIT_PREFIX" "$MONIT_CONFIG_DIR"; do
        monit::require_writable "$directory"
        mkdir -p "$directory"
        chmod 0755 "$directory"
        monit::info "$directory"
    done

    # The service writes here as monit:monit. Mode 2775 gives the group write
    # access, and the setgid bit (the leading 2) makes every file created here
    # inherit the monit group, so members of that group keep access to logs
    # written later.
    monit::require_writable "$MONIT_LOG_DIR"
    mkdir -p "$MONIT_LOG_DIR"
    monit::own "$MONIT_LOG_DIR" 2775
    monit::info "$MONIT_LOG_DIR (mode 2775, group $MONIT_GROUP, setgid)"
}

monit::write_env_file() {
    local force="$1"

    if [[ -e "$MONIT_ENV_FILE" && "$force" != "1" ]]; then
        monit::info "$MONIT_ENV_FILE already exists, leaving it alone"
        monit::info "use --force to regenerate it (this discards any secrets in it)"
        return 0
    fi

    monit::require_writable "$MONIT_ENV_FILE"

    cat >"$MONIT_ENV_FILE" <<EOF
# monit configuration
# Generated by config.sh on $(uname -n) at $(date '+%Y-%m-%d %H:%M:%S %Z').
#
# Loaded into the service environment by systemd (EnvironmentFile). Values are
# literal: do not quote them and do not use shell expansion.

# Directory the metrics logs are written to.
MONIT_LOG_DIR=$MONIT_LOG_DIR
EOF

    monit::own "$MONIT_ENV_FILE" 0640

    monit::info "wrote $MONIT_ENV_FILE (mode 0640)"
}

monit::show_settings() {
    printf 'user        %s:%s\n' "$MONIT_USER" "$MONIT_GROUP"
    printf 'log access  %s (added to the %s group)\n' \
        "$MONIT_ADMIN_USER" "$MONIT_GROUP"
    printf 'prefix      %s\n' "$MONIT_PREFIX"
    printf 'command     %s/monit\n' "$MONIT_BIN_DIR"
    printf 'logs        %s\n' "$MONIT_LOG_DIR"
    printf 'env file    %s\n' "$MONIT_ENV_FILE"
    printf 'unit        %s/%s\n' "$MONIT_SYSTEMD_DIR" "$MONIT_SERVICE_NAME"
    printf 'source      %s\n' "$MONIT_REPO_ROOT"
}

monit::config_main() {
    local force=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) force=1 ;;
            --show)
                monit::show_settings
                return 0
                ;;
            -h | --help)
                sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) monit::die "unknown option: $1" ;;
        esac
        shift
    done

    monit::step "Settings"
    monit::show_settings

    monit::step "Service account"
    monit::ensure_group
    monit::ensure_user
    monit::add_admin_to_group

    monit::step "Directories"
    monit::ensure_directories

    monit::step "Configuration"
    monit::write_env_file "$force"

    printf '\nConfiguration complete. Next: scripts/install-tools.sh\n'
}

# Only act when executed. Sourcing this file just loads the settings above.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    monit::config_main "$@"
fi
