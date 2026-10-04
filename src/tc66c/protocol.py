"""TC66 poll response decoding; see docs/protocol.md for evidence."""

from dataclasses import dataclass
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Public protocol constant, shared by these meters; not a personal credential.
_KEY = bytes.fromhex(
    "5821fa5601b2f02687ff1204622a4fb086f40260816f9a0ba7f106619ab87288"
)
PACKET_SIZE = 192


class ProtocolError(ValueError):
    """A response is incomplete, corrupt or from an unsupported meter."""


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS: reflected 0xA001, initial 0xFFFF, no final XOR."""
    result = 0xFFFF
    for value in data:
        result ^= value
        for _ in range(8):
            result = (result >> 1) ^ (0xA001 if result & 1 else 0)
    return result


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    firmware: str
    serial_number: int
    boot_count: int


@dataclass(frozen=True)
class Reading:
    info: DeviceInfo
    voltage_v: float
    current_a: float
    power_w: float
    temperature_value: int
    resistance_raw: int
    charge_mah: tuple[int, int]
    energy_mwh: tuple[int, int]
    data_plus_v: float
    data_minus_v: float

    @property
    def resistance_ohm(self) -> float:
        return self.resistance_raw / 10

    @property
    def calculated_power_w(self) -> float:
        return self.voltage_v * self.current_a

    @property
    def calculated_resistance_ohm(self) -> float | None:
        return self.voltage_v / self.current_a if self.current_a else None


def decode_packet(encrypted: bytes) -> Reading:
    if len(encrypted) != PACKET_SIZE:
        raise ProtocolError(f"Antwortlänge {len(encrypted)} statt 192 Byte.")
    decryptor = Cipher(algorithms.AES(_KEY), modes.ECB()).decryptor()
    data = decryptor.update(encrypted) + decryptor.finalize()
    for index in range(3):
        start = index * 64
        magic = f"pac{index + 1}".encode("ascii")
        if data[start : start + 4] != magic:
            raise ProtocolError(f"Paketkennung {magic.decode()} fehlt.")
        expected = struct.unpack_from("<I", data, start + 60)[0]
        if crc16(data[start : start + 60]) != expected:
            raise ProtocolError(f"Prüfsumme von {magic.decode()} ungültig.")
    if data[4:8] != b"TC66":
        raise ProtocolError("Nicht unterstütztes Gerätemodell (erwartet TC66).")
    firmware = data[8:12].rstrip(b"\x00")
    if not firmware or any(value < 32 or value > 126 for value in firmware):
        raise ProtocolError("Ungültige Firmwarekennung.")

    def u32(offset: int) -> int:
        return struct.unpack_from("<I", data, offset)[0]

    sign = u32(88)
    if sign not in (0, 1):
        raise ProtocolError("Ungültiges Temperaturvorzeichen.")
    return Reading(
        info=DeviceInfo("TC66", firmware.decode("ascii"), u32(12), u32(44)),
        voltage_v=u32(48) / 10000,
        current_a=u32(52) / 100000,
        power_w=u32(56) / 10000,
        temperature_value=u32(92) * (-1 if sign else 1),
        resistance_raw=u32(68),
        charge_mah=(u32(72), u32(80)),
        energy_mwh=(u32(76), u32(84)),
        data_plus_v=u32(96) / 100,
        data_minus_v=u32(100) / 100,
    )


class StreamDecoder:
    """Accumulate fragmented responses and resynchronize after damaged bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.last_error: str | None = None

    def feed(self, data: bytes) -> list[Reading]:
        self.buffer.extend(data)
        if len(self.buffer) > 8192:
            raise ProtocolError("Antwort überschreitet das Empfangslimit (8192 Byte).")
        readings = []
        while len(self.buffer) >= PACKET_SIZE:
            try:
                reading = decode_packet(bytes(self.buffer[:PACKET_SIZE]))
            except ProtocolError as error:
                self.last_error = str(error)
                del self.buffer[0]
            else:
                readings.append(reading)
                del self.buffer[:PACKET_SIZE]
        return readings
