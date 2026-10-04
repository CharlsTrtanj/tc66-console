# TC66 Console

Python command-line application for reading Joy-IT JT-TC66C and RDTech
TC66/TC66C USB power meters on macOS and Linux. It supports Bluetooth Low Energy
(BLE) and the meter's Micro-USB serial interface. **BLE is the default transport.**

The application provides device discovery, device information, single readings
and continuous polling. It reports voltage, current, power, resistance,
temperature, charge, energy and USB data-line voltages. Text and JSON output are
available, and measurements can be recorded to CSV or JSON Lines files. The
commands query the meter without changing its settings or clearing its counters.

## Installation

Requires **Python 3.11 or later**. Clone the repository, create a virtual
environment and install the application:

```sh
git clone https://github.com/CharlsTrtanj/tc66-console.git
cd tc66-console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
tc66c --help
```

The installation includes `pyserial`, `cryptography` and `bleak`. Activate the
virtual environment before running `tc66c` in a new terminal session. The same
commands are also available through `python -m tc66c`. The Python distribution
is named `tc66-console`; the installed command and import package are named
`tc66c`.

## Quick start

Power the meter and enable Bluetooth data transmission in its configuration
menu. No device identifier is required for these commands:

```sh
tc66c list
tc66c info
tc66c read
tc66c watch --interval 1 --count 60
```

`info`, `read` and `watch` use a cached BLE device identifier when it is less than
24 hours old. Otherwise, they scan for devices and select the first discovered
device whose name contains `BT24-M`, ignoring case. When multiple matching meters
are present, use `--device` to select a specific one.

For continuous polling until **Ctrl+C**:

```sh
tc66c watch
```

`watch` keeps the connection open between readings. Ctrl+C stops polling and
closes the connection. Each reading includes a UTC timestamp. A failed request
ends the command; previously received values are not repeated as new readings.

## Commands and options

Options follow the command, for example `tc66c read --timeout 30`.

| Command | Function |
|---|---|
| `list` | Scan for BLE devices or list USB serial ports. |
| `info` | Read the model identifier, firmware version, serial number and boot count. |
| `read` | Read one current set of measurements. |
| `watch` | Poll repeatedly until Ctrl+C or the specified reading count is reached. |

### Common options

These options are available on all four commands:

| Option | Default | Function |
|---|---|---|
| `--transport ble\|usb` | `ble` | Select Bluetooth LE or USB serial communication. |
| `--timeout SECONDS` | See below | Set the scan or communication timeout to a finite, positive number of seconds. |
| `--json` | Text output | Output JSON; `watch` writes one JSON object per line. |
| `-h`, `--help` | — | Show help for the command. |

Timeout defaults are **5 seconds** for a BLE `list` scan, **15 seconds** for BLE
`info`, `read` and `watch`, and **3 seconds** for USB measurement commands. For BLE
measurement commands, the timeout applies separately to discovery, connection
setup and each response. It is not an overall limit on the command's duration.
`--timeout` has no effect on USB port listing.

### Command-specific options

| Option | Commands | Default | Function |
|---|---|---|---|
| `--filter NAME` | `list` | `BT24-M` | Case-insensitive substring filter for the BLE device name or USB port description. An explicit value replaces the default; `--filter ""` lists all devices. |
| `--device IDENTIFIER` | `info`, `read`, `watch` | Automatic selection | Use a specific BLE identifier or USB port path from `list`. |
| `--output FILE` | `read`, `watch` | No file | Record every valid measurement to the named file and append to an existing compatible measurement log. |
| `--output-format csv\|jsonl` | `read`, `watch` | `csv` | Select the recording format. Requires `--output`; independent of `--json` and the filename extension. |
| `--temperature-unit device\|C\|F` | `read`, `watch` | `device` | Label the temperature with the unit configured on the meter. `device` leaves the unit unknown; `C` and `F` label the value without converting it. |
| `--interval SECONDS` | `watch` | `1` | Polling interval, with a minimum of `0.5` seconds. Slow responses can make the actual interval longer. |
| `--count NUMBER` | `watch` | No limit | Stop after this positive integer number of valid readings. |

