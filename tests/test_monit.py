"""End-to-end checks for monit.

Run it directly; there is no test framework to install:

    uv run python tests/test_monit.py

Results are written to test-results.log in the project root as well as to the
terminal. Exits non-zero if anything fails, so it can gate a deploy.

Nothing here touches /var/log/monit or sends a real desktop notification:
every log write goes to a temporary directory and the notifier's sender is
replaced with a recorder.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = PROJECT_ROOT / "test-results.log"

import monit
from monit.collector.collector import collect_metrics_json
from monit.collector.cpu import get_cpu_metrics
from monit.collector.disk import get_disk_metrics
from monit.collector.hardware_temps import (
    _cpu_temperature_from,
    get_hardware_temperatures,
)
from monit.collector.host_info import get_host_info
from monit.collector.memory import get_memory_metrics
from monit.collector.network import get_network_metrics
from monit.logging import logger, notification

# psutil's sensor tuple, rebuilt here so sensor logic can be tested on any
# machine rather than only on hardware that happens to have the right chips.
SensorReading = namedtuple("SensorReading", "label current high critical")

# A collection blocks for ~2.2s, so it is done once and shared.
_DOCUMENT: str | None = None


def metrics_document() -> str:
    global _DOCUMENT

    if _DOCUMENT is None:
        _DOCUMENT = collect_metrics_json()

    return _DOCUMENT


class TestCollector(unittest.TestCase):
    """The collector produces the JSON shape everything downstream assumes."""

    def test_document_is_valid_json(self) -> None:
        json.loads(metrics_document())

    def test_document_is_a_single_line(self) -> None:
        # The .jsonl log depends on one document occupying exactly one line.
        self.assertNotIn("\n", metrics_document())

    def test_top_level_schema(self) -> None:
        document: dict[str, Any] = json.loads(metrics_document())

        self.assertEqual(
            sorted(document),
            [
                "cpu",
                "disk",
                "hardware",
                "host",
                "memory",
                "network",
                "processes",
                "timestamp",
            ],
        )

    def test_timestamp_is_parseable(self) -> None:
        document: dict[str, Any] = json.loads(metrics_document())

        datetime.strptime(document["timestamp"], "%Y-%m-%d %H:%M:%S")

    def test_cpu_percentages_are_in_range(self) -> None:
        cpu = get_cpu_metrics()

        self.assertGreaterEqual(cpu.usage_percent, 0.0)
        self.assertLessEqual(cpu.usage_percent, 100.0)

        for core in cpu.per_core_usage:
            self.assertGreaterEqual(core, 0.0)
            self.assertLessEqual(core, 100.0)

    def test_memory_totals_are_consistent(self) -> None:
        memory = get_memory_metrics()

        self.assertGreater(memory.virtual_memory.total, 0)
        self.assertLessEqual(
            memory.virtual_memory.used,
            memory.virtual_memory.total,
        )

    def test_host_info_is_populated(self) -> None:
        host = get_host_info()

        self.assertTrue(host.hostname)
        self.assertEqual(host.operating_system, "Linux")
        self.assertGreater(host.uptime_seconds, 0)

    def test_network_interfaces_are_listed(self) -> None:
        interfaces = get_network_metrics()

        self.assertTrue(interfaces, "expected at least a loopback interface")
        self.assertIn("lo", [interface.name for interface in interfaces])


class TestDiskRateState(unittest.TestCase):
    """Per-second rates are deltas against the previous call, not absolutes."""

    def test_rates_are_non_negative(self) -> None:
        get_disk_metrics()
        io = get_disk_metrics().io

        self.assertGreaterEqual(io.read_bytes_per_sec, 0.0)
        self.assertGreaterEqual(io.write_bytes_per_sec, 0.0)

    def test_usage_percent_is_sane(self) -> None:
        usage = get_disk_metrics().usage

        self.assertGreater(usage.total, 0)
        self.assertGreaterEqual(usage.percent, 0.0)
        self.assertLessEqual(usage.percent, 100.0)

    def test_first_call_reports_zero_rates(self) -> None:
        # A fresh baseline has nothing to diff against.
        with mock.patch.object(monit.collector.disk, "_previous_io", None), \
             mock.patch.object(monit.collector.disk, "_previous_timestamp", None):
            io = get_disk_metrics().io

        self.assertEqual(io.read_bytes_per_sec, 0.0)
        self.assertEqual(io.write_bytes_per_sec, 0.0)


class TestTemperatureAttribution(unittest.TestCase):
    """cpu_temperature must be the CPU, not the hottest part in the machine."""

    def test_prefers_cpu_chip_over_hotter_drive(self) -> None:
        sensors = {
            "nvme": [SensorReading("Composite", 78.0, None, None)],
            "coretemp": [
                SensorReading("Package id 0", 45.0, None, None),
                SensorReading("Core 0", 44.0, None, None),
            ],
        }

        self.assertEqual(_cpu_temperature_from(sensors), 45.0)

    def test_prefers_package_over_individual_cores(self) -> None:
        sensors = {
            "coretemp": [
                SensorReading("Core 0", 70.0, None, None),
                SensorReading("Package id 0", 55.0, None, None),
            ]
        }

        self.assertEqual(_cpu_temperature_from(sensors), 55.0)

    def test_falls_back_to_hottest_core_without_package_label(self) -> None:
        sensors = {
            "coretemp": [
                SensorReading("Core 0", 44.0, None, None),
                SensorReading("Core 1", 51.0, None, None),
            ]
        }

        self.assertEqual(_cpu_temperature_from(sensors), 51.0)

    def test_supports_amd_naming(self) -> None:
        sensors = {
            "nvme": [SensorReading("Composite", 60.0, None, None)],
            "k10temp": [SensorReading("Tctl", 52.0, None, None)],
        }

        self.assertEqual(_cpu_temperature_from(sensors), 52.0)

    def test_returns_none_when_no_cpu_sensor_exists(self) -> None:
        sensors = {"nvme": [SensorReading("Composite", 60.0, None, None)]}

        self.assertIsNone(_cpu_temperature_from(sensors))

    def test_handles_absent_sensors(self) -> None:
        self.assertIsNone(_cpu_temperature_from({}))

    def test_collector_actually_uses_cpu_attribution(self) -> None:
        # Guards the wiring, not just the helper: a machine-wide max() inside
        # get_hardware_temperatures would report the drive at 78C as the CPU.
        sensors = {
            "nvme": [SensorReading("Composite", 78.0, None, None)],
            "coretemp": [SensorReading("Package id 0", 45.0, None, None)],
        }

        with mock.patch("psutil.sensors_temperatures", return_value=sensors):
            self.assertEqual(get_hardware_temperatures().cpu_temperature, 45.0)

    def test_collector_reports_fastest_fan(self) -> None:
        fans = {
            "dell_smm": [
                SensorReading(None, 0, None, None),
                SensorReading(None, 3200, None, None),
            ]
        }

        with mock.patch("psutil.sensors_fans", return_value=fans):
            self.assertEqual(get_hardware_temperatures().fan_speed, 3200)

    def test_real_hardware_reading_is_plausible(self) -> None:
        hardware = get_hardware_temperatures()

        if hardware.cpu_temperature is not None:
            self.assertGreater(hardware.cpu_temperature, 0.0)
            self.assertLess(hardware.cpu_temperature, 150.0)


class TestLogFormatting(unittest.TestCase):
    """format_log renders syslog-style lines and never raises on odd input."""

    def test_lines_use_syslog_shape(self) -> None:
        rendered = logger.format_log(metrics_document())

        for line in rendered.splitlines():
            self.assertRegex(line, r"^[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} ")
            self.assertIn("monit[", line)

    def test_expected_subsystems_are_present(self) -> None:
        rendered = logger.format_log(metrics_document())

        for subsystem in ("host:", "cpu:", "memory:", "disk:", "net:"):
            self.assertIn(subsystem, rendered)

    def test_severity_escalates_with_usage(self) -> None:
        self.assertEqual(logger._severity(10.0), "INFO")
        self.assertEqual(logger._severity(85.0), "WARN")
        self.assertEqual(logger._severity(95.0), "CRIT")

    def test_severity_boundaries(self) -> None:
        self.assertEqual(logger._severity(logger.WARNING_PERCENT), "WARN")
        self.assertEqual(logger._severity(logger.CRITICAL_PERCENT), "CRIT")
        self.assertEqual(logger._severity(logger.WARNING_PERCENT - 0.1), "INFO")

    def test_human_bytes_scales_units(self) -> None:
        self.assertEqual(logger._human_bytes(0), "0.0B")
        self.assertEqual(logger._human_bytes(2048), "2.0KiB")
        self.assertEqual(logger._human_bytes(1048576), "1.0MiB")

    def test_human_duration_formats(self) -> None:
        self.assertEqual(logger._human_duration(90), "1m")
        self.assertEqual(logger._human_duration(3660), "1h1m")
        self.assertEqual(logger._human_duration(90000), "1d1h0m")

    def test_syslog_timestamp_pads_single_digit_days(self) -> None:
        self.assertEqual(
            logger._syslog_timestamp("2026-08-05 07:01:02"),
            "Aug  5 07:01:02",
        )

    def test_syslog_timestamp_falls_back_on_bad_input(self) -> None:
        self.assertEqual(logger._syslog_timestamp("nonsense"), "nonsense")

    def test_empty_document_does_not_raise(self) -> None:
        self.assertTrue(logger.format_log("{}"))


class TestLogWriting(unittest.TestCase):
    """write_log creates the directory and keeps both files in step."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name) / "monit"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_creates_missing_directory(self) -> None:
        self.assertFalse(self.directory.exists())

        logger.write_log(metrics_document(), self.directory)

        self.assertTrue(self.directory.is_dir())

    def test_log_files_have_stable_names(self) -> None:
        # logrotate rotates a fixed path. Per-run filenames would defeat it and
        # let the log directory grow without limit.
        self.assertEqual(logger.log_file_path(self.directory).name, "monit.log")
        self.assertEqual(
            logger.jsonl_file_path(self.directory).name, "monit.jsonl"
        )

    def test_writes_both_log_and_jsonl(self) -> None:
        logger.write_log(metrics_document(), self.directory)

        self.assertTrue(logger.log_file_path(self.directory).exists())
        self.assertTrue(logger.jsonl_file_path(self.directory).exists())

    def test_jsonl_holds_one_document_per_line(self) -> None:
        for _ in range(3):
            logger.write_log(metrics_document(), self.directory)

        lines = (
            logger.jsonl_file_path(self.directory)
            .read_text(encoding="utf-8")
            .splitlines()
        )

        self.assertEqual(len(lines), 3)

        for line in lines:
            json.loads(line)

    def test_repeated_writes_append_to_one_file(self) -> None:
        for _ in range(3):
            logger.write_log(metrics_document(), self.directory)

        self.assertEqual(len(list(self.directory.glob("*.jsonl"))), 1)
        self.assertEqual(len(list(self.directory.glob("*.log"))), 1)

    def test_both_files_describe_the_same_collection(self) -> None:
        logger.write_log(metrics_document(), self.directory)

        document: dict[str, Any] = json.loads(metrics_document())
        temperature = document["hardware"]["cpu_temperature"]

        if temperature is None:
            self.skipTest("no temperature sensor on this machine")

        text = logger.log_file_path(self.directory).read_text(encoding="utf-8")

        self.assertIn(f"cpu_temp={float(temperature):.1f}C", text)

    def test_unwritable_directory_explains_itself(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")

        readonly = Path(self._temporary.name) / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)

        try:
            with self.assertRaises(PermissionError) as caught:
                logger.ensure_log_directory(readonly / "monit")
        finally:
            # Restore before tearDown removes the temporary directory.
            readonly.chmod(0o700)

        self.assertIn("MONIT_LOG_DIR", str(caught.exception))


