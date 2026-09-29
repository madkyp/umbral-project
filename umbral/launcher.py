"""Construcción del comando/entorno y control de procesos lanzados."""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from . import gpu as gpumod
from . import paths
from .installers import windows_path
from .config import BATTLENET_DEFAULTS, Config, Game, LaunchOptions, Prefix, merged
from .runners import Runner, resolve
from .i18n import _

log = logging.getLogger(__name__)

# Opciones de MangoHud 0.8 (MangoHud.conf.example), pasadas por MANGOHUD_CONFIG.
MANGOHUD_PRESETS = {
    "fps": (_('Solo FPS'), "fps_only"),
    "basic": (_('Básico: FPS, frametime, CPU y GPU con temperatura'),
              "fps,frametime,frame_timing,gpu_stats,gpu_temp,cpu_stats,cpu_temp"),
    "full": (_('Completo: además RAM, VRAM, GPU y driver'),
             "fps,frametime,frame_timing,gpu_stats,gpu_temp,gpu_name,vulkan_driver,"
             "cpu_stats,cpu_temp,ram,vram"),
}
MANGOHUD_POSITIONS = {"top-left": _('Arriba a la izquierda'), "top-right": _('Arriba a la derecha'),
                      "bottom-left": _('Abajo a la izquierda'), "bottom-right": _('Abajo a la derecha')}


def mangohud_config(preset: str | None, position: str | None) -> str:
    opts = MANGOHUD_PRESETS.get(preset or "basic", MANGOHUD_PRESETS["basic"])[1]
    pos = position if position in MANGOHUD_POSITIONS else "top-left"
    return f"{opts},position={pos}"

# Patrones del log -> consejo para el usuario.
HINTS = [
    (re.compile(r"BLZBNTBNA00000005|Agent.*(sleep|dormido)", re.I),
     _('El Agent de Battle.net se ha quedado dormido: usa Reparar → Reiniciar Agent.')),
    (re.compile(r"err:vulkan|vkCreateInstance failed|No Vulkan", re.I),
     _('Error de Vulkan: revisa la sección GPU (faltan paquetes lib32 o el driver no carga).')),
    (re.compile(r"Failed to create swapchain", re.I),
     _('Fallo de swapchain (pantalla negra). Prueba con PROTON_ENABLE_WAYLAND desactivado, sin gamescope, o reinicia el juego.')),
    (re.compile(r"PROTONPATH.*(not found|does not exist)|No such file.*proton", re.I),
     _('No se encuentra el runner: instala o selecciona otro Proton en Ajustes.')),
    (re.compile(r"urlopen error|Temporary failure in name resolution|Network is unreachable", re.I),
     _('Sin conexión a Internet (umu necesita descargar el runtime la primera vez).')),
]


@dataclass
class LaunchPlan:
    argv: list[str]
    env: dict[str, str]          # solo las variables que añade Umbral
    cwd: str
    runner: Runner
    gpu: gpumod.Gpu | None
    warnings: list[str] = field(default_factory=list)

    def full_env(self) -> dict[str, str]:
        return {**os.environ, **self.env}

    def describe(self) -> str:
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items()))
        return f"{env} {shlex.join(self.argv)}"


def effective_options(cfg: Config, prefix: Prefix, game: Game | None) -> LaunchOptions:
    base = BATTLENET_DEFAULTS if prefix.id == "battlenet" else LaunchOptions()
    return merged(base, prefix.options, game.options if game else LaunchOptions())


def _flag(v: bool | None) -> str:
    return "1" if v else "0"


