import tempfile
import unittest
import unittest.mock
from pathlib import Path

from umbral import installers, launcher


class TestInstallers(unittest.TestCase):
    def test_is_installer(self):
        for name in ("GameSetup.exe", "setup_x64.exe", "Instalador.exe", "7z2409-x64.msi", "install.exe"):
            self.assertTrue(installers.is_installer(name), name)
        for name in ("Game.exe", "WowB.exe", "notepad.exe"):
            self.assertFalse(installers.is_installer(name), name)

    def test_windows_path(self):
        self.assertEqual(installers.windows_path("/home/x/Descargas/juego.msi"),
                         "Z:\\home\\x\\Descargas\\juego.msi")

    def test_commands(self):
        self.assertEqual(launcher._windows_command("/tmp/a.msi"), ["msiexec", "/i", "Z:\\tmp\\a.msi"])
        self.assertEqual(launcher._windows_command("/tmp/a.bat"), ["cmd", "/c", "Z:\\tmp\\a.bat"])
        self.assertEqual(launcher._windows_command("/tmp/a.exe"), ["/tmp/a.exe"])

    def test_new_executables_after_install(self):
        with tempfile.TemporaryDirectory() as d:
            pfx = Path(d)
            (pfx / "drive_c/windows/system32").mkdir(parents=True)
            (pfx / "drive_c/windows/system32/cmd.exe").write_bytes(b"MZ")
            before = installers.snapshot(pfx)
            self.assertEqual(before, set())             # windows/ se ignora
            game = pfx / "drive_c/Program Files/Mi Juego"
            (game / "bin").mkdir(parents=True)
            (game / "bin/MiJuego.exe").write_bytes(b"MZ" * 500_000)
            (game / "unins000.exe").write_bytes(b"MZ" * 900_000)   # desinstalador: fuera
            (game / "Tool.exe").write_bytes(b"MZ" * 40_000)
            (game / "tiny.exe").write_bytes(b"MZ" * 10)               # relleno: fuera
            stub = pfx / "drive_c/Program Files/Internet Explorer"     # de serie en Wine: fuera
            stub.mkdir(parents=True)
            (stub / "iexplore.exe").write_bytes(b"MZ" * 500_000)
            found = installers.new_executables(pfx, before)
            self.assertEqual([p.name for p in found], ["MiJuego.exe", "Tool.exe"])   # el grande primero
            self.assertEqual(installers.pretty_name(found[0]), "Mi Juego")         # salta la carpeta bin



class TestBallThumbnail(unittest.TestCase):
    def test_cover_and_icon_become_same_square(self):
        from PIL import Image

        from umbral import exeicon
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(exeicon, "ICON_CACHE", Path(d) / "c"):
            wide = Path(d) / "portada.jpg"
            Image.new("RGB", (1280, 720), "red").save(wide)            # foto apaisada
            tall = Path(d) / "icono.png"
            Image.new("RGBA", (64, 200), (0, 0, 255, 255)).save(tall)  # icono estrecho
            a = Image.open(exeicon.ball_thumbnail(str(wide), crop=True))
            b = Image.open(exeicon.ball_thumbnail(str(tall), crop=False))
            self.assertEqual(a.size, (256, 256))
            self.assertEqual(b.size, (256, 256))
            self.assertEqual(a.getpixel((0, 0))[3], 0)        # esquina redondeada: transparente
            self.assertEqual(a.getpixel((128, 128))[3], 255)
            self.assertEqual(b.getpixel((10, 128))[3], 0)      # el icono no se deforma: bordes libres
            self.assertEqual(b.getpixel((128, 128))[3], 255)


if __name__ == "__main__":
    unittest.main()
