# Verification procedures

These procedures cover installation, automated software checks and communication
with a physical meter. Passing software tests does not establish hardware
compatibility or measurement accuracy.

## Software checks without a meter

Use Python 3.11 or later in a virtual environment. From the repository root:

```sh
python -m pip install -e .
python -m pip check
python -m unittest discover -s tests -v
tc66c --help
tc66c list --help
tc66c info --help
tc66c read --help
tc66c watch --help
tc66c list --transport usb --filter ""
```

The test suite covers:

- AES response decoding, block identifiers, model and firmware validation,
  checksums, field scales, invalid packets and fragmented streams.
- USB communication through simulated responses and a POSIX pseudoterminal
  using the installed CLI and PySerial.
- Simulated BLE discovery, supported GATT profiles, subscription before queries,
  notification fragmentation, bounded timeouts and response sizes, disconnects
  and interruption cleanup.
- Command parsing, help examples, BLE defaults, explicit USB selection, name
  filters, automatic selection and explicitly selected devices.
- The 24-hour device cache, per-user platform paths, expiry, invalid entries,
  atomic writes, reuse across processes and one discovery retry before the first
  valid reading.
- CSV and JSON Lines recording, compatible append behavior, metadata and units,
  file errors and preservation of completed records after interruption.

Synthetic and sanitized test fixtures are described in
[protocol.md](protocol.md). Automated tests do not access personal BLE caches or
require physical Bluetooth devices.

To check the distribution rather than an editable installation, use a clean
checkout and a fresh virtual environment, then build and install a wheel:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install build
python -m build --wheel
python -m pip install dist/*.whl
python -m pip check
python -m unittest discover -s tests -v
tc66c --help
```

Use a clean `dist` directory so the wheel pattern selects only the intended
build. GitHub Actions performs wheel installation, dependency checks, tests and
CLI smoke checks on macOS and Linux with Python 3.11 and 3.14. It does not start
BLE discovery or test a physical USB connection.

## BLE communication with a physical meter

Power the meter, enable Bluetooth data transmission and allow Bluetooth access
for the terminal or application running the commands. Linux additionally
requires a working Bluetooth adapter, BlueZ and the system D-Bus.

Discover the meter and read its information and current measurements:

```sh
tc66c list --timeout 15
tc66c info --timeout 15
tc66c read --timeout 15 --temperature-unit C
```

Use `C` only when Celsius is configured on the display; use `F` for Fahrenheit
or omit the option to leave the unit unknown. With multiple matching meters,
add `--device BLE_IDENTIFIER` using the first column from `list` on the same
computer. Confirm that the displayed firmware and model correspond to the
selected meter.

Compare a reading with the display at approximately the same time. Check
voltage, current, reported power, reported resistance, temperature, both charge
and energy groups and D+/D− when displayed. Voltage × current and voltage /
current are useful consistency checks, but are not independent accuracy
references. Quantitative accuracy testing requires an appropriate calibrated
reference and controlled load.

Check polling for at least one minute and recording to a new file:

```sh
tc66c watch --timeout 15 --interval 1 --count 65 --output measurements.csv --temperature-unit C
```

Expect 65 valid data rows plus one CSV header. Check that timestamps increase,
values use the documented units and fields match the terminal output. Actual
intervals can exceed one second when the meter or transport responds slowly.

Then check interruption and a new connection:

```sh
tc66c watch --timeout 15 --interval 1
```

Press Ctrl+C after several readings, then immediately run:

```sh
tc66c read --timeout 15
```

The interrupted command should exit with code `130`, release its connection and
allow the next command to obtain a new reading. Check cache reuse by running
`read` again: the saved identifier should be used without a full device-name
scan while its original timestamp is less than 24 hours old. `tc66c list` always
performs a new scan and refreshes the cached selection.

## USB communication with a physical meter

Connect the meter's Micro-USB PC/data connector using a data-capable cable and
enable PC communication. The USB-C measurement path does not replace the
Micro-USB data connection.

```sh
tc66c list --transport usb --filter ""
```

Use the listed path instead of the Linux example below. On macOS, it normally
has a `/dev/cu.*` form.

```sh
tc66c info --transport usb --device /dev/ttyACM0 --timeout 5
tc66c read --transport usb --device /dev/ttyACM0 --timeout 5
tc66c watch --transport usb --device /dev/ttyACM0 --timeout 5 --interval 1 --count 65 --output measurements.csv
```

Repeat the display comparison and Ctrl+C/reconnection checks from the BLE
procedure. Record the platform, meter firmware and transport with the results;
compatibility observed for one combination does not establish compatibility
for every firmware or hardware variant.

## Recording and error handling

Use a new file to check JSON Lines independently of CSV:

```sh
tc66c watch --timeout 15 --count 10 --output measurements.jsonl --output-format jsonl --json
```

Check that each line is valid JSON and has the `received_at`, `device`,
`device_values` and `calculated` properties. Reusing a compatible log should
append readings. An incompatible file or an incomplete final line should be
rejected before a connection is attempted.

During a separate polling run, turn off the meter's data transmission or remove
its data connection. The command should end with an error, preserve completed
records and avoid presenting an old reading as a new sample. Restore the
connection and start a new command to resume.

Keep measurement logs outside versioned source files or in an ignored
`measurements/` directory. Logs include meter serial numbers and device
metadata. Remove personal identifiers from reports and sanitize packet captures
as described in [protocol.md](protocol.md) before sharing them.