def build(cfg: Config, prefix: Prefix, exe: str, args: list[str] | None = None,
          game: Game | None = None, report: gpumod.GpuReport | None = None,
          debug: bool = False, runners: list[Runner] | None = None) -> LaunchPlan:
    opts = effective_options(cfg, prefix, game)
    runner = resolve(opts.runner or prefix.runner or cfg.settings.default_runner, runners)
    if runner is None:
        raise LookupError(_("Runner '{0}' no encontrado. Instálalo o elige otro en Ajustes → Runners.").format(opts.runner or prefix.runner))
    warnings: list[str] = []
    env: dict[str, str] = {"WINEPREFIX": prefix.path}

    if runner.kind == "proton":
        if not shutil.which("umu-run"):
            raise FileNotFoundError(_('umu-run no está instalado: sudo pacman -S umu-launcher'))
        env.update({"PROTONPATH": str(runner.path), "GAMEID": "umu-default"})
        if opts.use_wined3d is not None:
            env["PROTON_USE_WINED3D"] = _flag(opts.use_wined3d)
        if opts.no_esync is not None:
            env["PROTON_NO_ESYNC"] = _flag(opts.no_esync)
        if opts.no_fsync is not None:
            env["PROTON_NO_FSYNC"] = _flag(opts.no_fsync)
        if opts.no_ntsync is not None:
            if runner.is_ge:
                env["PROTON_NO_NTSYNC"] = _flag(opts.no_ntsync)
            elif opts.no_ntsync:
                warnings.append(_('PROTON_NO_NTSYNC solo existe en GE-Proton; se ignora.'))
        if opts.wayland is not None:
            env["PROTON_ENABLE_WAYLAND"] = _flag(opts.wayland)
        if opts.writecopy is not None:
            env["WINE_SIMULATE_WRITECOPY"] = _flag(opts.writecopy)
        if opts.gpu_shader_cache:
            cache = Path(prefix.path) / "shadercache"
            cache.mkdir(parents=True, exist_ok=True)
            env["PROTON_LOCAL_SHADER_CACHE"] = "1"
            env["STEAM_COMPAT_SHADER_PATH"] = str(cache)
        if debug:
            env["PROTON_LOG"] = "1"
            env["PROTON_LOG_DIR"] = str(paths.LOG_DIR)
            env["UMU_LOG"] = "1"
        cmd = ["umu-run", *_windows_command(exe)]
    else:
        cmd = [str(runner.path), *_windows_command(exe)]

    target = None
    if report is not None:
        genv, target = gpumod.gpu_env(report, opts.gpu)
        env.update(genv)

    wrappers: list[str] = []
    if opts.gamescope:
        if shutil.which("gamescope"):
            gs = shlex.split(opts.gamescope_args or "")
            if opts.mangohud and "--mangoapp" not in gs:
                gs.append("--mangoapp")
            wrappers += ["gamescope", *gs, "--"]
        else:
            warnings.append(_('gamescope no está instalado; se lanza sin él.'))
    if opts.gamemode:
        if shutil.which("gamemoderun"):
            wrappers.append("gamemoderun")
        else:
            warnings.append(_('gamemode no está instalado (sudo pacman -S gamemode lib32-gamemode).'))
    if opts.mangohud:
        env["MANGOHUD_CONFIG"] = mangohud_config(opts.mangohud_preset, opts.mangohud_position)
        if not opts.gamescope:
            env["MANGOHUD"] = "1"

    env.update(opts.env)  # las variables del usuario siempre ganan
    argv = wrappers + cmd + (args or []) + shlex.split(opts.args or "")
    cwd = str(Path(exe).parent) if Path(exe).is_absolute() and Path(exe).parent.exists() else prefix.path
    return LaunchPlan(argv, env, cwd, runner, target, warnings)


def _windows_command(exe: str) -> list[str]:
    """.msi -> msiexec /i; .bat/.cmd -> cmd /c; el resto se ejecuta tal cual."""
    ext = Path(exe).suffix.lower()
    if ext == ".msi":
        return ["msiexec", "/i", windows_path(exe)]
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", windows_path(exe)]
    return [exe]


# ---------------------------------------------------------------- procesos

