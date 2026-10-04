import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bleak import BleakError

from tc66c import ble
from tc66c.device import DeviceError, NoDeviceError, ResponseTimeout
from tc66c.protocol import ProtocolError, decode_packet
from tests.fixtures import KNOWN_RESPONSE, SANITIZED_BLE_RESPONSE, modified_response


def characteristic(service, identifier, properties):
    return SimpleNamespace(service_uuid=ble._uuid(service), uuid=ble._uuid(identifier), properties=properties)


class FakeClient:
    def __init__(self, responses=(), *, legacy=False, unsupported=False, connect_error=None):
        self.responses = iter(responses)
        write_service, write_id, notify_service, notify_id = (
            ("ffe5", "ffe9", "ffe0", "ffe4") if legacy else ("ffe0", "ffe2", "ffe0", "ffe1")
        )
        chars = {} if unsupported else {
            ble._uuid(write_id): characteristic(write_service, write_id, ["write", "write-without-response"]),
            ble._uuid(notify_id): characteristic(notify_service, notify_id, ["notify"]),
        }
        self.services = SimpleNamespace(get_characteristic=chars.get)
        self.is_connected = False
        self.connect_error = connect_error
        self.notify = None
        self.disconnected_callback = None
        self.disconnect_count = 0
        self.events = []

    def factory(self, address, *, timeout, disconnected_callback):
        self.disconnected_callback = disconnected_callback
        return self

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True
        self.events.append("connect")

    async def start_notify(self, char, callback):
        self.notify = callback
        self.events.append(("notify", char.uuid))

    async def write_gatt_char(self, char, command, *, response):
        self.events.append(("write", char.uuid, command, response))
        if self.notify is None:
            raise AssertionError("Subscription must be active before polling")
        for chunk in next(self.responses, ()):
            if chunk == "disconnect":
                self.is_connected = False
                self.disconnected_callback(self)
            elif isinstance(chunk, BaseException):
                raise chunk
            else:
                # Includes delivery before the write acknowledgement.
                self.notify(None, bytearray(chunk))
                await asyncio.sleep(0)

    async def disconnect(self):
        self.is_connected = False
        self.disconnect_count += 1


class BleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission = patch("tc66c.ble._check_permission")
        permission.start()
        self.addCleanup(permission.stop)

    async def test_both_documented_profiles_subscribe_before_read_only_poll(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                fake = FakeClient([[KNOWN_RESPONSE]], legacy=legacy)
                with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
                    async with ble.BleMeter("fake", 0.05) as meter:
                        reading = await meter.read()
                    self.assertIsNone(meter.client)
                self.assertAlmostEqual(reading.voltage_v, 5.1234)
                self.assertEqual(fake.events[1], ("notify", ble._uuid("ffe4" if legacy else "ffe1")))
                self.assertEqual(fake.events[2], ("write", ble._uuid("ffe9" if legacy else "ffe2"), b"bgetva\r\n", True))
                self.assertEqual(fake.disconnect_count, 1)

    async def test_fragmented_and_coalesced_notifications_are_decoded(self):
        response = KNOWN_RESPONSE + modified_response(48, 50000)
        fake = FakeClient([[response[:7], response[7:19], response[19:]]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 0.05) as meter:
                reading = await meter.read()
        self.assertAlmostEqual(reading.voltage_v, 5)

    async def test_timeout_or_invalid_reply_closes_and_cannot_reuse_late_reply(self):
        for chunks, expected in (([], ResponseTimeout), ([KNOWN_RESPONSE[:-1]], ProtocolError),
                                 ([modified_response(48, 50000, valid_crc=False)], ProtocolError)):
            with self.subTest(expected=expected, size=sum(map(len, chunks))):
                fake = FakeClient([chunks, [KNOWN_RESPONSE]])
                with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
                    async with ble.BleMeter("fake", 0.01) as meter:
                        with self.assertRaises(expected):
                            await meter.read()
                        fake.notify(None, bytearray(KNOWN_RESPONSE))
                        with self.assertRaises(DeviceError):
                            await meter.read()
                        self.assertIsNone(meter.client)
                self.assertEqual(fake.disconnect_count, 1)
                self.assertEqual(len([event for event in fake.events if isinstance(event, tuple) and event[0] == "write"]), 1)

    async def test_successful_sample_is_not_repeated_by_next_request(self):
        fake = FakeClient([[KNOWN_RESPONSE], []])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 0.01) as meter:
                await meter.read()
                fake.notify(None, bytearray(KNOWN_RESPONSE))
                with self.assertRaises(ResponseTimeout):
                    await meter.read()
        self.assertEqual(fake.disconnect_count, 1)

    async def test_disconnect_during_reply_ends_without_a_sample(self):
        fake = FakeClient([[KNOWN_RESPONSE[:80], "disconnect"]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 0.05) as meter:
                with self.assertRaisesRegex(DeviceError, "unterbrochen"):
                    await meter.read()
        self.assertEqual(fake.disconnect_count, 1)

    async def test_receive_limit_applies_even_when_buffer_contains_a_valid_frame(self):
        fake = FakeClient([[KNOWN_RESPONSE + bytes(8193)]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 0.05) as meter:
                with self.assertRaisesRegex(ProtocolError, "8192"):
                    await meter.read()
                self.assertLessEqual(len(meter._buffer), 8192)
        self.assertEqual(fake.disconnect_count, 1)

    async def test_wrong_profile_rejected_before_any_poll_and_connection_closed(self):
        fake = FakeClient(unsupported=True)
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            with self.assertRaisesRegex(DeviceError, "Kanalpaarung"):
                async with ble.BleMeter("fake", 0.05):
                    self.fail("Unsupported GATT profile must not be opened")
        self.assertEqual(fake.events, ["connect"])
        self.assertEqual(fake.disconnect_count, 1)

    async def test_connect_error_is_mapped_and_partial_connection_freed(self):
        fake = FakeClient(connect_error=BleakError("unavailable"))
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            with self.assertRaisesRegex(DeviceError, "unavailable"):
                async with ble.BleMeter("fake", 0.05):
                    self.fail("Connect must fail")
        self.assertEqual(fake.disconnect_count, 1)

    async def test_cancellation_releases_connection(self):
        fake = FakeClient([[]])
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 1) as meter:
                task = asyncio.create_task(meter.read())
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNone(meter.client)
        self.assertEqual(fake.disconnect_count, 1)

    async def test_connection_and_write_acknowledgement_have_bounded_timeouts(self):
        for phase in ("connect", "write"):
            fake = FakeClient()
            async def hang(*args, **kwargs):
                if phase == "write":
                    fake.notify(None, bytearray(KNOWN_RESPONSE))
                await asyncio.Event().wait()
            if phase == "connect":
                fake.connect = hang
            else:
                fake.write_gatt_char = hang
            with self.subTest(phase=phase), patch("tc66c.ble.BleakClient", side_effect=fake.factory):
                with self.assertRaises(ResponseTimeout):
                    async with ble.BleMeter("fake", 0.01) as meter:
                        await meter.read()
            self.assertEqual(fake.disconnect_count, 1)

    async def test_valid_packet_checked_after_deadline_is_rejected(self):
        fake = FakeClient([[KNOWN_RESPONSE]])
        reading = decode_packet(KNOWN_RESPONSE)
        def delayed_decode(data):
            time.sleep(0.02)
            return [reading]
        with patch("tc66c.ble.BleakClient", side_effect=fake.factory):
            async with ble.BleMeter("fake", 0.01) as meter:
                with patch("tc66c.ble.StreamDecoder.feed", side_effect=delayed_decode):
                    with self.assertRaises(ResponseTimeout):
                        await meter.read()
        self.assertEqual(fake.disconnect_count, 1)

    async def test_scan_preserves_discovery_order_and_selects_first_matching_name(self):
        found = {
            "other": (SimpleNamespace(address="headset", name="Headset"),
                      SimpleNamespace(local_name=None, rssi=-40, service_uuids=[])),
            "module": (SimpleNamespace(address="z-first", name="BT24-M"),
                       SimpleNamespace(local_name=None, rssi=-50, service_uuids=[ble._uuid("ffe0")])),
            "second": (SimpleNamespace(address="a-second", name=None),
                       SimpleNamespace(local_name="Adapter bt24-m", rssi=-30, service_uuids=[])),
        }
        async def discover(**kwargs):
            return found
        with patch("tc66c.ble.BleakScanner.discover", side_effect=discover):
            devices = await ble.list_devices(0.05)
            self.assertEqual([device.device for device in devices], ["headset", "z-first", "a-second"])
            self.assertEqual(await ble.select_device(None, 0.05), "z-first")
            found.pop("module")
            self.assertEqual(await ble.select_device(None, 0.05), "a-second")
        with patch("tc66c.ble.list_devices") as scan:
            self.assertEqual(await ble.select_device("module", 0.05), "module")
            scan.assert_not_called()

    async def test_no_matching_name_reports_how_to_find_or_select_another_device(self):
        for devices in ([], [ble.BleDevice("headset", "Headset", -50, (ble._uuid("ffe0"),), False)]):
            with self.subTest(devices=devices), patch("tc66c.ble.list_devices", return_value=devices):
                with self.assertRaises(NoDeviceError) as raised:
                    await ble.select_device(None, 0.05)
                self.assertIn("BT24-M", str(raised.exception))
                self.assertIn('tc66c list --filter ""', str(raised.exception))
                self.assertIn("--device KENNUNG", str(raised.exception))

    def test_sanitized_real_response_retains_units_without_device_identifiers(self):
        reading = decode_packet(SANITIZED_BLE_RESPONSE)
        self.assertEqual(reading.info.model, "TC66")
        self.assertEqual(reading.info.firmware, "1.18")
        self.assertEqual((reading.info.serial_number, reading.info.boot_count), (0, 0))
        self.assertAlmostEqual(reading.voltage_v, 8.9542)
        self.assertAlmostEqual(reading.current_a, 1.42123)
        self.assertAlmostEqual(reading.power_w, 12.72)
        self.assertAlmostEqual(reading.resistance_ohm, reading.calculated_resistance_ohm, places=3)
        self.assertEqual(reading.temperature_value, 28)


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_macos_permission_stops_before_creating_a_scanner(self):
        manager = SimpleNamespace(authorization=lambda: 2)
        with patch("tc66c.ble.sys.platform", "darwin"), patch.dict("sys.modules", {"CoreBluetooth": SimpleNamespace(CBManager=manager)}):
            with patch("tc66c.ble.BleakScanner.discover") as scan:
                with self.assertRaisesRegex(DeviceError, "Datenschutz"):
                    await ble.list_devices()
                scan.assert_not_called()
