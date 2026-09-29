"""Estado de la aplicación y orquestación (independiente de los widgets).

Los trabajos largos corren en hilos; los callbacks hacia la UI se entregan
en el hilo principal con GLib.idle_add.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from collections.abc import Callable

from gi.repository import GLib

from . import battlenet, exeicon, gpu, installers, integration, library, paths, prefixes, runners, updates
from .config import BATTLENET_ID, Config, Game, Prefix
from .launcher import GameProcess, State, build, kill_wineserver
from .i18n import _

log = logging.getLogger(__name__)
MAX_LOG_LINES = 4000


class Controller:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.cfg = Config.load()
        self.report: gpu.GpuReport | None = None
        self.runners: list[runners.Runner] = runners.discover()
        self.procs: dict[str, GameProcess] = {}
        self.external: set[str] = set()  # juegos en marcha lanzados por Battle.net
        self._icons: dict[str, tuple[Path | None, str]] = {}   # exe -> (png, color)
        self._icon_jobs: set[str] = set()
        self.checking: set[str] = set()   # juegos comprobando versión
        self.moving: set[str] = set()     # juegos cuyos archivos se están moviendo
        self.logs: dict[str, list[str]] = {}
        self._listeners: list[Callable[..., None]] = []

    # ------------------------------------------------------------ eventos
    def connect(self, fn: Callable[..., None]) -> None:
        self._listeners.append(fn)

    def emit(self, event: str, *args) -> None:
        def _do():
            for fn in list(self._listeners):
                try:
                    fn(event, *args)
                except Exception:  # un widget roto no debe tumbar la app
                    log.exception("listener falló en %s", event)
            return False
        GLib.idle_add(_do)

    def error(self, msg: str) -> None:
        log.error(msg)
        self.emit("error", msg)

    def save(self) -> None:
        try:
            self.cfg.save()
        except OSError as e:
            self.error(_('No se pudo guardar la configuración: {0}').format(e))

    # ------------------------------------------------------------ sistema
    def refresh_system(self) -> None:
        def work():
            self.report = gpu.gather()
            self.runners = runners.discover()
            if self.debug:
                log.debug("GPU:\n%s", self.report.summary())
                for i in self.report.issues:
                    log.debug("GPU %s: %s | %s", i.level, i.message, i.fix)
                log.debug("Runners: %s", [f"{r.name} ({r.path})" for r in self.runners])
            self.emit("system")
        threading.Thread(target=work, daemon=True).start()

    def refresh_all(self) -> None:
        """Botón «Actualizar»: vuelve a detectar juegos, runners y GPU."""
        self._icons.clear()
        self._icon_jobs.clear()
        self.sync_library()
        self.refresh_system()
        self.emit("library")

    def sync_library(self) -> None:
        changes = library.sync_detected(self.cfg)
        # Autorreparación: juegos movidos a la carpeta de Umbral cuya ruta quedó desactualizada
        for g in self.cfg.games:
            if g.kind == "custom" and g.id not in self.moving:
                found = installers.find_moved(g.exe, self.games_root())
                if found:
                    log.info("Ruta de %s corregida: %s -> %s", g.name, g.exe, found)
                    g.exe = found
                    changes.append(f"~ {g.name}")
        if changes:
            self.save()
            for c in changes:
                if c.startswith("+"):
                    integration.notify(_('Juego detectado'), c[2:] + _(' ya está listo para lanzarse.'))
            self.emit("library")

    # ------------------------------------------------------------ iconos
    def game_icon(self, g: Game) -> tuple[Path | None, str] | None:
        """Icono de una entrada: el del usuario, el incluido para el producto o el del .exe."""
        custom = None
        if g.icon and Path(g.icon).exists():
            custom = exeicon.ball_thumbnail(g.icon, crop=False)
        custom = custom or exeicon.product_icon(g.product)
        if custom:
            key = str(custom)
            if key not in self._icons:
                self._icons[key] = (custom, exeicon.dominant_color(custom))
            return self._icons[key]
        return self.icon(g.exe, g.kind)

    def icon(self, exe: str, kind: str) -> tuple[Path | None, str] | None:
        """(png, color) del icono del juego; None mientras se extrae en segundo plano."""
        if not exe:
            return (None, "")
        if exe in self._icons:
            return self._icons[exe]
        if exe not in self._icon_jobs:
            self._icon_jobs.add(exe)

            def work():
                png = exeicon.best_icon(exe, kind)
                color = exeicon.dominant_color(png) if png else ""
                self._icons[exe] = (png, color)
                self.emit("icons")
            threading.Thread(target=work, daemon=True).start()
        return None

    def set_cover(self, game_id: str, image: str | None, field: str = "cover") -> None:
        """Copia la imagen elegida a ~/.local/share/umbral/covers (o la quita).

        field: "cover" (portada a sangre) o "icon" (icono sobre el color del juego).
        """
        g = self.cfg.game(game_id)
        if g is None:
            return
        old = getattr(g, field)
        if old:
            Path(old).unlink(missing_ok=True)
            setattr(g, field, "")
        if image:
            import shutil
            paths.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            safe = game_id.replace(":", "_").replace("/", "_")
            suffix = "-icon" if field == "icon" else ""
            dest = paths.COVERS_DIR / f"{safe}{suffix}{Path(image).suffix.lower()}"
            try:
                shutil.copy2(image, dest)
            except OSError as e:
                self.error(_('No se pudo copiar la imagen: {0}').format(e))
                return
            setattr(g, field, str(dest))
            self._icons.pop(str(dest), None)
        self.save()
        self.emit("library")

    # ------------------------------------------------------------ logs
    def append_log(self, key: str, line: str) -> None:
        buf = self.logs.setdefault(key, [])
        buf.append(line)
        if len(buf) > MAX_LOG_LINES:
            del buf[: len(buf) - MAX_LOG_LINES]
        self.emit("log", key, line)

    # ------------------------------------------------------------ lanzar
    def is_running(self, key: str) -> bool:
        p = self.procs.get(key)
        # «Iniciando» incluye la espera por la copia de seguridad previa a un cambio de Proton
        starting = bool(p and p.proc is None and p.state == State.STARTING)
        return bool(p and p.running()) or starting or key in self.external or key in self.moving

    def state(self, key: str) -> str:
        if key in self.checking:
            return _('Comprobando versión')
        if key in self.moving:
            return _("Moviendo…")
        if key in self.external:
            return State.RUNNING
        p = self.procs.get(key)
        return p.state if p else ""

    def scan_external(self) -> bool:
        """Juegos abiertos a través de Battle.net: su proceso no es hijo nuestro,
        así que se buscan por nombre de ejecutable en /proc. Devuelve si cambió."""
        wanted = {Path(g.exe).name.lower(): g.id for g in self.cfg.games
                  if g.kind == "blizzard" and g.exe}
        found: set[str] = set()
        if wanted:
            for d in Path("/proc").iterdir():
                if not d.name.isdigit():
                    continue
                try:
                    argv0 = (d / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
                except OSError:
                    continue
                name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
                if name in wanted:
                    found.add(wanted[name])
        found -= {k for k, p in self.procs.items() if p.running()}
        changed = found != self.external
        self.external = found
        return changed

    def run(self, key: str, prefix_id: str, exe: str, args: list[str] | None = None,
            game: Game | None = None, on_exit: Callable[[int | None], None] | None = None,
            notify_user: bool = True) -> GameProcess | None:
        prefix = self.cfg.prefix(prefix_id)
        if prefix is None:
            self.error(_('El prefijo no existe en la configuración.'))
            return None
        # Un directorio vacío o inexistente es un prefijo por crear (Proton lo inicializa).
        ppath = Path(prefix.path)
        fresh = not ppath.exists() or not any(ppath.iterdir())
        problems = [] if fresh else prefixes.health(ppath)
        if fresh:
            ppath.mkdir(parents=True, exist_ok=True)
        if problems:
            self.error(_('Prefijo dañado: ') + " ".join(problems) + _(' Usa Reparar → Recrear prefijo.'))
            return None
        try:
            plan = build(self.cfg, prefix, exe, args, game, self.report, self.debug, self.runners)
        except (LookupError, FileNotFoundError) as e:
            self.error(str(e))
            return None
        name = game.name if game else key
        self.logs[key] = []
        self.append_log(key, f"$ {plan.describe()}")
        if plan.gpu:
            self.append_log(key, f"# GPU: {plan.gpu.name} ({plan.gpu.pci_id}) · runner: {plan.runner.name}")
        for w in plan.warnings:
            self.append_log(key, f"⚠ {w}")
        if self.debug:
            log.debug("Lanzando %s: %s", key, plan.describe())

        def on_state(st: str, code: int | None):
            self.emit("state", key, st)
            if st == State.EXITED and notify_user:
                integration.notify(_('{0} se ha cerrado').format(name))
            elif st == State.ERROR:
                p = self.procs.get(key)
                hint = p.hints[0] if p and p.hints else _('Revisa el registro.')
                integration.notify(_('{0}: error (código {1})').format(name, code), hint, urgency="critical")
            if st in (State.EXITED, State.ERROR) and on_exit:
                GLib.idle_add(lambda: (on_exit(code), False)[1])

        proc = GameProcess(key.replace(":", "_"), plan, prefix.path,
                           on_line=lambda s: self.append_log(key, s), on_state=on_state)
        self.procs[key] = proc

        def start():
            proc.start()
            if proc.state == State.RUNNING and notify_user:
                integration.notify(_('Iniciando {0}').format(name), _('GPU: {0}').format(plan.gpu.name if plan.gpu else 'predeterminada'))
                threading.Thread(target=self._watch_gpu, args=(key,), daemon=True).start()

        previous = self._previous_runner(prefix)
        if plan.runner.kind == "proton" and previous and previous != plan.runner.name and not fresh:
            # Cambio de Proton: el prefijo se actualizará o degradará. Copia antes (sin juegos).
            self.append_log(key, f"# Cambio de Proton en el prefijo: {previous} → {plan.runner.name}. "
                                 "Guardando copia de seguridad (sin juegos)…")
            self.emit("toast", _('Guardando copia del prefijo antes de cambiar de Proton…'))

            def backup_then_start():
                try:
                    dest = prefixes.backup(ppath)
                    self.append_log(key, f"# Copia guardada: {dest}")
                except prefixes.PrefixError as e:
                    self.append_log(key, f"⚠ {e}")
                    self.error(_('{0}. No se lanza para no arriesgar el prefijo.').format(e))
                    self.procs.pop(key, None)
                    return
                prefix.last_runner = plan.runner.name
                self.save()
                start()
            threading.Thread(target=backup_then_start, daemon=True).start()
        else:
            if plan.runner.kind == "proton" and prefix.last_runner != plan.runner.name:
                prefix.last_runner = plan.runner.name
                self.save()
            start()
        return proc

    def _previous_runner(self, prefix) -> str:
        """Proton con el que se usó el prefijo por última vez (o el que lo creó)."""
        if prefix.last_runner:
            return prefix.last_runner
        r = prefixes.matching_runner(Path(prefix.path), self.runners)
        return r.name if r else ""

    def _watch_gpu(self, key: str) -> None:
        """Comprueba qué GPU abre realmente el juego (tras unos segundos)."""
        time.sleep(15)
        p = self.procs.get(key)
        if not (p and p.running() and self.report):
            return
        used = gpu.gpus_in_use(p.pids(), self.report)
        if used:
            self.append_log(key, "# GPU en uso: " + ", ".join(g.name for g in used))
            if p.plan.gpu and all(g.slot != p.plan.gpu.slot for g in used):
                self.append_log(key, _('⚠ Se pidió {0} pero se usa otra GPU.').format(p.plan.gpu.name))
        self.emit("gpu_used", key, used)

    def launch_game(self, game_id: str, skip_check: bool = False) -> None:
        game = self.cfg.game(game_id)
        if game is None:
            self.error(_("No existe el juego '{0}'.").format(game_id))
            return
        if self.is_running(game_id) or game_id in self.checking:
            self.error(_('{0} ya está en ejecución.').format(game.name))
            return
        if game.kind == "blizzard" and game.product and not skip_check:
            self._check_then_launch(game)
            return
        try:
            exe, args = library.launch_target(self.cfg, game)
        except (LookupError, FileNotFoundError) as e:
            self.error(str(e))
            return
        on_exit = None
        prefix = self.cfg.prefix(game.prefix_id)
        if game.kind == "custom" and prefix and installers.is_installer(exe):
            # Instalador: se anotan los .exe de antes para proponer después los nuevos
            before = installers.snapshot(Path(prefix.path))

            def on_exit(code, gid=game.id, ppath=Path(prefix.path), before=before):
                def work():
                    found = installers.new_executables(ppath, before)
                    self.emit("installed_candidates", gid, found)
                threading.Thread(target=work, daemon=True).start()
        self.run(game_id, game.prefix_id, exe, args, game, on_exit=on_exit)

    def _check_then_launch(self, game: Game) -> None:
        """Comprueba la versión sin abrir Battle.net; si está al día, lanza el .exe."""
        self.checking.add(game.id)
        self.emit("state", game.id, _('Comprobando versión'))

        def work():
            st = updates.check(game.exe, game.product)

            def done():
                self.checking.discard(game.id)
                if st.state == "update":
                    # Versión distinta: se abre Battle.net para que actualice
                    msg = (_('{0}: tienes la {1} y hay la {2}. Abriendo Battle.net para actualizar…').format(game.name, st.local or '?', st.remote or '?'))
                    self.emit("toast", msg)
                    integration.notify(_('Actualización disponible'), msg)
                    if not self.is_running(BATTLENET_ID):
                        self.launch_game(BATTLENET_ID)
                else:
                    if st.state == "unknown":
                        self.emit("toast", _('No se pudo comprobar la versión ({0}); se lanza igualmente').format(st.reason))
                    self.launch_game(game.id, skip_check=True)
                self.emit("state", game.id, "")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def stop(self, key: str) -> None:
        p = self.procs.get(key)
        if p:
            threading.Thread(target=p.stop, daemon=True).start()

    def kill_prefix(self, prefix_id: str) -> None:
        prefix = self.cfg.prefix(prefix_id)
        r = runners.resolve(prefix.runner, self.runners) if prefix else None
        if prefix and r:
            threading.Thread(target=kill_wineserver, args=(r, prefix.path), daemon=True).start()

    def running_in_prefix(self, prefix_id: str) -> bool:
        prefix = self.cfg.prefix(prefix_id)
        return bool(prefix) and any(p.running() and p.prefix_path == prefix.path
                                    for p in self.procs.values())

    # ------------------------------------------------------------ herramientas
    def run_winetricks(self, prefix_id: str, verbs: list[str]) -> None:
        prefix = self.cfg.prefix(prefix_id)
        r = runners.resolve(prefix.runner, self.runners) if prefix else None
        if not (prefix and r and r.kind == "proton"):
            self.error(_('winetricks vía umu requiere un runner Proton.'))
            return
        key = f"winetricks-{prefix_id}"
        env = {"WINEPREFIX": prefix.path, "PROTONPATH": str(r.path), "GAMEID": "umu-default"}
        self.logs[key] = []
        self.append_log(key, "$ umu-run winetricks -q " + shlex.join(verbs))

        def work():
            try:
                p = subprocess.Popen(["umu-run", "winetricks", "-q", *verbs],
                                     env={**os.environ, **env},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     errors="replace")
            except OSError as e:
                self.error(_('No se pudo ejecutar winetricks: {0}').format(e))
                return
            for line in p.stdout:  # type: ignore[union-attr]
                self.append_log(key, line.rstrip())
            code = p.wait()
            self.emit("toast", _('Dependencias instaladas') if code == 0 else _('winetricks falló ({0})').format(code))
        threading.Thread(target=work, daemon=True).start()
        self.emit("show_log", key)

    def create_prefix(self, name: str, runner: str) -> Prefix:
        """Prefijo propio para un juego: ~/Games/umbral/<nombre>. Proton lo crea al primer uso."""
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "juego"
        root = Path(self.cfg.settings.prefix_root)
        path, n = root / slug, 2
        while path.exists() or any(p.path == str(path) for p in self.cfg.prefixes):
            path, n = root / f"{slug}-{n}", n + 1
        p = Prefix(f"p-{path.name}", name, str(path), runner)
        self.cfg.prefixes.append(p)
        self.save()
        return p

    def games_root(self) -> Path:
        return Path(self.cfg.settings.games_root).expanduser()

    def can_move(self, g: Game) -> bool:
        return g.kind == "custom" and installers.can_move(g.exe, self.games_root(),
                                                          [p.path for p in self.cfg.prefixes])

    def owns_files(self, g: Game) -> bool:
        """¿Los archivos del juego están en la carpeta de juegos de Umbral?"""
        try:
            return Path(g.exe).resolve().is_relative_to(self.games_root().resolve())
        except (OSError, ValueError):
            return False

    def move_game_files(self, game_id: str) -> None:
        """Mueve la carpeta del juego a la de Umbral (en segundo plano) y actualiza su ruta."""
        g = self.cfg.game(game_id)
        if g is None or not self.can_move(g):
            return
        if self.is_running(game_id):
            self.error(_("Cierra el juego antes de moverlo."))
            return
        self.moving.add(game_id)
        self.emit("state", game_id, _("Moviendo…"))

        def work():
            try:
                new = installers.move_game(g.exe, self.games_root())
            except OSError as e:
                GLib.idle_add(lambda: (self.moving.discard(game_id), False)[1])
                self.error(_("No se pudo mover el juego: {0}").format(e))
                self.emit("library")
                return

            def done():
                self.moving.discard(game_id)
                g.exe = new
                self.save()
                self.emit("toast", _("«{0}» movido a {1}").format(g.name, Path(new).parent))
                self.emit("library")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def prefix_users(self, prefix_id: str) -> list[Game]:
        return [g for g in self.cfg.games if g.prefix_id == prefix_id]

    def add_custom_game(self, name: str, exe: str, prefix_id: str) -> Game:
        g = Game(Config.new_id(), name, "custom", prefix_id, exe)
        self.cfg.games.append(g)
        self.save()
        self.emit("library")
        return g

    def remove_game(self, game_id: str) -> None:
        g = self.cfg.game(game_id)
        if g and g.id != BATTLENET_ID:
            if g.auto:
                g.hidden = True
            else:
                self.cfg.games.remove(g)
            self.save()
            self.emit("library")

    def battlenet_ready(self) -> bool:
        p = self.cfg.battlenet_prefix()
        return bool(p and battlenet.is_installed(Path(p.path)))