class TestGuiDetection(unittest.TestCase):
    """Notifications must be silently skipped without a desktop session."""

    GUI_VARIABLES = (
        "XDG_SESSION_TYPE",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_CURRENT_DESKTOP",
        "DESKTOP_SESSION",
    )

    def session(self, **environment: str) -> Any:
        cleared = {name: "" for name in self.GUI_VARIABLES}

        return mock.patch.dict(
            "os.environ",
            {**cleared, **environment},
            clear=False,
        )

    def test_gnome_wayland_is_a_gui(self) -> None:
        with self.session(
            XDG_SESSION_TYPE="wayland",
            WAYLAND_DISPLAY="wayland-0",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
        ):
            self.assertTrue(notification.is_gui_session())

    def test_kde_x11_is_a_gui(self) -> None:
        with self.session(
            XDG_SESSION_TYPE="x11",
            DISPLAY=":0",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
        ):
            self.assertTrue(notification.is_gui_session())

    def test_bare_tty_is_not_a_gui(self) -> None:
        with self.session(XDG_SESSION_TYPE="tty"):
            self.assertFalse(notification.is_gui_session())

    def test_tty_wins_over_leaked_display_variables(self) -> None:
        with self.session(
            XDG_SESSION_TYPE="tty",
            DISPLAY=":0",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
        ):
            self.assertFalse(notification.is_gui_session())

    def test_ssh_without_display_is_not_a_gui(self) -> None:
        with self.session(SSH_CONNECTION="10.0.0.1 22 10.0.0.2 22"):
            self.assertFalse(notification.is_gui_session())

    def test_display_without_a_session_bus_is_not_a_gui(self) -> None:
        with self.session(XDG_SESSION_TYPE="x11", DISPLAY=":0"):
            self.assertFalse(notification.is_gui_session())

    def test_desktop_environment_is_reported(self) -> None:
        with self.session(XDG_CURRENT_DESKTOP="GNOME"):
            self.assertEqual(notification.desktop_environment(), "GNOME")

        with self.session():
            self.assertEqual(notification.desktop_environment(), "unknown")


