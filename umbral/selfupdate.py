"""Actualizar Umbral desde su repositorio de GitHub, sin pasar por la terminal.

Equivale a `git pull && makepkg -sif`: se baja el código a una copia en la caché, se construye el
paquete como usuario normal y solo la instalación (`pacman -U`) pide permisos, con el diálogo de
polkit (pkexec). Solo se ofrece si Umbral está instalada como paquete (/usr/lib/umbral)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import VERSION, paths
from .i18n import _

log = logging.getLogger(__name__)

REPO = "https://github.com/madkyp/umbral-project.git"
PKGBUILD_URL = "https://raw.githubusercontent.com/madkyp/umbral-project/main/PKGBUILD"
SRC_DIR = paths.CACHE_DIR / "selfupdate"
INSTALL_ROOT = Path("/usr/lib/umbral")


class UpdateError(Exception):
    pass


def installed() -> bool:
    """¿Umbral corre desde el paquete instalado (y no desde una copia de desarrollo)?"""
    try:
        return Path(__file__).resolve().is_relative_to(INSTALL_ROOT)
    except OSError:
        return False


def parse_version(pkgbuild: str) -> tuple[str, str] | None:
    """(pkgver, pkgrel) de un PKGBUILD."""
    ver = re.search(r"^pkgver=([0-9][0-9A-Za-z.]*)\s*$", pkgbuild, re.M)
    rel = re.search(r"^pkgrel=(\d+)\s*$", pkgbuild, re.M)
    return (ver.group(1), rel.group(1) if rel else "1") if ver else None


def _key(ver: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", ver))


def is_newer(remote: str, local: str = VERSION) -> bool:
    return _key(remote) > _key(local)


def fetch_remote_version(timeout: float = 8) -> str | None:
    """Versión publicada en GitHub (rama main), o None si no se pudo consultar."""
    try:
        req = urllib.request.Request(PKGBUILD_URL, headers={"User-Agent": f"umbral/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = parse_version(r.read(20000).decode("utf-8", "replace"))
    except (OSError, ValueError) as e:
        log.warning("No se pudo comprobar la versión de Umbral: %s", e)
        return None
    return got[0] if got else None


def _run(cmd: list[str], cwd: Path | None, log_line: Callable[[str], None], what: str) -> None:
    log_line("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        raise UpdateError(f"{what}: {e}") from e
    assert p.stdout is not None
    for line in p.stdout:
        log_line(line.rstrip())
    if p.wait() != 0:
        raise UpdateError(_("{0} falló (código {1}). Mira el registro de Umbral.").format(what, p.returncode))


def build(log_line: Callable[[str], None]) -> Path:
    """Descarga la última versión y construye el paquete. Devuelve el .pkg.tar.zst."""
    for tool in ("git", "makepkg"):
        if not shutil.which(tool):
            raise UpdateError(_("Falta «{0}»: instala git y base-devel para actualizar desde Umbral.").format(tool))
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SRC_DIR.exists():
        shutil.rmtree(SRC_DIR)            # copia limpia cada vez: solo es una caché
    _run(["git", "clone", "--depth", "1", REPO, str(SRC_DIR)], None, log_line, _("La descarga"))
    _run(["makepkg", "-f", "--noconfirm", "--nocolor"], SRC_DIR, log_line, _("La construcción del paquete"))
    pkgs = sorted(SRC_DIR.glob("umbral-*.pkg.tar.zst"), key=lambda p: p.stat().st_mtime)
    if not pkgs:
        raise UpdateError(_("No se encontró el paquete construido."))
    return pkgs[-1]


def install(pkg: Path, log_line: Callable[[str], None]) -> None:
    """Instala el paquete con pacman; pkexec pide la contraseña con el diálogo del sistema."""
    if not shutil.which("pkexec"):
        raise UpdateError(_("Falta «pkexec» (polkit) para instalar el paquete."))
    _run(["pkexec", "pacman", "-U", "--noconfirm", str(pkg)], None, log_line, _("La instalación"))
