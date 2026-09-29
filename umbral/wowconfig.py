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


# ---------------------------------------------------------------- informes de error de WoW
def crash_report(exe: str, since: float) -> tuple[Path, str] | None:
    """Informe que WoW deja en <cliente>/Errors al cerrarse por un fallo (posterior a `since`).
    Devuelve (archivo, resumen) con la línea «Error:» y el módulo en el que falló."""
    folder = Path(exe).parent / "Errors"
    try:
        reports = sorted((p for p in folder.glob("*.txt") if p.stat().st_mtime >= since - 2),
                         key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    if not reports:
        return None
    rep = reports[-1]
    error = where = ""
    try:
        for line in rep.read_text(errors="replace").splitlines()[:60]:
            if not error and line.startswith("Error:"):
                error = line[6:].strip()
            elif not where and line.startswith("File:"):
                where = line[5:].strip().replace("\\", "/").rsplit("/", 1)[-1]
    except OSError:
        return None
    summary = error or rep.name
    if where:
        summary += f" · {where}"
    return rep, summary


def crash_advice(summary: str) -> str:
    """Consejo para fallos conocidos de WoW en Wine/Proton."""
    s = summary.lower()
    if "voice" in s or "speak" in s or "tts" in s:
        return _("Fallo del módulo de voz / texto a voz de WoW (Windows lo usa y Wine no lo implementa). "
                 "Prueba a desactivar el chat de voz y el texto a voz en las opciones del juego.")
    return ""