class TestAlertDetection(unittest.TestCase):
    """Thresholds fire when they should and stay quiet when they should not."""

    def document(self, **overrides: Any) -> str:
        return json.dumps(
            {
                "timestamp": "2026-08-20 10:00:00",
                "host": {"hostname": "testhost"},
                "cpu": {"usage_percent": overrides.get("cpu", 5.0)},
                "memory": {
                    "virtual_memory": {"percent": overrides.get("memory", 10.0)}
                },
                "disk": {"usage": {"percent": overrides.get("disk", 20.0)}},
                "hardware": {
                    "cpu_temperature": overrides.get("temperature"),
                    "fan_speed": 1200,
                },
            }
        )

    def keys(self, **overrides: Any) -> list[str]:
        return [
            alert.key
            for alert in notification.detect_alerts(self.document(**overrides))
        ]

    def test_healthy_system_raises_nothing(self) -> None:
        self.assertEqual(self.keys(temperature=45.0), [])

    def test_temperature_above_limit_alerts(self) -> None:
        self.assertIn("cpu_temperature", self.keys(temperature=97.4))

    def test_temperature_exactly_at_limit_does_not_alert(self) -> None:
        # The threshold is "exceeds", so equality must stay quiet.
        self.assertEqual(
            self.keys(
                temperature=notification.CRITICAL_TEMPERATURE_CELSIUS
            ),
            [],
        )

    def test_temperature_just_above_limit_alerts(self) -> None:
        self.assertIn(
            "cpu_temperature",
            self.keys(
                temperature=notification.CRITICAL_TEMPERATURE_CELSIUS + 0.1
            ),
        )

    def test_missing_temperature_sensor_is_not_an_alert(self) -> None:
        self.assertEqual(self.keys(temperature=None), [])

    def test_temperature_alert_is_urgent(self) -> None:
        alerts = notification.detect_alerts(self.document(temperature=99.0))

        self.assertTrue(alerts[0].urgent)

    def test_temperature_alert_names_the_host_and_reading(self) -> None:
        alerts = notification.detect_alerts(self.document(temperature=97.4))

        self.assertIn("testhost", alerts[0].detail)
        self.assertIn("97.4", alerts[0].detail)

    def test_resource_thresholds_alert(self) -> None:
        self.assertIn("cpu_usage", self.keys(cpu=99.0))
        self.assertIn("memory_usage", self.keys(memory=95.0))
        self.assertIn("disk_usage", self.keys(disk=93.0))

    def test_several_problems_all_reported(self) -> None:
        self.assertEqual(
            sorted(self.keys(temperature=101.0, cpu=99.0, memory=95.0, disk=93.0)),
            ["cpu_temperature", "cpu_usage", "disk_usage", "memory_usage"],
        )

    def test_empty_document_is_not_an_alert(self) -> None:
        self.assertEqual(notification.detect_alerts("{}"), [])


