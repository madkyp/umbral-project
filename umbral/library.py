"""Biblioteca: entrada fija de Battle.net + juegos detectados + manuales."""
from __future__ import annotations

from pathlib import Path

from . import battlenet
from .config import BATTLENET_ID, Config, Game, Prefix
from .runners import Runner
from .i18n import _


def ensure_battlenet_prefix(cfg: Config, path: Path, runner: str, imported: bool = False) -> Prefix:
    p = cfg.battlenet_prefix()
    if p is None:
        p = Prefix(BATTLENET_ID, "Battle.net", str(path), runner, imported=imported)
        cfg.prefixes.insert(0, p)
    else:
        p.path, p.runner, p.imported = str(path), runner, imported
    if cfg.game(BATTLENET_ID) is None:
        cfg.games.insert(0, Game(BATTLENET_ID, "Battle.net", "battlenet", BATTLENET_ID, ""))
    return p


def sync_prefix_name(cfg: Config, g: Game) -> bool:
    """El prefijo propio de un juego se llama como él (al añadirlo pudo tomar el nombre del
    .exe, p. ej. «Game»). Solo si ningún otro juego lo usa y no es el de Battle.net."""
    p = cfg.prefix(g.prefix_id)
    if p is None or p.id == BATTLENET_ID or p.name == g.name:
        return False
    if any(x.prefix_id == p.id for x in cfg.games if x is not g):
        return False
    p.name = g.name
    return True


def sync_detected(cfg: Config) -> list[str]:
    """Añade/actualiza/quita entradas auto de juegos Blizzard. Devuelve cambios."""
    changes: list[str] = []
    for prefix in cfg.prefixes:
        # Prefijo borrado o sin Battle.net: sus juegos detectados desaparecen de la biblioteca
        installed = battlenet.is_installed(Path(prefix.path))
        found = {d.uid: d for d in battlenet.detect_games(Path(prefix.path))} if installed else {}
        for g in [g for g in cfg.games if g.auto and g.prefix_id == prefix.id]:
            if g.product not in found:
                cfg.games.remove(g)
                changes.append(f"- {g.name}")
        for uid, d in found.items():
            gid = f"{prefix.id}:{uid}"
            g = cfg.game(gid)
            if g is None:
                cfg.games.append(Game(gid, d.name, "blizzard", prefix.id, str(d.exe),
                                      product=uid, auto=True))
                changes.append(f"+ {d.name}")
            elif g.exe != str(d.exe):
                g.exe = str(d.exe)
    return changes


def launch_target(cfg: Config, game: Game) -> tuple[str, list[str]]:
    """(.exe, argumentos) reales para una entrada de la biblioteca."""
    prefix = cfg.prefix(game.prefix_id)
    if prefix is None:
        raise LookupError(_('El prefijo de este juego ya no existe.'))
    # Los juegos de Blizzard se lanzan directamente (antes se comprueba la versión);
    # ni --exec ni battlenet:// arrancan WoW Forever desde Battle.net (probado 2026-09-28).
    if game.kind == "battlenet":
        client = battlenet.client_exe(Path(prefix.path))
        if client is None:
            raise FileNotFoundError(_('Battle.net no está instalado en el prefijo. Usa el asistente.'))
        return str(client), []
    if not Path(game.exe).exists():
        raise FileNotFoundError(_('No existe el ejecutable: {0}').format(game.exe))
    return game.exe, []


def runner_names(runners: list[Runner]) -> list[str]:
    names = [r.name for r in runners]
    if any(r.is_ge for r in runners):
        names.insert(0, "GE-Proton")
    return names