Running `tc66c` without arguments shows the main help. Command-specific help is
available as follows:

```sh
tc66c --help
tc66c list --help
tc66c info --help
tc66c read --help
tc66c watch --help
```

The command help and human-readable measurement output currently use German.
JSON property names use English.

## Bluetooth LE

The meter must be powered and have Bluetooth data transmission enabled. Under
macOS, allow the terminal or application running `tc66c` to access Bluetooth in
**System Settings → Privacy & Security → Bluetooth**. Under Linux, a Bluetooth
adapter, BlueZ and the system D-Bus must be available.

The BLE text list contains two tab-separated fields: the device identifier and
the device name. The default filter includes names containing `BT24-M`. To change
the filter, list all devices, or include discovery metadata in JSON:

```sh
tc66c list --timeout 15
tc66c list --filter TC66
tc66c list --filter ""
tc66c list --json
```

To connect to a specific meter, replace `BLE_IDENTIFIER` below with the first
column from `tc66c list` on the same computer:

```sh
tc66c info --device BLE_IDENTIFIER
tc66c read --device BLE_IDENTIFIER --timeout 30
tc66c watch --device BLE_IDENTIFIER --interval 1 --count 10
```

macOS uses a CoreBluetooth UUID as the identifier; Linux uses a Bluetooth address.
BLE communication does not create a serial port. The `list` filter affects the
displayed list only; it does not change the automatic selection rule for
measurement commands. Devices with other names can be selected explicitly.

A matching name is a discovery hint. The application checks the GATT channels
and validates the TC66 response before reporting measurements.

### Device cache

The BLE device identifier and its UTC save timestamp are stored in
`ble-device.json` in the current user's cache directory:

| Platform | File |
|---|---|
| macOS | `~/Library/Caches/tc66c/ble-device.json` |
| Linux | `$XDG_CACHE_HOME/tc66c/ble-device.json`, or `~/.cache/tc66c/ble-device.json` when the variable is unset, empty or relative. |

The cache is shared across invocations and working directories. Its lifetime is
24 hours from the time it was saved; reusing an entry does not extend its lifetime.

- BLE `list` always performs a fresh scan and caches the first matching `BT24-M`,
  independently of the display filter. If no matching device is found, it removes
  the old entry.
- A successful measurement command following a fresh scan saves the selected
  identifier. An explicit `--device` takes priority over the cache and is saved
  after a successful response.
- Missing, expired, invalid or unreadable entries cause a fresh scan. Cache write
  failures do not prevent measurements.
- If a cached device fails during connection setup or before the first valid
  response, the application discards the entry and makes one fresh discovery and
  connection attempt. After the first valid response, `watch` ends on failure
  without switching meters.

Use `tc66c list` to refresh the cached selection. The cache skips the application's
full device-name scan; the BLE backend may still need discovery to resolve the
specific identifier during connection setup. Each command opens its own
connection.

## USB serial connection

