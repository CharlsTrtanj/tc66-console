from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tc66c import cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tc66c" / "ble-device.json"
        location = patch("tc66c.cache.cache_path", return_value=self.path)
        location.start()
        self.addCleanup(location.stop)

    def test_identifier_and_utc_timestamp_persist_across_processes(self):
        cache.save_device("remembered")
        record = json.loads(self.path.read_text())
        self.assertEqual(record["device"], "remembered")
        self.assertEqual(datetime.fromisoformat(record["saved_at"]).utcoffset(), timedelta(0))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        script = (
            "import sys; from pathlib import Path; from tc66c import cache; "
            "cache.cache_path = lambda: Path(sys.argv[1]); "
            "print(cache.load_device())"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                cwd=self.path.parent, capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "remembered"))
        cache.forget_device()
        self.assertIsNone(cache.load_device())

    def test_timestamp_expires_at_exactly_twenty_four_hours_and_rejects_future_dates(self):
        saved = datetime(2026, 10, 4, tzinfo=timezone.utc)
        with patch("tc66c.cache.datetime", wraps=datetime) as clock:
            clock.now.return_value = saved
            cache.save_device("remembered")
            original = self.path.read_bytes()
            for age, expected in ((0, "remembered"), (86399, "remembered"), (86400, None), (90000, None), (-1, None)):
                with self.subTest(age=age):
                    clock.now.return_value = saved + timedelta(seconds=age)
                    self.assertEqual(cache.load_device(), expected)
            self.assertEqual(self.path.read_bytes(), original)

    def test_missing_or_malformed_cache_is_ignored(self):
        self.assertIsNone(cache.load_device())
        self.path.parent.mkdir()
        for content in ('{', '{}', 'null', '[]',
                        '{"device": null, "saved_at": "invalid"}',
                        '{"device": "", "saved_at": "2026-10-04T00:00:00+00:00"}',
                        '{"device": "remembered", "saved_at": "2026-10-04T00:00:00"}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertIsNone(cache.load_device())

    def test_file_access_errors_do_not_prevent_measurement(self):
        with patch("tc66c.cache.Path.read_text", side_effect=PermissionError):
            self.assertIsNone(cache.load_device())
        with patch("tc66c.cache.Path.mkdir", side_effect=PermissionError):
            cache.save_device("remembered")
        self.assertFalse(self.path.exists())
        with patch("tc66c.cache.Path.unlink", side_effect=PermissionError):
            cache.forget_device()


class CacheLocationTests(unittest.TestCase):
    def test_platform_cache_paths_do_not_depend_on_the_working_directory(self):
        with patch("tc66c.cache.Path.home", return_value=Path("/example/home")):
            with patch("tc66c.cache.sys.platform", "darwin"):
                self.assertEqual(cache.cache_path(), Path("/example/home/Library/Caches/tc66c/ble-device.json"))
            with patch("tc66c.cache.sys.platform", "linux"):
                for configured, expected in (("", "/example/home/.cache"), ("/example/cache", "/example/cache"), ("relative", "/example/home/.cache")):
                    with self.subTest(configured=configured), patch.dict("os.environ", {"XDG_CACHE_HOME": configured}):
                        self.assertEqual(cache.cache_path(), Path(expected) / "tc66c" / "ble-device.json")
