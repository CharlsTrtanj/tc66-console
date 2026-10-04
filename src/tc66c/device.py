"""USB serial discovery and bounded, read-only polling."""

from dataclasses import dataclass
import errno
import math
import time

import serial
from serial.tools import list_ports

from .protocol import ProtocolError, Reading, StreamDecoder


class DeviceError(Exception):
    exit_code = 4


class NoDeviceError(DeviceError):
    exit_code = 3


class ResponseTimeout(DeviceError):
    exit_code = 5


@dataclass(frozen=True)
class Port:
    device: str
    description: str
    vid: int | None
    pid: int | None
    candidate: bool


def list_devices() -> list[Port]:
    ports = []
    try:
        for port in list_ports.comports():
            description = port.description or "unbekannt"
            identity = " ".join(
                value or "" for value in (description, port.manufacturer, port.product)
            ).casefold()
            # No TC66-specific VID/PID is established by the available sources.
            candidate = "tc66" in identity
            ports.append(Port(port.device, description, port.vid, port.pid, candidate))
    except (OSError, serial.SerialException) as error:
        raise DeviceError(f"Schnittstellen konnten nicht aufgelistet werden: {error}") from error
    return sorted(ports, key=lambda port: port.device)


def select_device(path: str | None) -> str:
    if path:
        return path
    candidates = [port.device for port in list_devices() if port.candidate]
    if not candidates:
        raise NoDeviceError(
            "Kein eindeutiger TC66-Kandidat gefunden. Micro-USB-Datenkabel und "
            "USB/PC-Kommunikation im Gerätemenü prüfen; 'tc66c list' zeigt alle "
            "seriellen Anschlüsse. Generische USB-CDC-Ports mit --device auswählen."
        )
    if len(candidates) > 1:
        raise NoDeviceError(
            "Mehrere passende Geräte: " + ", ".join(candidates) + ". Bitte --device angeben."
        )
    return candidates[0]


def _connection_error(port: str, error: Exception) -> DeviceError:
    code = getattr(error, "errno", None)
    if code in (errno.EACCES, errno.EPERM):
        hint = "Zugriff verweigert; unter Linux Geräte-Gruppe/ACL prüfen (siehe README)."
    elif code in (errno.EBUSY, errno.EAGAIN) or "exclusively lock" in str(error).lower():
        hint = "Schnittstelle belegt; andere Messgeräte-Programme schließen."
    elif code == errno.ENOENT:
        hint = "Schnittstelle fehlt; Gerät/Kabel und Pfad prüfen."
    else:
        hint = "Verbindung fehlgeschlagen oder abgebrochen; Kabel, Port und Belegung prüfen."
    return DeviceError(f"{port}: {hint} ({error})")


class Meter:
    def __init__(self, port: str, timeout: float = 3.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout muss endlich und größer als null sein.")
        self.port = port
        self.timeout = timeout
        self.serial: serial.Serial | None = None

    def __enter__(self) -> "Meter":
        try:
            self.serial = serial.Serial(
                port=None, baudrate=115200, timeout=min(0.1, self.timeout),
                write_timeout=self.timeout, exclusive=True,
            )
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = self.port
            self.serial.open()
        except (serial.SerialException, OSError) as error:
            self.close()
            raise _connection_error(self.port, error) from error
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self.serial is not None:
            connection = self.serial
            self.serial = None
            connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def read(self) -> Reading:
        try:
            return self._read()
        except BaseException:
            # Late data after a failed request must never become the next sample.
            self.close()
            raise

    def _read(self) -> Reading:
        if self.serial is None:
            raise DeviceError("Verbindung ist nicht geöffnet.")
        decoder = StreamDecoder()
        received = 0
        deadline = time.monotonic() + self.timeout
        try:
            # Discard local transport leftovers; this does not reset meter data.
            self.serial.reset_input_buffer()
            if self.serial.write(b"getva") != 5:
                raise DeviceError("Messabfrage konnte nicht vollständig gesendet werden.")
            while time.monotonic() < deadline:
                self.serial.timeout = min(0.1, max(0, deadline - time.monotonic()))
                chunk = self.serial.read(min(4096, max(1, self.serial.in_waiting)))
                received += len(chunk)
                if received > 8192:
                    raise ProtocolError("Antwort überschreitet das Empfangslimit (8192 Byte).")
                readings = decoder.feed(chunk)
                if readings:
                    if time.monotonic() >= deadline:
                        raise ResponseTimeout("Antwort konnte nicht innerhalb des Timeouts geprüft werden.")
                    # Coalesced replies: emit only the latest validated response.
                    return readings[-1]
        except serial.SerialTimeoutException as error:
            raise ResponseTimeout("Timeout beim Senden der Messabfrage.") from error
        except (serial.SerialException, OSError) as error:
            raise _connection_error(self.port, error) from error
        if decoder.last_error:
            raise ProtocolError(f"Keine gültige Antwort: {decoder.last_error}")
        if received:
            raise ProtocolError(f"Unvollständige Antwort: {received} Byte vor Ablauf des Timeouts.")
        raise ResponseTimeout(
            "Keine Antwort innerhalb des Timeouts. USB/PC-Kommunikation am Gerät "
            "aktivieren und Micro-USB-Datenverbindung prüfen."
        )
