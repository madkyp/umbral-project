import unittest

from umbral import gameopts
from umbral.config import BATTLENET_ID, Config, Game, Prefix

RUNNERS = ["GE-Proton", "GE-Proton11-7-x86_64", "UMU-Proton-10.0-4"]


def make_cfg():
    cfg = Config()
    cfg.prefixes += [Prefix(BATTLENET_ID, "Battle.net", "/pfx/bnet", "UMU-Proton-10.0-4"),
                     Prefix("p-game", "Pokemon", "/pfx/game", "GE-Proton")]
    cfg.games += [Game(BATTLENET_ID, "Battle.net", "battlenet", BATTLENET_ID, ""),
                  Game("wow", "WoW", "blizzard", BATTLENET_ID, "/w/WowB.exe", product="wow_classic_beta"),
                  Game("poke", "Pokemon", "custom", "p-game", "/g/Game.exe")]
    return cfg


def setp(cfg, gid, *pairs):
    return gameopts.apply(cfg, cfg.game(gid), gameopts.parse(list(pairs)), RUNNERS)


class TestGameOpts(unittest.TestCase):
    def test_set_and_describe(self):
        cfg = make_cfg()
        changed = setp(cfg, "poke", "gamemode=on", "mangohud=yes", "mangohud_preset=full", "fps_limit=120",
                       "esync=off", "gamescope_resolution=640x480", "gamescope_filter=nearest",
                       "env.DXVK_HUD=fps", "runner=UMU-Proton-10.0-4")
        o = cfg.game("poke").options
        self.assertEqual((o.gamemode, o.mangohud, o.fps_limit, o.no_esync), (True, True, 120, True))
        self.assertEqual((o.gs_resolution, o.gs_filter, o.runner), ("640x480", "nearest", "UMU-Proton-10.0-4"))
        self.assertEqual(o.env, {"DXVK_HUD": "fps"})
        self.assertIn("env.DXVK_HUD", changed)
        d = gameopts.describe(cfg, cfg.game("poke"))
        self.assertEqual(d["options"]["esync"], False)                 # clave pública, no «no_esync»
        self.assertEqual(d["effective"]["fsync"], True)                # sin ajuste: Proton lo usa
        self.assertEqual(d["effective"]["runner"], "UMU-Proton-10.0-4")
        # default vuelve a heredar; env.X= quita la variable; «screen» = resolución de la pantalla
        setp(cfg, "poke", "gamemode=default", "env.DXVK_HUD=", "gamescope_resolution=screen")
        self.assertIsNone(o.gamemode)
        self.assertEqual(o.env, {})
        self.assertEqual(gameopts.describe(cfg, cfg.game("poke"))["options"]["gamescope_resolution"], "screen")

    def test_battlenet_edits_prefix_and_wow_inherits(self):
        cfg = make_cfg()
        setp(cfg, BATTLENET_ID, "fps_limit=60", "runner=GE-Proton11-7-x86_64")
        self.assertEqual(cfg.prefix(BATTLENET_ID).options.fps_limit, 60)
        self.assertEqual(cfg.prefix(BATTLENET_ID).runner, "GE-Proton11-7-x86_64")
        d = gameopts.describe(cfg, cfg.game("wow"))
        self.assertIsNone(d["options"]["fps_limit"])                   # WoW no lo tiene puesto…
        self.assertEqual(d["effective"]["fps_limit"], 60)              # …pero lo hereda del prefijo
        self.assertTrue(d["effective"]["writecopy"])                   # valores base de Battle.net

    def test_validation_is_all_or_nothing(self):
        cfg = make_cfg()
        bad = [("gamemode=quizas",), ("fps_limit=-5",), ("gamescope_mode=giant",), ("gpu=nvidia",),
               ("runner=Proton-9000",), ("nokey=1",), ("sinigual",), ("env.1X=a",)]
        for pairs in bad:
            with self.assertRaises(gameopts.OptionError, msg=pairs):
                setp(cfg, "poke", "gamemode=on", *pairs)
            self.assertIsNone(cfg.game("poke").options.gamemode, pairs)  # nada aplicado
        with self.assertRaises(gameopts.OptionError):
            setp(cfg, "wow", "runner=GE-Proton")                         # Blizzard: Proton del prefijo

    def test_listing(self):
        self.assertEqual([g["id"] for g in gameopts.listing(make_cfg())], [BATTLENET_ID, "wow", "poke"])


if __name__ == "__main__":
    unittest.main()
