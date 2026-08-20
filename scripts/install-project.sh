#!/usr/bin/env bash
#
# Install monit into $MONIT_PREFIX, link the command into $MONIT_BIN_DIR and
# install the systemd unit.
#
#   sudo ./scripts/install-project.sh
#   sudo ./scripts/install-project.sh --uninstall
#   ./scripts/install-project.sh --no-service    skip the systemd unit
#
# Settings come from config.sh; every path can be overridden from the
# environment. Run scripts/install-tools.sh first so uv is available.
#
set -euo pipefail

# shellcheck source=scripts/config.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

INSTALL_SERVICE=1

# Files that make up a working installation. Tests and scratch files stay out.
PAYLOAD=(src pyproject.toml uv.lock README.md)

monit::find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    # sudo resets PATH, so a per-user uv install is easy to miss.
    local candidate
    for candidate in "$HOME/.local/bin/uv" "/usr/local/bin/uv" \
        "${SUDO_USER:+/home/$SUDO_USER/.local/bin/uv}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

monit::install_logrotate() {
    local template="$MONIT_REPO_ROOT/services/monit.logrotate"

    [[ -f "$template" ]] || {
        monit::warn "no logrotate template; logs will grow without limit"
        return 0
    }

    if ! command -v logrotate >/dev/null 2>&1; then
        monit::warn "logrotate is not installed, so nothing will trim the logs.
  Install it (e.g. sudo dnf install logrotate) and re-run, or the log
  directory grows by roughly 100 MiB a day."
    fi

    monit::require_writable "$MONIT_LOGROTATE_DIR"
    mkdir -p "$MONIT_LOGROTATE_DIR"

    sed \
        -e "s|^/var/log/monit/|$MONIT_LOG_DIR/|" \
        -e "s|^    su .*|    su $MONIT_USER $MONIT_GROUP|" \
        -e "s|^    create 0664 .*|    create 0664 $MONIT_USER $MONIT_GROUP|" \
        "$template" >"$MONIT_LOGROTATE_DIR/monit"

    chmod 0644 "$MONIT_LOGROTATE_DIR/monit"

    monit::info "$MONIT_LOGROTATE_DIR/monit (daily, max 50M, keep 7)"
}

monit::install_files() {
    local item

    monit::require_writable "$MONIT_PREFIX"
    mkdir -p "$MONIT_PREFIX"

    for item in "${PAYLOAD[@]}"; do
        [[ -e "$MONIT_REPO_ROOT/$item" ]] ||
            monit::die "missing $item in $MONIT_REPO_ROOT"

        # Replace rather than merge, so a removed source file does not linger.
        rm -rf "${MONIT_PREFIX:?}/$item"
        cp -R "$MONIT_REPO_ROOT/$item" "$MONIT_PREFIX/$item"

        monit::info "$item -> $MONIT_PREFIX/$item"
    done
}

monit::build_venv() {
    local uv
    uv="$(monit::find_uv)" ||
        monit::die "uv not found. Run scripts/install-tools.sh first."

    monit::info "using $uv"

    # --frozen installs exactly what uv.lock pins. --no-dev keeps test-only
    # dependencies out of a production install.
    ( cd "$MONIT_PREFIX" && "$uv" sync --frozen --no-dev ) ||
        monit::die "uv sync failed in $MONIT_PREFIX"

    [[ -x "$MONIT_PREFIX/.venv/bin/monit" ]] ||
        monit::die "expected $MONIT_PREFIX/.venv/bin/monit after uv sync"

    monit::info "virtualenv ready at $MONIT_PREFIX/.venv"
}

monit::link_command() {
    monit::require_writable "$MONIT_BIN_DIR"
    mkdir -p "$MONIT_BIN_DIR"

    # The console script has an absolute shebang, so a symlink is enough.
    ln -sfn "$MONIT_PREFIX/.venv/bin/monit" "$MONIT_BIN_DIR/monit"

    monit::info "$MONIT_BIN_DIR/monit -> $MONIT_PREFIX/.venv/bin/monit"
}

monit::set_ownership() {
    if ! monit::is_root; then
        monit::info "not root: leaving ownership as $(id -un)"
        return 0
    fi

    if ! id "$MONIT_USER" >/dev/null 2>&1; then
        monit::warn "account $MONIT_USER does not exist; run scripts/config.sh"
        return 0
    fi

    chown -R "$MONIT_USER:$MONIT_GROUP" "$MONIT_PREFIX"

    monit::info "$MONIT_PREFIX owned by $MONIT_USER:$MONIT_GROUP"
}

