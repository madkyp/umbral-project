import tempfile
import unittest
from pathlib import Path

from umbral import updates

REMOTE = """Region!STRING:0|BuildConfig!HEX:16|CDNConfig!HEX:16|KeyRing!HEX:16|BuildId!DEC:4|VersionsName!String:0|ProductConfig!HEX:16
## seqn = 4038352
us|05215079e3905ef5922ae0b03ffefb73|9b3c|  |70009|1.60.1.70009|fcfd
eu|05215079e3905ef5922ae0b03ffefb73|9b3c|  |70009|1.60.1.70009|fcfd
"""
BUILD_INFO = ("Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|Version!STRING:0|Product!STRING:0\n"
              "us|1|{key}|1.60.1.70009|wow_classic_beta\n")


class TestUpdates(unittest.TestCase):
    def _local(self, key):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name) / "World of Warcraft"
        (root / "_classic_beta_").mkdir(parents=True)
        (root / ".build.info").write_text(BUILD_INFO.format(key=key))
        exe = root / "_classic_beta_" / "WowB.exe"
        exe.write_bytes(b"MZ")
        return updates.local_build(str(exe), "wow_classic_beta")

    def test_up_to_date(self):
        st = updates.compare(self._local("05215079e3905ef5922ae0b03ffefb73"), updates.parse_table(REMOTE), "us")
        self.assertEqual((st.state, st.remote), ("ok", "1.60.1.70009"))

    def test_update_available(self):
        st = updates.compare(self._local("aaaa079e3905ef5922ae0b03ffefb73"), updates.parse_table(REMOTE), "eu")
        self.assertEqual(st.state, "update")

    def test_unknown_region_or_install(self):
        self.assertEqual(updates.compare(None, updates.parse_table(REMOTE), "us").state, "unknown")
        local = self._local("05215079e3905ef5922ae0b03ffefb73")
        self.assertEqual(updates.compare(local, updates.parse_table(REMOTE), "kr").state, "unknown")


if __name__ == "__main__":
    unittest.main()
