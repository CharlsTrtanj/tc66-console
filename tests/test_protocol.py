import struct
import unittest

from tc66c.protocol import ProtocolError, StreamDecoder, crc16, decode_packet

from tests.fixtures import (
    KNOWN_RESPONSE, encrypt, fixture_crc, modified_response, plaintext, update_crc,
)


class ProtocolTests(unittest.TestCase):
    def test_modbus_known_reference(self):
        self.assertEqual(crc16(b"123456789"), 0x4B37)
        self.assertEqual(fixture_crc(b"123456789"), 0x4B37)

    def test_fixed_encrypted_reference_units_and_identity(self):
        self.assertEqual(encrypt(plaintext()), KNOWN_RESPONSE)
        result = decode_packet(KNOWN_RESPONSE)
        self.assertEqual(result.info.model, "TC66")
        self.assertEqual(result.info.firmware, "1.14")
        self.assertEqual(result.info.serial_number, 424242)
        self.assertEqual(result.info.boot_count, 42)
        self.assertAlmostEqual(result.voltage_v, 5.1234)
        self.assertAlmostEqual(result.current_a, 0.12345)
        self.assertAlmostEqual(result.power_w, 0.6324)
        self.assertEqual(result.resistance_raw, 1234)
        self.assertAlmostEqual(result.resistance_ohm, 123.4)
        self.assertEqual(result.charge_mah, (123, 789))
        self.assertEqual(result.energy_mwh, (456, 987))
        self.assertEqual(result.temperature_value, -7)
        self.assertAlmostEqual(result.data_plus_v, 3.3)
        self.assertAlmostEqual(result.data_minus_v, 2.8)
        # Reported power and resistance must remain distinct from V*I and V/I.
        self.assertAlmostEqual(result.calculated_power_w, 0.63248373)
        self.assertAlmostEqual(result.calculated_resistance_ohm, 5.1234 / 0.12345)

    def test_zero_current_has_no_calculated_resistance(self):
        result = decode_packet(modified_response(52, 0))
        self.assertEqual(result.current_a, 0)
        self.assertIsNone(result.calculated_resistance_ohm)

    def test_positive_temperature(self):
        self.assertEqual(decode_packet(modified_response(88, 0)).temperature_value, 7)

    def test_every_subpacket_checksum_is_checked(self):
        for offset in (12, 72, 140):
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_packet(modified_response(offset, 1, valid_crc=False))

    def test_checksum_upper_bytes_must_be_zero(self):
        data = plaintext()
        checksum = struct.unpack_from("<I", data, 60)[0]
        struct.pack_into("<I", data, 60, checksum | 0x10000)
        with self.assertRaises(ProtocolError):
            decode_packet(encrypt(data))

    def test_subpackets_must_have_ordered_headers(self):
        data = plaintext()
        data[64:68] = b"pac3"
        update_crc(data)
        with self.assertRaises(ProtocolError):
            decode_packet(encrypt(data))

    def test_exact_packet_length_is_required(self):
        for data in (b"", KNOWN_RESPONSE[:-1], KNOWN_RESPONSE + b"x"):
            with self.subTest(length=len(data)), self.assertRaises(ProtocolError):
                decode_packet(data)

    def test_stream_reassembles_single_byte_fragments(self):
        stream = StreamDecoder()
        results = []
        for value in KNOWN_RESPONSE:
            results.extend(stream.feed(bytes([value])))
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].voltage_v, 5.1234)
        self.assertEqual(stream.feed(b""), [])

    def test_stream_delivers_coalesced_responses(self):
        stream = StreamDecoder()
        results = stream.feed(KNOWN_RESPONSE + modified_response(48, 50000))
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(results[0].voltage_v, 5.1234)
        self.assertAlmostEqual(results[1].voltage_v, 5.0)

    def test_stream_waits_for_complete_response(self):
        stream = StreamDecoder()
        self.assertEqual(stream.feed(KNOWN_RESPONSE[:191]), [])
        self.assertEqual(len(stream.feed(KNOWN_RESPONSE[191:])), 1)

    def test_stream_recovers_after_noise_and_invalid_response(self):
        stream = StreamDecoder()
        invalid = modified_response(48, 50000, valid_crc=False)
        results = stream.feed(b"noise" + invalid + KNOWN_RESPONSE)
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].voltage_v, 5.1234)

    def test_invalid_response_is_never_a_reading(self):
        stream = StreamDecoder()
        self.assertEqual(stream.feed(modified_response(48, 50000, valid_crc=False)), [])
        self.assertIsNotNone(stream.last_error)

    def test_stream_has_bounded_input_buffer(self):
        with self.assertRaises(ProtocolError):
            StreamDecoder().feed(b"x" * 8193)


if __name__ == "__main__":
    unittest.main()
