import errno
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import serial

from tc66c.device import (
    DeviceError, Meter, NoDeviceError, Port, ResponseTimeout, list_devices, select_device,
)
from tc66c.protocol import ProtocolError

from tests.fixtures import KNOWN_RESPONSE, modified_response


class FakeSerial:
    """Each query releases its planned chunks, like a serial reply arriving later."""

    def __init__(self, responses=(), *, write_size=5, open_error=None):
        self.responses = iter(responses)
        self.chunks = []
        self.write_size = write_size
        self.open_error = open_error
        self.dtr = self.rts = True
        self.port = None
        self.timeout = None
        self.open_state = None
        self.close_count = 0
        self.reset_count = 0
        self.writes = []

    def open(self):
        self.open_state = (self.port, self.dtr, self.rts)
        if self.open_error:
            raise self.open_error

    def close(self):
        self.close_count += 1

    def reset_input_buffer(self):
        self.reset_count += 1
        self.chunks.clear()

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks and isinstance(self.chunks[0], bytes) else 0

    def write(self, value):
        self.writes.append(value)
        self.chunks = list(next(self.responses, ()))
        return self.write_size

    def read(self, size):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        if len(value) > size:
            self.chunks.insert(0, value[size:])
        return value[:size]


class DeviceTests(unittest.TestCase):
    def setUp(self):
        # A deterministic advancing clock makes all timeout tests immediate.
        values = itertools.count(0, 0.001)
        self.clock = patch("tc66c.device.time.monotonic", side_effect=lambda: next(values))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_serial_parameters_control_lines_and_read_only_command(self):
        fake = FakeSerial([[KNOWN_RESPONSE]])
        with patch("tc66c.device.serial.Serial", return_value=fake) as factory:
            with Meter("/dev/fake", timeout=0.02) as meter:
                self.assertAlmostEqual(meter.read().voltage_v, 5.1234)
            self.assertIsNone(meter.serial)
        self.assertEqual(fake.open_state, ("/dev/fake", False, False))
        self.assertEqual(fake.writes, [b"getva"])
        self.assertEqual(fake.reset_count, 1)
        self.assertEqual(fake.close_count, 1)
        self.assertEqual(factory.call_args.kwargs["baudrate"], 115200)
        self.assertIsNone(factory.call_args.kwargs["port"])
        self.assertTrue(factory.call_args.kwargs["exclusive"])
        self.assertLessEqual(factory.call_args.kwargs["timeout"], 0.1)

    def test_fragmented_reply_is_reassembled(self):
        chunks = [KNOWN_RESPONSE[:1], KNOWN_RESPONSE[1:17], KNOWN_RESPONSE[17:91], KNOWN_RESPONSE[91:]]
        fake = FakeSerial([chunks])
        with patch("tc66c.device.serial.Serial", return_value=fake), Meter("fake", 0.02) as meter:
            self.assertAlmostEqual(meter.read().power_w, 0.6324)

    def test_coalesced_replies_return_latest_validated_reading(self):
        fake = FakeSerial([[KNOWN_RESPONSE + modified_response(48, 50000)]])
        with patch("tc66c.device.serial.Serial", return_value=fake), Meter("fake", 0.02) as meter:
            self.assertAlmostEqual(meter.read().voltage_v, 5.0)

    def test_valid_reply_decoded_after_deadline_is_rejected_and_closed(self):
        fake = FakeSerial([[KNOWN_RESPONSE]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with Meter("fake", 0.02) as meter:
                # Deadline creation, loop test, read timeout, then decoding finishes
                # exactly at the deadline. A valid packet must still be rejected.
                with patch("tc66c.device.time.monotonic", side_effect=[0, 0, 0, 0.02]):
                    with self.assertRaises(ResponseTimeout):
                        meter.read()
                self.assertIsNone(meter.serial)
                self.assertEqual(fake.close_count, 1)

    def test_missing_reply_times_out_and_closes(self):
        fake = FakeSerial([[]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(ResponseTimeout), Meter("fake", 0.02) as meter:
                meter.read()
        self.assertEqual(fake.close_count, 1)

    def test_incomplete_reply_is_a_protocol_error(self):
        fake = FakeSerial([[KNOWN_RESPONSE[:-1]]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(ProtocolError), Meter("fake", 0.02) as meter:
                meter.read()
        self.assertEqual(fake.close_count, 1)

    def test_corrupt_reply_is_a_protocol_error(self):
        fake = FakeSerial([[modified_response(48, 50000, valid_crc=False)]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(ProtocolError), Meter("fake", 0.02) as meter:
                meter.read()

    def test_partial_write_is_an_error(self):
        fake = FakeSerial(write_size=3)
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(DeviceError), Meter("fake", 0.02) as meter:
                meter.read()
        self.assertEqual(fake.close_count, 1)

    def test_disconnect_is_mapped_and_closes(self):
        fake = FakeSerial([[serial.SerialException("disconnected")]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(DeviceError), Meter("fake", 0.02) as meter:
                meter.read()
        self.assertEqual(fake.close_count, 1)

    def test_next_query_never_reuses_last_reading(self):
        fake = FakeSerial([[KNOWN_RESPONSE], []])
        with patch("tc66c.device.serial.Serial", return_value=fake), Meter("fake", 0.02) as meter:
            self.assertAlmostEqual(meter.read().voltage_v, 5.1234)
            with self.assertRaises(ResponseTimeout):
                meter.read()
            self.assertIsNone(meter.serial)
            self.assertEqual(fake.close_count, 1)
            with self.assertRaises(DeviceError):
                meter.read()
        self.assertEqual(fake.writes, [b"getva", b"getva"])
        self.assertEqual(fake.reset_count, 2)

    def test_keyboard_interrupt_frees_connection(self):
        fake = FakeSerial([[KeyboardInterrupt()]])
        with patch("tc66c.device.serial.Serial", return_value=fake):
            with self.assertRaises(KeyboardInterrupt), Meter("fake", 0.02) as meter:
                meter.read()
        self.assertEqual(fake.close_count, 1)

    def test_open_failure_closes_and_has_actionable_diagnosis(self):
        for code, hint in ((errno.EACCES, "Zugriff"), (errno.EBUSY, "belegt"), (errno.ENOENT, "fehlt")):
            with self.subTest(errno=code):
                fake = FakeSerial(open_error=OSError(code, "open failed"))
                with patch("tc66c.device.serial.Serial", return_value=fake):
                    with self.assertRaisesRegex(DeviceError, hint):
                        with Meter("fake", 0.02):
                            self.fail("Opening an unavailable port must fail")
                self.assertEqual(fake.close_count, 1)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_names_and_exposes_unidentified_ports(self):
        raw_ports = [
            SimpleNamespace(device="/dev/z", description="TC66 USB", manufacturer=None,
                            product=None, vid=0x1234, pid=0x5678),
            SimpleNamespace(device="/dev/a", description="Generic USB CDC", manufacturer=None,
                            product=None, vid=0x1234, pid=0x5678),
            SimpleNamespace(device="/dev/b", description=None, manufacturer="RuiDeng",
                            product=None, vid=None, pid=None),
        ]
        with patch("tc66c.device.list_ports.comports", return_value=raw_ports):
            ports = list_devices()
        self.assertEqual([port.device for port in ports], ["/dev/a", "/dev/b", "/dev/z"])
        self.assertEqual([port.candidate for port in ports], [False, False, True])

    def test_explicit_port_does_not_need_discovery(self):
        with patch("tc66c.device.list_devices") as listing:
            self.assertEqual(select_device("/dev/ttyACM0"), "/dev/ttyACM0")
        listing.assert_not_called()

    def test_unique_candidate_is_selected(self):
        ports = [Port("other", "generic", None, None, False), Port("meter", "TC66", None, None, True)]
        with patch("tc66c.device.list_devices", return_value=ports):
            self.assertEqual(select_device(None), "meter")

    def test_no_candidate_and_ambiguous_candidates_require_selection(self):
        for ports in ([], [Port("a", "TC66", None, None, True), Port("b", "TC66", None, None, True)]):
            with self.subTest(ports=ports), patch("tc66c.device.list_devices", return_value=ports):
                with self.assertRaises(NoDeviceError):
                    select_device(None)


if __name__ == "__main__":
    unittest.main()
