"""Comprobación de actualizaciones de juegos de Blizzard sin abrir Battle.net.

Compara la build instalada (columna «Build Key» de .build.info, la que escribe
Battle.net) con la que publica el servidor oficial de versiones de Blizzard:
https://<región>.version.battle.net/v2/products/<producto>/versions
(columna «BuildConfig»). Verificado el 2026-09-28 con wow_classic_beta.
"""
from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from .i18n import _

log = logging.getLogger(__name__)

VERSIONS_URL = "https://{region}.version.battle.net/v2/products/{product}/versions"
REGIONS = {"us", "eu", "kr", "tw", "cn"}


@dataclass
class UpdateStatus:
    state: str               # "ok" | "update" | "unknown"
    local: str = ""          # versión instalada (legible)
    remote: str = ""         # versión publicada
    reason: str = ""


def parse_table(text: str) -> list[dict[str, str]]:
    """Formato BPSV de Blizzard: cabecera «Nombre!TIPO:n|…», comentarios «##», filas «a|b|…»."""
    rows, header = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("##"):
            continue
        cols = line.split("|")
        if header is None:
            header = [c.split("!", 1)[0] for c in cols]
            continue
        rows.append(dict(zip(header, cols, strict=False)))
    return rows


def find_root(exe: Path) -> Path | None:
    for d in (exe.parent, *exe.parents):
        if (d / ".build.info").exists():
            return d
    return None


def local_build(exe: str, product: str) -> dict[str, str] | None:
    root = find_root(Path(exe))
    if root is None:
        return None
    try:
        rows = parse_table((root / ".build.info").read_text(errors="replace"))
    except OSError:
        return None
    for r in rows:
        if r.get("Product") == product:
            return r
    return None


def fetch_remote(product: str, region: str, timeout: float = 6) -> list[dict[str, str]]:
    url = VERSIONS_URL.format(region=region, product=product)
    req = urllib.request.Request(url, headers={"User-Agent": "umbral-launcher"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_table(r.read().decode(errors="replace"))


def compare(local: dict[str, str] | None, remote_rows: list[dict[str, str]], region: str) -> UpdateStatus:
    if not local:
        return UpdateStatus("unknown", reason=_('No se encontró la instalación en .build.info'))
    remote = next((r for r in remote_rows if r.get("Region") == region), None)
    if remote is None:
        return UpdateStatus("unknown", reason=_('El servidor no publica la región «{0}»').format(region))
    lv = local.get("Version") or ""
    rv = remote.get("VersionsName", "")
    if local.get("Build Key", "").lower() == remote.get("BuildConfig", "").lower():
        return UpdateStatus("ok", lv or rv, rv)
    return UpdateStatus("update", lv, rv)


def check(exe: str, product: str) -> UpdateStatus:
    local = local_build(exe, product)
    region = (local or {}).get("Branch", "us").lower()
    region = region if region in REGIONS else "us"
    try:
        rows = fetch_remote(product, region)
    except OSError as e:
        log.warning("No se pudo comprobar la versión de %s: %s", product, e)
        return UpdateStatus("unknown", reason=_('Sin conexión con el servidor de versiones ({0})').format(e))
    st = compare(local, rows, region)
    log.info("Versión %s: %s (local %s, publicada %s)", product, st.state, st.local, st.remote)
    return st
