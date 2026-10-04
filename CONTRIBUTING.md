# Contributing

External pull requests are currently not accepted. Changes are maintained by
the project owner. This policy may change in a future release.

## Bug reports

Report reproducible bugs through
[GitHub Issues](https://github.com/CharlsTrtanj/tc66-console/issues). Include:

- Application version or commit, Python version and operating system.
- Transport (`ble` or `usb`) and meter model/firmware when available.
- The complete command, expected behavior and actual behavior.
- Relevant error output and steps that reproduce the problem.

Remove device identifiers, serial numbers, local account names and paths,
credentials and other private data from reports and attachments. Measurement
logs include device metadata. Raw encrypted meter captures can also contain
identifiers; see [the protocol documentation](docs/protocol.md) before sharing
packet data.

## Security vulnerabilities

Report suspected security vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/CharlsTrtanj/tc66-console/security/advisories/new),
as described in [SECURITY.md](SECURITY.md). Do not open a public issue containing
exploit details or exposed credentials.

## Local development

The project requires Python 3.11 or later. In a virtual environment, install an
editable copy and run the tests:

```sh
python -m pip install -e .
python -m pip check
python -m unittest discover -s tests -v
```

The automated tests use synthetic packets, sanitized reference data, simulated
BLE communication and a POSIX serial pseudoterminal. They do not require a
physical meter. Software and hardware verification procedures are documented
in [docs/validation.md](docs/validation.md).
