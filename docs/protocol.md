# TC66 communication protocol

TC66 Console implements the TC66 protocol independently using the protocol
references listed below. Reference implementations were used to establish
transport commands, field offsets, scaling and integrity checks; their
implementation code was not copied into this project.

## Transports and queries

The meter's Micro-USB connector is the PC data interface; the USB-C connector is
part of the measurement path. PC communication must be enabled in the meter's
configuration menu. See the [Joy-IT product page](https://www.joy-it.net/en/products/JT-TC66C)
and [manufacturer manual](https://media.elv.com/file/jt-tc66c-anleitung.pdf).

### USB serial

The USB transport uses a virtual serial port at **115200 baud, 8 data bits, no
parity and 1 stop bit (8N1)**. This configuration follows the sigrok driver.
Kolinger uses the same baud rate with PySerial defaults; TheHWcave uses two stop
bits. These reference differences should be considered when checking another
firmware or hardware variant. See [sigrok api.c](https://github.com/sigrokproject/libsigrok/blob/master/src/hardware/rdtech-tc/api.c),
[Kolinger tc.py](https://github.com/kolinger/rd-usb/blob/d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68/interfaces/tc.py)
and [TheHWcave TC66C.py](https://github.com/TheHWcave/TC66/blob/52b64a6e4b08ef35cbb08a9d40e88553c062a328/TC66C.py).

A measurement request sends the five ASCII bytes **`getva`**, without a prefix
or line ending. The response contains 192 encrypted bytes. The application sends
only this query. Clearing the local serial receive buffer before a request does
not clear the meter's stored measurements. See the
[sigrok protocol description](https://sigrok.org/wiki/RDTech_TC66C) and
[sigrok protocol.c](https://github.com/sigrokproject/libsigrok/blob/master/src/hardware/rdtech-tc/protocol.c).

No TC66-specific USB vendor/product ID is established by these references.
USB descriptor text is used only to select candidates; a valid TC66 response
confirms protocol compatibility. Select serial communication explicitly with
`--transport usb`.

### Bluetooth Low Energy

BLE is the default transport. TC66C Bluetooth communication uses GATT rather
than a serial RFCOMM port. A measurement request sends the eight ASCII bytes
**`bgetva\r\n`**. The implementation supports these documented channel pairs:

| Profile | Request service / characteristic | Response service / characteristic |
|---|---|---|
| FFE2 / FFE1 | `FFE0` / `FFE2`, write with response | `FFE0` / `FFE1`, notify |
| FFE9 / FFE4 | `FFE5` / `FFE9`, write with response | `FFE0` / `FFE4`, notify |

Short identifiers use the Bluetooth base UUID, for example
`0000ffe2-0000-1000-8000-00805f9b34fb`. The application checks the characteristic
services and required properties before sending a request. It subscribes to
notifications before the first query and keeps that subscription until the
connection closes. Notifications received outside an active query are ignored.
A deadline and a maximum of 8192 received bytes bound each response. On failure,
the connection closes. Both transports use the same decoder for the encrypted
192-byte response. See [Kolinger README](https://github.com/kolinger/rd-usb/blob/d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68/README.md),
[Kolinger BLE channels and request](https://github.com/kolinger/rd-usb/blob/d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68/interfaces/tc.py)
and the [sigrok protocol description](https://sigrok.org/wiki/RDTech_TC66C).

Without `--device`, the application uses a cached identifier less than 24 hours
old or scans for the first device whose name contains `BT24-M`, ignoring case.
The name is a discovery hint; channel properties, model, packet structure and
checksums are checked before a reading is returned. If a cached identifier
fails during connection setup or before the first valid response, the entry is
discarded and discovery is repeated once. After the first valid reading, a
polling failure ends the command without switching meters.

## Decryption and integrity

The complete 192-byte response is decrypted with **AES-256 in ECB mode, without
padding**, using `cryptography`. The published protocol key is:

```text
5821fa5601b2f02687ff1204622a4fb086f40260816f9a0ba7f106619ab87288
```

This is a public protocol constant, not a private credential or an
application-level authentication mechanism.

The plaintext contains three 64-byte blocks identified by `pac1`, `pac2` and
`pac3`. Each block has a 32-bit little-endian checksum field at relative offset
60. Its complete value must equal CRC-16/MODBUS of the first 60 plaintext bytes
of that block: initial value `0xffff`, reflected polynomial `0xa001`, no final
XOR. Consequently, the upper two bytes of each stored CRC field must be zero.
The decoder checks all three block identifiers and checksums, the `TC66` model
identifier and printable firmware text before returning a reading. See the
[sigrok protocol description](https://sigrok.org/wiki/RDTech_TC66C),
[sigrok protocol.c](https://github.com/sigrokproject/libsigrok/blob/master/src/hardware/rdtech-tc/protocol.c)
and [sigrok crc.c](https://github.com/sigrokproject/libsigrok/blob/master/src/crc.c).

Transport reads can fragment a frame or combine several frames. The bounded
stream decoder accumulates bytes and checks complete 192-byte candidates. It
moves the candidate boundary by one byte after invalid data because plaintext
block identifiers cannot be searched directly in the encrypted byte stream. If
one transport read contains multiple valid responses, the request returns the
last validated response. A timeout, interruption or invalid response returns no
new measurement and closes the connection.

## Decoded fields

Offsets refer to the full decrypted 192-byte frame. Unless stated otherwise,
numbers are unsigned 32-bit little-endian integers.

| Offset | Field | Interpretation |
|---:|---|---|
| 0 | Block identifier | Four ASCII bytes `pac1` |
| 4 | Model | Four ASCII bytes `TC66` |
| 8 | Firmware | Four ASCII bytes, with trailing NUL bytes removed |
| 12 | Serial number | Integer |
| 44 | Boot count | Integer |
| 48 | Voltage | Raw value / 10,000 V |
| 52 | Current | Raw value / 100,000 A |
| 56 | Meter-reported power | Raw value / 10,000 W |
| 60 | Block 1 CRC | CRC of bytes 0–59 |
| 64 | Block identifier | Four ASCII bytes `pac2` |
| 68 | Meter-reported resistance | Raw value / 10 Ω |
| 72 / 76 | Group 0 charge / energy | mAh / mWh, without further scaling |
| 80 / 84 | Group 1 charge / energy | mAh / mWh, without further scaling |
| 88 | Temperature sign | `0` positive, `1` negative; other values rejected |
| 92 | Temperature magnitude | Integer in the meter's configured unit |
| 96 / 100 | D+ / D− voltage | Raw value / 100 V |
| 124 | Block 2 CRC | CRC of bytes 64–123 |
| 128 | Block identifier | Four ASCII bytes `pac3` |
| 188 | Block 3 CRC | CRC of bytes 128–187 |

Field mappings are supported by [Ralim scan.py](https://github.com/Ralim/TC66C/blob/195d284795b8bd7feeb1ce777ba4b56876881dba/scan.py),
[Kolinger tc.py](https://github.com/kolinger/rd-usb/blob/d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68/interfaces/tc.py)
and [TheHWcave TC66C.py](https://github.com/TheHWcave/TC66/blob/52b64a6e4b08ef35cbb08a9d40e88553c062a328/TC66C.py).
Other bytes are not interpreted as additional measurement fields. Uptime, the
active data group, commercial brand and the TC66/TC66C variant cannot be
reliably determined from the decoded fields.

**Resistance:** The sigrok wiki lists a scale of 0.01 Ω. Ralim and Kolinger use
0.1 Ω, and TheHWcave documents a correction to 0.1 Ω. The implementation divides
the raw value by **10** and includes a reference test for `1234 → 123.4 Ω`.
Reported resistance remains separate from the resistance calculated as voltage
/ current. See the [sigrok protocol description](https://sigrok.org/wiki/RDTech_TC66C)
and [TheHWcave correction](https://github.com/TheHWcave/TC66/blob/52b64a6e4b08ef35cbb08a9d40e88553c062a328/TC66C.py).

**Temperature:** Older reference programs label this field in Celsius. The
sigrok description lists Celsius or Fahrenheit, and the meter menu has a unit
setting. The decoder does not identify a reliable unit flag. By default, the
unit is therefore unknown. `--temperature-unit C` or `F` labels the value using
the setting checked on the display; it does not convert the value. See the
[sigrok protocol description](https://sigrok.org/wiki/RDTech_TC66C) and
[Joy-IT manual](https://media.elv.com/file/jt-tc66c-anleitung.pdf).

## Reference licenses and test data

The application is licensed under the [MIT License](../LICENSE). The following
external projects are protocol references and retain their respective licenses:

| Reference | Referenced revision | License |
|---|---|---|
| Ralim/TC66C | `195d284795b8bd7feeb1ce777ba4b56876881dba` | No license declared at this revision; protocol reference only |
| kolinger/rd-usb | `d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68` | GPL-3.0 |
| TheHWcave/TC66 | `52b64a6e4b08ef35cbb08a9d40e88553c062a328` | MIT |
| sigrokproject/libsigrok, TC driver | Linked `master` sources | GPL-3.0-or-later according to the source headers |

License references: [Ralim source tree](https://api.github.com/repos/Ralim/TC66C/git/trees/195d284795b8bd7feeb1ce777ba4b56876881dba?recursive=1),
[Kolinger LICENSE](https://github.com/kolinger/rd-usb/blob/d4ee89ee5ea62b8d52ffbd16aabdc3a4dd69cd68/LICENSE),
[TheHWcave LICENSE](https://github.com/TheHWcave/TC66/blob/52b64a6e4b08ef35cbb08a9d40e88553c062a328/LICENSE)
and [sigrok source header](https://github.com/sigrokproject/libsigrok/blob/master/src/hardware/rdtech-tc/protocol.c).

The test suite includes synthetic packets with invented identifiers, values,
CRCs and encrypted payloads. `tests/fixtures.py` also contains a sanitized
reference derived from a real response. Plaintext ranges 12–47 (serial number,
boot count and unknown metadata), 72–87 (stored counters), 96–123 (D+/D− and
unused fields) and 132–187 (unknown fields) were cleared before recalculating
all CRC fields and encrypting the packet again. The retained voltage, current,
power, resistance and temperature fields exercise the decoder. Edited fixtures
are not hardware validation of the command-line application.

Public packet fixtures should follow the same process: remove identifiers and
unneeded metadata in the plaintext, recalculate all checksums and re-encrypt
before publication. The public protocol key does not make raw captures private.
