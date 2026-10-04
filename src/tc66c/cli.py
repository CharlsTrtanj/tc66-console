"""Console commands; device values and calculations are labelled separately."""

import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
import json
import math
import sys
import time

from . import cache
from .device import DeviceError, Meter, list_devices, select_device
from .protocol import DeviceInfo, ProtocolError, Reading
from .recording import MeasurementLog, RecordingError, measurement_record, open_log


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Wert muss endlich und größer als null sein.")
    return number


def _interval(value: str) -> float:
    number = _positive(value)
    if number < 0.5:
        raise argparse.ArgumentTypeError("Intervall muss mindestens 0,5 Sekunden sein (Gerät: 2 Hz).")
    return number


def _count(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("Anzahl muss größer als null sein.")
    return count


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tc66c",
        description=(
            "TC66/TC66C über Bluetooth LE oder eine serielle USB-Verbindung auslesen.\n\n"
            "Bluetooth LE ist der Standard. Die Gerätekennung wird 24 Stunden gemerkt.\n"
            "Ohne --device wird die gemerkte Kennung verwendet. Fehlt sie, wird nach\n"
            "dem ersten Gerät mit BT24-M im Namen gesucht. Zum Messen reicht:\n"
            "  tc66c read"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Optionen stehen hinter dem jeweiligen Befehl, zum Beispiel:
  tc66c read --timeout 30

Bei allen Befehlen:
  --transport ble|usb   Verbindung wählen; Standard: ble (Bluetooth LE).
                       Für eine serielle Verbindung --transport usb angeben.
  --timeout SEKUNDEN   Wartezeit ändern: list sucht standardmäßig 5 Sekunden;
                       info/read/watch warten bei BLE je Suche, Verbindungsaufbau
                       und Antwort 15 Sekunden, bei USB auf die Antwort 3 Sekunden.
  --json               Ergebnisse als JSON ausgeben; watch: eine Zeile pro Messung.

Bei list:
  --filter NAME        Teil des Gerätenamens suchen, bei USB der Beschreibung.
                       Standard: BT24-M; Groß-/Kleinschreibung spielt keine Rolle.
                       --filter "" zeigt alle Geräte. Gilt nur für die Liste.

Bei info, read und watch:
  --device KENNUNG     Ein bestimmtes Gerät aus list wählen. Ohne diese Option
                       wird die gemerkte BLE-Kennung (24 Stunden) verwendet;
                       fehlt sie, wird das erste gefundene BT24-M gewählt.

Die 24 Stunden zählen ab der Speicherung. Folgeaufrufe verlängern sie nicht.
tc66c list sucht immer aktuell und erneuert die gemerkte Kennung. Kann das
gespeicherte Gerät nicht ausgelesen werden, wird vor der ersten Ausgabe einmal
neu gesucht. --device hat Vorrang und wird nach erfolgreicher Abfrage gemerkt.

Bei read und watch:
  --output DATEI       Jede Messung in eine Datei schreiben; Standardformat: CSV.
                       Vorhandene Messprotokolle werden ergänzt, nicht überschrieben.
  --output-format csv|jsonl
                       Dateiformat wählen (Standard: csv); benötigt --output DATEI.
  --temperature-unit device|C|F
                       Temperatureinheit vom Display übernehmen: C = Celsius,
                       F = Fahrenheit. Standard: device (Einheit unbekannt).
                       Kennzeichnet die Einheit; rechnet den Wert nicht um.

Bei watch:
  --interval SEKUNDEN  Zeit zwischen Messungen; Standard: 1, mindestens 0,5.
  --count ANZAHL       Nach dieser Anzahl Messungen beenden; sonst bis Strg+C.

Vollständige Beispiele:
  tc66c list
  tc66c info
  tc66c read
  tc66c watch
  tc66c watch --interval 1 --count 60 --timeout 30
  tc66c list --timeout 15 --filter "TC66"
  tc66c list --filter ""
  tc66c read --temperature-unit C --json
  tc66c read --output messungen.csv
  tc66c watch --output messungen.csv --temperature-unit C
  tc66c watch --output messungen.jsonl --output-format jsonl --count 60
  tc66c list --transport usb --filter ""
  tc66c read --transport usb --device /dev/ttyACM0 --timeout 5

Im USB-Beispiel /dev/ttyACM0 durch den Anschluss aus der USB-Liste ersetzen.
C oder F nur wählen, wenn diese Einheit am Display eingestellt ist.
Weitere Beispiele und Befehlsoptionen: tc66c list --help, tc66c info --help,
tc66c read --help und tc66c watch --help.''',
    )
    commands = result.add_subparsers(dest="command", required=True, title="Befehle", metavar="BEFEHL")
    listing = commands.add_parser(
        "list", help="Verfügbare Messgeräte anzeigen",
        description="BLE-Geräte suchen und Kennung sowie Name anzeigen; Standardfilter: BT24-M.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Vollständige Beispiele:
  tc66c list
  tc66c list --timeout 15
  tc66c list --filter "TC66"
  tc66c list --filter ""
  tc66c list --json
  tc66c list --transport usb --filter ""

Die BLE-Liste sucht immer aktuell und merkt das erste BT24-M für 24 Stunden.
--filter "" zeigt alle Geräte. Bei USB wird die Anschlussbeschreibung gefiltert.
Die erste Spalte enthält die Kennung für eine optionale Auswahl mit --device.''',
    )
    listing.add_argument("--json", action="store_true", help="JSON ausgeben")
    listing.add_argument("--transport", choices=("usb", "ble"), default="ble", help="Verbindung: ble = Bluetooth LE (Standard), usb = serieller USB-Anschluss")
    listing.add_argument("--timeout", type=_positive, default=5.0, metavar="SEKUNDEN", help="Dauer der Bluetooth-Suche in Sekunden (Standard: 5)")
    listing.add_argument("--filter", default="BT24-M", metavar="NAME", help='Teil des Gerätenamens (USB: Beschreibung), ohne Beachtung der Groß-/Kleinschreibung (Standard: BT24-M; --filter "": alle)')
    examples = {
        "info": '''Vollständige Beispiele:
  tc66c info
  tc66c info --timeout 30 --json
  tc66c info --device KENNUNG_AUS_LIST
  tc66c info --transport usb --device /dev/cu.usbmodem1234 --timeout 5''',
        "read": '''Vollständige Beispiele:
  tc66c read
  tc66c read --timeout 30
  tc66c read --temperature-unit C --json
  tc66c read --output messungen.csv
  tc66c read --device KENNUNG_AUS_LIST
  tc66c read --transport usb --device /dev/ttyACM0 --timeout 5''',
        "watch": '''Vollständige Beispiele:
  tc66c watch
  tc66c watch --interval 1 --count 60 --timeout 30
  tc66c watch --temperature-unit C --json
  tc66c watch --output messungen.csv --temperature-unit C
  tc66c watch --output messungen.jsonl --output-format jsonl --count 60
  tc66c watch --device KENNUNG_AUS_LIST --count 10
  tc66c watch --transport usb --device /dev/ttyACM0 --interval 1 --count 10

Ohne --count läuft die Anzeige bis Strg+C; dabei wird die Verbindung geschlossen.''',
    }
    for name, help_text in (
        ("info", "Modell, Firmware und Seriennummer anzeigen"),
        ("read", "Einen aktuellen Messwertsatz auslesen"),
        ("watch", "Fortlaufend messen; Strg+C beendet und schließt die Verbindung"),
    ):
        command = commands.add_parser(
            name, help=help_text,
            description=(
                f"{help_text}.\n\n"
                "Standard: Bluetooth LE. Ohne --device wird die für 24 Stunden gemerkte\n"
                "Kennung verwendet; fehlt sie, wird das erste BT24-M gesucht.\n"
                "Zum erneuten Suchen und Merken der Kennung: tc66c list."
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=examples[name] + '''

KENNUNG_AUS_LIST durch die erste Spalte von tc66c list ersetzen.
Im USB-Beispiel den tatsächlichen Anschluss aus
tc66c list --transport usb --filter "" verwenden.'''
            + ("\nC oder F nur wählen, wenn diese Einheit am Display eingestellt ist."
               if name in ("read", "watch") else ""),
        )
        command.add_argument("--transport", choices=("usb", "ble"), default="ble", help="Verbindung: ble = Bluetooth LE (Standard), usb = serieller USB-Anschluss")
        command.add_argument("--device", metavar="KENNUNG", help="BLE-Kennung oder USB-Port aus list; BLE-Standard: gemerkte Kennung (24 Stunden), sonst erstes BT24-M")
        command.add_argument("--timeout", type=_positive, metavar="SEKUNDEN", help="Wartezeit in Sekunden (Standard: BLE 15 je Suche, Verbindung und Antwort; USB 3)")
        command.add_argument("--json", action="store_true", help="JSON; bei watch eine Zeile pro Antwort")
        if name in ("read", "watch"):
            command.add_argument(
                "--output", metavar="DATEI",
                help="Messungen in diese Datei schreiben; Standard: CSV. Vorhandene Messprotokolle ergänzen",
            )
            command.add_argument(
                "--output-format", choices=("csv", "jsonl"),
                help="Dateiformat für --output (Standard: csv); unabhängig von --json und der Dateiendung",
            )
            command.add_argument(
                "--temperature-unit", choices=("device", "C", "F"), default="device",
                help="Einheit vom Display: C = Celsius, F = Fahrenheit; device = unbekannt (Standard). Keine Umrechnung",
            )
        if name == "watch":
            command.add_argument("--interval", type=_interval, default=1.0, metavar="SEKUNDEN", help="Abfrageintervall ≥ 0,5 (Standard: 1)")
            command.add_argument("--count", type=_count, metavar="ANZAHL", help="Nach dieser Anzahl Messungen beenden; sonst bis Strg+C")
    return result


def _output_reading(reading: Reading, unit: str, as_json: bool, log: MeasurementLog | None = None) -> None:
    record = measurement_record(reading, unit)
    timestamp = record["received_at"]
    if log is not None:
        log.write(record)
    if as_json:
        print(json.dumps(record), flush=True)
        return
    temperature_unit = "Geräteeinheit unbekannt" if unit == "device" else f"°{unit} (vom Nutzer angegeben)"
    calculated_r = reading.calculated_resistance_ohm
    print(
        f"{timestamp}  U={reading.voltage_v:.4f} V  I={reading.current_a:.5f} A  "
        f"P={reading.power_w:.4f} W  R={reading.resistance_ohm:.1f} Ω  "
        f"T={reading.temperature_value} {temperature_unit}\n"
        f"  Gruppe 0: {reading.charge_mah[0]} mAh / {reading.energy_mwh[0]} mWh; "
        f"Gruppe 1: {reading.charge_mah[1]} mAh / {reading.energy_mwh[1]} mWh; "
        f"D+={reading.data_plus_v:.2f} V; D-={reading.data_minus_v:.2f} V\n"
        f"  Berechnet: U×I={reading.calculated_power_w:.4f} W; "
        f"U/I={f'{calculated_r:.3f} Ω' if calculated_r is not None else 'nicht verfügbar (I=0)'}",
        flush=True,
    )


def _output_info(info: DeviceInfo, device: str, as_json: bool, *, ble: bool = False) -> None:
    transport = "Bluetooth LE" if ble else "Micro-USB serial"
    if as_json:
        print(json.dumps({**asdict(info), "address" if ble else "port": device,
                          "transport": transport, "variant": None,
                          "uptime_seconds": None, "active_group": None}))
    else:
        print(f"{'BLE-Kennung' if ble else 'Anschluss'}: {device}\n"
              f"Transport: {'Bluetooth LE' if ble else 'Micro-USB / seriell, 115200 8N1'}\n"
              f"Modellkennung: {info.model}\nVariante TC66/TC66C und Handelsmarke: nicht auslesbar\n"
              f"Firmware: {info.firmware}\nSeriennummer: {info.serial_number:08d}\n"
              f"Startzähler: {info.boot_count}\nLaufzeit und aktive Datengruppe: nicht verfügbar")


async def _run_ble(args: argparse.Namespace, log: MeasurementLog | None = None) -> int:
    from .ble import BleMeter, _check_permission, list_devices as list_ble, select_device as select_ble

    if args.command == "list":
        devices = await list_ble(args.timeout)
        first = next((device.device for device in devices if "bt24-m" in device.name.casefold()), None)
        if first is not None:
            cache.save_device(first)
        else:
            cache.forget_device()
        if args.filter:
            devices = [device for device in devices if args.filter.casefold() in device.name.casefold()]
        if args.json:
            print(json.dumps([asdict(device) for device in devices]))
        elif not devices:
            if args.filter:
                print(f"Keine BLE-Geräte für Namensfilter '{args.filter}' gefunden.")
            else:
                print("Keine BLE-Geräte gefunden. Bluetooth und Berechtigung prüfen.")
        else:
            for device in devices:
                print(f"{device.device}\t{device.name}")
        return 0
    # Check permission before considering a retry; a denied guard leaves the cache intact.
    _check_permission()
    cached = cache.load_device() if not args.device else None
    address = await select_ble(args.device or cached, args.timeout)
    retry_cached = cached is not None
    while True:
        try:
            async with BleMeter(address, timeout=args.timeout) as meter:
                started = time.monotonic()
                reading = await meter.read()
                # Only retry a cached target before the first valid response.
                retry_cached = False
                if address != cached or args.device:
                    cache.save_device(address)
                if args.command == "info":
                    _output_info(reading.info, address, args.json, ble=True)
                    return 0
                if args.command == "read":
                    _output_reading(reading, args.temperature_unit, args.json, log)
                    return 0
                count = 0
                while True:
                    _output_reading(reading, args.temperature_unit, args.json, log)
                    count += 1
                    if args.count is not None and count >= args.count:
                        return 0
                    # Process BLE notifications and disconnects between requests.
                    await asyncio.sleep(max(0, args.interval - (time.monotonic() - started)))
                    started = time.monotonic()
                    reading = await meter.read()
        except (DeviceError, ProtocolError):
            if not retry_cached:
                raise
            retry_cached = False
            cached = None
            cache.forget_device()
            print("Gespeichertes Messgerät konnte nicht ausgelesen werden; suche erneut.", file=sys.stderr)
            address = await select_ble(None, args.timeout)


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        argument_parser.print_help()
        return 0
    args = argument_parser.parse_args(arguments)
    if getattr(args, "output_format", None) is not None and not args.output:
        argument_parser.error("--output-format benötigt --output DATEI.")
    if getattr(args, "output", None) == "":
        argument_parser.error("--output benötigt einen Dateinamen.")
    if args.timeout is None:
        args.timeout = 15.0 if args.transport == "ble" else 3.0
    try:
        output = getattr(args, "output", None)
        with open_log(output, args.output_format or "csv") if output else nullcontext() as log:
            if args.transport == "ble":
                return asyncio.run(_run_ble(args, log))
            if args.command == "list":
                ports = list_devices()
                if args.filter:
                    ports = [port for port in ports if args.filter.casefold() in port.description.casefold()]
                if args.json:
                    print(json.dumps([asdict(port) for port in ports]))
                elif not ports:
                    if args.filter:
                        print(f"Keine seriellen Anschlüsse für Namensfilter '{args.filter}' gefunden.")
                    else:
                        print("Keine seriellen Anschlüsse gefunden. Micro-USB-Datenverbindung prüfen.")
                else:
                    for port in ports:
                        usb_id = f"{port.vid:04x}:{port.pid:04x}" if port.vid is not None and port.pid is not None else "unbekannt"
                        status = "TC66-Kandidat (noch nicht bestätigt)" if port.candidate else "nicht identifiziert; ggf. --device wählen"
                        print(f"{port.device}\t{port.description}\tUSB {usb_id}\t{status}")
                return 0
            port = select_device(args.device)
            with Meter(port, timeout=args.timeout) as meter:
                if args.command == "info":
                    _output_info(meter.read().info, port, args.json)
                    return 0
                if args.command == "read":
                    _output_reading(meter.read(), args.temperature_unit, args.json, log)
                    return 0
                count = 0
                while True:
                    started = time.monotonic()
                    _output_reading(meter.read(), args.temperature_unit, args.json, log)
                    count += 1
                    if args.count is not None and count >= args.count:
                        return 0
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nBeendet; Verbindung freigegeben.", file=sys.stderr)
        return 130
    except RecordingError as error:
        print(f"Fehler: {error} Messung beendet.", file=sys.stderr)
        return 7
    except ProtocolError as error:
        print(f"Fehler: {error} Keine aktuellen Messwerte verfügbar.", file=sys.stderr)
        return 6
    except DeviceError as error:
        print(f"Fehler: {error} Keine aktuellen Messwerte verfügbar.", file=sys.stderr)
        return error.exit_code
    except BrokenPipeError:
        return 0