class State:
    # Textos visibles; se comparan siempre por la constante, así que traducirlos es seguro
    STARTING = _("Iniciando")
    RUNNING = _("En ejecución")
    STOPPING = _("Cerrando")
    EXITED = _("Cerrado")
    ERROR = _("Error")


class GameProcess:
    """Proceso lanzado con log a archivo y callbacks (llamados desde un hilo)."""

    def __init__(self, key: str, plan: LaunchPlan, prefix_path: str,
                 on_line: Callable[[str], None] | None = None,
                 on_state: Callable[[str, int | None], None] | None = None):
        self.key = key
        self.plan = plan
        self.prefix_path = prefix_path
        self.on_line = on_line or (lambda s: None)
        self.on_state = on_state or (lambda s, c: None)
        self.proc: subprocess.Popen | None = None
        self.state = State.STARTING
        self.returncode: int | None = None
        self.started = time.time()
        self.hints: list[str] = []
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = paths.LOG_DIR / f"{key}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        _rotate_logs(key)

    def _set(self, state: str, code: int | None = None) -> None:
        self.state = state
        self.on_state(state, code)

    def start(self) -> None:
        with open(self.log_path, "w") as f:
            f.write(f"# {self.plan.describe()}\n")
        try:
            self.proc = subprocess.Popen(
                self.plan.argv, env=self.plan.full_env(), cwd=self.plan.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, text=True, errors="replace", bufsize=1)
        except OSError as e:
            self._emit(_('No se pudo lanzar: {0}').format(e))
            self._set(State.ERROR, None)
            return
        self._set(State.RUNNING)
        threading.Thread(target=self._pump, daemon=True).start()

    def _emit(self, line: str) -> None:
        with open(self.log_path, "a") as f:
            f.write(line + "\n")
        for rx, hint in HINTS:
            if hint not in self.hints and rx.search(line):
                self.hints.append(hint)
                self.on_line(f"⚠ {hint}")
        self.on_line(line)

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._emit(line.rstrip("\n"))
        self.returncode = self.proc.wait()
        self._emit(_("# Proceso terminado con código {0}").format(self.returncode))
        ok = self.returncode == 0 or self.state == State.STOPPING
        self._set(State.EXITED if ok else State.ERROR, self.returncode)

    def pids(self) -> list[int]:
        """PID del lanzador y de todos sus descendientes (juego incluido)."""
        # pressure-vessel (umu) crea sesiones nuevas: se recorre el árbol por ppid.
        if not self.proc:
            return []
        children: dict[int, list[int]] = {}
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                stat = (d / "stat").read_text()
            except OSError:
                continue
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            children.setdefault(ppid, []).append(int(d.name))
        out, todo = [], [self.proc.pid]
        while todo:
            pid = todo.pop()
            out.append(pid)
            todo += children.get(pid, [])
        return out

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 8.0) -> None:
        """Cierre limpio: wineserver -k, luego SIGTERM y SIGKILL al grupo."""
        if not self.running():
            return
        self._set(State.STOPPING)
        kill_wineserver(self.plan.runner, self.prefix_path)
        deadline = time.monotonic() + timeout
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)  # type: ignore[union-attr]
            except (ProcessLookupError, PermissionError):
                return
            while time.monotonic() < deadline and self.running():
                time.sleep(0.2)
            if not self.running():
                return
            deadline = time.monotonic() + 3


def kill_wineserver(runner: Runner, prefix_path: str) -> bool:
    ws = runner.wineserver
    if not ws:
        return False
    try:
        subprocess.run([str(ws), "-k"], env={**os.environ, "WINEPREFIX": prefix_path},
                       timeout=15, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("wineserver -k falló: %s", e)
        return False


def _rotate_logs(key: str, keep: int = 10) -> None:
    logs = sorted(paths.LOG_DIR.glob(f"{key}-*.log"))
    for old in logs[:-keep + 1]:
        old.unlink(missing_ok=True)
