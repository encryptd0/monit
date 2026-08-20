#!/usr/bin/env bash
#
# Rehearse the whole install in a throwaway container before touching a real
# machine. Nothing outside the container is modified.
#
#   ./scripts/test-install.sh              install, exercise, then uninstall
#   ./scripts/test-install.sh --keep       leave the container running to poke at
#   ./scripts/test-install.sh --image IMG  try another distribution
#
# It performs a genuine root install: creates the monit group and account,
# writes to /opt, /var/log, /etc and /usr/local/bin, installs uv, runs the
# collector as the service account, forces a log rotation, and then reverts
# everything with uninstall.sh --purge.
#
# systemd itself is not exercised here: a container has no PID 1 systemd, so
# "systemctl enable" cannot be tested this way. Use a real VM for that.
#
set -euo pipefail

IMAGE="${MONIT_TEST_IMAGE:-registry.fedoraproject.org/fedora:41}"
KEEP=0
CONTAINER="monit-install-test-$$"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

runtime() {
    if command -v podman >/dev/null 2>&1; then
        printf 'podman\n'
    elif command -v docker >/dev/null 2>&1; then
        printf 'docker\n'
    else
        die "needs podman or docker installed"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --image)
            shift
            [[ $# -gt 0 ]] || die "--image needs a value"
            IMAGE="$1"
            ;;
        -h | --help)
            sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

ENGINE="$(runtime)"

# A clean copy: the host .venv points at host paths and would confuse uv.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

tar --exclude=.git --exclude=.venv --exclude=__pycache__ \
    --exclude=test-results.log -C "$REPO_ROOT" -cf - . |
    tar -xf - -C "$STAGING"

say "Rehearsing the install in $IMAGE using $ENGINE"

# -i is required: without stdin attached, "bash -s" reads an empty script,
# exits 0, and the rehearsal would report a pass having done nothing at all.
"$ENGINE" run --rm -i --name "$CONTAINER" -v "$STAGING:/src:ro,Z" "$IMAGE" bash -s <<'CONTAINER_SCRIPT'
set -euo pipefail

step() { printf '\n\033[1m-- %s\033[0m\n' "$*"; }
check() {
    if eval "$2" >/dev/null 2>&1; then
        printf '   PASS  %s\n' "$1"
    else
        printf '   FAIL  %s\n' "$1"
        FAILURES=$((FAILURES + 1))
    fi
}

FAILURES=0

step "Preparing the container"
dnf install -q -y python3 logrotate shadow-utils util-linux >/dev/null 2>&1
useradd -m tester
cp -r /src /home/tester/monit
chown -R tester /home/tester/monit
cd /home/tester/monit
export MONIT_ADMIN_USER=tester
printf '   %s, python %s\n' "$(. /etc/os-release && echo "$PRETTY_NAME")" \
    "$(python3 --version | cut -d' ' -f2)"

step "1/3 config.sh"
./scripts/config.sh 2>&1 | sed 's/^/   /' | tail -8

step "2/3 install-tools.sh"
su - tester -c "cd /home/tester/monit && ./scripts/install-tools.sh --allow-remote-install" \
    2>&1 | tail -4 | sed 's/^/   /'

step "3/3 install-project.sh"
PATH="/home/tester/.local/bin:$PATH" SUDO_USER=tester \
    ./scripts/install-project.sh 2>&1 | grep -E "wrote|logrotate.d|virtualenv|owned" | sed 's/^/   /'

step "Checking the installed system"
check "the monit command is linked"        '[ -x /usr/local/bin/monit ]'
check "the virtualenv was built"           '[ -x /opt/monit/.venv/bin/monit ]'
check "the system unit is installed"       '[ -f /etc/systemd/system/monit.service ]'
check "the user unit is installed"         '[ -f /etc/systemd/user/monit.service ]'
check "logrotate is configured"            '[ -f /etc/logrotate.d/monit ]'
check "the logrotate config parses"        'logrotate -d /etc/logrotate.d/monit'
check "the service account exists"         'id monit'
check "the group exists"                   'getent group monit'
check "the admin is in the group"          'id -nG tester | grep -qw monit'
check "the log dir is setgid+group-write"  '[ "$(stat -c %a /var/log/monit)" = 2775 ]'
check "the env file is not world-readable" '[ "$(stat -c %a /etc/monit/monit.env)" = 640 ]'

step "Running the collector as the service account"
runuser -u monit -- timeout --signal=INT 16 /usr/local/bin/monit >/dev/null 2>&1 || true
check "the human-readable log was written" '[ -s /var/log/monit/monit.log ]'
check "the machine-readable log was written" '[ -s /var/log/monit/monit.jsonl ]'
check "every jsonl line is valid json" \
    'python3 -c "import json,sys;[json.loads(l) for l in open(\"/var/log/monit/monit.jsonl\")]"'
check "a group member can read the logs" 'runuser -u tester -- cat /var/log/monit/monit.log'
printf '   %s cycles recorded\n' "$(wc -l < /var/log/monit/monit.jsonl)"

step "Forcing a log rotation"
logrotate -f /etc/logrotate.d/monit
check "the log was rotated away"     '[ -f /var/log/monit/monit.log.1 ]'
check "a fresh log was created"      '[ -f /var/log/monit/monit.log ]'
check "the fresh log is group-writable" '[ "$(stat -c %a /var/log/monit/monit.log)" = 664 ]'
runuser -u monit -- timeout --signal=INT 10 /usr/local/bin/monit >/dev/null 2>&1 || true
check "monit kept logging afterwards" '[ -s /var/log/monit/monit.jsonl ]'

step "Reverting with uninstall.sh --purge"
./scripts/uninstall.sh --purge >/dev/null 2>&1
check "the prefix is gone"        '[ ! -d /opt/monit ]'
check "the command is gone"       '[ ! -e /usr/local/bin/monit ]'
check "the units are gone"        '[ ! -f /etc/systemd/system/monit.service ]'
check "the logrotate config is gone" '[ ! -f /etc/logrotate.d/monit ]'
check "the logs are gone"         '[ ! -d /var/log/monit ]'
check "the account is gone"       '! id monit'
check "the group is gone"         '! getent group monit'

printf '\n'
if [[ "$FAILURES" -eq 0 ]]; then
    printf '\033[1mAll checks passed.\033[0m\n'
else
    printf '\033[1m%s check(s) FAILED.\033[0m\n' "$FAILURES"
fi

exit "$FAILURES"
CONTAINER_SCRIPT

status=$?

if [[ "$KEEP" == "1" ]]; then
    say "--keep was given, but the container already exited; re-run without it"
fi

if [[ "$status" -eq 0 ]]; then
    say "Rehearsal passed. The install is safe to try on a real machine."
else
    say "Rehearsal FAILED with $status problem(s). Do not install yet."
fi

exit "$status"
