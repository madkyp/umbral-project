import re
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umbral import engines, launcher, running
from umbral.config import Config, Game, LaunchOptions

SECTOR = 2048


def _dir_record(name: bytes, lba: int, size: int, is_dir: bool = False) -> bytes:
    n = 33 + len(name) + (len(name) + 1) % 2
    rec = bytearray(n)
    rec[0] = n
    struct.pack_into("<I", rec, 2, lba)
    struct.pack_into("<I", rec, 10, size)
    rec[25] = 2 if is_dir else 0
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def make_iso(files: dict[str, bytes], raw: bool = False) -> bytes:
    """ISO 9660 mínima: PVD en el sector 16, raíz en el 18 y los archivos detrás.
    raw=True la envuelve en sectores de 2352 bytes en modo 2 (como un .bin de PS1)."""
    sectors: dict[int, bytes] = {}
    root = _dir_record(b"\x00", 18, SECTOR, True) + _dir_record(b"\x01", 18, SECTOR, True)
    lba = 19
    for name, data in files.items():
        root += _dir_record(name.encode() + b";1", lba, len(data))
        for i in range(0, max(1, len(data)), SECTOR):
            sectors[lba] = data[i:i + SECTOR]
            lba += 1
    pvd = bytearray(SECTOR)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[156:156 + 34] = _dir_record(b"\x00", 18, SECTOR, True)
    sectors[16], sectors[18] = bytes(pvd), root
    out = bytearray()
    for i in range(lba):
        data = sectors.get(i, b"").ljust(SECTOR, b"\x00")
        if raw:
            head = b"\x00" + b"\xff" * 10 + b"\x00" + b"\x00\x02\x00" + b"\x02" + b"\x00" * 8
            out += head + data + b"\x00" * (2352 - 24 - SECTOR)
        else:
            out += data
    return bytes(out)


