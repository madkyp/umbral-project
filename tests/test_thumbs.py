import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from umbral import exeicon


class TestCoverThumbnails(unittest.TestCase):
    def test_thumbnail_is_small_cached_and_color_remembered(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "cover.png"
            Image.new("RGB", (1840, 860), (200, 40, 40)).save(src)
            with mock.patch.object(exeicon, "THUMB_DIR", Path(d) / "thumbs"):
                t1 = exeicon.cover_thumbnail(str(src))
                self.assertNotEqual(t1, src)
                self.assertLessEqual(Image.open(t1).size[0], 552)            # ya no la imagen entera
                self.assertEqual(exeicon.cover_thumbnail(str(src)), t1)      # misma miniatura, sin recalcular
                with mock.patch.object(exeicon, "_dominant_color", wraps=exeicon._dominant_color) as calc:
                    c1 = exeicon.dominant_color(t1)
                    c2 = exeicon.dominant_color(t1)
                self.assertEqual(c1, c2)
                self.assertEqual(calc.call_count, 1)                         # el color se calcula una vez
                self.assertTrue(c1.startswith("#c"))                         # rojizo


if __name__ == "__main__":
    unittest.main()
