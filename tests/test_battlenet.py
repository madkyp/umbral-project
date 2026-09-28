import tempfile
import unittest
from pathlib import Path

from umbral import battlenet, library
from umbral.config import Config


def _v(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _ld(num, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    return _v(num << 3 | 2) + _v(len(payload)) + payload


def product(uid, code, path, sub="", version="1.60.1.70009"):
    settings = _ld(1, path) + _ld(2, "us") + _v(4 << 3) + _v(1) + (_ld(13, sub) if sub else b"")
    state = _ld(1, _ld(7, version))
    return _ld(1, _ld(1, uid) + _ld(2, code) + _ld(3, settings) + _ld(4, state))


def make_prefix(root: Path, products: bytes | None, files: list[str]) -> Path:
    p = root / "pfx"
    (p / "drive_c/windows/system32").mkdir(parents=True)
    (p / "system.reg").write_text("")
    (p / "dosdevices").mkdir()
    (p / "dosdevices/c:").symlink_to("../drive_c")
    for f in files:
        fp = p / "drive_c" / f
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b"MZ" + b"\0" * 100)
    if products is not None:
        (p / battlenet.AGENT_DIR).mkdir(parents=True, exist_ok=True)
        (p / battlenet.PRODUCT_DB).write_bytes(products)
    return p


BNET = "Program Files (x86)/Battle.net/Battle.net.exe"
WOW = "Program Files (x86)/World of Warcraft"
DB_FOREVER = (product("agent", "agent", "C:/ProgramData/Battle.net/Agent")
              + product("battle.net", "bna", "C:/Program Files (x86)/Battle.net")
              + product("wow_classic_beta", "wow_classic_beta",
                        "C:/Program Files (x86)/World of Warcraft", "_classic_beta_"))


class TestProductDb(unittest.TestCase):
    def test_parse(self):
        prods = battlenet.parse_product_db(DB_FOREVER)
        self.assertEqual([p.uid for p in prods], ["agent", "battle.net", "wow_classic_beta"])
        wow = prods[2]
        self.assertEqual(wow.install_path, "C:/Program Files (x86)/World of Warcraft")
        self.assertEqual(wow.subfolder, "_classic_beta_")
        self.assertEqual(wow.version, "1.60.1.70009")

    def test_corrupt_db_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), b"\xff\xff\xff", [BNET, f"{WOW}/.build.info",
                                                        f"{WOW}/_classic_beta_/WowB.exe"])
            games = battlenet.detect_games(p)
            self.assertEqual([g.uid for g in games], ["wow_classic_beta"])


class TestDetection(unittest.TestCase):
    def test_only_battlenet_no_wow(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), product("battle.net", "bna", "C:/Program Files (x86)/Battle.net"), [BNET])
            self.assertTrue(battlenet.is_installed(p))
            self.assertEqual(battlenet.detect_games(p), [])

    def test_wow_registered_but_not_downloaded(self):
        # product.db lo lista pero el .exe aún no existe (descarga en curso) -> no aparece
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET, f"{WOW}/.build.info"])
            self.assertEqual(battlenet.detect_games(p), [])

    def test_wow_forever_installed(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET, f"{WOW}/.build.info",
                                                  f"{WOW}/_classic_beta_/WowB.exe",
                                                  f"{WOW}/_classic_beta_/BlizzardError.exe",
                                                  f"{WOW}/World of Warcraft Launcher.exe"])
            games = battlenet.detect_games(p)
            self.assertEqual(len(games), 1)
            g = games[0]
            self.assertEqual(g.name, "World of Warcraft: Forever (beta)")
            self.assertTrue(g.confirmed and g.is_wow)
            self.assertEqual(g.exe.name, "WowB.exe")

    def test_unknown_future_product(self):
        # producto final de Forever con uid aún desconocido: se detecta genéricamente
        db = product("wow_forever", "wow_forever", "C:/Program Files (x86)/World of Warcraft", "_forever_")
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), db, [BNET, f"{WOW}/.build.info", f"{WOW}/_forever_/WowF.exe"])
            g = battlenet.detect_games(p)[0]
            self.assertEqual(g.name, "World of Warcraft: Forever")
            self.assertFalse(g.confirmed)

    def test_library_sync_adds_and_removes(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET, f"{WOW}/.build.info",
                                                  f"{WOW}/_classic_beta_/WowB.exe"])
            cfg = Config()
            library.ensure_battlenet_prefix(cfg, p, "GE-Proton")
            self.assertEqual(library.sync_detected(cfg), ["+ World of Warcraft: Forever (beta)"])
            self.assertEqual([g.kind for g in cfg.games], ["battlenet", "blizzard"])
            # por defecto: el .exe directamente (la versión se comprueba antes en el controlador)
            exe, args = library.launch_target(cfg, cfg.games[1])
            self.assertTrue(exe.endswith("WowB.exe"))
            self.assertEqual(args, [])
            (p / "drive_c" / WOW / "_classic_beta_/WowB.exe").unlink()
            self.assertEqual(library.sync_detected(cfg), ["- World of Warcraft: Forever (beta)"])

    def test_deleted_prefix_removes_games(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET, f"{WOW}/.build.info", f"{WOW}/_classic_beta_/WowB.exe"])
            cfg = Config()
            library.ensure_battlenet_prefix(cfg, p, "GE-Proton", imported=True)
            library.sync_detected(cfg)
            self.assertEqual(len(cfg.games), 2)
            shutil.rmtree(p)   # el usuario borra la carpeta (p. ej. ~/Faugus)
            self.assertEqual(library.sync_detected(cfg), ["- World of Warcraft: Forever (beta)"])
            self.assertEqual([g.id for g in cfg.games], ["battlenet"])



class TestProductIcon(unittest.TestCase):
    def test_forever_icon_bundled(self):
        from umbral import exeicon
        # El logo de WoW Forever no se publica en el repositorio (marca de Blizzard):
        # si falta, no hay icono incluido y se usa el extraído del propio juego.
        if not (exeicon.ASSETS / "wow-forever.png").exists():
            self.assertIsNone(exeicon.product_icon("wow_classic_beta"))
            return
        self.assertEqual(exeicon.product_icon("wow_classic_beta").name, "wow-forever.png")
        self.assertEqual(exeicon.product_icon("wow_forever").name, "wow-forever.png")
        self.assertIsNone(exeicon.product_icon("w3"))
        self.assertIsNone(exeicon.product_icon(""))



if __name__ == "__main__":
    unittest.main()
