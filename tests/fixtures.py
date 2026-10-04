"""Synthetic data plus a sanitized response derived from a real BLE capture.

The constant response encodes TC66 / 1.14, made-up serial 424242, 42 boots,
5.1234 V, 0.12345 A, 0.6324 W, 123.4 ohm, groups (123, 456)/(789, 987),
temperature -7 in the device's configured unit and D+ / D- 3.30 / 2.80 V.
The encrypted bytes are fixed so unit or key errors cannot silently change
the expected reference along with the implementation.
"""

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


KEY = bytes.fromhex(
    "5821fa5601b2f02687ff1204622a4fb086f40260816f9a0ba7f106619ab87288"
)
KNOWN_RESPONSE = bytes.fromhex(
    "dcd19f99a61be7b21a048976e90a96d32aca6ec4a4554d5542e8674b097e259c"
    "c0a1c6a01367b6f0e8c86f46107e46f402b5287d9cfd2fbfe80683035efc33fe"
    "2e9d1a256f12b8e85e6f93faa8cce0970f362f52808e4145a5a53314667e2baa7"
    "98068b126b6ca315a043b292f87f849b35a40c6dbb69ba8027b2218cb7fec4a4"
    "231f411e56f5c203c590b4a68fd5b092aca6ec4a4554d5542e8674b097e259c"
    "2aca6ec4a4554d5542e8674b097e259ce09c4004c92da948c3165eebdb8b9ec2"
)

# Derived from a user-supplied BLE response on 2026-10-04 (TC66 / 1.18).
# Serial, boot count, unknown metadata, accumulated counters and D+/D- were
# zeroed at offsets 12:48, 72:88, 96:124 and 132:188 before recalculating all
# CRCs and re-encrypting. The original personal packet is not published.
# Retained measurement fields: 8.9542 V, 1.42123 A, 12.72 W, 6.3 ohm, 28 in
# the device's configured unit. This is edited reference data, not a fresh
# hardware test by the application. See docs/validation.md.
SANITIZED_BLE_RESPONSE = bytes.fromhex(
    "1b5feb8e9cba25974d4715b85b56603b2aca6ec4a4554d5542e8674b097e259c"
    "2aca6ec4a4554d5542e8674b097e259cbd6bfe2a867299ecf96613161a6b3871"
    "985453b7ea1c98e73dcb4b8d3923121ad6513f2d136a7dc2faed89bfc4c48d6c"
    "2aca6ec4a4554d5542e8674b097e259c7b471b200edfbb9f6235c9c08f8f88fd"
    "4231f411e56f5c203c590b4a68fd5b092aca6ec4a4554d5542e8674b097e259c"
    "2aca6ec4a4554d5542e8674b097e259ce09c4004c92da948c3165eebdb8b9ec2"
)


def fixture_crc(data):
    """Fixture-only MODBUS CRC, independent of the application helper."""
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def plaintext():
    data = bytearray(192)
    data[:12] = b"pac1TC661.14"
    data[64:68] = b"pac2"
    data[128:132] = b"pac3"
    for offset, value in {
        12: 424242, 44: 42, 48: 51234, 52: 12345, 56: 6324,
        68: 1234, 72: 123, 76: 456, 80: 789, 84: 987,
        88: 1, 92: 7, 96: 330, 100: 280,
    }.items():
        struct.pack_into("<I", data, offset, value)
    update_crc(data)
    return data


def update_crc(data):
    for start in (0, 64, 128):
        struct.pack_into("<I", data, start + 60, fixture_crc(data[start:start + 60]))


def encrypt(data):
    context = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return context.update(data) + context.finalize()


def modified_response(offset, value, *, valid_crc=True):
    data = plaintext()
    struct.pack_into("<I", data, offset, value)
    if valid_crc:
        update_crc(data)
    return encrypt(data)