monit::install_unit() {
    if [[ "$INSTALL_SERVICE" != "1" ]]; then
        monit::info "skipping systemd unit (--no-service)"
        return 0
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        monit::info "systemd not present, skipping the unit"
        return 0
    fi

    local template="$MONIT_REPO_ROOT/services/$MONIT_SERVICE_NAME"

    [[ -f "$template" ]] || monit::die "missing unit template: $template"

    monit::require_writable "$MONIT_SYSTEMD_DIR"
    mkdir -p "$MONIT_SYSTEMD_DIR"

    # services/monit.service is the single source of truth; only the paths are
    # substituted here, so the two never drift apart. Each expression is
    # anchored to the start of a line so only the directive is replaced.
    sed \
        -e "s|^User=.*|User=$MONIT_USER|" \
        -e "s|^Group=.*|Group=$MONIT_GROUP|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$MONIT_PREFIX|" \
        -e "s|^EnvironmentFile=-.*|EnvironmentFile=-$MONIT_ENV_FILE|" \
        -e "s|^ExecStart=.*|ExecStart=$MONIT_PREFIX/.venv/bin/monit|" \
        -e "s|^ReadWritePaths=.*|ReadWritePaths=$MONIT_LOG_DIR|" \
        "$template" >"$MONIT_SYSTEMD_DIR/$MONIT_SERVICE_NAME"

    chmod 0644 "$MONIT_SYSTEMD_DIR/$MONIT_SERVICE_NAME"

    # The per-user variant, for desktop notifications. Installed alongside but
    # never enabled automatically; the two are alternatives, not a pair.
    local user_template="$MONIT_REPO_ROOT/services/monit-user.service"

    if [[ -f "$user_template" ]]; then
        mkdir -p "$MONIT_USER_UNIT_DIR"

        sed \
            -e "s|^WorkingDirectory=.*|WorkingDirectory=$MONIT_PREFIX|" \
            -e "s|^EnvironmentFile=-.*|EnvironmentFile=-$MONIT_ENV_FILE|" \
            -e "s|^ExecStart=.*|ExecStart=$MONIT_PREFIX/.venv/bin/monit|" \
            -e "s|^ReadWritePaths=.*|ReadWritePaths=$MONIT_LOG_DIR|" \
            "$user_template" >"$MONIT_USER_UNIT_DIR/$MONIT_SERVICE_NAME"

        chmod 0644 "$MONIT_USER_UNIT_DIR/$MONIT_SERVICE_NAME"

        monit::info "wrote $MONIT_USER_UNIT_DIR/$MONIT_SERVICE_NAME (user variant)"
    fi

    monit::info "wrote $MONIT_SYSTEMD_DIR/$MONIT_SERVICE_NAME"

    if monit::is_root; then
        # Not fatal: this fails in containers and chroots where systemd is not
        # running as PID 1, and the rest of the install is still valid there.
        if systemctl daemon-reload 2>/dev/null; then
            monit::info "reloaded systemd"
        else
            monit::warn "could not reload systemd (not booted with it?).
  Run 'systemctl daemon-reload' yourself before starting the service."
        fi
    fi
}

monit::uninstall() {
    monit::step "Removing monit"

    if command -v systemctl >/dev/null 2>&1 && monit::is_root; then
        systemctl disable --now "$MONIT_SERVICE_NAME" 2>/dev/null || true
    fi

    rm -f "$MONIT_SYSTEMD_DIR/$MONIT_SERVICE_NAME"
    rm -f "$MONIT_BIN_DIR/monit"
    rm -rf "${MONIT_PREFIX:?}"

    monit::info "removed the unit, the command and $MONIT_PREFIX"
    monit::info "kept $MONIT_LOG_DIR and $MONIT_ENV_FILE; delete them by hand"

    printf '\nUninstalled.\n'
}

monit::install_main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-service) INSTALL_SERVICE=0 ;;
            --uninstall)
                monit::uninstall
                return 0
                ;;
            -h | --help)
                sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) monit::die "unknown option: $1" ;;
        esac
        shift
    done

    monit::step "Target"
    monit::show_settings

    monit::step "Copying files"
    monit::install_files

    monit::step "Building the virtualenv"
    monit::build_venv

    monit::step "Linking the command"
    monit::link_command

    monit::step "Ownership"
    monit::set_ownership

    monit::step "Service"
    monit::install_unit

    monit::step "Log rotation"
    monit::install_logrotate

    cat <<EOF

Installed. Check it with:

  $MONIT_BIN_DIR/monit          run in the foreground, Ctrl-C to stop

Then enable the service:

  sudo systemctl enable --now $MONIT_SERVICE_NAME
  journalctl -u $MONIT_SERVICE_NAME -f
  ls $MONIT_LOG_DIR

Note: desktop notifications need a user session bus, which a system service
does not have. Alerts are written to the log either way.
EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    monit::install_main "$@"
fi
