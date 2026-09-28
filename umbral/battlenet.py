"""Battle.net: instalador oficial, rutas del cliente y detección de juegos.

Verificado el 2026-09-28:
- URL oficial del instalador (redirige a downloader.battle.net/.../Battle.net-Setup.exe).
- product.db (protobuf) en ProgramData/Battle.net/Agent guarda, por producto:
  1=uid, 2=product_code, 3=ajustes{1=ruta de instalación, 13=subcarpeta}.
- WoW: Forever beta = uid wow_classic_beta, subcarpeta _classic_beta_, WowB.exe
  (comprobado en el prefijo de Faugus del usuario, versión 1.60.1).
Blizzard no publica hashes del instalador: se comprueba origen HTTPS,
tamaño y cabecera PE.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import paths
from .i18n import _

log = logging.getLogger(__name__)

INSTALLER_URL = "https://www.battle.net/download/getInstallerForGame?os=win&gameProgram=BATTLENET_APP"
TRUSTED_HOSTS = (".battle.net", ".blizzard.com")
BNET_DIR = "drive_c/Program Files (x86)/Battle.net"
AGENT_DIR = "drive_c/ProgramData/Battle.net/Agent"
PRODUCT_DB = f"{AGENT_DIR}/product.db"
CACHE_DIRS = [
    "drive_c/users/steamuser/AppData/Local/Battle.net/Cache",
    "drive_c/users/steamuser/AppData/Local/Battle.net/BrowserCaches",
    "drive_c/ProgramData/Battle.net/Setup",
]
IGNORED_UIDS = {"agent", "battle.net", "bna"}


class BattleNetError(Exception):
    pass


# Productos conocidos. confirmed=True solo para lo verificado en un prefijo real.
KNOWN = {
    "wow_classic_beta": ("World of Warcraft: Forever (beta)", "_classic_beta_", "WowB.exe", True),
    "wow": ("World of Warcraft", "_retail_", "Wow.exe", False),
    "wow_classic": ("WoW Classic", "_classic_", "WowClassic.exe", False),
    "wow_classic_era": ("WoW Classic Era", "_classic_era_", "WowClassic.exe", False),
}


@dataclass
class Product:
    uid: str
    code: str
    install_path: str     # ruta Windows, p. ej. C:/Program Files (x86)/World of Warcraft
    subfolder: str = ""
    version: str = ""


@dataclass
class DetectedGame:
    uid: str
    name: str
    exe: Path
    confirmed: bool
    is_wow: bool
    version: str = ""


# ---------------------------------------------------------------- rutas

def client_exe(prefix: Path) -> Path | None:
    """Ejecutable de Battle.net. Battle.net.exe es el que usa la instalación
    funcional verificada; el Launcher queda como alternativa."""
    for name in ("Battle.net.exe", "Battle.net Launcher.exe"):
        p = prefix / BNET_DIR / name
        if p.exists():
            return p
    return None


def is_installed(prefix: Path) -> bool:
    return client_exe(prefix) is not None


def win_to_linux(prefix: Path, win: str) -> Path:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", win)
    if not m:
        return prefix / "drive_c" / win
    drive = prefix / "dosdevices" / f"{m[1].lower()}:"
    base = drive if drive.exists() else prefix / f"drive_{m[1].lower()}"
    return base / m[2].replace("\\", "/")


# ---------------------------------------------------------------- product.db

def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError(_('varint truncado'))
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(_('tipo de campo protobuf no soportado: {0}').format(wt))
        out.append((num, wt, v))
    return out


def _str(v: object) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def parse_product_db(data: bytes) -> list[Product]:
    prods = []
    for num, wt, v in _fields(data):
        if num != 1 or wt != 2:
            continue
        uid = code = path = sub = version = ""
        for n2, w2, v2 in _fields(v):  # type: ignore[arg-type]
            if n2 == 1 and w2 == 2:
                uid = _str(v2)
            elif n2 == 2 and w2 == 2:
                code = _str(v2)
            elif n2 == 3 and w2 == 2:
                for n3, w3, v3 in _fields(v2):  # type: ignore[arg-type]
                    if n3 == 1 and w3 == 2:
                        path = _str(v3)
                    elif n3 == 13 and w3 == 2:
                        sub = _str(v3)
            elif n2 == 4 and w2 == 2:
                m = re.search(rb"\d+\.\d+\.\d+\.\d+", v2)  # type: ignore[arg-type]
                version = m.group().decode() if m else ""
        if uid:
            prods.append(Product(uid, code, path, sub, version))
    return prods


def read_products(prefix: Path) -> list[Product] | None:
    p = prefix / PRODUCT_DB
    if not p.exists():
        return None
    try:
        return parse_product_db(p.read_bytes())
    except (OSError, ValueError, IndexError) as e:
        log.warning("product.db ilegible (%s); se usa escaneo de carpetas", e)
        return None


# ---------------------------------------------------------------- juegos

_SKIP_EXE = re.compile(r"launcher|error|crash|uninstall|setup|helper|agent|update|repair|"
                       r"clientsdk|editor|mdns", re.I)


def _find_exe(folder: Path, hint: str = "") -> Path | None:
    if hint and (folder / hint).is_file():
        return folder / hint
    if not folder.is_dir():
        return None
    wow = sorted(folder.glob("Wow*.exe"))
    wow = [p for p in wow if not _SKIP_EXE.search(p.name)]
    if wow:
        return wow[0]
    cands = [p for p in [*folder.glob("*.exe"), *folder.glob("*/*.exe"), *folder.glob("*/*/*.exe")]
             if not _SKIP_EXE.search(p.name)]
    if not cands:
        return None
    # Preferir el .exe que se llama como la carpeta del juego (Warcraft III.exe)
    named = [p for p in cands if p.stem.lower() in (folder.name.lower(), folder.parent.name.lower())]
    return max(named or cands, key=lambda p: p.stat().st_size)


def _pretty(uid: str, install_path: str) -> str:
    if uid.startswith("wow_forever"):  # nombre del producto final: sin confirmar
        return "World of Warcraft: Forever"
    name = re.split(r"[\\/]", install_path.rstrip("/\\"))[-1] or uid.replace("_", " ").title()
    for suffix, label in (("_beta", "beta"), ("_ptr", "PTR")):
        if uid.endswith(suffix):
            name += f" ({label})"
    return name


def detect_games(prefix: Path) -> list[DetectedGame]:
    """Juegos instalados por Battle.net en el prefijo. Solo devuelve entradas
    cuyo ejecutable existe de verdad."""
    games: list[DetectedGame] = []
    seen: set[Path] = set()
    products = read_products(prefix)
    for pr in products or []:
        if pr.uid in IGNORED_UIDS or pr.code in IGNORED_UIDS:
            continue
        name, sub, exe_name, confirmed = KNOWN.get(pr.uid, (_pretty(pr.uid, pr.install_path), "", "", False))
        root = win_to_linux(prefix, pr.install_path)
        folder = root / (pr.subfolder or sub)
        exe = _find_exe(folder, exe_name) or _find_exe(root, exe_name)
        exe = exe.resolve() if exe else None
        if exe and exe not in seen:
            seen.add(exe)
            games.append(DetectedGame(pr.uid, name, exe, confirmed,
                                      pr.uid.startswith("wow"), pr.version))
    # Respaldo sin product.db: carpetas de WoW con subcarpetas _xxx_
    for info in (prefix / "drive_c").glob("Program Files*/*/.build.info"):
        root = info.parent
        for sub in sorted(root.glob("_*_")):
            exe = _find_exe(sub)
            exe = exe.resolve() if exe else None
            if not exe or exe in seen:
                continue
            uid = next((u for u, k in KNOWN.items() if k[1] == sub.name), "")
            name = KNOWN[uid][0] if uid else f"{root.name} ({sub.name.strip('_')})"
            seen.add(exe)
            games.append(DetectedGame(uid or sub.name.strip("_"), name, exe,
                                      bool(uid and KNOWN[uid][3]), "warcraft" in root.name.lower()))
    return games


# ---------------------------------------------------------------- instalador

def download_installer(progress: Callable[[float], None] | None = None) -> Path:
    paths.ensure_dirs()
    dest = paths.CACHE_DIR / "Battle.net-Setup.exe"
    req = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "Mozilla/5.0 umbral"})
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            final = urllib.parse.urlparse(r.geturl())
            if final.scheme != "https" or not any(final.hostname.endswith(h) for h in TRUSTED_HOSTS):
                raise BattleNetError(_('Redirección inesperada a {0}; descarga abortada.').format(r.geturl()))
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
        size = tmp.stat().st_size
        if total and size != total:
            raise BattleNetError(_('Descarga incompleta ({0}/{1} bytes). Reintenta.').format(size, total))
        with open(tmp, "rb") as f:
            if f.read(2) != b"MZ":
                raise BattleNetError(_('El archivo descargado no es un ejecutable de Windows.'))
        if size < 1_000_000:
            raise BattleNetError(_('El instalador es sospechosamente pequeño; descarga descartada.'))
        tmp.replace(dest)
        log.info("Instalador descargado de %s (%d bytes)", final.geturl(), size)
        return dest
    except OSError as e:
        raise BattleNetError(_('No se pudo descargar el instalador (¿sin conexión?): {0}').format(e)) from e
    finally:
        tmp.unlink(missing_ok=True)

