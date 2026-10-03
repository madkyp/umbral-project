"""Juegos que no van por Wine: ScummVM, emuladores de consola y máquinas virtuales.

`detect(ruta)` mira qué es lo elegido (carpeta, ROM, imagen de disco, .exe…) y devuelve
las formas de abrirlo, la mejor primero. `command(game)` da el comando para lanzarlo.

Umbral no incluye emuladores, BIOS ni sistemas: usa los que tengas instalados (paquete
del sistema, AppImage o Flatpak de Flathub) y puede instalar los de Flathub y las AppImage
oficiales para tu usuario, sin sudo.
"""
from __future__ import annotations

import functools
import logging
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _

log = logging.getLogger(__name__)

# kind de Game para cada motor (los de Wine son "custom", "blizzard" y "battlenet")
SCUMMVM, EMULATOR, VM = "scummvm", "emulator", "vm"
NATIVE_KINDS = (SCUMMVM, EMULATOR, VM)

WINDOWS_EXTS = {".exe", ".msi", ".bat", ".cmd"}
DISC_EXTS = {".iso", ".bin", ".cue", ".img", ".chd", ".mdf"}
VM_EXTS = {".qcow2", ".qcow", ".vmdk", ".vdi", ".vhd", ".vhdx"}


@dataclass(frozen=True)
class Emulator:
    name: str
    binaries: tuple[str, ...]           # en el PATH, por orden de preferencia
    flatpak: str = ""                   # id en Flathub (Umbral lo instala para tu usuario, sin sudo)
    args: tuple[str, ...] = ("{rom}",)  # {rom} = la ROM o imagen · {ares} = nombre del sistema en ares
    fullscreen: tuple[str, ...] = ()    # argumentos para pantalla completa (comprobados en su código)
    package: str = ""                   # paquete de Arch si no hay Flatpak
    appimage: str = ""                  # patrón de su AppImage (en ~/Applications, ~/AppImages, ~/.local/bin)
    appimage_url: str = ""              # descarga oficial de la AppImage


EMULATORS = {
    "ares": Emulator("ares", ("ares",), "dev.ares.ares", ("--system", "{ares}", "{rom}"), ("--fullscreen",),
                     package="ares-emu"),
    "mgba": Emulator("mGBA", ("mgba-qt", "mgba"), "io.mgba.mGBA", fullscreen=("-f",), package="mgba-qt"),
    "melonds": Emulator("melonDS", ("melonDS", "melonds"), "net.kuribo64.melonDS", fullscreen=("-f",)),
    "azahar": Emulator("Azahar", ("azahar",), "org.azahar_emu.Azahar", fullscreen=("-f",)),
    "dolphin": Emulator("Dolphin", ("dolphin-emu",), "org.DolphinEmu.dolphin-emu", ("-b", "-e", "{rom}"),
                        ("-C", "Dolphin.Display.Fullscreen=True"), package="dolphin-emu"),
    # DuckStation ya no está en Flathub: AppImage oficial (o el paquete duckstation-gpl de chaotic-aur)
    "duckstation": Emulator("DuckStation", ("duckstation-qt", "duckstation"), "", ("-batch", "--", "{rom}"),
                            ("-fullscreen",), package="duckstation-gpl", appimage="DuckStation*.AppImage",
                            appimage_url="https://github.com/stenzek/duckstation/releases/latest/download/"
                                         "DuckStation-x64.AppImage"),
    "pcsx2": Emulator("PCSX2", ("pcsx2-qt", "pcsx2"), "net.pcsx2.PCSX2", ("-batch", "--", "{rom}"), ("-fullscreen",)),
    "ppsspp": Emulator("PPSSPP", ("PPSSPPSDL", "ppsspp", "PPSSPPQt"), "org.ppsspp.PPSSPP",
                       fullscreen=("--fullscreen",), package="ppsspp"),
    "blastem": Emulator("BlastEm", ("blastem",), "com.retrodev.blastem", fullscreen=("-f",)),
    "stella": Emulator("Stella", ("stella",), fullscreen=("-fullscreen", "1"), package="stella"),
    "mednafen": Emulator("Mednafen", ("mednafen",), fullscreen=("-video.fs", "1"), package="mednafen"),
    "mupen64plus": Emulator("Mupen64Plus", ("mupen64plus",), fullscreen=("--fullscreen",), package="mupen64plus"),
    "scummvm": Emulator("ScummVM", ("scummvm",), "org.scummvm.ScummVM", package="scummvm"),
}