class TestNotificationDelivery(unittest.TestCase):
    """Delivery is gated on a GUI and rate limited. Nothing is really sent."""

    def setUp(self) -> None:
        notification._last_notified.clear()

        self.sent: list[notification.Alert] = []

        patcher = mock.patch.object(
            notification,
            "send_notification",
            side_effect=lambda alert: bool(self.sent.append(alert) or True),
        )

        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(notification._last_notified.clear)

        self.hot = json.dumps(
            {
                "host": {"hostname": "testhost"},
                "hardware": {"cpu_temperature": 99.0, "fan_speed": 0},
            }
        )

    def gui(self, present: bool) -> Any:
        return mock.patch.object(
            notification, "is_gui_session", return_value=present
        )

    def test_no_gui_sends_nothing(self) -> None:
        with self.gui(False):
            self.assertEqual(notification.notify(self.hot), [])

        self.assertEqual(self.sent, [])

    def test_no_gui_never_reads_the_log(self) -> None:
        with self.gui(False), mock.patch.object(
            notification, "read_latest_metrics"
        ) as reader:
            notification.notify()

        reader.assert_not_called()

    def test_gui_delivers_the_alert(self) -> None:
        with self.gui(True):
            delivered = notification.notify(self.hot)

        self.assertEqual([alert.key for alert in delivered], ["cpu_temperature"])
        self.assertEqual(len(self.sent), 1)

    def test_cooldown_suppresses_repeats(self) -> None:
        with self.gui(True):
            notification.notify(self.hot)
            second = notification.notify(self.hot)

        self.assertEqual(second, [])
        self.assertEqual(len(self.sent), 1)

    def test_force_bypasses_cooldown(self) -> None:
        with self.gui(True):
            notification.notify(self.hot)
            notification.notify(self.hot, force=True)

        self.assertEqual(len(self.sent), 2)

    def test_failed_delivery_is_not_recorded_as_delivered(self) -> None:
        with self.gui(True), mock.patch.object(
            notification, "send_notification", return_value=False
        ):
            self.assertEqual(notification.notify(self.hot), [])

        # Nothing was shown, so the cooldown must not have been started.
        self.assertNotIn("cpu_temperature", notification._last_notified)


