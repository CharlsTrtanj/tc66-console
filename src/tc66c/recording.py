"""Streaming measurement files, independent of the console output format."""

from contextlib import contextmanager
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from typing import TextIO

from .protocol import Reading


CSV_FIELDS = (
    "received_at", "model", "firmware", "serial_number", "boot_count",
    "voltage_v", "current_a", "power_w", "resistance_raw", "resistance_ohm",
    "temperature_value", "temperature_unit",
    "charge_group0_mah", "energy_group0_mwh", "charge_group1_mah", "energy_group1_mwh",
    "data_plus_v", "data_minus_v", "calculated_power_w", "calculated_resistance_ohm",
)


class RecordingError(Exception):
    """A measurement file cannot be opened, appended to or written."""


def measurement_record(reading: Reading, unit: str) -> dict:
    values = asdict(reading)
    values.pop("info")
    values["resistance_ohm"] = reading.resistance_ohm
    values["temperature_unit"] = None if unit == "device" else unit
    return {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "device": asdict(reading.info),
        "device_values": values,
        "calculated": {"power_w": reading.calculated_power_w,
                       "resistance_ohm": reading.calculated_resistance_ohm},
    }


class MeasurementLog:
    def __init__(self, stream: TextIO, format: str) -> None:
        self.stream = stream
        self.format = format
        self.csv = csv.DictWriter(stream, fieldnames=CSV_FIELDS) if format == "csv" else None

    def write(self, record: dict) -> None:
        try:
            self._write(record)
            # Preserve completed readings when a later poll fails or the command is interrupted.
            self.stream.flush()
        except (OSError, UnicodeError, csv.Error) as error:
            raise RecordingError(f"{self.stream.name}: Messprotokoll konnte nicht geschrieben werden ({error}).") from error

    def _write(self, record: dict) -> None:
        if self.csv is not None:
            values = record["device_values"]
            row = {"received_at": record["received_at"], **record["device"]}
            row.update({key: values[key] for key in (
                "voltage_v", "current_a", "power_w", "resistance_raw", "resistance_ohm",
                "temperature_value", "temperature_unit", "data_plus_v", "data_minus_v",
            )})
            for group in (0, 1):
                row[f"charge_group{group}_mah"] = values["charge_mah"][group]
                row[f"energy_group{group}_mwh"] = values["energy_mwh"][group]
            row.update({f"calculated_{key}": value for key, value in record["calculated"].items()})
            self.csv.writerow(row)
        else:
            self.stream.write(json.dumps(record) + "\n")


@contextmanager
def open_log(path: str, format: str):
    stream = None
    try:
        try:
            stream = open(path, "a+", encoding="utf-8", newline="")
            log = MeasurementLog(stream, format)
            stream.seek(0)
            first_line = stream.readline()
            if first_line:
                if format == "csv":
                    compatible = next(csv.reader([first_line])) == list(CSV_FIELDS)
                else:
                    first = json.loads(first_line)
                    compatible = isinstance(first, dict) and set(first) == {
                        "received_at", "device", "device_values", "calculated",
                    }
                if not compatible:
                    raise RecordingError(f"{path}: vorhandene Datei passt nicht zum Messprotokoll ({format}).")
                stream.seek(0, 2)
                stream.seek(stream.tell() - 1)
                if stream.read(1) != "\n":
                    raise RecordingError(f"{path}: letzte Zeile ist unvollständig; anderen Dateinamen verwenden.")
            elif log.csv is not None:
                log.csv.writeheader()
                stream.flush()
            stream.seek(0, 2)
        except (OSError, UnicodeError, csv.Error, json.JSONDecodeError) as error:
            raise RecordingError(f"{path}: Messprotokoll konnte nicht geöffnet oder geschrieben werden ({error}).") from error
        yield log
    finally:
        if stream is not None:
            try:
                stream.close()
            except (OSError, UnicodeError) as error:
                raise RecordingError(f"{path}: Messprotokoll konnte nicht geschlossen werden ({error}).") from error
