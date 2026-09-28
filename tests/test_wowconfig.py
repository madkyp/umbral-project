import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umbral import prefixes, wowconfig


class TestConfigWtf(unittest.TestCase):
    def test_set_get_preserves_crlf_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "WTF" / "Config.wtf"
            p.parent.mkdir()
            p.write_bytes(b'SET graphicsQuality "1"\r\nSET portal "test"\r\n')
            self.assertIsNone(wowconfig.get_cvar(p, "gxApi"))
            wowconfig.set_cvar(p, "gxApi", "D3D11")
            self.assertEqual(wowconfig.get_cvar(p, "gxapi"), "D3D11")   # WoW no distingue mayúsculas
            raw = p.read_bytes()
            self.assertIn(b'SET portal "test"\r\n', raw)
            self.assertNotIn(b"\n\n", raw.replace(b"\r\n", b"|"))
            wowconfig.set_cvar(p, "gxApi", "D3D12")
            self.assertEqual(raw.count(b"gxApi"), 1)
            self.assertEqual(p.read_bytes().count(b"gxApi"), 1)           # sustituye, no duplica
            wowconfig.set_cvar(p, "gxApi", None)
            self.assertIsNone(wowconfig.get_cvar(p, "gxApi"))
            self.assertNotIn(b"gxApi", p.with_name("Config.wtf.umbral-bak").read_bytes())


class TestPrefixBackup(unittest.TestCase):
    def test_backup_excludes_games_and_restores(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pfx = root / "battlenet"
            (pfx / "drive_c/windows/system32").mkdir(parents=True)
            (pfx / "drive_c/Program Files (x86)/World of Warcraft").mkdir(parents=True)
            (pfx / "drive_c/Program Files (x86)/World of Warcraft/WowB.exe").write_bytes(b"MZ" * 1000)
            (pfx / "system.reg").write_text("antes")
            (pfx / "version").write_text("CachyOS-11.0-100")
            with mock.patch.object(prefixes, "backup_dir", lambda p: root / "backups"):
                arch = prefixes.backup(pfx)
                self.assertIn("CachyOS-11.0-100", arch.name)
                (pfx / "system.reg").write_text("después")
                (pfx / "version").write_text("UMU-Proton-10.0-4")
                self.assertEqual(prefixes.latest_backup(pfx), arch)
                prefixes.restore_backup(pfx, arch)
            self.assertEqual((pfx / "system.reg").read_text(), "antes")
            self.assertEqual(prefixes.proton_version(pfx), "CachyOS-11.0-100")
            self.assertTrue((pfx / "drive_c/Program Files (x86)/World of Warcraft/WowB.exe").exists())
            import subprocess
            members = subprocess.run(["tar", "--zstd", "-tf", str(arch)], capture_output=True, text=True).stdout
            self.assertNotIn("WowB.exe", members)


if __name__ == "__main__":
    unittest.main()
