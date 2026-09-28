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
