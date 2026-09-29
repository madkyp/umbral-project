"""Carátulas de SteamGridDB (https://www.steamgriddb.com/api/v2).

La clave de la API es del usuario y se guarda aparte, en ~/.config/umbral/steamgriddb.key
(permisos 600), nunca en config.json ni en el repositorio.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .i18n import _

log = logging.getLogger(__name__)

API = "https://www.steamgriddb.com/api/v2"
KEY_FILE = paths.CONFIG_DIR / "steamgriddb.key"
# Portadas apaisadas primero: la cabecera de la tarjeta es más ancha que alta.
COVER_DIMENSIONS = "920x430,460x215"


class SGDBError(Exception):
    pass


@dataclass
class SGDBGame:
    id: int
    name: str
    year: str = ""


@dataclass
class SGDBImage:
    url: str
    thumb: str
    width: int
    height: int
    style: str = ""


def get_key() -> str:
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        return ""


def set_key(key: str) -> None:
    key = key.strip()
    if not key:
        KEY_FILE.unlink(missing_ok=True)
        return
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)


def _get(path: str, key: str | None = None, params: dict | None = None) -> list:
    key = key or get_key()
    if not key:
        raise SGDBError(_("Falta la clave de SteamGridDB (Sistema → SteamGridDB)."))
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "umbral-launcher"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SGDBError(_("SteamGridDB rechazó la clave: revísala en Sistema → SteamGridDB.")) from e
        raise SGDBError(_("SteamGridDB respondió con un error ({0}).").format(e.code)) from e
    except (OSError, ValueError) as e:
        raise SGDBError(_("No se pudo conectar con SteamGridDB: {0}").format(e)) from e
    if not data.get("success"):
        raise SGDBError(_("SteamGridDB no devolvió resultados."))
    return data.get("data") or []


def search(term: str, key: str | None = None) -> list[SGDBGame]:
    items = _get("/search/autocomplete/" + urllib.parse.quote(term.strip()), key)
    out = []
    for g in items:
        year = ""
        if g.get("release_date"):
            import datetime as dt
            year = str(dt.datetime.fromtimestamp(g["release_date"], dt.UTC).year)
        out.append(SGDBGame(int(g["id"]), g.get("name", "?"), year))
    return out


def images(game_id: int, kind: str = "grids", key: str | None = None) -> list[SGDBImage]:
    """kind: grids (portadas), logos (transparentes, para el hueco del icono), heroes, icons."""
    params = {"types": "static", "nsfw": "false", "humor": "false"}
    items = _get(f"/{kind}/game/{game_id}", key, params)
    out = [SGDBImage(i["url"], i.get("thumb") or i["url"], int(i.get("width") or 0),
                     int(i.get("height") or 0), i.get("style", "")) for i in items
           if not str(i.get("mime", "")).endswith("icon")]      # los .ico no se pueden mostrar
    if kind == "grids":   # apaisadas primero
        wide = {tuple(map(int, d.split("x"))) for d in COVER_DIMENSIONS.split(",")}
        out.sort(key=lambda i: (i.width, i.height) not in wide)
    return out


def download(url: str, name: str) -> Path:
    """Descarga una imagen de la CDN de SteamGridDB a la caché y devuelve la ruta."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith("steamgriddb.com"):
        raise SGDBError(_("Dirección de imagen no válida."))
    dest_dir = paths.CACHE_DIR / "sgdb"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urllib.parse.urlparse(url).path).suffix or ".png"
    dest = dest_dir / f"{name}{ext}"
    req = urllib.request.Request(url, headers={"User-Agent": "umbral-launcher"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            dest.write_bytes(r.read())
    except OSError as e:
        raise SGDBError(_("No se pudo descargar la imagen: {0}").format(e)) from e
    return dest


def best_cover(name: str, key: str | None = None) -> Path | None:
    """Portada automática: primer resultado de la búsqueda y su primera portada apaisada."""
    found = search(name, key)
    if not found:
        return None
    imgs = images(found[0].id, "grids", key)
    if not imgs:
        return None
    log.info("SteamGridDB: «%s» → %s (%s)", name, found[0].name, imgs[0].url)
    return download(imgs[0].url, f"auto-{found[0].id}")