class TestDiscs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_ps2_iso(self):
        p = self.write("Okami (Europe).iso", make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLES_546.15;1\r\n"}))
        c = engines.detect(p)
        self.assertEqual([(x.engine, x.system, x.title) for x in c], [("emulator", "ps2", "Okami")])

    def test_ps1_raw_bin_and_cue(self):
        self.write("Crash.bin", make_iso({"SYSTEM.CNF": b"BOOT = cdrom:\\SCES_009.67;1\r\n"}, raw=True))
        cue = self.write("Crash (Spain).cue", b'FILE "Crash.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        self.assertEqual(engines.disc_system(self.dir / "Crash.bin"), "ps1")
        self.assertEqual([(x.system, x.title) for x in engines.detect(cue)], [("ps1", "Crash")])

    def test_pc_iso_needs_extracting(self):
        p = self.write("Peligrosa.iso", make_iso({"SETUP.EXE": b"MZ"}))
        c = engines.detect(p)
        self.assertEqual([x.engine for x in c], ["extract"])

    def test_gamecube_and_wii(self):
        gc = bytearray(0x40)
        gc[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
        wii = bytearray(0x40)
        wii[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
        self.assertEqual(engines.detect(self.write("Zelda.iso", bytes(gc)))[0].system, "gc")
        self.assertEqual(engines.detect(self.write("Mario.iso", bytes(wii)))[0].system, "wii")
        rvz = bytearray(0x100)
        rvz[:4] = b"RVZ\x01"
        struct.pack_into(">I", rvz, 0x48, 2)
        self.assertEqual(engines.detect(self.write("Metroid.rvz", bytes(rvz)))[0].system, "wii")

    def test_chd_cd_vs_dvd(self):
        def chd(tag: bytes) -> bytes:
            head = bytearray(124 + 16)
            head[:8] = b"MComprHD"
            struct.pack_into(">I", head, 12, 5)
            struct.pack_into(">Q", head, 48, 124)
            head[124:128] = tag
            return bytes(head)
        self.assertEqual(engines.detect(self.write("a.chd", chd(b"CHT2")))[0].system, "ps1")
        self.assertEqual(engines.detect(self.write("b.chd", chd(b"DVD ")))[0].system, "ps2")

    def test_roms_by_extension(self):
        self.assertEqual(engines.detect(self.write("Golden Sun (Europe) (En,Fr,De) [!].gba", b"x"))[0].title,
                         "Golden Sun")
        self.assertEqual(engines.detect(self.write("Tetris.gb", b"x"))[0].system, "gb")

    def test_psp_iso_and_pbp(self):
        self.assertEqual(engines.detect(self.write("Patapon.iso", make_iso({"UMD_DATA.BIN": b"x"})))[0].system, "psp")

        def pbp(category: bytes) -> bytes:
            keys = b"CATEGORY\x00"
            sfo = bytearray(b"\x00PSF" + struct.pack("<IIII", 0x101, 20 + 16, 20 + 16 + len(keys), 1))
            sfo += struct.pack("<HHIII", 0, 0x204, 3, 4, 0) + keys + category + b"\x00\x00"
            return b"\x00PBP" + struct.pack("<II", 0x10000, 0x28) + struct.pack("<II", 0x28 + len(sfo), 0) + \
                b"\x00" * 20 + bytes(sfo)              # cabecera de 0x28 bytes y PARAM.SFO detrás
        self.assertEqual(engines.detect(self.write("EBOOT.PBP", pbp(b"ME")))[0].system, "ps1")
        self.assertEqual(engines.detect(self.write("game.pbp", pbp(b"UG")))[0].system, "psp")

    def test_tanda1_extensions(self):
        for name, system in (("Mario.nes", "nes"), ("Zelda.sfc", "snes"), ("Mario 64.z64", "n64"),
                             ("Pokemon Perla.nds", "ds"), ("Zelda.3ds", "3ds"), ("Sonic.md", "md"),
                             ("Alex Kidd.sms", "sms"), ("Sonic.gg", "gg"), ("Bonk.pce", "pce"),
                             ("Pitfall.a26", "a2600")):
            self.assertEqual(engines.detect(self.write(name, b"x"))[0].system, system, name)

    def test_roms_go_to_system_and_game_folder(self):
        root = self.dir / "games"
        zafiro = engines.organize_rom(str(self.write("Pokemon Zafiro (Spain).gba", b"x")), root, "gba", "Pokémon Zafiro")
        rubi = engines.organize_rom(str(self.write("Pokemon Rubi.gba", b"y")), root, "gba", "Pokémon Rubí")
        self.assertEqual(Path(zafiro), root / "GBA" / "Pokémon Zafiro" / "Pokemon Zafiro (Spain).gba")
        self.assertEqual(Path(rubi), root / "GBA" / "Pokémon Rubí" / "Pokemon Rubi.gba")
        self.assertTrue(engines.is_organized(zafiro, root, "gba"))
        # un .cue se lleva sus .bin
        self.write("Crash (Track 1).bin", b"1")
        self.write("Crash (Track 2).bin", b"2")
        cue = self.write("Crash.cue", b'FILE "Crash (Track 1).bin" BINARY\nFILE "Crash (Track 2).bin" BINARY\n')
        new = Path(engines.organize_rom(str(cue), root, "ps1", "Crash Bandicoot"))
        self.assertEqual(sorted(x.name for x in new.parent.iterdir()),
                         ["Crash (Track 1).bin", "Crash (Track 2).bin", "Crash.cue"])
        self.assertFalse(cue.exists())

    def test_vm_pack_folder(self):
        pack = self.dir / "La Pantera Rosa - Qemu-Win98 CD"
        (pack / "Qemu").mkdir(parents=True)
        (pack / "CD").mkdir()
        (pack / "Qemu" / "Disco98.qcow2").write_bytes(b"QFI\xfb")
        (pack / "CD" / "juego.iso").write_bytes(make_iso({"SETUP.EXE": b"MZ"}))
        with mock.patch.object(engines, "scummvm_detect", return_value=[]):
            c = engines.detect(pack)
        self.assertEqual([x.engine for x in c], ["vm", "extract"])
        self.assertEqual(c[0].title, "La Pantera Rosa")
        self.assertTrue(c[0].extra["cdrom"].endswith("juego.iso"))


class TestScummVM(unittest.TestCase):
    OUT = ("GameID                         Description                                                Full Path\n"
           "------------------------------ ---------------------------------------------------------- ---------\n"
           "pink:peril                     The Pink Panther: Passport to Peril (Windows/Spanish)      /g/pink\n")

    def test_detect_parses_table(self):
        with mock.patch.object(engines, "scummvm_binary", return_value=["scummvm"]), \
                mock.patch("subprocess.run") as run, mock.patch.object(Path, "is_dir", return_value=True):
            run.return_value.stdout = self.OUT
            self.assertEqual(engines.scummvm_detect(Path("/g/pink")),
                             [("pink:peril", "The Pink Panther: Passport to Peril (Windows/Spanish)")])
            self.assertEqual(engines.scummvm_detect(Path("/g/otro")), [])   # otra carpeta: no cuenta

    def test_exe_inside_scummvm_game(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "game" / "INSTALL" / "PPTP.EXE"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            found = {exe.parent.parent: [("pink:peril", "The Pink Panther: Passport to Peril (Windows/Spanish)")]}
            with mock.patch.object(engines, "scummvm_detect", side_effect=lambda f: found.get(f, [])):
                c = engines.detect(exe)
        self.assertEqual([(x.engine, x.title) for x in c],
                         [("scummvm", "The Pink Panther: Passport to Peril"), ("wine", "PPTP")])


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rom = Path(self.tmp.name) / "juego.iso"
        self.rom.write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scummvm_command(self):
        g = Game("a", "Pink", "scummvm", "", self.tmp.name, target="pink:peril")
        with mock.patch.object(engines, "scummvm_binary", return_value=["/usr/bin/scummvm"]):
            argv, cwd = engines.command(g, True)
        self.assertEqual(argv, ["/usr/bin/scummvm", f"--path={self.tmp.name}", "--fullscreen", "pink:peril"])
        self.assertEqual(cwd, self.tmp.name)

    def test_emulator_fullscreen_goes_before_the_rom(self):
        for system, base, expect in (
                ("ps2", ["pcsx2-qt"], ["pcsx2-qt", "-batch", "-fullscreen", "--", str(self.rom)]),
                ("gc", ["flatpak", "run", "org.DolphinEmu.dolphin-emu"],
                 ["flatpak", "run", f"--filesystem={self.rom.parent}", "org.DolphinEmu.dolphin-emu", "-b", "-C",
                  "Dolphin.Display.Fullscreen=True", "-e", str(self.rom)]),
                ("gba", ["mgba-qt"], ["mgba-qt", "-f", str(self.rom)])):
            g = Game("a", "x", "emulator", "", str(self.rom), system=system)
            with mock.patch.object(engines, "find_emulator", return_value=base):
                self.assertEqual(engines.command(g, True)[0], expect)

    def test_missing_emulator_says_how_to_install(self):
        g = Game("a", "x", "emulator", "", str(self.rom), system="ps2")
        with mock.patch.object(engines, "find_emulator", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, re.escape(engines.install_hint("pcsx2"))):  # en cualquier idioma
                engines.command(g, False)
        g = Game("a", "x", "emulator", "", str(self.rom), system="a2600")
        with mock.patch.object(engines, "find_emulator", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "sudo pacman -S stella"):   # sin Flatpak
                engines.command(g, False)

    def test_ares_gets_its_system_name(self):
        g = Game("a", "x", "emulator", "", str(self.rom), system="n64")
        with mock.patch.object(engines, "find_emulator", side_effect=lambda k: ["/usr/bin/ares"] if k == "ares" else None):
            self.assertEqual(engines.command(g, True)[0],
                             ["/usr/bin/ares", "--system", "Nintendo 64", "--fullscreen", str(self.rom)])

    def test_first_installed_emulator_wins(self):
        with mock.patch.object(engines, "find_emulator", side_effect=lambda k: ["blastem"] if k == "blastem" else None):
            self.assertEqual(engines.emulator_for("md"), ("blastem", ["blastem"]))
        with mock.patch.object(engines, "find_emulator", return_value=None):
            self.assertEqual(engines.emulator_for("md"), ("ares", None))     # el recomendado

    def test_build_native_plan(self):
        g = Game("a", "x", "emulator", "", str(self.rom), system="ps2",
                 options=LaunchOptions(gamemode=True, env={"FOO": "1"}))
        with mock.patch.object(engines, "find_emulator", return_value=["pcsx2-qt"]), \
                mock.patch.object(launcher, "deck_hook", return_value={}), \
                mock.patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            plan = launcher.build_native(Config(), g)
        self.assertEqual(plan.argv[:2], ["gamemoderun", "pcsx2-qt"])
        self.assertIn("-fullscreen", plan.argv)                 # pantalla completa por defecto
        self.assertEqual(plan.env, {"FOO": "1"})                # sin WINEPREFIX ni Proton
        self.assertEqual(plan.runner.kind, "native")


class TestNoWineserverWithoutPrefix(unittest.TestCase):
    def test_wineserver_kill_needs_a_prefix(self):
        with mock.patch("subprocess.run") as run:
            self.assertFalse(running.wineserver_kill("", ""))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
