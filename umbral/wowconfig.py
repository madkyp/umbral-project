"""Lectura y escritura de CVars en WTF/Config.wtf de World of Warcraft.

Formato: una línea por variable, `SET nombre "valor"`, con finales CRLF.
Solo se toca con el juego cerrado (WoW reescribe el archivo al salir) y se
guarda una copia la primera vez.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .i18n import _

# Valores admitidos por gxApi (el cliente de Forever incluye ambos: D3D11 y D3D12).
GX_APIS = {"": _("Automática (DirectX 12)"), "D3D12": "DirectX 12 (VKD3D-Proton)",
           "D3D11": "DirectX 11 (DXVK)"}


def config_path(exe: str) -> Path:
    return Path(exe).parent / "WTF" / "Config.wtf"


def _line_re(name: str) -> re.Pattern:
    return re.compile(rf'^SET\s+{re.escape(name)}\s+"([^"]*)"\s*$', re.I)


def get_cvar(path: Path, name: str) -> str | None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    rx = _line_re(name)
    for line in text.splitlines():
        m = rx.match(line)
        if m:
            return m[1]
    return None


def set_cvar(path: Path, name: str, value: str | None) -> None:
    """value=None elimina la variable (el juego vuelve a su valor por defecto)."""
    raw = path.read_bytes() if path.exists() else b""
    text = raw.decode("utf-8", errors="replace")
    eol = "\r\n" if "\r\n" in text or not text else "\n"
    rx = _line_re(name)
    lines = [ln for ln in text.splitlines() if not rx.match(ln)]
    if value is not None:
        lines.append(f'SET {name} "{value}"')
    if path.exists():
        backup = path.with_name(path.name + ".umbral-bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".umbral-tmp")
    tmp.write_bytes((eol.join(lines) + eol).encode("utf-8"))
    tmp.replace(path)
