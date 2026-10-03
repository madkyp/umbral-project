import tempfile
import unittest
from pathlib import Path

from umbral import library
from umbral.config import BATTLENET_ID, Config, Game, Prefix


class TestSyncPrefixName(unittest.TestCase):
    def cfg(self):
        c = Config()
        c.prefixes = [Prefix(BATTLENET_ID, "Battle.net", "/p/bn", "GE"), Prefix("p-game", "Game", "/p/game", "GE"),
                      Prefix("p-shared", "Shared", "/p/shared", "GE")]
        c.games = [Game(BATTLENET_ID, "Battle.net", "battlenet", BATTLENET_ID, ""),
                   Game("wow", "World of Warcraft", "blizzard", BATTLENET_ID, ""),
                   Game("poke", "Pokemon Iberia", "custom", "p-game", "/g/Game.exe"),
                   Game("a", "A", "custom", "p-shared", "/g/a.exe"), Game("b", "B", "custom", "p-shared", "/g/b.exe")]
        return c

    def test_own_prefix_takes_the_game_name(self):
        c = self.cfg()
        self.assertTrue(library.sync_prefix_name(c, c.game("poke")))
        self.assertEqual(c.prefix("p-game").name, "Pokemon Iberia")
        self.assertEqual(c.prefix("p-game").path, "/p/game")   # the folder is not touched
        self.assertFalse(library.sync_prefix_name(c, c.game("poke")))

    def test_shared_and_battlenet_prefixes_keep_their_name(self):
        c = self.cfg()
        self.assertFalse(library.sync_prefix_name(c, c.game("a")))
        self.assertFalse(library.sync_prefix_name(c, c.game("wow")))
        self.assertEqual(c.prefix("p-shared").name, "Shared")
        self.assertEqual(c.prefix(BATTLENET_ID).name, "Battle.net")


class TestPrunePrefixes(unittest.TestCase):
    def test_only_missing_and_unused_prefixes_go(self):
        with tempfile.TemporaryDirectory() as d:
            here = Path(d, "here"); here.mkdir()
            c = Config()
            c.prefixes = [Prefix(BATTLENET_ID, "Battle.net", "/nope/bn", "GE"),
                          Prefix("p-gone", "Gone", "/nope/gone", "GE"),
                          Prefix("p-used", "Used", "/nope/used", "GE"),
                          Prefix("p-here", "Here", str(here), "GE"),
                          Prefix("p-imp", "Imported", "/nope/imp", "GE", imported=True)]
            c.games = [Game("g", "G", "custom", "p-used", "/g/g.exe")]
            self.assertEqual(library.prune_prefixes(c), ["- prefix Gone"])
            self.assertEqual([p.id for p in c.prefixes], [BATTLENET_ID, "p-used", "p-here", "p-imp"])


if __name__ == "__main__":
    unittest.main()
