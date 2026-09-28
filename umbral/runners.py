"""Runners: detección de Proton instalados y descarga de GE-Proton.

GE-Proton se descarga solo del repositorio oficial
(github.com/GloriousEggroll/proton-ge-custom) y se verifica con el
.sha512sum que publica cada release.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import paths
from .i18n import _

log = logging.getLogger(__name__)

GE_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
# Fuentes oficiales (ambas publican .tar.gz + .sha512sum por release)
SOURCES = {
    "GE-Proton": GE_API,
    "UMU-Proton": "https://api.github.com/repos/Open-Wine-Components/umu-proton/releases",
}
SYSTEM_WINE = 'Wine del sistema'
USER_AGENT = "umbral-launcher"


class RunnerError(Exception):
    pass


@dataclass
class Runner:
    name: str
    path: Path            # directorio con el script `proton`, o binario wine
    kind: str             # "proton" | "wine"
    source: str = ""

    @property
    def is_ge(self) -> bool:
        return self.name.startswith("GE-Proton")

    @property
    def wineserver(self) -> Path | None:
        if self.kind == "wine":
            w = shutil.which("wineserver")
            return Path(w) if w else None
        for cand in ("files/bin/wineserver", "dist/bin/wineserver"):
            p = self.path / cand
            if p.exists():
                return p
        return None


def _version_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name))


def discover() -> list[Runner]:
    found: dict[str, Runner] = {}
    for base in paths.PROTON_SEARCH_DIRS:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if (d / "proton").is_file() and d.name not in found:
                found[d.name] = Runner(d.name, d, "proton", str(base))
    runners = sorted(found.values(), key=lambda r: (not r.is_ge, [-x for x in _version_key(r.name)]))
    if shutil.which("wine"):
        runners.append(Runner(SYSTEM_WINE, Path(shutil.which("wine")), "wine", "sistema"))
    return runners


def resolve(name: str | None, runners: list[Runner] | None = None) -> Runner | None:
    """'GE-Proton' = el GE más reciente instalado."""
    runners = runners if runners is not None else discover()
    if not name:
        return None
    if name == "GE-Proton":
        ge = [r for r in runners if r.is_ge]
        return max(ge, key=lambda r: _version_key(r.name)) if ge else None
    return next((r for r in runners if r.name == name), None)


def runner_for_prefix_version(version: str, runners: list[Runner]) -> Runner | None:
    """Relaciona el archivo `version` de un prefijo Proton con un runner instalado.

    Ej.: 'GE-Proton11-7' -> GE-Proton11-7-x86_64; 'CachyOS-11.0-100' -> un Proton-CachyOS.
    Evita abrir un prefijo con otro Proton (lo actualizaría o degradaría).
    """
    v = version.strip()
    if not v:
        return None
    for r in runners:
        if r.kind == "proton" and (r.name == v or r.name.startswith(v)):
            return r
    for r in runners:
        rv = (r.path / "version").read_text().split()[-1] if (r.path / "version").exists() else ""
        if rv and (rv == v or rv.lower().startswith(v.lower().split("-")[0])):
            return r
    return None


# ---------------------------------------------------------------- descargas

def _get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def ge_releases(limit: int = 10) -> list[dict]:
    return releases("GE-Proton", limit)


def releases(source: str, limit: int = 10) -> list[dict]:
    """[{tag, tarball_url, sha_url, size}] de las últimas releases x86_64 de una fuente."""
    try:
        data = _get_json(f"{SOURCES[source]}?per_page={limit}")
    except OSError as e:
        raise RunnerError(_('No se pudo consultar GitHub (¿sin conexión?): {0}').format(e)) from e
    out = []
    for rel in data:  # type: ignore[union-attr]
        assets = {a["name"]: a for a in rel.get("assets", [])}
        tag = rel["tag_name"]
        tar = assets.get(f"{tag}-x86_64.tar.gz") or assets.get(f"{tag}.tar.gz")
        sha = assets.get(f"{tag}-x86_64.sha512sum") or assets.get(f"{tag}.sha512sum")
        if tar and not rel.get("prerelease"):
            out.append({"tag": tag, "tarball_url": tar["browser_download_url"],
                        "sha_url": sha["browser_download_url"] if sha else "",
                        "size": tar["size"]})
    return out


def download(url: str, dest: Path, progress: Callable[[float], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                if cancelled():
                    raise RunnerError(_('Descarga cancelada'))
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        if total and tmp.stat().st_size != total:
            raise RunnerError(_('Descarga incompleta ({0}/{1} bytes)').format(tmp.stat().st_size, total))
        tmp.replace(dest)
        return dest
    except OSError as e:
        raise RunnerError(_('Error de descarga: {0}').format(e)) from e
    finally:
        tmp.unlink(missing_ok=True)


def sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def install_ge(release: dict, progress: Callable[[float], None] | None = None,
               cancelled: Callable[[], bool] = lambda: False) -> Runner:
    return install_release(release, progress, cancelled)


def install_release(release: dict, progress: Callable[[float], None] | None = None,
                    cancelled: Callable[[], bool] = lambda: False) -> Runner:
    paths.ensure_dirs()
    tag = release["tag"]
    target = paths.RUNNER_INSTALL_DIR / tag
    if (target / "proton").exists():
        return Runner(tag, target, "proton", str(paths.RUNNER_INSTALL_DIR))
    tarball = paths.CACHE_DIR / f"{tag}.tar.gz"
    download(release["tarball_url"], tarball, progress, cancelled)
    try:
        if release.get("sha_url"):
            shafile = paths.CACHE_DIR / f"{tag}.sha512sum"
            download(release["sha_url"], shafile)
            expected = shafile.read_text().split()[0].lower()
            shafile.unlink(missing_ok=True)
            if sha512(tarball) != expected:
                raise RunnerError(_('El SHA-512 no coincide: descarga corrupta, se descarta.'))
            log.info("SHA-512 de %s verificado", tag)
        else:
            log.warning("La release %s no publica sha512sum; no se puede verificar", tag)
        staging = paths.RUNNER_INSTALL_DIR / f".{tag}.extract"
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(tarball) as tf:
            tf.extractall(staging, filter="tar")
        inner = next((d for d in staging.iterdir() if (d / "proton").exists()), None)
        if inner is None:
            raise RunnerError(_('El archivo no contiene un Proton válido'))
        inner.rename(target)
        shutil.rmtree(staging, ignore_errors=True)
    finally:
        tarball.unlink(missing_ok=True)
    return Runner(tag, target, "proton", str(paths.RUNNER_INSTALL_DIR))
