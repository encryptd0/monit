import signal
import sys
import time
from types import FrameType

from monit.collector.collector import collect_metrics_json
from monit.logging.logger import ensure_log_directory, log_file_path, write_log
from monit.logging.notification import is_gui_session, notify

COLLECTION_INTERVAL_SECONDS = 5

# A monitor that fails every cycle is not monitoring anything. Exiting non-zero
# lets the service manager restart it cleanly instead of leaving a process that
# logs errors forever.
MAX_CONSECUTIVE_FAILURES = 10


def _run_cycle() -> None:
    """Collect, record, and alert once."""
    document = collect_metrics_json()

    write_log(document)

    # Notifications are best-effort: a broken desktop session must not cost
    # us the metrics that were already written.
    try:
        for alert in notify():
            print(f"monit: {alert.summary} - {alert.detail}")
    except Exception as error:
        print(f"monit: notification failed: {error!r}", file=sys.stderr)


def _stop(_signal: int, _frame: FrameType | None) -> None:
    """Turn systemd's SIGTERM into the same clean stop as Ctrl-C."""
    raise KeyboardInterrupt


def main() -> None:
    # "systemctl stop" sends SIGTERM, whose default action kills the process
    # outright, possibly mid-write. Route it through the shutdown path instead.
    signal.signal(signal.SIGTERM, _stop)

    # Fail before the loop starts if the log directory is unusable, rather
    # than on the first write several seconds in.
    try:
        ensure_log_directory()
    except OSError as error:
        raise SystemExit(f"monit: {error}") from error

    print(f"monit: logging to {log_file_path()}")

    if not is_gui_session():
        print("monit: no GUI session, desktop notifications disabled")

    consecutive_failures = 0

    try:
        while True:
            try:
                _run_cycle()
            except Exception as error:
                # Transient faults are expected over a long run: a disk can
                # stop reporting I/O counters, a sensor can disappear. Keep
                # monitoring rather than dying on one bad sample.
                consecutive_failures += 1

                print(
                    f"monit: cycle failed "
                    f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                    f"{error!r}",
                    file=sys.stderr,
                )

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise SystemExit(
                        f"monit: giving up after {consecutive_failures} "
                        f"consecutive failures"
                    ) from error
            else:
                consecutive_failures = 0

            time.sleep(COLLECTION_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nmonit: stopped")
