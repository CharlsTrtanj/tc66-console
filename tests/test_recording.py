from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tc66c import cache, cli
from tc66c.device import DeviceError
from tc66c.protocol import decode_packet
from tc66c.recording import measurement_record, open_log
from tests.fixtures import KNOWN_RESPONSE, modified_response
from tests.test_ble import FakeClient


class RecordingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        location = patch("tc66c.cache.cache_path", return_value=self.directory / "cache.json")
        location.start()
        self.addCleanup(location.stop)
        permission = patch("tc66c.ble._check_permission")
        permission.start()
        self.addCleanup(permission.stop)

    def run_cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(args)
        return code, output.getvalue(), errors.getvalue()

    def csv_rows(self, path):
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_read_and_watch_record_every_sample_on_both_transports_and_keep_console_json(self):
        packets = [KNOWN_RESPONSE, modified_response(48, 50000), modified_response(52, 0)]
        for transport in ("usb", "ble"):
            for command in ("read", "watch"):
                for format in ("csv", "jsonl"):
                    path = self.directory / f"{transport}-{command}-{format}"
                    args = [command, "--transport", transport, "--device", "fake",
                            "--json", "--output", str(path), "--temperature-unit", "C"]
                    selected = packets[:1] if command == "read" else packets
                    if command == "watch":
                        args += ["--count", "3", "--interval", "0.5"]
                    if format == "jsonl":
                        args += ["--output-format", "jsonl"]
                    if transport == "usb":
                        meter = MagicMock()
                        meter.__enter__.return_value = meter
                        meter.read.side_effect = [decode_packet(packet) for packet in selected]
                        connection = patch("tc66c.cli.Meter", return_value=meter)
                        sleep = patch("tc66c.cli.time.sleep")
                    else:
                        meter = FakeClient([[packet] for packet in selected])
                        connection = patch("tc66c.ble.BleakClient", side_effect=meter.factory)
                        sleep = patch("tc66c.cli.asyncio.sleep", new_callable=AsyncMock)
                    with self.subTest(transport=transport, command=command, format=format), connection, sleep:
                        code, output, errors = self.run_cli(args)
                        self.assertEqual((code, errors), (0, ""))
                        records = [json.loads(line) for line in output.splitlines()]
                        self.assertEqual(len(records), len(selected))
                        if format == "jsonl":
                            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], records)
                        else:
                            rows = self.csv_rows(path)
                            self.assertEqual(len(rows), len(records))
                            self.assertEqual(len(rows[0]), 20)
                            for row, record in zip(rows, records):
                                self.assertEqual(row["received_at"], record["received_at"])
                                self.assertEqual(row["model"], record["device"]["model"])
                                self.assertEqual(row["firmware"], record["device"]["firmware"])
                                self.assertEqual(int(row["serial_number"]), record["device"]["serial_number"])
                                self.assertEqual(int(row["boot_count"]), record["device"]["boot_count"])
                                for key in ("voltage_v", "current_a", "power_w", "resistance_raw", "resistance_ohm",
                                            "temperature_value", "data_plus_v", "data_minus_v"):
                                    self.assertEqual(float(row[key]), record["device_values"][key])
                                self.assertEqual(row["temperature_unit"], "C")
                                for group in (0, 1):
                                    self.assertEqual(int(row[f"charge_group{group}_mah"]), record["device_values"]["charge_mah"][group])
                                    self.assertEqual(int(row[f"energy_group{group}_mwh"]), record["device_values"]["energy_mwh"][group])
                                for key in ("power_w", "resistance_ohm"):
                                    value = record["calculated"][key]
                                    self.assertEqual(row[f"calculated_{key}"], "" if value is None else str(value))
                        if transport == "usb":
                            meter.__exit__.assert_called_once()
                        else:
                            self.assertEqual(meter.disconnect_count, 1)

    def test_files_append_across_runs_and_each_sample_is_visible_before_close(self):
        record = measurement_record(decode_packet(KNOWN_RESPONSE), "device")
        for format in ("csv", "jsonl"):
            path = self.directory / format
            with self.subTest(format=format):
                for count in (1, 2):
                    with open_log(str(path), format) as log:
                        log.write(record)
                        if format == "csv":
                            rows = self.csv_rows(path)
                            self.assertEqual(len(rows), count)
                            self.assertEqual(rows[-1]["temperature_unit"], "")
                        else:
                            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], [json.loads(json.dumps(record))] * count)
                if format == "csv":
                    self.assertEqual(path.read_text().count("received_at,model,firmware"), 1)

    def test_recording_format_is_independent_of_filename_and_text_or_json_console(self):
        path = self.directory / "readings.jsonl"
        meter = MagicMock()
        meter.__enter__.return_value = meter
        meter.read.return_value = decode_packet(KNOWN_RESPONSE)
        for console in ([], ["--json"]):
            with self.subTest(console=console), patch("tc66c.cli.Meter", return_value=meter):
                code, output, errors = self.run_cli(["read", "--transport", "usb", "--device", "fake", "--output", str(path), *console])
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("device_values" if console else "U=", output)
        self.assertEqual(len(self.csv_rows(path)), 2)

    def test_ctrl_c_and_disconnect_preserve_completed_samples_without_repeating_them(self):
        for transport in ("usb", "ble"):
            for format in ("csv", "jsonl"):
                for interrupt in (False, True):
                    path = self.directory / f"{transport}-{format}-{interrupt}"
                    if transport == "usb":
                        meter = MagicMock()
                        meter.__enter__.return_value = meter
                        meter.read.side_effect = [decode_packet(KNOWN_RESPONSE), KeyboardInterrupt() if interrupt else DeviceError("disconnected")]
                        connection = patch("tc66c.cli.Meter", return_value=meter)
                        sleep = patch("tc66c.cli.time.sleep")
                    else:
                        meter = FakeClient([[KNOWN_RESPONSE], [KeyboardInterrupt() if interrupt else "disconnect"]])
                        connection = patch("tc66c.ble.BleakClient", side_effect=meter.factory)
                        sleep = patch("tc66c.cli.asyncio.sleep", new_callable=AsyncMock)
                    with self.subTest(transport=transport, format=format, interrupt=interrupt), connection, sleep:
                        code, output, errors = self.run_cli(["watch", "--transport", transport, "--device", "fake", "--json",
                                                             "--output", str(path), "--output-format", format])
                        self.assertEqual(code, 130 if interrupt else 4)
                        self.assertEqual(len(output.splitlines()), 1)
                        self.assertTrue(errors)
                        if format == "csv":
                            self.assertEqual(len(self.csv_rows(path)), 1)
                        else:
                            self.assertEqual(json.loads(path.read_text()), json.loads(output))
                        if transport == "usb":
                            meter.__exit__.assert_called_once()
                        else:
                            self.assertEqual(meter.disconnect_count, 1)

    def test_incompatible_or_unterminated_files_are_preserved_without_connecting(self):
        csv_path, json_path = self.directory / "csv", self.directory / "jsonl"
        record = measurement_record(decode_packet(KNOWN_RESPONSE), "C")
        with open_log(str(csv_path), "csv") as log:
            log.write(record)
        with open_log(str(json_path), "jsonl") as log:
            log.write(record)
        cases = (
            ("csv", "other,header\n1,2\n"), ("jsonl", csv_path.read_text()),
            ("csv", json_path.read_text()), ("jsonl", "[]\n"),
            ("csv", csv_path.read_text().rstrip("\r\n")),
            ("jsonl", json_path.read_text().rstrip("\r\n")), ("jsonl", "{broken}\n"),
        )
        for index, (format, contents) in enumerate(cases):
            path = self.directory / f"invalid-{index}"
            path.write_text(contents)
            with self.subTest(format=format, index=index), patch("tc66c.ble.BleakClient") as client, patch("tc66c.ble.BleakScanner.discover") as scan:
                code, output, errors = self.run_cli(["read", "--output", str(path), "--output-format", format])
            self.assertEqual((code, output), (7, ""))
            self.assertIn(str(path), errors)
            self.assertEqual(path.read_text(), contents)
            client.assert_not_called()
            scan.assert_not_called()

    def test_output_open_failure_stops_before_discovery_or_hardware_access(self):
        for transport in ("usb", "ble"):
            with self.subTest(transport=transport), patch("tc66c.cli.Meter") as meter, patch("tc66c.ble.BleakScanner.discover") as scan:
                code, output, errors = self.run_cli(["read", "--transport", transport,
                                                     "--output", str(self.directory / "missing" / "readings.csv")])
            self.assertEqual((code, output), (7, ""))
            self.assertIn("Messprotokoll", errors)
            meter.assert_not_called()
            scan.assert_not_called()

    def test_invalid_protocol_responses_do_not_create_measurement_rows(self):
        for format in ("csv", "jsonl"):
            path = self.directory / f"invalid-response-{format}"
            fake = FakeClient([[modified_response(48, 50000, valid_crc=False)]])
            with self.subTest(format=format), patch("tc66c.ble.BleakClient", side_effect=fake.factory):
                code, output, errors = self.run_cli(["read", "--device", "fake", "--timeout", "0.01",
                                                     "--output", str(path), "--output-format", format])
            self.assertEqual((code, output), (6, ""))
            self.assertIn("Prüfsumme", errors)
            self.assertEqual(self.csv_rows(path) if format == "csv" else path.read_text(), [] if format == "csv" else "")
            self.assertEqual(fake.disconnect_count, 1)

    def test_file_write_failure_closes_ble_and_does_not_retry_the_cached_meter(self):
        cache.save_device("remembered")
        fake = FakeClient([[KNOWN_RESPONSE]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory), patch("tc66c.ble.list_devices", new_callable=AsyncMock) as scan, patch("tc66c.recording.MeasurementLog._write", side_effect=OSError("disk full")):
            code, output, errors = self.run_cli(["watch", "--json", "--output", str(self.directory / "readings.csv")])
        self.assertEqual((code, output), (7, ""))
        self.assertIn("disk full", errors)
        scan.assert_not_awaited()
        self.assertEqual(fake.disconnect_count, 1)
        self.assertEqual(self.csv_rows(self.directory / "readings.csv"), [])

    def test_stdout_broken_pipe_is_not_misreported_as_a_recording_error(self):
        path = self.directory / "readings.csv"
        meter = MagicMock()
        meter.__enter__.return_value = meter
        meter.read.return_value = decode_packet(KNOWN_RESPONSE)
        with patch("tc66c.cli.Meter", return_value=meter), patch("tc66c.cli.print", side_effect=BrokenPipeError()):
            code, output, errors = self.run_cli(["read", "--transport", "usb", "--device", "fake", "--output", str(path)])
        self.assertEqual((code, output, errors), (0, "", ""))
        self.assertEqual(len(self.csv_rows(path)), 1)
        meter.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
