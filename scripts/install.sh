#!/usr/bin/env bash
#
# One-command installer for monit.
#
#   curl -LsSf https://raw.githubusercontent.com/encryptd0/monit/main/scripts/install.sh | bash
#
# Runs the three install scripts in order, each with the privileges it needs:
#
#   1. config.sh          as root  service group and account, directories, env file
#   2. install-tools.sh   as you   uv, then psutil (uv installs into your home)
#   3. install-project.sh as root  application, /usr/local/bin/monit, systemd unit
#
# Options (after "bash -s --" when piping):
#
#   --enable    enable and start monit.service when the install finishes
#   --ref REF   install a branch or tag other than main
#
# Run it as your normal user, not with sudo: it elevates the two steps that
# need root and keeps uv in your own home. Running as root works too.
#
# Overrides such as MONIT_PREFIX and MONIT_LOG_DIR are honoured and forwarded
# across the sudo boundary.
#
set -euo pipefail

MONIT_REPO_URL="${MONIT_REPO_URL:-https://github.com/encryptd0/monit.git}"
MONIT_REPO_REF="${MONIT_REPO_REF:-main}"

# Point this at an existing checkout to install from it instead of cloning.
MONIT_SOURCE_DIR="${MONIT_SOURCE_DIR:-}"

ENABLE_SERVICE=0

# Settings that must survive "sudo", which otherwise resets the environment.
FORWARDED_SETTINGS=(
    MONIT_USER MONIT_GROUP MONIT_PREFIX MONIT_BIN_DIR MONIT_LOG_DIR
    MONIT_CONFIG_DIR MONIT_ENV_FILE MONIT_SYSTEMD_DIR MONIT_SERVICE_NAME
    MONIT_CREATE_USER MONIT_ADMIN_USER MONIT_ADD_ADMIN_TO_GROUP
)

say() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# --- privilege handling -----------------------------------------------------

is_root() { [[ ${EUID} -eq 0 ]]; }

# Explicit assignments rather than "sudo -E": some sudoers policies refuse to
# preserve the environment, and this works everywhere.
forwarded_assignments() {
    local name
    for name in "${FORWARDED_SETTINGS[@]}"; do
        if [[ -n "${!name:-}" ]]; then
            printf '%s=%s\n' "$name" "${!name}"
        fi
    done
}

run_as_root() {
    local -a assignments=()
    local line

    while IFS= read -r line; do
        assignments+=("$line")
    done < <(forwarded_assignments)

    if is_root; then
        env ${assignments[@]+"${assignments[@]}"} "$@"
        return
    fi

    command -v sudo >/dev/null 2>&1 ||
        die "this step needs root and sudo is not installed; re-run as root"

    sudo env ${assignments[@]+"${assignments[@]}"} "$@"
}

# --- source acquisition -----------------------------------------------------

# Returns the directory holding scripts/, cloning it if we are not already
# inside a checkout.
acquire_source() {
    if [[ -n "$MONIT_SOURCE_DIR" ]]; then
        [[ -f "$MONIT_SOURCE_DIR/scripts/config.sh" ]] ||
            die "MONIT_SOURCE_DIR=$MONIT_SOURCE_DIR has no scripts/config.sh"

        printf '%s\n' "$MONIT_SOURCE_DIR"
        return
    fi

    # When executed from a clone (rather than piped from curl) BASH_SOURCE
    # points into it, so there is nothing to download.
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"

    if [[ -n "$here" && -f "$here/scripts/config.sh" ]]; then
        printf '%s\n' "$here"
        return
    fi

    local workdir
    workdir="$(mktemp -d)"

    # Cleaned up by the caller's trap.
    printf '%s\n' "$workdir" >"${TMPDIR:-/tmp}/.monit-workdir.$$"

    if command -v git >/dev/null 2>&1; then
        say "Downloading monit ($MONIT_REPO_REF)" >&2
        note "git clone $MONIT_REPO_URL" >&2

        git clone --depth 1 --branch "$MONIT_REPO_REF" \
            "$MONIT_REPO_URL" "$workdir/monit" >&2 ||
            die "could not clone $MONIT_REPO_URL"

        printf '%s\n' "$workdir/monit"
        return
    fi

    # No git: fall back to a source tarball, which only works for GitHub URLs.
    command -v curl >/dev/null 2>&1 || die "need either git or curl installed"
    command -v tar >/dev/null 2>&1 || die "need tar installed"

    local tarball="${MONIT_REPO_URL%.git}/archive/$MONIT_REPO_REF.tar.gz"

    say "Downloading monit ($MONIT_REPO_REF)" >&2
    note "curl $tarball" >&2

    curl -LsSf "$tarball" | tar -xz -C "$workdir" ||
        die "could not download $tarball (install git and retry)"

    local extracted
    extracted="$(find "$workdir" -maxdepth 1 -mindepth 1 -type d | head -1)"

    [[ -n "$extracted" ]] || die "the downloaded archive was empty"

    printf '%s\n' "$extracted"
}