APPIMAGE_DIRS = [Path.home() / "Applications", Path.home() / "AppImages", Path.home() / ".local/bin"]


@dataclass(frozen=True)
class System:
    name: str
    short: str                     # etiqueta de la tarjeta y nombre de su carpeta (Juegos/GBA/…)
    emulators: tuple[str, ...]     # por preferencia: se usa el primero instalado
    ares: str = ""                 # nombre del sistema en ares (--system)


SYSTEMS = {
    "nes": System("Nintendo (NES)", "NES", ("ares", "mednafen"), "Famicom"),
    "snes": System("Super Nintendo", "SNES", ("ares", "mednafen"), "Super Famicom"),
    "n64": System("Nintendo 64", "N64", ("ares", "mupen64plus"), "Nintendo 64"),
    "gb": System("Game Boy / Game Boy Color", "Game Boy", ("mgba",)),
    "gba": System("Game Boy Advance", "GBA", ("mgba",)),
    "ds": System("Nintendo DS", "DS", ("melonds",)),
    "3ds": System("Nintendo 3DS", "3DS", ("azahar",)),
    "gc": System("GameCube", "GameCube", ("dolphin",)),
    "wii": System("Wii", "Wii", ("dolphin",)),
    "ps1": System("PlayStation", "PS1", ("duckstation", "mednafen")),
    "ps2": System("PlayStation 2", "PS2", ("pcsx2",)),
    "psp": System("PlayStation Portable", "PSP", ("ppsspp",)),
    "md": System("Mega Drive / Genesis", "Mega Drive", ("ares", "blastem", "mednafen"), "Mega Drive"),
    "sms": System("Master System", "Master System", ("ares", "mednafen"), "Master System"),
    "gg": System("Game Gear", "Game Gear", ("ares", "mednafen"), "Game Gear"),
    "pce": System("PC Engine / TurboGrafx-16", "PC Engine", ("ares", "mednafen"), "PC Engine"),
    "a2600": System("Atari 2600", "Atari 2600", ("stella",)),
}
ROM_EXTS = {".gba": "gba", ".gb": "gb", ".gbc": "gb", ".m3u": "ps1",
            ".nes": "nes", ".fds": "nes", ".unf": "nes", ".sfc": "snes", ".smc": "snes",
            ".n64": "n64", ".z64": "n64", ".v64": "n64", ".nds": "ds",
            ".3ds": "3ds", ".cci": "3ds", ".cxi": "3ds", ".3dsx": "3ds",
            ".gcm": "gc", ".gcz": "gc", ".rvz": "gc", ".wia": "gc", ".wbfs": "wii",
            ".pbp": "psp", ".cso": "psp",
            ".md": "md", ".gen": "md", ".smd": "md", ".sms": "sms", ".gg": "gg",
            ".pce": "pce", ".sgx": "pce", ".a26": "a2600"}


@dataclass
class Candidate:
    """Una forma de abrir lo elegido."""
    engine: str                    # SCUMMVM | EMULATOR | VM | "wine" | "extract"
    path: str                      # lo que se guarda en Game.exe (carpeta, ROM, disco o .exe)
    title: str = ""                # nombre propuesto
    system: str = ""               # gba, ps1… (emuladores) o win9x (VM)
    target: str = ""               # id de ScummVM (p. ej. pink:peril)
    detail: str = ""               # texto para el usuario (versión detectada, aviso…)
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.engine == SCUMMVM:
            return "ScummVM"
        if self.engine == EMULATOR:
            key, _base = emulator_for(self.system)
            return f"{EMULATORS[key].name} ({SYSTEMS[self.system].short})"
        if self.engine == VM:
            return _("Máquina virtual (QEMU)")
        if self.engine == "extract":
            return _("Disco de PC: extraer e identificar")
        return _("Wine / Proton")


