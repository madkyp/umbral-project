"""Juegos en marcha: archivo de estado para otras apps (p. ej. Control Deck) y cierre ordenado.

Umbral escribe $XDG_RUNTIME_DIR/umbral/running.json (o ~/.local/state/umbral si no hay
XDG_RUNTIME_DIR). Formato (version 1):

    {"version": 1, "umbral_pid": 1234, "updated": 1790000000.0,
     "games": [{"id": "battlenet:wow_classic_beta", "name": "…", "kind": "blizzard",
                "launched_by": "umbral" | "battlenet",
                "pid": 4321, "pid_starttime": 987654,        # lanzador (umu-run) o el .exe
                "game_pids": [{"pid": 4400, "starttime": 987700}],
                "exe": "/…/WowB.exe", "exe_name": "WowB.exe",
                "proton": "UMU-Proton-10.0-4", "proton_path": "/…",
                "prefix": "/home/…/battlenet", "started": 1790000000.0}]}

Los PIDs del juego se obtienen recorriendo el árbol de procesos que lanzó Umbral (no por
nombre), y `starttime` (campo 22 de /proc/<pid>/stat) permite detectar PIDs reutilizados:
una entrada solo es válida si el PID existe y su starttime coincide.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import paths

VERSION = 1


def running_file() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(base) / "umbral" if base else paths.STATE_DIR) / "running.json"


# ---------------------------------------------------------------- procesos
def _stat(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # el nombre va entre paréntesis y puede tener espacios: se corta por el último «)»
    return raw.rsplit(")", 1)[1].split()


def starttime(pid: int) -> int | None:
    """Instante de arranque del proceso (ticks desde el arranque del sistema)."""
    f = _stat(pid)
    try:
        return int(f[19]) if f else None   # campo 22 de stat; aquí índice 19 tras «) estado»
    except (IndexError, ValueError):
        return None


def alive(pid: int, start: int | None) -> bool:
    """El PID existe, sigue en marcha (no es un zombi) y es el mismo proceso (no un PID reutilizado)."""
    f = _stat(pid)
    if not f or f[0] in ("Z", "X"):          # terminado, pendiente de que su padre lo recoja
        return False
    try:
        st = int(f[19])
    except (IndexError, ValueError):
        return False
    return start is None or st == start


def process_tree(root: int) -> list[int]:
    """El proceso y todos sus descendientes (umu/pressure-vessel crean sesiones nuevas)."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        f = _stat(int(d.name))
        if f:
            children.setdefault(int(f[1]), []).append(int(d.name))
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo += children.get(pid, [])
    return out


def argv0_name(pid: int) -> str:
    """Nombre del ejecutable (también rutas de Windows: C:\\…\\Game.exe)."""
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        return ""
    return argv0.replace("\\", "/").rsplit("/", 1)[-1]


def exe_pids(pids: list[int], exe_name: str) -> list[int]:
    """De un árbol de procesos, los que son el .exe del juego."""
    name = exe_name.lower()
    return [p for p in pids if argv0_name(p).lower() == name]


# ---------------------------------------------------------------- archivo
def entry(game_id: str, name: str, kind: str, launched_by: str, pid: int, game_pids: list[int],
          exe: str, proton: str, proton_path: str, prefix: str, started: float) -> dict:
    return {
        "id": game_id, "name": name, "kind": kind, "launched_by": launched_by,
        "pid": pid, "pid_starttime": starttime(pid),
        "game_pids": [{"pid": p, "starttime": starttime(p)} for p in game_pids],
        "exe": exe, "exe_name": Path(exe).name if exe else "",
        "proton": proton, "proton_path": proton_path, "prefix": prefix, "started": started,
    }


def write(games: list[dict], path: Path | None = None) -> None:
    path = path or running_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": VERSION, "umbral_pid": os.getpid(), "updated": time.time(), "games": games}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def read_alive(g: dict) -> bool:
    """La entrada sigue viva: su lanzador o alguno de sus procesos del juego existe (mismo starttime)."""
    return alive(int(g.get("pid", 0)), g.get("pid_starttime")) or \
        any(alive(int(x["pid"]), x.get("starttime")) for x in g.get("game_pids", []))


def read(path: Path | None = None, only_alive: bool = True) -> list[dict]:
    try:
        data = json.loads((path or running_file()).read_text())
    except (OSError, ValueError):
        return []
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(games, list):
        return []
    if only_alive:
        games = [g for g in games if isinstance(g, dict) and read_alive(g)]
    return games


# ---------------------------------------------------------------- cierre
def _signal_all(pids: list[int], sig: int) -> None:
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_gone(targets: list[tuple[int, int | None]], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(p, s) for p, s in targets):
            return True
        time.sleep(0.2)
    return not any(alive(p, s) for p, s in targets)


def wineserver_kill(proton_path: str, prefix: str) -> bool:
    """`wineserver -k` del prefijo con el wineserver de ese Proton (o el del sistema)."""
    cands = [Path(proton_path) / "files/bin/wineserver", Path(proton_path) / "dist/bin/wineserver"] \
        if proton_path else []
    ws = next((str(c) for c in cands if c.exists()), None) or "wineserver"
    try:
        subprocess.run([ws, "-k"], env={**os.environ, "WINEPREFIX": prefix}, timeout=15, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def stop_game(game: dict, timeout: float = 8.0) -> bool:
    """Cierre ordenado: primero el .exe del juego (SIGTERM); si no responde, `wineserver -k`
    del prefijo (que cerraría también Battle.net si está en el mismo prefijo) y por último
    el lanzador. Devuelve True si al final ya no queda nada en marcha."""
    gp = [(int(x["pid"]), x.get("starttime")) for x in game.get("game_pids", [])]
    gp = [(p, s) for p, s in gp if alive(p, s)]
    root = (int(game.get("pid", 0)), game.get("pid_starttime"))
    if gp:
        _signal_all([p for p, _s in gp], signal.SIGTERM)
        if not _wait_gone(gp, timeout):
            wineserver_kill(game.get("proton_path", ""), game.get("prefix", ""))
            if not _wait_gone(gp, 3):
                _signal_all([p for p, _s in gp], signal.SIGKILL)
    elif alive(*root) and game.get("launched_by") == "umbral":
        wineserver_kill(game.get("proton_path", ""), game.get("prefix", ""))
    # el lanzador (umu-run) termina solo al cerrarse el juego; si no, se le ayuda
    if root[0] and game.get("launched_by") == "umbral" and alive(*root) and not _wait_gone([root], 5):
        _signal_all([root[0]], signal.SIGTERM)
        if not _wait_gone([root], 3):
            _signal_all([root[0]], signal.SIGKILL)
    return not any(alive(p, s) for p, s in gp) and not (game.get("launched_by") == "umbral" and alive(*root))