# --- steps ------------------------------------------------------------------

preflight() {
    say "Checking prerequisites"

    [[ "$(uname -s)" == "Linux" ]] ||
        die "monit collects Linux metrics and only installs on Linux"

    command -v python3 >/dev/null 2>&1 ||
        die "python3 is required (install it with your package manager)"

    note "linux, $(python3 --version 2>&1)"

    if ! command -v systemctl >/dev/null 2>&1; then
        note "systemd not found: the service step will be skipped"
    fi

    if ! is_root && ! command -v sudo >/dev/null 2>&1; then
        die "run as root, or install sudo"
    fi
}

enable_service() {
    local unit="${MONIT_SERVICE_NAME:-monit.service}"

    command -v systemctl >/dev/null 2>&1 || {
        note "no systemd, not enabling anything"
        return 0
    }

    say "Enabling $unit"

    run_as_root systemctl enable --now "$unit"
    run_as_root systemctl --no-pager --lines=0 status "$unit" || true
}

finished() {
    local unit="${MONIT_SERVICE_NAME:-monit.service}"
    local logs="${MONIT_LOG_DIR:-/var/log/monit}"
    local group="${MONIT_GROUP:-monit}"

    cat <<EOF

$(printf '\033[1mmonit is installed.\033[0m')

  monit                     run it in the foreground (Ctrl-C to stop)
  ls $logs

EOF

    if [[ "$ENABLE_SERVICE" != "1" ]]; then
        cat <<EOF
Start it as a service with:

  sudo systemctl enable --now $unit
  journalctl -u $unit -f

EOF
    fi

    # Only claim this when the group is really there: with MONIT_CREATE_USER=0
    # nothing was created and the advice would be wrong.
    if getent group "$group" >/dev/null 2>&1; then
        cat <<EOF
Reading the logs without sudo needs membership of the "$group" group, which
config.sh granted your account. Group membership is only read at login, so log
out and back in for it to apply, or run: newgrp $group
EOF
    fi
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --enable) ENABLE_SERVICE=1 ;;
            --ref)
                shift
                [[ $# -gt 0 ]] || die "--ref needs a branch or tag"
                MONIT_REPO_REF="$1"
                ;;
            -h | --help)
                sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done

    # config.sh puts this account in the monit group. Resolve it before any
    # sudo call, or it would end up being root.
    if [[ -z "${MONIT_ADMIN_USER:-}" ]] && ! is_root; then
        MONIT_ADMIN_USER="$(id -un)"
    fi

    preflight

    local source_dir
    source_dir="$(acquire_source)"

    local workdir_marker="${TMPDIR:-/tmp}/.monit-workdir.$$"
    if [[ -f "$workdir_marker" ]]; then
        # shellcheck disable=SC2064
        trap "rm -rf '$(cat "$workdir_marker")' '$workdir_marker'" EXIT
    fi

    note "source: $source_dir"

    say "Step 1 of 3: config.sh (needs root)"
    run_as_root bash "$source_dir/scripts/config.sh"

    say "Step 2 of 3: install-tools.sh"
    bash "$source_dir/scripts/install-tools.sh"

    say "Step 3 of 3: install-project.sh (needs root)"
    run_as_root bash "$source_dir/scripts/install-project.sh"

    if [[ "$ENABLE_SERVICE" == "1" ]]; then
        enable_service
    fi

    finished
}

main "$@"
