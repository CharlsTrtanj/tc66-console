"""Bounded BLE polling using the documented TC66 UART channel pairs."""

import asyncio
from dataclasses import dataclass
import math
import sys

from bleak import BleakClient, BleakError, BleakScanner

from .device import DeviceError, NoDeviceError, ResponseTimeout
from .protocol import ProtocolError, Reading, StreamDecoder


def _uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


# Write service/characteristic, notification service/characteristic.
# The first pair matches the user's BT24-M GATT discovery; both are documented
# by kolinger/rd-usb. See docs/protocol.md. No arbitrary UUID probing is used.
_PROFILES = (("ffe0", "ffe2", "ffe0", "ffe1"), ("ffe5", "ffe9", "ffe0", "ffe4"))


def _check_permission() -> None:
    if sys.platform == "darwin":
        from CoreBluetooth import CBManager

        authorization = CBManager.authorization()
        if authorization in (1, 2):
            raise DeviceError(
                "Bluetooth-Zugriff für diese Anwendung verweigert oder eingeschränkt. "
                "macOS: Systemeinstellungen → Datenschutz & Sicherheit → Bluetooth prüfen."
            )


def _error(error: Exception) -> DeviceError:
    return DeviceError(
        f"BLE-Verbindung fehlgeschlagen oder abgebrochen: {error}. "
        "Bluetooth am Messgerät und Rechner, App-Berechtigung und Belegung prüfen; "
        "unter Linux sind BlueZ und der System-D-Bus erforderlich."
    )


@dataclass(frozen=True)
class BleDevice:
    device: str
    name: str
    rssi: int
    services: tuple[str, ...]
    candidate: bool


async def list_devices(timeout: float = 5.0) -> list[BleDevice]:
    _check_permission()
    try:
        # The extra second bounds scanner startup/cleanup as well as the scan.
        async with asyncio.timeout(timeout + 1):
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TimeoutError as error:
        raise ResponseTimeout("BLE-Suche konnte nicht innerhalb des Timeouts beendet werden.") from error
    except (BleakError, OSError) as error:
        raise _error(error) from error
    devices = []
    for device, advertisement in found.values():
        name = advertisement.local_name or device.name or "unbekannt"
        devices.append(BleDevice(
            device.address, name, advertisement.rssi,
            tuple(advertisement.service_uuids), "tc66" in name.casefold(),
        ))
    # Keep discovery order so automatic selection uses the first matching device.
    return devices


async def select_device(address: str | None, timeout: float) -> str:
    if address:
        return address
    for device in await list_devices(timeout):
        if "bt24-m" in device.name.casefold():
            return device.device
    raise NoDeviceError(
        "Kein BLE-Gerät mit 'BT24-M' im Namen gefunden. Messgerät einschalten und "
        "Bluetooth-Datenübertragung aktivieren. Mit 'tc66c list --filter \"\"' alle "
        "Geräte anzeigen; ein anderes Gerät mit --device KENNUNG auswählen."
    )


class BleMeter:
    def __init__(self, address: str, timeout: float = 15.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout muss endlich und größer als null sein.")
        self.address = address
        self.timeout = timeout
        self.client: BleakClient | None = None
        self._write_char = None
        self._notify_char = None
        self._pending: asyncio.Event | None = None
        self._buffer = bytearray()
        self._received = 0
        self._lost = False

    async def __aenter__(self) -> "BleMeter":
        _check_permission()
        self._write_char = self._notify_char = None
        self._lost = False
        try:
            self.client = BleakClient(
                self.address, timeout=self.timeout, disconnected_callback=self._disconnected,
            )
            async with asyncio.timeout(self.timeout):
                await self.client.connect()
                for write_service, write_id, notify_service, notify_id in _PROFILES:
                    write_char = self.client.services.get_characteristic(_uuid(write_id))
                    notify_char = self.client.services.get_characteristic(_uuid(notify_id))
                    if (write_char is not None and notify_char is not None
                            and write_char.service_uuid.lower() == _uuid(write_service)
                            and notify_char.service_uuid.lower() == _uuid(notify_service)
                            and "write" in write_char.properties
                            and "notify" in notify_char.properties):
                        self._write_char, self._notify_char = write_char, notify_char
                        break
                if self._write_char is None:
                    raise DeviceError("Gerät bietet keine belegte TC66-BLE-Kanalpaarung an.")
                # Await subscription before sending any request; notifications may
                # arrive before the write acknowledgement, so polling must not
                # subscribe after writing the command.
                await self.client.start_notify(self._notify_char, self._notification)
        except TimeoutError as error:
            await self.close()
            raise ResponseTimeout("Timeout beim Verbinden oder Einrichten der BLE-Verbindung.") from error
        except (BleakError, OSError) as error:
            await self.close()
            raise _error(error) from error
        except BaseException:
            await self.close()
            raise
        return self

    def _disconnected(self, client: BleakClient) -> None:
        self._lost = True
        if self._pending is not None:
            self._pending.set()

    def _notification(self, sender: object, data: bytearray) -> None:
        if self._pending is None:
            return
        self._received += len(data)
        if self._received <= 8192:
            self._buffer.extend(data)
        self._pending.set()

    async def close(self) -> None:
        self._pending = None
        self._buffer.clear()
        client, self.client = self.client, None
        if client is not None:
            try:
                async with asyncio.timeout(self.timeout):
                    # Disconnect also stops notifications, including partial setup.
                    await client.disconnect()
            except (TimeoutError, BleakError, OSError) as error:
                raise DeviceError(f"BLE-Verbindung konnte nicht freigegeben werden: {error}") from error

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def read(self) -> Reading:
        try:
            return await self._read()
        except BaseException:
            # Late replies after a failed request cannot become a later sample.
            await self.close()
            raise
        finally:
            self._pending = None
            self._buffer.clear()

    async def _read(self) -> Reading:
        if self.client is None or not self.client.is_connected or self._lost:
            raise DeviceError("BLE-Verbindung ist nicht geöffnet oder wurde unterbrochen.")
        decoder = StreamDecoder()
        self._buffer.clear()
        self._received = 0
        self._pending = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        sent = False
        try:
            async with asyncio.timeout_at(deadline):
                await self.client.write_gatt_char(self._write_char, b"bgetva\r\n", response=True)
                sent = True
                while True:
                    await self._pending.wait()
                    self._pending.clear()
                    if self._lost or not self.client.is_connected:
                        raise DeviceError("BLE-Verbindung während der Abfrage unterbrochen.")
                    if self._received > 8192:
                        raise ProtocolError("Antwort überschreitet das Empfangslimit (8192 Byte).")
                    chunk = bytes(self._buffer)
                    self._buffer.clear()
                    readings = decoder.feed(chunk)
                    if readings:
                        if loop.time() >= deadline:
                            raise ResponseTimeout("Antwort konnte nicht innerhalb des Timeouts geprüft werden.")
                        return readings[-1]
        except TimeoutError as error:
            if not sent:
                raise ResponseTimeout("Timeout beim Senden der BLE-Messabfrage.") from error
            if decoder.last_error:
                raise ProtocolError(f"Keine gültige BLE-Antwort: {decoder.last_error}") from error
            if self._received:
                raise ProtocolError(f"Unvollständige BLE-Antwort: {self._received} Byte vor Ablauf des Timeouts.") from error
            raise ResponseTimeout("Keine BLE-Messwertantwort innerhalb des Timeouts.") from error
        except (BleakError, OSError) as error:
            raise _error(error) from error
