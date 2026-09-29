"""Detección de instaladores y de los ejecutables que dejan instalados."""
from __future__ import annotations

import os
import re
from pathlib import Path

INSTALLER_RX = re.compile(r"(setup|install|installer|instalar|instalador)", re.I)
# .exe que un instalador deja pero que no son el juego
SKIP_RX = re.compile(r"(unins|uninstall|desinstal|setup|install|update|updater|crash|report|helper|"
                     r"redist|vcredist|vc_redist|dxsetup|directx|dotnet|launcherpatcher|cef|"
                     r"easyanticheat_setup|ue4prereq|prereq)", re.I)
# Carpetas del prefijo que no interesan al buscar juegos nuevos
SKIP_DIRS = {"windows", "temp", "package cache", "installshield installation information", "cache",
             # programas de serie que Wine/Proton crea al inicializar un prefijo nuevo
             "internet explorer", "windows nt", "windows media player", "windows photo viewer",
             "common files", "microsoft.net"}
MIN_EXE_SIZE = 64 * 1024   # los .exe de relleno de Wine ocupan unos pocos KB


def is_installer(exe: str) -> bool:
    p = Path(exe)
    return p.suffix.lower() == ".msi" or bool(INSTALLER_RX.search(p.stem))


def windows_path(path: str) -> str:
    """/home/x/juego.msi -> Z:\\home\\x\\juego.msi (en Proton, Z: apunta a /)."""
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def snapshot(prefix: Path) -> set[str]:
    """Rutas relativas de todos los .exe de drive_c (salvo carpetas del sistema)."""
    root = prefix / "drive_c"
    found: set[str] = set()
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for f in filenames:
            if f.lower().endswith(".exe"):
                found.add(os.path.relpath(os.path.join(dirpath, f), root))
    return found


def new_executables(prefix: Path, before: set[str]) -> list[Path]:
    """.exe nuevos tras un instalador, los más probables (el juego) primero."""
    root = prefix / "drive_c"
    out = []
    for rel in snapshot(prefix) - before:
        p = root / rel
        if SKIP_RX.search(p.stem):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size >= MIN_EXE_SIZE:
            out.append((size, p))
    return [p for _s, p in sorted(out, key=lambda t: -t[0])][:12]


def pretty_name(exe: Path) -> str:
    """Nombre legible: la carpeta del programa suele ser mejor que el .exe."""
    parent = exe.parent.name
    if parent.lower() in {"bin", "bin64", "x64", "x86_64", "win64", "binaries", "game"}:
        parent = exe.parent.parent.name
    return parent if parent and parent.lower() not in {"drive_c", "program files", "program files (x86)"} \
        else exe.stem


# ---------------------------------------------------------------- mover juegos a la carpeta de Umbral
# Carpetas de binarios: el juego de verdad está un nivel más arriba
BIN_DIRS = {"bin", "bin32", "bin64", "x64", "x86", "x86_64", "win32", "win64", "binaries", "binaries64"}


def _unsafe_dirs() -> set[Path]:
    """Carpetas que nunca se mueven enteras (solo el .exe suelto)."""
    home = Path.home()
    dirs = {Path("/"), home, Path("/tmp"), home / "Descargas", home / "Downloads", home / "Escritorio",
            home / "Desktop", home / "Documentos", home / "Documents", home / "Games", Path("/mnt"),
            Path("/media"), Path("/run/media")}
    try:   # carpetas XDG reales del usuario (p. ej. Descargas con otro nombre)
        from gi.repository import GLib
        for d in (GLib.UserDirectory.DIRECTORY_DOWNLOAD, GLib.UserDirectory.DIRECTORY_DESKTOP,
                  GLib.UserDirectory.DIRECTORY_DOCUMENTS):
            p = GLib.get_user_special_dir(d)
            if p:
                dirs.add(Path(p))
    except (ImportError, ValueError):
        pass
    return {d.resolve() for d in dirs if d}


def game_source(exe: str, unsafe: set[Path] | None = None) -> tuple[Path, bool]:
    """(qué mover, es_carpeta). Sube desde bin/, Binaries/Win64/…; si la carpeta resultante
    es una carpeta «de sistema» (Descargas, home…), se mueve solo el archivo."""
    unsafe = _unsafe_dirs() if unsafe is None else unsafe
    p = Path(exe).resolve()
    folder = p.parent
    while folder.name.lower() in BIN_DIRS and folder.parent != folder:
        folder = folder.parent
    if folder in unsafe or folder.parent == folder:
        return p, False
    return folder, True


def can_move(exe: str, games_root: Path, prefix_paths: list[str]) -> bool:
    """Solo .exe/.bat sueltos que no estén ya en la carpeta de juegos ni dentro de un prefijo."""
    p = Path(exe).resolve()
    if is_installer(exe) or not p.exists():
        return False
    if p.is_relative_to(games_root.resolve()):
        return False
    return not any(p.is_relative_to(Path(pp).resolve()) for pp in prefix_paths if pp) and "drive_c" not in p.parts


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def move_game(exe: str, games_root: Path) -> str:
    """Mueve la carpeta (o el archivo) del juego a games_root y devuelve la nueva ruta del .exe.
    Mismo disco: es un renombrado instantáneo; si no, shutil copia y luego borra el original."""
    import shutil
    src, is_dir = game_source(exe)
    games_root.mkdir(parents=True, exist_ok=True)
    name = src.name if is_dir else src.stem
    dest, n = games_root / name, 2
    while dest.exists():
        dest, n = games_root / f"{name} ({n})", n + 1
    exe_path = Path(exe).resolve()
    if is_dir:
        rel = exe_path.relative_to(src)
        shutil.move(str(src), str(dest))
        return str(dest / rel)
    dest.mkdir()
    shutil.move(str(src), str(dest / src.name))
    return str(dest / src.name)
