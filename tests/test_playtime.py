import datetime as dt
import unittest
from unittest import mock

from umbral import i18n, playtime


class TestPlaytime(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(i18n, "LANG", "es")
        p.start()
        self.addCleanup(p.stop)

    def test_duration(self):
        self.assertEqual(playtime.format_duration(30), "menos de 1 min")
        self.assertEqual(playtime.format_duration(45 * 60), "45 min")
        self.assertEqual(playtime.format_duration(3 * 3600), "3 h")
        self.assertEqual(playtime.format_duration(3 * 3600 + 20 * 60 + 59), "3 h 20 min")

    def test_ago(self):
        now = dt.datetime(2026, 9, 29, 18, 0)
        self.assertEqual(playtime.format_ago("2026-09-29T09:00:00", now), "hoy")
        self.assertEqual(playtime.format_ago("2026-09-28T23:59:00", now), "ayer")
        self.assertEqual(playtime.format_ago("2026-09-25T10:00:00", now), "hace 4 días")
        self.assertEqual(playtime.format_ago("2026-08-12T10:00:00", now), "12/08/2026")
        self.assertEqual(playtime.format_ago("", now), "")
        self.assertEqual(playtime.format_ago("basura", now), "")

    def test_summary(self):
        now = dt.datetime(2026, 9, 29, 18, 0)
        self.assertEqual(playtime.summary(0, "", now), "")                 # nunca jugado: se ve el runner
        self.assertEqual(playtime.summary(12000, "2026-09-29T10:00:00", now), "3 h 20 min · hoy")


class TestSGDB(unittest.TestCase):
    def test_key_file_is_private(self):
        import os
        import tempfile
        from pathlib import Path

        from umbral import sgdb
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sgdb, "KEY_FILE", Path(d) / "k"):
            self.assertEqual(sgdb.get_key(), "")
            sgdb.set_key("  abc123  ")
            self.assertEqual(sgdb.get_key(), "abc123")
            self.assertEqual(os.stat(Path(d) / "k").st_mode & 0o777, 0o600)
            sgdb.set_key("")
            self.assertFalse((Path(d) / "k").exists())

    def test_download_only_from_steamgriddb(self):
        from umbral import sgdb
        with self.assertRaises(sgdb.SGDBError):
            sgdb.download("https://example.com/evil.png", "x")


if __name__ == "__main__":
    unittest.main()
