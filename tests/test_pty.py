"""Real POSIX serial and installed CLI over a simulated peer; no meter hardware."""

import json
import os
import select
import subprocess
import sys
import threading
import time
import unittest

from tests.fixtures import KNOWN_RESPONSE

if os.name == "posix":
    import pty


@unittest.skipUnless(os.name == "posix", "POSIX pseudo-terminal required")
class SerialIntegrationTests(unittest.TestCase):
    def test_installed_cli_reads_fragmented_reply_via_real_pyserial(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        path = os.ttyname(slave)
        commands, failures = [], []

        def respond():
            try:
                command = b""
                deadline = time.monotonic() + 4
                while len(command) < 5 and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        command += os.read(master, 5 - len(command))
                commands.append(command)
                if command == b"getva":
                    for chunk in (KNOWN_RESPONSE[:13], KNOWN_RESPONSE[13:80], KNOWN_RESPONSE[80:]):
                        os.write(master, chunk)
                        time.sleep(0.005)
            except BaseException as error:
                failures.append(error)

        responder = threading.Thread(target=respond, daemon=True)
        responder.start()
        result = subprocess.run(
            [sys.executable, "-m", "tc66c", "read", "--transport", "usb", "--device", path, "--timeout", "3", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        responder.join(timeout=2)
        self.assertFalse(responder.is_alive(), "Simulated responder did not stop")
        self.assertEqual(failures, [])
        self.assertEqual(commands, [b"getva"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        values = json.loads(result.stdout)["device_values"]
        self.assertAlmostEqual(values["voltage_v"], 5.1234)
        self.assertAlmostEqual(values["current_a"], 0.12345)
        self.assertAlmostEqual(values["power_w"], 0.6324)


if __name__ == "__main__":
    unittest.main()