Connect the meter's **Micro-USB PC/data connector** to the computer with a
data-capable cable and enable PC/USB communication in the meter's menu. The USB-C
measurement path is separate from the PC data interface. The connector and menu
settings are described in the [Joy-IT manual](https://media.elv.com/file/jt-tc66c-anleitung.pdf).

Select USB explicitly and list all serial ports:

```sh
tc66c list --transport usb --filter ""
```

Replace the example path below with the port from that list. On macOS:

```sh
tc66c info --transport usb --device /dev/cu.usbmodem1234
tc66c read --transport usb --device /dev/cu.usbmodem1234 --timeout 5
tc66c watch --transport usb --device /dev/cu.usbmodem1234 --interval 1
```

On Linux:

```sh
tc66c read --transport usb --device /dev/ttyACM0
tc66c watch --transport usb --device /dev/ttyACM0 --interval 1 --count 10
```

The serial connection uses **115200 baud, 8 data bits, no parity and 1 stop bit**.
Without `--device`, USB measurement commands select a port only when exactly one
port contains `TC66` in its description, manufacturer or product text. Generic
ports and multiple candidates require an explicit path. The USB text list also
includes the port description, USB vendor/product IDs when available and a
candidate status. USB commands do not use the BLE cache.

## Measurements and output

`read` and `watch` report the following values:

| Value | Unit or interpretation |
|---|---|
| Voltage | V, reported by the meter. |
| Current | A, reported by the meter. |
| Power | W, reported by the meter. |
| Resistance | Ω, the meter's raw resistance value divided by 10. |
| Temperature | Signed value from the meter; unit unknown unless specified with `--temperature-unit`. |
| Charge, groups 0 and 1 | mAh; both groups are included. |
| Energy, groups 0 and 1 | mWh; both groups are included. |
| D+ and D− voltage | V. |
| Calculated power | Voltage × current, in W. |
| Calculated resistance | Voltage / current, in Ω; unavailable when current is zero. |

Reported power and resistance are kept separate from calculated values. To label
temperature readings, choose the unit shown on the meter's display:

```sh
tc66c read --temperature-unit C
tc66c watch --temperature-unit F
```

This option does not convert the temperature. Without it, or with
`--temperature-unit device`, the unit remains unknown. The protocol's model
identifier is `TC66`; it does not distinguish the TC66/TC66C variant or commercial
brand. Uptime and the active data group are not exposed by the decoder.

### JSON output

All commands support `--json`. Discovery produces an array; `info` produces a
device information object; `read` produces one measurement object; `watch`
produces newline-delimited JSON (JSON Lines).

```sh
tc66c list --json
tc66c info --json
tc66c read --temperature-unit C --json
tc66c watch --temperature-unit C --interval 1 --count 60 --json > measurements.jsonl
tc66c list --transport usb --filter "" --json
```

Each measurement object has four top-level properties:

| Property | Contents |
|---|---|
| `received_at` | UTC timestamp in ISO 8601 format, with millisecond precision. |
| `device` | `model`, `firmware`, `serial_number` and `boot_count`. |
| `device_values` | `voltage_v`, `current_a`, `power_w`, `temperature_value`, `temperature_unit`, `resistance_raw`, `resistance_ohm`, `charge_mah`, `energy_mwh`, `data_plus_v` and `data_minus_v`. |
| `calculated` | `power_w` and `resistance_ohm` calculated from voltage and current. |

The charge and energy arrays contain group 0 followed by group 1. Unknown or
unavailable values are represented by `null`, including an unspecified
temperature unit and calculated resistance at zero current. BLE discovery JSON
also includes RSSI, advertised service UUIDs and a candidate flag. Errors and
connection diagnostics are written to stderr so they do not enter JSON output.

## Recording measurements

Use `--output FILE` with `read` or `watch` to save each valid measurement while
continuing to display it in the terminal. **CSV is the default file format.**

```sh
tc66c read --output measurements.csv
tc66c watch --output measurements.csv --temperature-unit C
tc66c watch --output measurements.csv --interval 1 --count 60
```

The file is created at the specified path. Relative paths are resolved from the
current working directory; the parent directory must already exist. Subsequent
commands using the same file append readings. CSV headers are written only once.
An incompatible file or a file whose final line is incomplete is rejected before
connecting to the meter.

Each CSV row contains the UTC timestamp, model, firmware, serial number, boot
count and all reported and calculated values. Charge and energy groups are
flattened into `charge_group0_mah`, `energy_group0_mwh`, `charge_group1_mah` and
`energy_group1_mwh`. Calculated values use `calculated_power_w` and
`calculated_resistance_ohm`; the other measurement columns use the property names
listed under JSON output. CSV uses UTF-8, comma separators and decimal points.
Unknown or unavailable values are empty fields.

For structured processing, select **JSON Lines** explicitly:

```sh
tc66c watch --output measurements.jsonl --output-format jsonl --temperature-unit C
tc66c read --output measurements.jsonl --output-format jsonl --json
```

Each JSON Lines record uses the same structure as console JSON, including nested
device information, measurement groups and `null` for unavailable values. The
file format is controlled by `--output-format`, independently of the filename
extension and `--json`, which only controls console output.

Each completed record is flushed to the file immediately. Ctrl+C, a polling
error or a disconnection closes the file and preserves completed records. A file
write error stops the measurement with exit code `7`. Use a separate file for
each concurrently running recording command.

Measurement logs include meter serial numbers and other device metadata. Store
logs outside the repository or in an ignored `measurements/` directory. The
repository ignores CSV and JSON Lines files in its root, and all files under
`measurements/`, to prevent accidental commits.
Remove personal metadata before sharing a log publicly.

## Protocol and implementation

The USB transport sends the ASCII query `getva`. The BLE transport subscribes to
notifications and sends `bgetva\r\n`. Supported GATT channel pairs are write `FFE2`
with notify `FFE1`, and write `FFE9` with notify `FFE4`.

Both transports use the same decoder for the 192-byte AES-256-ECB response. The
decoder checks the `TC66` model identifier, packet structure and CRC-16/MODBUS
checksums before exposing a reading. Fragmented responses are accumulated by the
stream decoder. The transport implementations handle discovery, connection
setup, polling timeouts and cleanup separately.

See [docs/protocol.md](docs/protocol.md) for packet fields and protocol references,
and [docs/validation.md](docs/validation.md) for reproducible software and hardware
verification procedures.

## Troubleshooting

| Problem | Action |
|---|---|
| No matching BLE device | Power the meter, enable Bluetooth transmission and run `tc66c list --timeout 15`. Use `tc66c list --filter ""` to inspect all names and select the meter with `--device`. |
| BLE access denied | On macOS, allow Bluetooth access for the application running the command. On Linux, check the Bluetooth adapter, BlueZ, system D-Bus and access policy. |
| BLE connection timeout | Place the meter closer to the computer, check the identifier using a fresh scan on that computer and increase the timeout with `--timeout 30`. |
| Unsupported GATT channels or invalid response | Check that the selected device implements the TC66 protocol; the advertised name alone does not identify its protocol. |
| No USB serial port | Run `tc66c list --transport usb --filter ""`. Check the Micro-USB data cable and PC communication setting. On Linux, check USB CDC enumeration and the `cdc_acm` driver. |
| USB access denied | Check permissions for the specific device path. On Linux, check the device's group or session ACL. |
| Port in use | Close other programs using the meter and run the command again. |
| USB response timeout | Check the port path, data cable and PC communication setting. Increase `--timeout` if responses are slow. |
| Connection interrupted | Restore the connection and restart the command. |
| Recording file cannot be opened or written | Check the parent directory, file permissions and available disk space. Use a different filename if the existing file has an incompatible format or an incomplete final line. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, including an empty device list or help output. |
| `2` | Invalid command arguments. |
| `3` | No matching device or no unique USB candidate. |
| `4` | Device access or connection error. |
| `5` | Scan, connection, write or response timeout. |
| `6` | Invalid or incomplete protocol response. |
| `7` | Measurement file open, format, write or close error. |
| `130` | Interrupted with Ctrl+C. |

## Development

Install an editable copy and run the local test suite:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The tests cover packet decoding, simulated serial and BLE communication, command
behavior and cache handling without requiring a connected meter. GitHub Actions
runs installation, tests and command-line smoke checks on macOS and Linux with
Python 3.11 and 3.14.

## Contributions and security reports

External pull requests are currently not accepted. Bug reports can be submitted
through [GitHub Issues](https://github.com/CharlsTrtanj/tc66-console/issues). Include
the application version, operating system, transport, command and observed
behavior, with personal device identifiers and other private data removed. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the current contribution policy.

Report security vulnerabilities privately using
[GitHub private vulnerability reporting](https://github.com/CharlsTrtanj/tc66-console/security/advisories/new).
See [SECURITY.md](SECURITY.md) for reporting guidance.

## License

TC66 Console is licensed under the [MIT License](LICENSE). Third-party
dependencies retain their own licenses. Protocol references and their licenses
are documented in [docs/protocol.md](docs/protocol.md).