# ---------------------------------------------------------------- emuladores instalados
@functools.lru_cache(maxsize=1)
def _flatpak_apps() -> frozenset[str]:
    """Apps Flatpak instaladas (de sistema y de usuario), con una sola llamada."""
    if not shutil.which("flatpak"):
        return frozenset()
    try:
        out = subprocess.run(["flatpak", "list", "--app", "--columns=application"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


def _flatpak_installed(app_id: str) -> bool:
    return bool(app_id) and app_id in _flatpak_apps()


def _appimage(emu: Emulator) -> Path | None:
    if not emu.appimage:
        return None
    for folder in APPIMAGE_DIRS:
        found = sorted((x for x in folder.glob(emu.appimage) if x.is_file()), reverse=True)
        if found:
            return found[0]
    return None


def installed_as(key: str) -> str:
    """Cómo está instalado: "package", "appimage", "flatpak" o «» si no lo está."""
    emu = EMULATORS[key]
    if any(shutil.which(b) for b in emu.binaries):
        return "package"
    if _appimage(emu):
        return "appimage"
    if _flatpak_installed(emu.flatpak):
        return "flatpak"
    return ""


def find_emulator(key: str) -> list[str] | None:
    """Comando base del emulador (binario, AppImage o `flatpak run`); None si no está."""
    emu = EMULATORS[key]
    for b in emu.binaries:
        if p := shutil.which(b):
            return [p]
    if img := _appimage(emu):
        return [str(img)]
    if _flatpak_installed(emu.flatpak):
        return ["flatpak", "run", emu.flatpak]
    return None


def emulator_for(system: str) -> tuple[str, list[str] | None]:
    """(emulador, comando) del sistema: el primero instalado de su lista; si no hay ninguno,
    (el recomendado, None)."""
    keys = SYSTEMS[system].emulators
    for k in keys:
        if base := find_emulator(k):
            return k, base
    return keys[0], None


def install_hint(key: str) -> str:
    emu = EMULATORS[key]
    if emu.flatpak or emu.appimage_url:
        return _("instálalo en Sistema → Emuladores")
    return f"sudo pacman -S {emu.package}"


def systems_of(key: str) -> list[str]:
    """Sistemas que abre un emulador (para la lista de Sistema → Emuladores)."""
    return [s.short for s in SYSTEMS.values() if key in s.emulators]


def refresh() -> None:
    """Vuelve a buscar los Flatpak (tras instalar un emulador con Umbral abierto)."""
    _flatpak_apps.cache_clear()


def scummvm_binary() -> list[str] | None:
    return find_emulator("scummvm")


def qemu_binary() -> str | None:
    return shutil.which("qemu-system-i386")


def engine_status(game) -> tuple[str, str]:
    """(nombre del programa que lo abre, «» si está instalado o cómo instalarlo)."""
    if game.kind == SCUMMVM:
        return "ScummVM", "" if scummvm_binary() else install_hint("scummvm")
    if game.kind == EMULATOR and game.system in SYSTEMS:
        key, base = emulator_for(game.system)
        return EMULATORS[key].name, "" if base else install_hint(key)
    if game.kind == VM:
        return "QEMU", "" if qemu_binary() else "sudo pacman -S qemu-system-x86 qemu-img"
    return "", ""


def system_name(system: str) -> str:
    return SYSTEMS[system].name if system in SYSTEMS else system


def short_label(game) -> str:
    """Etiqueta de la tarjeta: ScummVM, GBA, PS2…"""
    if game.kind == SCUMMVM:
        return "ScummVM"
    if game.kind == EMULATOR:
        return SYSTEMS[game.system].short if game.system in SYSTEMS else game.system.upper()
    if game.kind == VM:
        return "Windows 9x"
    return ""


# ---------------------------------------------------------------- instalar emuladores
FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def can_install(key: str) -> bool:
    """Umbral puede instalarlo solo (Flathub para tu usuario o AppImage oficial), sin sudo."""
    emu = EMULATORS[key]
    return bool(emu.appimage_url or (emu.flatpak and shutil.which("flatpak")))


def install(key: str, on_line=lambda s: None) -> None:
    """Instala el emulador sin sudo: su AppImage oficial en ~/Applications o el Flatpak de Flathub
    para tu usuario. RuntimeError si falla."""
    import os
    import urllib.request
    emu = EMULATORS[key]
    if emu.appimage_url:
        dest_dir = APPIMAGE_DIRS[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / emu.appimage_url.rsplit("/", 1)[1]
        tmp = dest.with_suffix(".part")
        on_line(_("Descargando {0}…").format(emu.appimage_url))
        try:
            req = urllib.request.Request(emu.appimage_url, headers={"User-Agent": "umbral-launcher"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            os.chmod(tmp, 0o755)
            tmp.replace(dest)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(str(e)) from e
        on_line(_("Guardado en {0}").format(dest))
    else:
        if not (emu.flatpak and shutil.which("flatpak")):
            raise RuntimeError(_("No se puede instalar desde Umbral: {0}").format(install_hint(key)))
        steps = [["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB],
                 ["flatpak", "install", "--user", "--noninteractive", "-y", "flathub", emu.flatpak]]
        for cmd in steps:
            on_line("$ " + " ".join(cmd))
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 errors="replace", stdin=subprocess.DEVNULL)
            for line in p.stdout:  # type: ignore[union-attr]
                on_line(line.rstrip())
            if p.wait() != 0:
                raise RuntimeError(_("flatpak terminó con código {0}").format(p.returncode))
    refresh()


def uninstall(key: str) -> None:
    """Quita lo que Umbral instaló (el Flatpak de usuario o la AppImage); los paquetes no."""
    emu = EMULATORS[key]
    how = installed_as(key)
    if how == "appimage" and (img := _appimage(emu)):
        img.unlink(missing_ok=True)
    elif how == "flatpak":
        r = subprocess.run(["flatpak", "uninstall", "--user", "--noninteractive", "-y", emu.flatpak],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    refresh()


# ---------------------------------------------------------------- ROMs por carpetas
def rom_files(rom: Path) -> list[Path]:
    """La ROM y los archivos que la acompañan (.bin de un .cue, discos de un .m3u)."""
    out = [rom]
    ext = rom.suffix.lower()
    if ext in (".cue", ".m3u"):
        try:
            lines = rom.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if ext == ".cue":
                m = re.match(r'\s*FILE\s+"?(.+?)"?\s+\w+\s*$', line, re.I)
                name = m.group(1) if m else ""
            else:
                name = line.strip() if line.strip() and not line.startswith("#") else ""
            f = rom.parent / name if name else None
            if f and f.is_file() and f not in out:
                out += [x for x in rom_files(f) if x not in out]
    return out


def rom_folder(games_root: Path, system: str, name: str) -> Path:
    """Juegos/<sistema>/<nombre del juego> (p. ej. games/GBA/Pokémon Zafiro), sin pisar otra."""
    base = games_root / SYSTEMS[system].short
    clean = name.replace("/", "-").strip() or "ROM"
    dest, n = base / clean, 2
    while dest.exists():
        dest, n = base / f"{clean} ({n})", n + 1
    return dest


def is_organized(rom: str, games_root: Path, system: str) -> bool:
    try:
        return Path(rom).resolve().is_relative_to((games_root / SYSTEMS[system].short).resolve())
    except (OSError, ValueError, KeyError):
        return False


def organize_rom(rom: str, games_root: Path, system: str, name: str) -> str:
    """Mueve la ROM (y sus .bin o discos) a su carpeta y devuelve la nueva ruta de la ROM."""
    src = Path(rom)
    dest = rom_folder(games_root, system, name)
    dest.mkdir(parents=True)
    for f in rom_files(src):
        shutil.move(str(f), str(dest / f.name))
    return str(dest / src.name)


# ---------------------------------------------------------------- ScummVM
_DETECT_LINE = re.compile(r"^(\S+:\S+|\S+)\s{2,}(.+?)\s{2,}(/.*)$")


def scummvm_detect(folder: Path) -> list[tuple[str, str]]:
    """[(id, descripción)] de los juegos de ScummVM en esa carpeta (sin añadirlos a su configuración)."""
    cmd = scummvm_binary()
    if not cmd or not folder.is_dir():
        return []
    try:
        if cmd[0] == "flatpak":
            cmd = [*cmd[:2], f"--filesystem={folder}:ro", *cmd[2:]]
        out = subprocess.run([*cmd, "--detect", f"--path={folder}"], capture_output=True, text=True,
                             timeout=30, errors="replace").stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("scummvm --detect falló: %s", e)
        return []
    found = []
    for line in out.splitlines():
        m = _DETECT_LINE.match(line.rstrip())
        if m and m.group(1) != "GameID" and Path(m.group(3)) == folder:
            found.append((m.group(1), m.group(2).strip()))
    return found


def _scummvm_title(desc: str) -> str:
    """«The Pink Panther: Passport to Peril (Windows/Spanish)» → sin el paréntesis final."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", desc).strip() or desc


# ---------------------------------------------------------------- imágenes de disco
SECTOR = 2048
_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


class _Disc:
    """Lectura mínima de ISO 9660 en .iso (2048) o .bin en bruto (2352, modo 1 o 2)."""

    def __init__(self, path: Path):
        self.f = open(path, "rb")
        head = self.f.read(16)
        self.raw = head[:12] == _SYNC
        self.size, self.offset = (2352, 24 if head[15] == 2 else 16) if self.raw else (SECTOR, 0)

    def close(self):
        self.f.close()

    def sector(self, lba: int, count: int = 1) -> bytes:
        out = b""
        for i in range(count):
            self.f.seek((lba + i) * self.size + self.offset)
            out += self.f.read(SECTOR)
        return out

    def root(self) -> list[tuple[str, int, int, bool]]:
        pvd = self.sector(16)
        if pvd[1:6] != b"CD001":
            return []
        return self._dir(struct.unpack_from("<I", pvd, 156 + 2)[0], struct.unpack_from("<I", pvd, 156 + 10)[0])

    def _dir(self, lba: int, size: int) -> list[tuple[str, int, int, bool]]:
        data = self.sector(lba, max(1, -(-size // SECTOR)))[:size]
        out, i = [], 0
        while i < len(data):
            n = data[i]
            if n == 0:                       # relleno hasta el siguiente sector
                i = (i // SECTOR + 1) * SECTOR
                continue
            rec = data[i:i + n]
            name = rec[33:33 + rec[32]].decode("latin-1").split(";")[0]
            if name not in ("\x00", "\x01"):
                out.append((name.upper(), struct.unpack_from("<I", rec, 2)[0],
                            struct.unpack_from("<I", rec, 10)[0], bool(rec[25] & 2)))
            i += n
        return out

    def read_file(self, name: str, limit: int = 4096) -> bytes | None:
        for n, lba, size, is_dir in self.root():
            if n == name and not is_dir:
                return self.sector(lba, max(1, -(-min(size, limit) // SECTOR)))[:min(size, limit)]
        return None


def _playstation(path: Path) -> str:
    """ps1 / ps2 según SYSTEM.CNF (BOOT2 = PS2, BOOT = PS1); «» si no es de PlayStation."""
    try:
        d = _Disc(path)
    except OSError:
        return ""
    try:
        cnf = d.read_file("SYSTEM.CNF")
    except (OSError, struct.error, IndexError):
        cnf = None
    finally:
        d.close()
    if not cnf:
        return ""
    text = cnf.decode("latin-1", "replace").upper()
    if "BOOT2" in text:
        return "ps2"
    if "BOOT" in text:
        return "ps1"
    return ""


def _nintendo(head: bytes) -> str:
    """GameCube / Wii por la firma de su cabecera de disco."""
    if len(head) >= 0x20 and head[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
        return "gc"
    if len(head) >= 0x1C and head[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
        return "wii"
    return ""


def _chd_system(path: Path) -> str:
    """CHD: los de CD suelen ser de PS1 y los de DVD de PS2 (metadatos CHT2/CHCD frente a DVD)."""
    try:
        with open(path, "rb") as f:
            head = f.read(124)
            if head[:8] != b"MComprHD":
                return ""
            version = struct.unpack_from(">I", head, 12)[0]
            meta = struct.unpack_from(">Q", head, 48 if version >= 5 else 36)[0]
            f.seek(meta)
            tag = f.read(4)
    except (OSError, struct.error):
        return ""
    if tag in (b"CHT2", b"CHTR", b"CHCD", b"CHGD"):
        return "ps1"
    if tag == b"DVD ":
        return "ps2"
    return ""


def _cue_data(cue: Path) -> Path | None:
    """Primer archivo de datos al que apunta un .cue."""
    try:
        for line in cue.read_text(errors="replace").splitlines():
            m = re.match(r'\s*FILE\s+"?(.+?)"?\s+\w+\s*$', line, re.I)
            if m:
                p = cue.parent / m.group(1)
                return p if p.exists() else None
    except OSError:
        pass
    return None


def _psp_iso(path: Path) -> bool:
    """UMD de PSP: carpeta PSP_GAME o UMD_DATA.BIN en la raíz."""
    try:
        d = _Disc(path)
    except OSError:
        return False
    try:
        names = {n for n, *_r in d.root()}
    except (OSError, struct.error, IndexError):
        names = set()
    finally:
        d.close()
    return "PSP_GAME" in names or "UMD_DATA.BIN" in names


def _pbp_system(path: Path) -> str:
    """EBOOT.PBP: los clásicos de PS1 para PSP llevan CATEGORY=ME en su PARAM.SFO."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x28)
            if head[:4] != b"\x00PBP":
                return ""
            start, end = struct.unpack_from("<II", head, 8)
            f.seek(start)
            sfo = f.read(min(max(0, end - start), 65536))
        if sfo[:4] != b"\x00PSF":
            return "psp"
        keys, data, count = struct.unpack_from("<III", sfo, 8)
        for i in range(count):
            koff, _fmt, length, _mx, doff = struct.unpack_from("<HHIII", sfo, 20 + 16 * i)
            key = sfo[keys + koff:sfo.index(b"\x00", keys + koff)]
            if key == b"CATEGORY":
                return "ps1" if sfo[data + doff:data + doff + length].rstrip(b"\x00") == b"ME" else "psp"
    except (OSError, struct.error, ValueError):
        return ""
    return "psp"


def disc_system(path: Path) -> str:
    """Sistema de una imagen de disco: ps1, ps2, psp, gc, wii o «» (p. ej. un CD de PC)."""
    ext = path.suffix.lower()
    if ext == ".chd":
        return _chd_system(path)
    if ext == ".cue":
        data = _cue_data(path)
        return _playstation(data) if data else ""
    try:
        with open(path, "rb") as f:
            head = f.read(0x20)
    except OSError:
        return ""
    return _nintendo(head) or _playstation(path) or ("psp" if _psp_iso(path) else "")


def clean_title(name: str) -> str:
    """«Golden Sun (Europe) (En,Fr,De) [!]» → «Golden Sun»."""
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", name).replace("_", " ").strip()
    return re.sub(r"\s{2,}", " ", t) or name


# ---------------------------------------------------------------- detección
def folder_title(folder: Path) -> str:
    """«La Pantera Rosa en Mision Peligrosa - Qemu-Win98 CD» → «La Pantera Rosa en Mision Peligrosa»."""
    return clean_title(re.sub(r"\s*-\s*(Qemu|DOSBox|ScummVM)\b.*$", "", folder.name, flags=re.I))


def _scummvm_candidates(folder: Path) -> list[Candidate]:
    return [Candidate(SCUMMVM, str(folder), _scummvm_title(desc), target=gid, detail=desc)
            for gid, desc in scummvm_detect(folder)]


def detect(path: str | Path) -> list[Candidate]:
    """Formas de abrir lo elegido, la mejor primero. Puede tardar (ScummVM): fuera del hilo de la UI."""
    p = Path(path)
    ext = p.suffix.lower()
    out: list[Candidate] = []
    if p.is_dir():
        out += _scummvm_candidates(p)
        for disk in sorted(x for x in p.rglob("*") if x.suffix.lower() in VM_EXTS):
            out.append(_vm_candidate(disk, p))
        for iso in sorted(x for x in p.rglob("*") if x.suffix.lower() == ".iso"):
            sysid = disc_system(iso)
            out.append(Candidate(EMULATOR, str(iso), clean_title(iso.stem), system=sysid) if sysid
                       else Candidate("extract", str(iso), folder_title(p),
                                      detail=_("{0}: hay que extraerlo para identificar el juego.").format(iso.name)))
        return out
    if ext in WINDOWS_EXTS:
        # Un .exe dentro de un juego que ScummVM conoce (p. ej. una aventura de LucasArts o Sierra)
        for folder in (p.parent, p.parent.parent):
            if folder != Path.home() and len(folder.parts) > 2:
                out += _scummvm_candidates(folder)
                if out:
                    break
        out.append(Candidate("wine", str(p), p.stem))
        return out
    if ext in VM_EXTS:
        return [_vm_candidate(p, p.parent)]
    if ext in ROM_EXTS:
        sysid = ROM_EXTS[ext]
        if ext in (".rvz", ".wia", ".gcz") or sysid == "gc":
            sysid = _rvz_system(p) or sysid
        elif ext == ".pbp":
            sysid = _pbp_system(p) or sysid
        return [Candidate(EMULATOR, str(p), clean_title(p.stem), system=sysid)]
    if ext in DISC_EXTS:
        sysid = disc_system(p)
        if sysid:
            return [Candidate(EMULATOR, str(p), clean_title(p.stem), system=sysid)]
        if ext == ".iso":
            return [Candidate("extract", str(p), clean_title(p.stem),
                              detail=_("No es de ninguna consola conocida: será un CD o DVD de PC."))]
    return out


def _rvz_system(p: Path) -> str:
    """RVZ/WIA guardan una copia de la cabecera del disco: GameCube o Wii."""
    try:
        with open(p, "rb") as f:
            data = f.read(0x100)
    except OSError:
        return ""
    if data[:4] in (b"RVZ\x01", b"WIA\x01"):
        # cabecera 2 (desde 0x48): plataforma (4 bytes, 1 = GameCube, 2 = Wii)
        try:
            plat = struct.unpack_from(">I", data, 0x48)[0]
        except struct.error:
            return ""
        return {1: "gc", 2: "wii"}.get(plat, "")
    return _nintendo(data)


def _vm_candidate(disk: Path, folder: Path) -> Candidate:
    """Disco de un Windows 9x ya instalado (packs con QEMU); el CD es la .iso que lo acompañe."""
    isos = sorted(folder.rglob("*.iso"))
    return Candidate(VM, str(disk), folder_title(folder), system="win9x",
                     extra={"cdrom": str(isos[0]) if isos else ""},
                     detail=_("Disco {0}").format(disk.name) + (f" · CD {isos[0].name}" if isos else ""))


# ---------------------------------------------------------------- extracción de discos de PC
def extract_disc(iso: Path, dest: Path) -> None:
    """Copia el contenido de una ISO a una carpeta (bsdtar, sin montar ni sudo)."""
    tar = shutil.which("bsdtar")
    if not tar:
        raise FileNotFoundError(_("Falta bsdtar (paquete libarchive)."))
    dest.mkdir(parents=True, exist_ok=False)
    r = subprocess.run([tar, "-xf", str(iso), "-C", str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise OSError(r.stderr.strip() or _("No se pudo extraer {0}").format(iso.name))
    # Los CD son de solo lectura: se da permiso de escritura (partidas, configuración)
    for x in [dest, *dest.rglob("*")]:
        try:
            x.chmod(x.stat().st_mode | (0o700 if x.is_dir() else 0o600))
        except OSError:
            pass


def disc_size(iso: Path) -> int:
    try:
        return iso.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------- lanzamiento
def command(game, fullscreen: bool | None) -> tuple[list[str], str]:
    """(argv, carpeta de trabajo) del juego. FileNotFoundError si falta el programa o el juego."""
    path = Path(game.exe)
    if not path.exists():
        raise FileNotFoundError(_("No existe: {0}").format(game.exe))
    if game.kind == SCUMMVM:
        base = scummvm_binary()
        if not base:
            raise FileNotFoundError(_("ScummVM no está instalado: sudo pacman -S scummvm"))
        if base[0] == "flatpak":
            base = [*base[:2], f"--filesystem={path}", *base[2:]]
        argv = [*base, f"--path={path}"]
        if fullscreen is not None:
            argv.append("--fullscreen" if fullscreen else "--no-fullscreen")
        return [*argv, game.target], str(path)
    if game.kind == EMULATOR:
        if game.system not in SYSTEMS:
            raise FileNotFoundError(_("Sistema desconocido: {0}").format(game.system))
        key, base = emulator_for(game.system)
        emu = EMULATORS[key]
        if not base:
            raise FileNotFoundError(_("{0} no está instalado: {1}").format(emu.name, install_hint(key)))
        args = [a.replace("{rom}", str(path)).replace("{ares}", SYSTEMS[game.system].ares) for a in emu.args]
        if fullscreen:
            # las opciones van antes del «--» que separa la ROM
            cut = args.index("--") if "--" in args else (args.index("-e") if "-e" in args else len(args) - 1)
            args[cut:cut] = list(emu.fullscreen)
        if base[0] == "flatpak":
            # el Flatpak solo ve tu carpeta personal si se lo permites: se le da la de la ROM
            base = [*base[:2], f"--filesystem={path.parent}", *base[2:]]
        return [*base, *args], str(path.parent)
    raise FileNotFoundError(_("Este tipo de juego aún no se puede lanzar."))
