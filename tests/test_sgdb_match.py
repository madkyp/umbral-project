import unittest
from unittest import mock

from umbral import sgdb


class TestCoverMatch(unittest.TestCase):
    def test_queries_clean_versions_tags_and_parts(self):
        self.assertEqual(sgdb.queries("Pokemon Unbound v2.0.3.2"), ["Pokemon Unbound"])
        self.assertEqual(sgdb.queries("Golden Sun (Europe) (En,Fr)"), ["Golden Sun"])
        self.assertEqual(sgdb.queries("Piramide - El Sueño del Faraon"),
                         ["Piramide - El Sueño del Faraon", "Piramide", "El Sueño del Faraon"])

    def test_wrong_first_result_is_not_used(self):
        """El fallo real: para La Pirámide el primer resultado era «Estación Salhate»."""
        results = {"Piramide - El Sueño del Faraon": [sgdb.SGDBGame(1, "Estación Salhate")],
                   "Piramide": [sgdb.SGDBGame(1, "Estación Salhate")],
                   "El Sueño del Faraon": []}
        with mock.patch.object(sgdb, "search", side_effect=lambda q, key=None: results.get(q, [])):
            game, score = sgdb.match("Piramide - El Sueño del Faraon")
            self.assertLess(score, sgdb.MIN_SCORE)
            with mock.patch.object(sgdb, "images") as images:
                self.assertIsNone(sgdb.best_cover("Piramide - El Sueño del Faraon"))
                images.assert_not_called()               # ni siquiera pide sus portadas

    def test_best_scored_result_wins_and_original_title_helps(self):
        results = {"Pokemon Unbound": [sgdb.SGDBGame(9, "Pokémon Sword"), sgdb.SGDBGame(7, "Pokémon Unbound")],
                   "The Pink Panther: Passport to Peril": [sgdb.SGDBGame(3, "The Pink Panther: Passport to Peril")]}
        with mock.patch.object(sgdb, "search", side_effect=lambda q, key=None: results.get(q, [])):
            self.assertEqual(sgdb.match("Pokemon Unbound v2.0.3.2")[0].id, 7)
            # título en español: no hay coincidencia… salvo con el nombre original que detectó ScummVM
            self.assertIsNone(sgdb.match("Pantera Rosa: Mision Peligrosa")[0])
            game, score = sgdb.match(["Pantera Rosa: Mision Peligrosa", "The Pink Panther: Passport to Peril"])
            self.assertEqual((game.id, score >= sgdb.MIN_SCORE), (3, True))


if __name__ == "__main__":
    unittest.main()
