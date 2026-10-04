from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import shlex
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tc66c import cache, cli
from tc66c.device import DeviceError, NoDeviceError, Port, ResponseTimeout
from tc66c.protocol import ProtocolError, decode_packet

from tests.fixtures import KNOWN_RESPONSE
from tests.test_ble import FakeClient


class CliTests(unittest.TestCase):
    def run_cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(args)
        return code, output.getvalue(), errors.getvalue()

    def test_no_arguments_show_help_without_accessing_hardware(self):
        with patch("tc66c.cli.Meter") as meter, patch("tc66c.ble.BleakScanner.discover") as scan:
            code, output, errors = self.run_cli([])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("--timeout SEKUNDEN", output)
        self.assertIn("--filter NAME", output)
        self.assertIn("tc66c read", output)
        meter.assert_not_called()
        scan.assert_not_called()

    def test_help_explains_options_and_contains_valid_complete_examples(self):
        common = ("--transport", "--timeout", "--json")
        for command, options in (
            (None, (*common, "--filter", "--device", "--interval", "--count", "--temperature-unit", "--output", "--output-format")),
            ("list", (*common, "--filter")),
            ("info", (*common, "--device")),
            ("read", (*common, "--device", "--temperature-unit", "--output", "--output-format")),
            ("watch", (*common, "--device", "--temperature-unit", "--interval", "--count", "--output", "--output-format")),
        ):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    cli.main([*([command] if command else []), "--help"])
                self.assertEqual(raised.exception.code, 0)
                help_text = output.getvalue()
                for option in options:
                    self.assertIn(option, help_text)
                self.assertIn("BT24-M", help_text)
                self.assertIn("Vollständige Beispiele:", help_text)
                examples = [line.strip() for line in help_text.splitlines() if line.startswith("  tc66c ")]
                self.assertGreaterEqual(len(examples), 4)
                for example in examples:
                    # Parse every displayed command without running a scan or measurement.
                    cli.parser().parse_args(shlex.split(example)[1:])

    def test_empty_list_succeeds_without_opening_hardware(self):
        with patch("tc66c.cli.list_devices", return_value=[]), patch("tc66c.cli.Meter") as meter:
            code, output, errors = self.run_cli(["list", "--transport", "usb", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [])
        self.assertEqual(errors, "")
        meter.assert_not_called()

    def test_usb_list_default_filter_can_be_overridden_in_text_and_json(self):
        ports = [Port("meter", "Adapter BT24-M", None, None, False),
                 Port("other", "Debug console", None, None, False)]
        for filters, expected in (
            ([], ["meter"]),
            (["--filter", "bt24-m"], ["meter"]),
            (["--filter", "DEBUG"], ["other"]),
            (["--filter", ""], ["meter", "other"]),
        ):
            for extra in ([], ["--json"]):
                with self.subTest(filters=filters, extra=extra), patch("tc66c.cli.list_devices", return_value=ports), patch("tc66c.cli.Meter") as meter:
                    code, output, errors = self.run_cli(["list", "--transport", "usb", *filters, *extra])
                self.assertEqual((code, errors), (0, ""))
                if extra:
                    listed = [port["device"] for port in json.loads(output)]
                else:
                    listed = [line.split("\t", 1)[0] for line in output.splitlines()]
                self.assertEqual(listed, expected)
                meter.assert_not_called()

    def test_no_device_paths_have_exit_three(self):
        for command in ("info", "read", "watch"):
            with self.subTest(command=command), patch("tc66c.cli.select_device", side_effect=NoDeviceError("missing")):
                code, output, errors = self.run_cli([command, "--transport", "usb"])
            self.assertEqual(code, 3)
            self.assertEqual(output, "")
            self.assertIn("missing", errors)

    def test_read_json_distinguishes_reported_and_calculated_values(self):
        meter = MagicMock()
        meter.__enter__.return_value = meter
        meter.read.return_value = decode_packet(KNOWN_RESPONSE)
        with patch("tc66c.cli.Meter", return_value=meter):
            code, output, errors = self.run_cli(["read", "--transport", "usb", "--device", "fake", "--json"])
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertAlmostEqual(result["device_values"]["power_w"], 0.6324)
        self.assertAlmostEqual(result["calculated"]["power_w"], 0.63248373)
        self.assertIsNone(result["device_values"]["temperature_unit"])
        meter.__exit__.assert_called_once()

    def test_usb_timeout_stays_three_seconds_and_explicit_override_is_used(self):
        for command in ("info", "read", "watch"):
            for extra, expected in (([], 3.0), (["--timeout", "5"], 5.0)):
                meter = MagicMock()
                meter.__enter__.return_value = meter
                meter.read.return_value = decode_packet(KNOWN_RESPONSE)
                arguments = [command, "--transport", "usb", "--device", "fake", "--json", *extra]
                if command == "watch":
                    arguments += ["--count", "1"]
                with self.subTest(command=command, timeout=expected), patch("tc66c.cli.Meter", return_value=meter) as factory:
                    code, output, errors = self.run_cli(arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(len(output.splitlines()), 1)
                factory.assert_called_once_with("fake", timeout=expected)

    def test_errors_have_distinct_exit_codes_and_no_reading(self):
        for error, expected in ((DeviceError("unavailable"), 4), (ResponseTimeout("timeout"), 5), (ProtocolError("invalid"), 6)):
            with self.subTest(error=error):
                meter = MagicMock()
                meter.__enter__.return_value = meter
                meter.read.side_effect = error
                with patch("tc66c.cli.Meter", return_value=meter):
                    code, output, errors = self.run_cli(["read", "--transport", "usb", "--device", "fake"])
                self.assertEqual(code, expected)
                self.assertEqual(output, "")
                self.assertIn(str(error), errors)
                meter.__exit__.assert_called_once()

    def test_watch_does_not_repeat_stale_values_after_disconnect(self):
        meter = MagicMock()
        meter.__enter__.return_value = meter
        meter.read.side_effect = [decode_packet(KNOWN_RESPONSE), DeviceError("disconnected")]
        with patch("tc66c.cli.Meter", return_value=meter), patch("tc66c.cli.time.sleep"):
            code, output, errors = self.run_cli(["watch", "--transport", "usb", "--device", "fake", "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("disconnected", errors)
        meter.__exit__.assert_called_once()

    def test_ctrl_c_and_finite_watch_release_context(self):
        for readings, args, expected in (
            ([decode_packet(KNOWN_RESPONSE), KeyboardInterrupt()], [], 130),
            ([decode_packet(KNOWN_RESPONSE)], ["--count", "1"], 0),
        ):
            with self.subTest(expected=expected):
                meter = MagicMock()
                meter.__enter__.return_value = meter
                meter.read.side_effect = readings
                with patch("tc66c.cli.Meter", return_value=meter), patch("tc66c.cli.time.sleep"):
                    code, output, errors = self.run_cli(["watch", "--transport", "usb", "--device", "fake", "--json", *args])
                self.assertEqual(code, expected)
                self.assertEqual(len(output.splitlines()), 1)
                meter.__exit__.assert_called_once()

    def test_invalid_or_unbounded_arguments_fail_before_hardware_open(self):
        arguments = [
            ["read", "--timeout", value] for value in ("0", "-1", "nan", "inf", "-inf")
        ] + [
            ["watch", "--interval", value] for value in ("0.49", "nan", "inf")
        ] + [
            ["watch", "--count", "0"], ["read", "--output"], ["read", "--output", ""],
            ["read", "--output-format", "csv"], ["watch", "--output-format", "jsonl"],
            ["read", "--output", "file.csv", "--output-format", "invalid"],
            ["list", "--output", "file.csv"], ["info", "--output", "file.csv"],
        ]
        for args in arguments:
            with self.subTest(args=args), patch("tc66c.cli.Meter") as meter:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cli.main(args)
                self.assertEqual(raised.exception.code, 2)
                meter.assert_not_called()


class BleCliTests(unittest.TestCase):
    run_cli = CliTests.run_cli

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache_file = Path(directory.name) / "ble-device.json"
        location = patch("tc66c.cache.cache_path", return_value=self.cache_file)
        location.start()
        self.addCleanup(location.stop)
        permission = patch("tc66c.ble._check_permission")
        permission.start()
        self.addCleanup(permission.stop)

    def test_ble_info_and_read_use_the_shared_decoder_without_opening_serial(self):
        for command in ("info", "read"):
            fake = FakeClient([[KNOWN_RESPONSE]])
            with self.subTest(command=command), patch("tc66c.ble.BleakClient", side_effect=fake.factory), patch("tc66c.cli.Meter") as serial_meter:
                code, output, errors = self.run_cli([command, "--device", "fake", "--json"])
            result = json.loads(output)
            self.assertEqual((code, errors), (0, ""))
            if command == "info":
                self.assertEqual(result["transport"], "Bluetooth LE")
                self.assertEqual(result["address"], "fake")
                self.assertEqual(result["firmware"], "1.14")
            else:
                self.assertAlmostEqual(result["device_values"]["voltage_v"], 5.1234)
            serial_meter.assert_not_called()
            self.assertEqual(fake.disconnect_count, 1)

    def test_ble_setup_receives_fifteen_seconds_by_default_and_honors_override(self):
        from tc66c.ble import BleDevice
        candidates = [BleDevice("unrelated", "Headset", -40, (), False),
                      BleDevice("z-first", "Adapter bt24-m", -70, ("ffe0",), False),
                      BleDevice("a-second", "BT24-M", -50, ("ffe0",), False)]
        for command in ("info", "read", "watch"):
            for extra, expected in (([], 15.0), (["--timeout", "3"], 3.0)):
                cache.forget_device()
                fake = FakeClient([[KNOWN_RESPONSE]])
                arguments = [command, "--json", *extra]
                if command == "watch":
                    arguments += ["--count", "1"]
                with self.subTest(command=command, timeout=expected), patch("tc66c.ble.BleakClient", side_effect=fake.factory) as factory, patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=candidates) as scanner:
                    code, output, errors = self.run_cli(arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(len(output.splitlines()), 1)
                self.assertEqual(factory.call_args.args[0], "z-first")
                self.assertEqual(factory.call_args.kwargs["timeout"], expected)
                scanner.assert_awaited_once_with(expected)
                self.assertEqual(fake.disconnect_count, 1)

    def test_following_measurement_commands_reuse_the_file_without_extending_its_lifetime(self):
        from tc66c.ble import BleDevice
        first = FakeClient([[KNOWN_RESPONSE]])
        with patch("tc66c.ble.BleakClient", side_effect=first.factory), patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=[BleDevice("remembered", "BT24-M", -50, (), False)]) as scan:
            code, output, errors = self.run_cli(["read", "--json"])
        self.assertEqual((code, errors), (0, ""))
        scan.assert_awaited_once()
        saved = self.cache_file.read_bytes()
        for command in ("info", "read", "watch"):
            fake = FakeClient([[KNOWN_RESPONSE]])
            extra = ["--count", "1"] if command == "watch" else []
            with self.subTest(command=command), patch("tc66c.ble.BleakClient", side_effect=fake.factory) as factory, patch("tc66c.ble.list_devices", new_callable=AsyncMock) as scan:
                code, output, errors = self.run_cli([command, "--json", *extra])
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(len(output.splitlines()), 1)
            self.assertEqual(factory.call_args.args[0], "remembered")
            self.assertEqual(fake.disconnect_count, 1)
            scan.assert_not_awaited()
            self.assertEqual(self.cache_file.read_bytes(), saved)

    def test_expired_cache_triggers_discovery_and_saves_the_new_target(self):
        from tc66c.ble import BleDevice
        self.cache_file.write_text(json.dumps({"device": "expired", "saved_at": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()}))
        fake = FakeClient([[KNOWN_RESPONSE]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory) as factory, patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=[BleDevice("new", "BT24-M", -50, (), False)]) as scan:
            code, output, errors = self.run_cli(["read", "--json"])
        self.assertEqual((code, errors), (0, ""))
        scan.assert_awaited_once_with(15.0)
        self.assertEqual(factory.call_args.args[0], "new")
        self.assertEqual(cache.load_device(), "new")

    def test_explicit_device_takes_priority_and_is_remembered_after_a_valid_response(self):
        cache.save_device("old")
        fake = FakeClient([[KNOWN_RESPONSE]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory) as factory, patch("tc66c.ble.list_devices", new_callable=AsyncMock) as scan:
            code, output, errors = self.run_cli(["info", "--device", "chosen", "--json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["address"], "chosen")
        self.assertEqual(factory.call_args.args[0], "chosen")
        scan.assert_not_awaited()
        self.assertEqual(cache.load_device(), "chosen")

    def test_cached_setup_or_first_response_failure_rediscovers_once_before_output(self):
        from bleak import BleakError
        from tc66c.ble import BleDevice
        for old in (FakeClient(connect_error=BleakError("unavailable")), FakeClient(unsupported=True), FakeClient([[KNOWN_RESPONSE[:-1]]])):
            cache.save_device("old")
            fresh = FakeClient([[KNOWN_RESPONSE]])
            def factory(address, **options):
                return (old if address == "old" else fresh).factory(address, **options)
            with self.subTest(old=old), patch("tc66c.ble.BleakClient", side_effect=factory) as client, patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=[BleDevice("new", "BT24-M", -50, (), False)]) as scan:
                code, output, errors = self.run_cli(["read", "--timeout", "0.01", "--json"])
            self.assertEqual(code, 0)
            self.assertIn("suche erneut", errors)
            self.assertEqual(len(output.splitlines()), 1)
            self.assertEqual(client.call_count, 2)
            self.assertEqual((old.disconnect_count, fresh.disconnect_count), (1, 1))
            scan.assert_awaited_once_with(0.01)
            self.assertEqual(cache.load_device(), "new")

    def test_a_failed_fresh_connection_does_not_retry_or_save_an_unverified_target(self):
        from bleak import BleakError
        from tc66c.ble import BleDevice
        for remembered in (False, True):
            cache.forget_device()
            if remembered:
                cache.save_device("old")
            clients = []
            def factory(address, **options):
                fake = FakeClient(connect_error=BleakError("unavailable"))
                clients.append(fake)
                return fake.factory(address, **options)
            with self.subTest(remembered=remembered), patch("tc66c.ble.BleakClient", side_effect=factory) as client, patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=[BleDevice("new", "BT24-M", -50, (), False)]) as scan:
                code, output, errors = self.run_cli(["read", "--json"])
            self.assertEqual((code, output), (4, ""))
            self.assertIn("unavailable", errors)
            scan.assert_awaited_once()
            self.assertEqual(client.call_count, 2 if remembered else 1)
            self.assertTrue(all(fake.disconnect_count == 1 for fake in clients))
            self.assertIsNone(cache.load_device())

    def test_cached_watch_does_not_retry_after_a_measurement_has_been_output(self):
        cache.save_device("remembered")
        fake = FakeClient([[KNOWN_RESPONSE], ["disconnect"]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory), patch("tc66c.ble.list_devices", new_callable=AsyncMock) as scan, patch("tc66c.cli.asyncio.sleep", new_callable=AsyncMock):
            code, output, errors = self.run_cli(["watch", "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(len(output.splitlines()), 1)
        scan.assert_not_awaited()
        self.assertNotIn("suche erneut", errors)
        self.assertEqual(fake.disconnect_count, 1)

    def test_denied_permission_does_not_invalidate_cache_or_trigger_retry(self):
        cache.save_device("remembered")
        saved = self.cache_file.read_bytes()
        with patch("tc66c.ble._check_permission", side_effect=DeviceError("Bluetooth denied")), patch("tc66c.ble.list_devices", new_callable=AsyncMock) as scan, patch("tc66c.ble.BleakClient") as client:
            code, output, errors = self.run_cli(["read"])
        self.assertEqual((code, output), (4, ""))
        self.assertIn("Bluetooth denied", errors)
        self.assertEqual(self.cache_file.read_bytes(), saved)
        scan.assert_not_awaited()
        client.assert_not_called()

    def test_list_refreshes_the_remembered_target_and_removes_it_if_no_match_is_found(self):
        from tc66c.ble import BleDevice
        cache.save_device("old")
        devices = [BleDevice("headset", "Headset", -40, (), False), BleDevice("new", "BT24-M", -50, (), False)]
        with patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=devices) as scan:
            code, output, errors = self.run_cli(["list", "--filter", "Headset", "--json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)[0]["device"], "headset")
        self.assertEqual(cache.load_device(), "new")
        scan.assert_awaited_once()
        with patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=[]):
            code, output, errors = self.run_cli(["list", "--json"])
        self.assertEqual((code, errors, json.loads(output)), (0, "", []))
        self.assertIsNone(cache.load_device())

    def test_ble_watch_stops_at_count_or_first_error_and_releases_connection(self):
        for responses, extra, expected in (
            ([[KNOWN_RESPONSE]], ["--count", "1"], 0),
            ([[KNOWN_RESPONSE], ["disconnect"]], [], 4),
            ([[KNOWN_RESPONSE], [KeyboardInterrupt()]], [], 130),
        ):
            fake = FakeClient(responses)
            with self.subTest(expected=expected), patch("tc66c.ble.BleakClient", side_effect=fake.factory), patch("tc66c.cli.asyncio.sleep", new_callable=AsyncMock):
                code, output, errors = self.run_cli(["watch", "--device", "fake", "--json", *extra])
            self.assertEqual(code, expected)
            self.assertEqual(len(output.splitlines()), 1)
            self.assertEqual(fake.disconnect_count, 1)

    def test_ble_list_uses_only_the_ble_scanner(self):
        from tc66c.ble import BleDevice
        devices = [BleDevice("id", "BT24-M", -50, ("ffe0",), False)]
        for extra, expected in (([], 5.0), (["--timeout", "15"], 15.0)):
            with self.subTest(timeout=expected), patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=devices) as scanner, patch("tc66c.cli.list_devices") as serial_ports:
                code, output, errors = self.run_cli(["list", "--json", *extra])
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output)[0]["device"], "id")
            scanner.assert_awaited_once_with(expected)
            serial_ports.assert_not_called()

    def test_ble_list_default_filter_can_be_overridden_in_text_and_json(self):
        from tc66c.ble import BleDevice
        devices = [BleDevice("meter", "BT24-M", -50, ("ffe0",), False),
                   BleDevice("meter2", "Adapter BT24-M", -60, ("ffe0",), False),
                   BleDevice("other", "Headset", -40, (), False)]
        for filters, expected in (
            ([], ["meter", "meter2"]),
            (["--filter", "bt24-m"], ["meter", "meter2"]),
            (["--filter", "HEAD"], ["other"]),
            (["--filter", ""], ["meter", "meter2", "other"]),
        ):
            for extra in ([], ["--json"]):
                with self.subTest(filters=filters, extra=extra), patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=devices) as scanner:
                    code, output, errors = self.run_cli(["list", *filters, *extra])
                self.assertEqual((code, errors), (0, ""))
                if extra:
                    filtered = json.loads(output)
                    listed = [device["device"] for device in filtered]
                    self.assertTrue(all(not device["candidate"] for device in filtered))
                else:
                    rows = [line.split("\t") for line in output.splitlines()]
                    self.assertEqual(rows, [[device.device, device.name] for device in devices if device.device in expected])
                    listed = [row[0] for row in rows]
                self.assertEqual(listed, expected)
                scanner.assert_awaited_once_with(5.0)

    def test_ble_list_filter_with_no_match_reports_empty_results(self):
        from tc66c.ble import BleDevice
        devices = [BleDevice("meter", "BT24-M", -50, ("ffe0",), False)]
        for extra in ([], ["--json"]):
            with self.subTest(extra=extra), patch("tc66c.ble.list_devices", new_callable=AsyncMock, return_value=devices):
                code, output, errors = self.run_cli(["list", "--filter", "BR24-M", *extra])
            self.assertEqual((code, errors), (0, ""))
            if extra:
                self.assertEqual(json.loads(output), [])
            else:
                self.assertIn("Keine BLE-Geräte für Namensfilter 'BR24-M' gefunden.", output)
                self.assertNotIn("BT24-M", output)


if __name__ == "__main__":
    unittest.main()
