"""Gestión de prefijos: creación, identificación del runner y reparación."""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

from . import battlenet
from .runners import Runner, runner_for_prefix_version
from .i18n import _

log = logging.getLogger(__name__)

# Otros lanzadores conocidos cuyos prefijos se pueden importar (solo lectura).
IMPORT_CANDIDATES = [
    Path.home() / "Faugus" / "battlenet",
    Path.home() / "Games" / "battlenet",
    Path.home() / "Games" / "battle-net",
    Path.home() / "Games" / "umu" / "battlenet",
]


class PrefixError(Exception):
    pass


def exists(path: Path) -> bool:
    return (path / "drive_c").is_dir() and (path / "system.reg").exists()


def proton_version(path: Path) -> str:
    try:
        return (path / "version").read_text().strip()
    except OSError:
        return ""


def matching_runner(path: Path, runners: list[Runner]) -> Runner | None:
    return runner_for_prefix_version(proton_version(path), runners)


def find_importable() -> list[Path]:
    return [p for p in IMPORT_CANDIDATES if exists(p) and battlenet.is_installed(p)]


def health(path: Path) -> list[str]:
    """Problemas visibles del prefijo (vacío = sano)."""
    problems = []
    if not path.exists():
        return [_('El prefijo no existe.')]
    if not exists(path):
        problems.append(_('Prefijo incompleto: falta drive_c o system.reg.'))
    if (path / "drive_c").exists() and not (path / "drive_c/windows/system32").exists():
        problems.append(_('Falta drive_c/windows/system32: prefijo dañado.'))
    return problems


def clear_client_cache(path: Path) -> list[str]:
    """Borra cachés de Battle.net (no toca cuentas, ajustes ni juegos)."""
    removed = []
    for rel in battlenet.CACHE_DIRS:
        p = path / rel
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(rel)
    return removed


def reset_agent(path: Path) -> Path | None:
    """Arreglo del error 'Agent went to sleep' (BLZBNTBNA00000005).

    Se renombra Agent (no se borra) y se restaura product.db para que
    Battle.net siga sabiendo qué juegos hay instalados.
    """
    agent = path / battlenet.AGENT_DIR
    if not agent.exists():
        return None
    backup = agent.with_name(f"Agent.umbral-bak-{time.strftime('%Y%m%d-%H%M%S')}")
    agent.rename(backup)
    agent.mkdir()
    if (backup / "product.db").exists():
        shutil.copy2(backup / "product.db", agent / "product.db")
    return backup


def stash_games(path: Path) -> Path:
    """Aparta los juegos (todo Program Files salvo Battle.net) antes de recrear."""
    stash = path.parent / f".{path.name}.umbral-games"
    stash.mkdir(exist_ok=True)
    for pf in ("Program Files (x86)", "Program Files"):
        src = path / "drive_c" / pf
        if not src.is_dir():
            continue
        for d in src.iterdir():
            if (d / ".build.info").exists():
                (stash / pf).mkdir(exist_ok=True)
                d.rename(stash / pf / d.name)
    db = path / battlenet.PRODUCT_DB
    if db.exists():
        shutil.copy2(db, stash / "product.db")
    return stash


def restore_games(path: Path, stash: Path) -> None:
    for pf in ("Program Files (x86)", "Program Files"):
        s = stash / pf
        if not s.is_dir():
            continue
        dst = path / "drive_c" / pf
        dst.mkdir(parents=True, exist_ok=True)
        for d in s.iterdir():
            if not (dst / d.name).exists():
                d.rename(dst / d.name)
    if (stash / "product.db").exists():
        (path / battlenet.AGENT_DIR).mkdir(parents=True, exist_ok=True)
        target = path / battlenet.PRODUCT_DB
        if not target.exists():
            shutil.copy2(stash / "product.db", target)
    shutil.rmtree(stash, ignore_errors=True)


def delete_for_recreate(path: Path, imported: bool) -> Path:
    """Aparta los juegos y borra el prefijo. Nunca actúa sobre prefijos importados."""
    if imported:
        raise PrefixError(_('Umbral no recrea prefijos importados de otros lanzadores.'))
    if not exists(path):
        raise PrefixError(_('{0} no parece un prefijo de Wine; no se borra nada.').format(path))
    stash = stash_games(path)
    shutil.rmtree(path)
    return stash


# ---------------------------------------------------------------- copias de seguridad
BACKUP_EXCLUDES = ["./drive_c/Program Files", "./drive_c/Program Files (x86)", "./shadercache"]
KEEP_BACKUPS = 3


def backup_dir(path: Path) -> Path:
    from . import paths
    return paths.DATA_HOME / "umbral" / "backups" / path.name


def backup(path: Path, reason: str = "") -> Path:
    """Copia el prefijo SIN juegos (Program Files) ni caché de shaders, en tar.zst."""
    import subprocess
    dest_dir = backup_dir(path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", proton_version(path) or "desconocido")
    dest = dest_dir / f"{stamp}_{tag}.tar.zst"
    cmd = ["tar", "--zstd", "-cf", str(dest), "-C", str(path)]
    cmd += [f"--exclude={e}" for e in BACKUP_EXCLUDES] + ["."]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):   # 1 = algún archivo cambió durante la copia
        dest.unlink(missing_ok=True)
        raise PrefixError(_('No se pudo copiar el prefijo: {0}').format(r.stderr.strip()[-300:]))
    for old in sorted(dest_dir.glob("*.tar.zst"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def latest_backup(path: Path) -> Path | None:
    found = sorted(backup_dir(path).glob("*.tar.zst"))
    return found[-1] if found else None


def restore_backup(path: Path, archive: Path) -> None:
    """Restaura una copia encima del prefijo (los juegos no están en la copia y no se tocan)."""
    import subprocess
    r = subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PrefixError(_('No se pudo restaurar la copia: {0}').format(r.stderr.strip()[-300:]))
