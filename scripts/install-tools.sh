#!/usr/bin/env bash
#
# Install and verify the tooling monit needs: uv, then psutil via uv.
#
#   ./scripts/install-tools.sh                         install what is missing, then verify
#   ./scripts/install-tools.sh --check                 verify only; change nothing
#   ./scripts/install-tools.sh --allow-remote-install  permit the astral.sh installer,
#                                                      which downloads and runs a script
#
# Run this before scripts/install-project.sh. uv brings its own Python, so the
# system Python here does not need to satisfy the project's requires-python.
#
set -euo pipefail

# shellcheck source=scripts/config.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

CHECK_ONLY=0
ALLOW_REMOTE=0

ASTRAL_INSTALLER="https://astral.sh/uv/install.sh"

# Needed by logging/notification.py to raise desktop alerts, and to run monit
# as a service. Both are optional: the notifier degrades to doing nothing.
OPTIONAL_COMMANDS=(
    "notify-send:desktop notifications (package: libnotify)"
    "systemctl:running monit as a service (systemd)"
)

# --- uv ---------------------------------------------------------------------

monit::uv_path() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    # pipx and "pip --user" install here, which is often not on PATH yet.
    # sudo also resets PATH, so check the invoking user's home as well.
    local candidate
    for candidate in "$HOME/.local/bin/uv" \
        "${SUDO_USER:+/home/$SUDO_USER/.local/bin/uv}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

monit::install_uv() {
    local output

    if command -v pipx >/dev/null 2>&1; then
        monit::step "Installing uv with pipx"

        if pipx install uv; then
            return 0
        fi

        monit::warn "pipx install failed, trying pip"
    fi

    monit::step "Installing uv with pip"

    if output="$(python3 -m pip install --user uv 2>&1)"; then
        monit::info "installed uv with pip"
        return 0
    fi

    # Fedora, Debian and friends mark the system Python as externally managed
    # (PEP 668), which refuses pip installs outside a virtualenv.
    if printf '%s' "$output" | grep -q 'externally-managed-environment'; then
        monit::info "system Python is externally managed (PEP 668), so pip refused"
    else
        printf '%s\n' "$output" | tail -2 | sed 's/^/  /'
    fi

    if [[ "$ALLOW_REMOTE" != "1" ]]; then
        monit::warn "could not install uv from a package.
  Install it with your package manager (e.g. sudo dnf install uv), or
  re-run with --allow-remote-install to download and execute
  $ASTRAL_INSTALLER"
        return 1
    fi

    monit::step "Installing uv from $ASTRAL_INSTALLER"
    monit::info "this downloads a shell script and runs it"

    if ! command -v curl >/dev/null 2>&1; then
        monit::warn "curl is not installed, cannot fetch the installer"
        return 1
    fi

    # Piping a remote script into a shell is why this path is opt-in.
    if ! curl -LsSf "$ASTRAL_INSTALLER" | sh; then
        monit::warn "the astral.sh installer failed"
        return 1
    fi

    return 0
}

# Only the resolved uv path goes to stdout; every message goes to stderr, or
# the caller's $(...) would capture the commentary as part of the path.
monit::ensure_uv() {
    monit::step "Checking for uv" >&2

    local uv
    if uv="$(monit::uv_path)"; then
        monit::info "found $uv ($("$uv" --version))" >&2
        printf '%s\n' "$uv"
        return 0
    fi

    monit::info "uv is not installed" >&2

    if [[ "$CHECK_ONLY" == "1" ]]; then
        monit::warn "--check given, not installing"
        return 1
    fi

    monit::install_uv >&2 || return 1

    if ! uv="$(monit::uv_path)"; then
        monit::warn "uv still not found after installing; check your PATH"
        return 1
    fi

    monit::info "installed $uv" >&2
    printf '%s\n' "$uv"
}

# --- dependencies -----------------------------------------------------------

monit::sync_dependencies() {
    local uv="$1"

    monit::step "Installing project dependencies (psutil)"

    if [[ "$CHECK_ONLY" == "1" ]]; then
        monit::info "--check given, not syncing"
        return 0
    fi

    # --frozen installs exactly what uv.lock pins and never rewrites it.
    if ! ( cd "$MONIT_REPO_ROOT" && "$uv" sync --frozen ); then
        monit::warn "uv sync failed"
        return 1
    fi
}

# --- verification -----------------------------------------------------------

monit::check_python() {
    local uv="$1" label="$2" code="$3"
    local output

    if output="$( cd "$MONIT_REPO_ROOT" && "$uv" run --frozen python -c "$code" 2>&1 )"; then
        monit::info "$label: $output"
        return 0
    fi

    monit::warn "$label: FAILED - $(printf '%s' "$output" | tail -1)"
    return 1
}

# Prove the toolchain works rather than trusting exit codes from the install.
monit::verify() {
    local uv="$1"
    local failures=0

    monit::step "Verifying the installation"

    monit::check_python "$uv" "psutil" \
        'import psutil; print(psutil.__version__)' || failures=1

    monit::check_python "$uv" "python" \
        "import sys; print('.'.join(map(str, sys.version_info[:3])))" || failures=1

    monit::check_python "$uv" "sensors readable" \
        "import psutil; print('yes' if psutil.sensors_temperatures() else 'none found')" ||
        failures=1

    monit::check_python "$uv" "monit imports" \
        "import monit; print('ok')" || failures=1

    monit::check_python "$uv" "collector runs" \
        "from monit.collector.collector import collect_metrics_json; import json; \
         print(len(json.loads(collect_metrics_json())), 'sections')" || failures=1

    return "$failures"
}

monit::report_optional_commands() {
    monit::step "Optional system commands"

    local entry command purpose location

    for entry in "${OPTIONAL_COMMANDS[@]}"; do
        command="${entry%%:*}"
        purpose="${entry#*:}"

        if location="$(command -v "$command" 2>/dev/null)"; then
            monit::info "$command: $location"
        else
            monit::info "$command: not found - needed for $purpose"
        fi
    done
}

# --- entry point ------------------------------------------------------------

monit::tools_main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check) CHECK_ONLY=1 ;;
            --allow-remote-install) ALLOW_REMOTE=1 ;;
            -h | --help)
                sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) monit::die "unknown option: $1" ;;
        esac
        shift
    done

    printf 'monit tooling setup\n'
    printf '  repository: %s\n' "$MONIT_REPO_ROOT"
    printf '  system python: %s\n' "$(python3 --version 2>&1)"

    local uv
    uv="$(monit::ensure_uv)" || monit::die "uv is required; install it and re-run"

    monit::sync_dependencies "$uv" || monit::die "could not install dependencies"

    monit::verify "$uv" || monit::die "verification failed"

    monit::report_optional_commands

    printf '\nTooling ready.\nNext: sudo scripts/install-project.sh\n'
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    monit::tools_main "$@"
fi