class TestLogReading(unittest.TestCase):
    """Alerts come from the log, so reading it must be robust."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

        self.addCleanup(self._temporary.cleanup)

    def write_lines(self, *documents: dict[str, Any]) -> Path:
        path = self.directory / "20260820T100000-monit.jsonl"

        with path.open("w", encoding="utf-8") as handle:
            for document in documents:
                handle.write(json.dumps(document) + "\n")

        return path

    def test_reads_the_most_recent_line(self) -> None:
        self.write_lines(
            {"hardware": {"cpu_temperature": 40.0}},
            {"hardware": {"cpu_temperature": 88.8}},
        )

        document = notification.read_latest_metrics(self.directory)

        self.assertIsNotNone(document)
        self.assertEqual(
            json.loads(str(document))["hardware"]["cpu_temperature"],
            88.8,
        )

    def test_missing_directory_returns_none(self) -> None:
        self.assertIsNone(
            notification.read_latest_metrics(self.directory / "absent")
        )

    def test_empty_directory_returns_none(self) -> None:
        self.assertIsNone(notification.read_latest_metrics(self.directory))
        self.assertIsNone(notification.latest_metrics_file(self.directory))

    def test_half_written_final_line_returns_none(self) -> None:
        path = self.write_lines({"hardware": {"cpu_temperature": 40.0}})

        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"hardware": ')

        self.assertIsNone(notification.read_latest_metrics(self.directory))

    def test_picks_the_newest_file(self) -> None:
        older = self.directory / "20260819T100000-monit.jsonl"
        older.write_text(json.dumps({"marker": "old"}) + "\n", encoding="utf-8")

        newer = self.directory / "20260820T100000-monit.jsonl"
        newer.write_text(json.dumps({"marker": "new"}) + "\n", encoding="utf-8")

        import os

        os.utime(older, (1_600_000_000, 1_600_000_000))

        self.assertEqual(
            notification.latest_metrics_file(self.directory),
            newer,
        )

    def test_ignores_logrotate_archives(self) -> None:
        # logrotate leaves monit.jsonl.1 and monit.jsonl.2.gz behind. Alerting
        # on those would report metrics from days ago, so they must be skipped
        # even when their mtime is newer than the live file's.
        live = self.directory / "monit.jsonl"
        live.write_text(json.dumps({"marker": "live"}) + "\n", encoding="utf-8")

        for stale in ("monit.jsonl.1", "monit.jsonl.2.gz"):
            (self.directory / stale).write_text(
                json.dumps({"marker": "rotated"}) + "\n", encoding="utf-8"
            )

        os.utime(live, (1_600_000_000, 1_600_000_000))

        document = notification.read_latest_metrics(self.directory)

        self.assertIsNotNone(document)
        self.assertEqual(json.loads(str(document))["marker"], "live")

    def test_round_trip_through_the_real_writer(self) -> None:
        logger.write_log(metrics_document(), self.directory)

        document = notification.read_latest_metrics(self.directory)

        self.assertIsNotNone(document)
        self.assertEqual(
            json.loads(str(document))["timestamp"],
            json.loads(metrics_document())["timestamp"],
        )


class TestLoopErrorHandling(unittest.TestCase):
    """The loop survives transient faults but gives up on permanent ones."""

    def setUp(self) -> None:
        self.written: list[str] = []

        # main() installs a SIGTERM handler process-wide; put it back after.
        original = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, original)

        # The loop reports progress and faults on stdout/stderr; keep that
        # out of the test results.
        for redirect in (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

        patches = (
            mock.patch.object(monit, "COLLECTION_INTERVAL_SECONDS", 0),
            mock.patch.object(monit, "ensure_log_directory"),
            mock.patch.object(monit, "log_file_path", return_value=Path("/dev/null")),
            mock.patch.object(monit, "is_gui_session", return_value=False),
            mock.patch.object(monit, "notify", return_value=[]),
            mock.patch.object(
                monit,
                "write_log",
                side_effect=lambda document: self.written.append(document),
            ),
        )

        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def collector(self, *outcomes: Any, stop_after: int) -> Any:
        """Return a collector yielding the given outcomes, then ending the loop."""
        calls = {"count": 0}

        def collect() -> str:
            index = calls["count"]
            calls["count"] += 1

            if index >= stop_after:
                raise KeyboardInterrupt

            outcome = outcomes[index % len(outcomes)]

            if isinstance(outcome, Exception):
                raise outcome

            return str(outcome)

        collect.calls = calls  # type: ignore[attr-defined]

        return collect

    def test_transient_failure_does_not_stop_the_loop(self) -> None:
        collect = self.collector(
            RuntimeError("Could not retrieve disk I/O counters"),
            "{}",
            "{}",
            stop_after=3,
        )

        with mock.patch.object(monit, "collect_metrics_json", collect):
            monit.main()

        # The failure was survived and later cycles still recorded metrics.
        self.assertEqual(self.written, ["{}", "{}"])

    def test_gives_up_after_repeated_failures(self) -> None:
        collect = self.collector(RuntimeError("sensor gone"), stop_after=99)

        with mock.patch.object(monit, "MAX_CONSECUTIVE_FAILURES", 3), \
             mock.patch.object(monit, "collect_metrics_json", collect), \
             self.assertRaises(SystemExit):
            monit.main()

        self.assertEqual(collect.calls["count"], 3)

    def test_failure_counter_resets_after_a_success(self) -> None:
        # Alternating failures must never accumulate to the give-up threshold.
        collect = self.collector(
            RuntimeError("flap"),
            "{}",
            stop_after=20,
        )

        with mock.patch.object(monit, "MAX_CONSECUTIVE_FAILURES", 3), \
             mock.patch.object(monit, "collect_metrics_json", collect):
            monit.main()

        self.assertEqual(len(self.written), 10)

    def test_notification_failure_does_not_lose_metrics(self) -> None:
        collect = self.collector("{}", stop_after=2)

        with mock.patch.object(monit, "collect_metrics_json", collect), \
             mock.patch.object(
                 monit, "notify", side_effect=OSError("dbus is down")
             ):
            monit.main()

        # Metrics were written even though every notification failed.
        self.assertEqual(len(self.written), 2)

    def test_sigterm_raises_the_shutdown_exception(self) -> None:
        # systemctl stop sends SIGTERM; it must reach the clean-stop path
        # rather than killing the process mid-write.
        with self.assertRaises(KeyboardInterrupt):
            monit._stop(signal.SIGTERM, None)

    def test_main_installs_the_sigterm_handler(self) -> None:
        collect = self.collector("{}", stop_after=1)

        with mock.patch.object(monit, "collect_metrics_json", collect):
            monit.main()

        self.assertIs(signal.getsignal(signal.SIGTERM), monit._stop)

    def test_unusable_log_directory_exits_before_the_loop(self) -> None:
        with mock.patch.object(
            monit,
            "ensure_log_directory",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(SystemExit):
            monit.main()


class _Tee:
    """Write test output to the terminal and the results file at once."""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)

        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def main() -> int:
    loader = unittest.TestLoader()
    module = sys.modules[__name__]
    selected = sys.argv[1:]

    # Bare names select a class or a single test, as unittest does.
    suite = (
        loader.loadTestsFromNames(selected, module)
        if selected
        else loader.loadTestsFromModule(module)
    )

    started = datetime.now().astimezone()

    with RESULTS_FILE.open("w", encoding="utf-8") as handle:
        handle.write(f"monit test run - {started:%Y-%m-%d %H:%M:%S %Z}\n")
        handle.write(f"python {sys.version.split()[0]} on {sys.platform}\n")
        handle.write(f"project {PROJECT_ROOT}\n")
        handle.write("=" * 70 + "\n\n")

        stream = _Tee(sys.stderr, handle)
        result = unittest.TextTestRunner(
            stream=stream,  # type: ignore[arg-type]
            verbosity=2,
        ).run(suite)

        elapsed = (datetime.now().astimezone() - started).total_seconds()

        handle.write("\n" + "=" * 70 + "\n")
        handle.write(
            f"ran {result.testsRun} tests in {elapsed:.1f}s: "
            f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
            f"{len(result.failures)} failed, "
            f"{len(result.errors)} errored, "
            f"{len(result.skipped)} skipped\n"
        )
        handle.write(
            f"RESULT: {'PASS' if result.wasSuccessful() else 'FAIL'}\n"
        )

    print(f"\nResults written to {RESULTS_FILE}")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
