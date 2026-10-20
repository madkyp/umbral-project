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

from . import agent, battlenet, engines, exeicon, gpu, installers, integration, library, paths, prefixes, runners, running, selfupdate, sgdb, updates, wowconfig
from . import VERSION
from .config import BATTLENET_ID, Config, Game, Prefix
from .launcher import GameProcess, State, build, build_native, kill_wineserver, start_deck_session
from .i18n import _

log = logging.getLogger(__name__)
MAX_LOG_LINES = 4000


# Ventana de Battle.net: Proton le pone la clase genérica de todos los juegos, así que se distingue por el título
BATTLENET_WINDOW = (r"^steam_app_default$", r"^Battle\.net$")

class Controller:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.notify_errors = False          # True cuando Umbral se abre solo para ejecutar un programa
        self.cfg = Config.load()
        self.report: gpu.GpuReport | None = None
        self.runners: list[runners.Runner] = runners.discover()
        self.procs: dict[str, GameProcess] = {}
        self.external: set[str] = set()  # juegos en marcha lanzados por Battle.net
        self._ext_pids: dict[str, list[int]] = {}       # sus PIDs exactos (el .exe)
        self._ext_started: dict[str, float] = {}
        # Juegos que seguían abiertos de una sesión anterior de Umbral (siguen en running.json)
        self._adopted: dict[str, dict] = {g["id"]: g for g in running.read() if g.get("id")}
        self._running_cache = ""
        GLib.timeout_add_seconds(5, self._refresh_running)
        self._icons: dict[str, tuple[Path | None, str]] = {}   # exe -> (png, color)
        self._icon_jobs: set[str] = set()
        self.checking: set[str] = set()   # juegos comprobando versión
        # Actualizaciones de juegos de Blizzard: ¿hay una nueva? y, si Battle.net la está bajando, su progreso
        self.update_status: dict[str, updates.UpdateStatus] = {}
        self.update_progress: dict[str, agent.Progress] = {}
        self._agent: agent.AgentClient | None = None
        self._update_check_busy = self._poll_busy = False
        self._bnet_was_running = False
        # «Actualizar desde Umbral»: juego → {"opened": Battle.net lo abrió Umbral, "tries", "seen": se vio la descarga}
        self._direct: dict[str, dict] = {}
        self._finishing: dict[str, int] = {}     # juego → reintentos que quedan para ver su versión al día
        GLib.timeout_add_seconds(6, lambda: (self.check_updates(), False)[1])
        # Actualizar Umbral desde GitHub: versión publicada (si es más nueva) y estado de la actualización en curso
        self.self_remote = ""
        self.self_busy = False
        self.self_msg = ""
        if selfupdate.installed():
            GLib.timeout_add_seconds(25, lambda: (self.check_self_update(), False)[1])
            GLib.timeout_add_seconds(6 * 3600, lambda: (self.check_self_update(), True)[1])
        GLib.timeout_add_seconds(15 * 60, lambda: (self.check_updates(), True)[1])
        GLib.timeout_add_seconds(3, self._poll_agent)
        self.moving: set[str] = set()     # juegos cuyos archivos se están moviendo
        self.installing: set[str] = set() # emuladores instalándose
        self._sessions: dict[str, float] = {}   # juego -> instante desde el que falta sumar tiempo
        GLib.timeout_add_seconds(60, self._tick_playtime)
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
        if self.notify_errors:                     # sin ventana abierta nadie vería el aviso
            integration.notify("Umbral", msg)
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
            if g.kind == "custom" and library.sync_prefix_name(self.cfg, g):
                changes.append(f"~ prefix {g.name}")
        changes += library.prune_prefixes(self.cfg)
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
        return bool(p and p.running()) or starting or key in self.external or key in self.moving \
            or key in self._adopted

    def state(self, key: str) -> str:
        if key in self.checking:
            return _('Comprobando versión')
        if key in self.moving:
            return _("Moviendo…")
        if key in self.external or key in self._adopted:
            return State.RUNNING
        p = self.procs.get(key)
        return p.state if p else ""

    def scan_external(self) -> bool:
        """Juegos abiertos a través de Battle.net: su proceso no es hijo nuestro,
        así que se buscan por nombre de ejecutable en /proc. Devuelve si cambió."""
        wanted = {Path(g.exe).name.lower(): g.id for g in self.cfg.games
                  if g.kind == "blizzard" and g.exe}
        found: set[str] = set()
        pids: dict[str, list[int]] = {}
        if wanted:
            for d in Path("/proc").iterdir():
                if not d.name.isdigit():
                    continue
                name = running.argv0_name(int(d.name)).lower()
                if name in wanted:
                    found.add(wanted[name])
                    pids.setdefault(wanted[name], []).append(int(d.name))
        found -= {k for k, p in self.procs.items() if p.running()}
        self._ext_pids = {k: v for k, v in pids.items() if k in found}
        for gid in found - self.external:
            self._ext_started[gid] = time.time()
        for gid in self.external - found:
            self._ext_started.pop(gid, None)
        changed = found != self.external
        for gid in found - self.external:
            self._session_start(gid)
        for gid in self.external - found:
            self._session_end(gid)
        self.external = found
        return changed

    def run(self, key: str, prefix_id: str, exe: str, args: list[str] | None = None,
            game: Game | None = None, on_exit: Callable[[int | None], None] | None = None,
            notify_user: bool = True) -> GameProcess | None:
        native = game is not None and game.kind in engines.NATIVE_KINDS
        prefix = None if native else self.cfg.prefix(prefix_id)
        if prefix is None and not native:
            self.error(_('El prefijo no existe en la configuración.'))
            return None
        fresh = True
        if prefix is not None:
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
            plan = build_native(self.cfg, game, self.report) if native else \
                build(self.cfg, prefix, exe, args, game, self.report, self.debug, self.runners,
                      shared=self._others_in_prefix(key, prefix.path))
        except (LookupError, FileNotFoundError) as e:
            self.error(str(e))
            return None
        name = game.name if game else key
        self.logs[key] = []
        self.append_log(key, f"$ {plan.describe()}")
        if plan.gpu:
            self.append_log(key, f"# GPU: {plan.gpu.name} ({plan.gpu.pci_id}) · runner: {plan.runner.name}")
        elif native:
            self.append_log(key, f"# {plan.runner.name}")
        for w in plan.warnings:
            self.append_log(key, f"⚠ {w}")
        if self.debug:
            log.debug("Lanzando %s: %s", key, plan.describe())

        def on_state(st: str, code: int | None):
            self.emit("state", key, st)
            if st in (State.EXITED, State.ERROR):
                self._session_end(key)
                self._auto_wtf_backup(key)
            if st == State.EXITED and notify_user:
                integration.notify(_('{0} se ha cerrado').format(name))
            elif st == State.ERROR:
                p = self.procs.get(key)
                hint = p.hints[0] if p and p.hints else _('Revisa el registro.')
                report = self._crash_report(key, game, p)
                if report:
                    hint = report
                integration.notify(_('{0}: error (código {1})').format(name, code), hint, urgency="critical")
            if st in (State.EXITED, State.ERROR) and on_exit:
                GLib.idle_add(lambda: (on_exit(code), False)[1])

        proc = GameProcess(key.replace(":", "_"), plan, prefix.path if prefix else "",
                           on_line=lambda s: self.append_log(key, s), on_state=on_state)
        self.procs[key] = proc

        def start():
            proc.start()
            if proc.state == State.RUNNING and plan.deck_session and proc.proc is not None and game is not None:
                start_deck_session(proc.proc.pid, game.id)
            if proc.state == State.RUNNING and notify_user:
                integration.notify(_('Iniciando {0}').format(name),
                                   _('GPU: {0}').format(plan.gpu.name if plan.gpu else _("predeterminada")))
                threading.Thread(target=self._watch_gpu, args=(key,), daemon=True).start()
                self.emit("game_started", key)
                self._session_start(key)

        previous = self._previous_runner(prefix) if prefix else ""
        if prefix is not None and plan.runner.kind == "proton" and previous and previous != plan.runner.name and not fresh:
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
            if prefix is not None and plan.runner.kind == "proton" and prefix.last_runner != plan.runner.name:
                prefix.last_runner = plan.runner.name
                self.save()
            start()
        return proc

    def _crash_report(self, key: str, game: Game | None, p) -> str:
        """Si el juego es de Blizzard y dejó un informe en Errors/, lo resume en el aviso y el registro."""
        if not (game and game.kind == "blizzard" and game.exe and p):
            return ""
        found = wowconfig.crash_report(game.exe, p.started)
        if not found:
            self.append_log(key, _("# El juego no dejó informe de error en Errors/ (cierre sin volcado)."))
            return ""
        path, summary = found
        advice = wowconfig.crash_advice(summary)
        self.append_log(key, _("⚠ Informe de error de WoW: {0}").format(summary))
        self.append_log(key, _("# Informe completo: {0}").format(path))
        if advice:
            self.append_log(key, f"⚠ {advice}")
        return f"{summary}\n{advice}".strip()

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
        if game.kind in engines.NATIVE_KINDS:
            self.run(game_id, "", game.exe, None, game)
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

    ADHOC_NAME = "Programas sueltos"          # prefijo compartido; su id sale de create_prefix

    def run_adhoc(self, path: str, on_exit: Callable[[int | None], None] | None = None) -> bool:
        """Ejecuta un .exe/.msi/.bat suelto (p. ej. «Abrir con Umbral») sin añadirlo a la biblioteca.
        Todos comparten el prefijo «Programas sueltos». True si se lanzó."""
        p = Path(path).expanduser()
        if p.suffix.lower() not in engines.WINDOWS_EXTS or not p.is_file():
            self.error(_('Umbral solo ejecuta archivos .exe, .msi o .bat existentes: {0}').format(p.name))
            return False
        prefix = next((x for x in self.cfg.prefixes if x.name == self.ADHOC_NAME), None)
        if prefix is None:
            prefix = self.create_prefix(self.ADHOC_NAME, self.cfg.settings.default_runner)
        key = f"adhoc:{p.name}"
        if self.is_running(key):
            self.error(_('{0} ya está en ejecución.').format(p.name))
            return False
        return self.run(key, prefix.id, str(p), None, None, on_exit=on_exit) is not None

    def _check_then_launch(self, game: Game) -> None:
        """Comprueba la versión sin abrir Battle.net; si está al día, lanza el .exe."""
        self.checking.add(game.id)
        self.emit("state", game.id, _('Comprobando versión'))

        def work():
            st = updates.check(game.exe, game.product)

            def done():
                self.checking.discard(game.id)
                if st.state == "update":
                    self.update_status[game.id] = st
                    if self.can_update_direct(game) and self.battlenet_ready():
                        # Umbral se la pide al Agent de Battle.net, que se abre oculto: sin ventana
                        integration.notify(_('Actualización disponible'), _('{0}: tienes la {1} y hay la {2}. Actualizando…').format(
                            game.name, st.local or '?', st.remote or '?'))
                        self.update_direct(game.id)
                    else:
                        # No se puede pedir desde Umbral: se abre Battle.net para que actualice él
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

    # ------------------------------------------------------------ actualizaciones (Blizzard)
    def _blizzard_games(self) -> list[Game]:
        return [g for g in self.cfg.games if g.kind == "blizzard" and g.product and g.exe and not g.hidden]

    def check_updates(self) -> None:
        """Compara en segundo plano la versión instalada de cada juego de Blizzard con la del servidor
        oficial (solo lectura, sin abrir Battle.net) y avisa a la biblioteca si algo cambió."""
        games = self._blizzard_games()
        if self._update_check_busy or not games:
            return
        self._update_check_busy = True

        def work():
            found = {g.id: updates.check(g.exe, g.product) for g in games}

            def done():
                def key(st):
                    return (st.state, st.remote) if st else None
                self._update_check_busy = False
                changed = [gid for gid, st in found.items() if key(st) != key(self.update_status.get(gid))]
                self.update_status.update(found)
                retry = False
                for gid in list(self._finishing):           # acababa una descarga: ¿quedó al día?
                    g, st = self.cfg.game(gid), found.get(gid)
                    if g is not None and st is not None and st.state == "ok":
                        del self._finishing[gid]
                        integration.notify(_('{0} actualizado').format(g.name), st.local)
                        self._direct_finished(gid, True)
                    elif self._finishing[gid] > 0 and g is not None:
                        self._finishing[gid] -= 1           # Battle.net escribe .build.info unos segundos después
                        retry = True
                    else:
                        del self._finishing[gid]            # cancelada o sin terminar: se deja como está
                        self._direct_finished(gid, False)
                if retry:
                    GLib.timeout_add_seconds(10, lambda: (self.check_updates(), False)[1])
                if changed:
                    self.emit("updates", changed)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ actualizar Umbral
    def self_update_available(self) -> bool:
        return bool(self.self_remote) and selfupdate.is_newer(self.self_remote)

    def check_self_update(self, manual: bool = False) -> None:
        """Pregunta a GitHub por la última versión (en segundo plano) y avisa a la interfaz."""
        if self.self_busy:
            return

        def work():
            remote = selfupdate.fetch_remote_version()

            def done():
                if remote:
                    new = remote != self.self_remote and selfupdate.is_newer(remote)
                    self.self_remote = remote
                    if new:
                        self.emit("toast", _("Hay una versión nueva de Umbral: {0}. Actualízala en Sistema.").format(remote))
                elif manual:
                    self.emit("toast", _("No se pudo comprobar si hay una versión nueva de Umbral."))
                if manual and remote and not selfupdate.is_newer(remote):
                    self.emit("toast", _("Umbral está al día ({0}).").format(VERSION))
                self.emit("selfupdate")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def run_self_update(self) -> None:
        """git clone + makepkg + pacman -U (con pkexec). Al terminar avisa para reiniciar Umbral."""
        if self.self_busy or not selfupdate.installed():
            return
        self.self_busy = True
        self.self_msg = _("Descargando…")
        self.emit("selfupdate")

        def line(text: str):
            log.info("selfupdate: %s", text)

        def work():
            err = ""
            try:
                pkg = selfupdate.build(line)
                GLib.idle_add(lambda: (setattr(self, "self_msg", _("Instalando (te pedirá la contraseña)…")),
                                       self.emit("selfupdate"), False)[2])
                selfupdate.install(pkg, line)
            except selfupdate.UpdateError as e:
                err = str(e)

            def done():
                self.self_busy = False
                self.self_msg = ""
                if err:
                    self.error(err)
                else:
                    self.emit("selfupdate_done")
                self.emit("selfupdate")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ actualizar desde Umbral (experimental)
    def can_update_direct(self, g: Game) -> bool:
        """Solo si Umbral ya vio una versión nueva y el juego no está en marcha ni actualizándose."""
        st = self.update_status.get(g.id)
        return (g.kind == "blizzard" and bool(g.product) and bool(st and st.state == "update")
                and g.id not in self.update_progress and g.id not in self._direct and not self.is_running(g.id))

    def direct_active(self) -> bool:
        return bool(self._direct)

    def direct_pending(self, game_id: str) -> bool:
        """Se ha pedido la actualización y aún no se ve la descarga (Battle.net arrancando, etc.)."""
        return game_id in self._direct and game_id not in self.update_progress

    def update_direct(self, game_id: str) -> None:
        """Abre Battle.net si hace falta, le pide la actualización a su Agent y deja que la barra de la
        tarjeta muestre el avance. Al terminar cierra Battle.net si lo abrió Umbral."""
        g = self.cfg.game(game_id)
        if g is None or not self.can_update_direct(g):
            self.error(_("Este juego no tiene una actualización pendiente que Umbral pueda pedir."))
            return
        if not self.battlenet_ready():
            self.error(_("Battle.net no está instalado en el prefijo. Usa el asistente."))
            return
        opened = not self.is_running(BATTLENET_ID)
        self._direct[game_id] = {"opened": opened, "tries": 0, "seen": False}
        self.emit("toast", _("Actualizando «{0}» desde Umbral: Battle.net se abrirá un momento…").format(g.name)
                  if opened else _("Pidiendo la actualización de «{0}»…").format(g.name))
        if opened:
            integration.hyprland_hide_windows(*BATTLENET_WINDOW)      # que Battle.net no se vea ni robe el foco
            self.launch_game(BATTLENET_ID)
        self.emit("updates", [game_id])
        GLib.timeout_add_seconds(2, lambda: (self._direct_request(game_id), False)[1])

    def _direct_fail(self, game_id: str, msg: str) -> None:
        info = self._direct.pop(game_id, None)
        self.error(msg)
        self.emit("updates", [game_id])
        if info and info["opened"]:
            self.emit("toast", _("Battle.net se queda abierto: puedes actualizar desde su ventana."))

    def _direct_request(self, game_id: str) -> None:
        info, g = self._direct.get(game_id), self.cfg.game(game_id)
        if info is None or g is None:
            return
        bnet = self.cfg.battlenet_prefix()
        if bnet is None or not self.is_running(BATTLENET_ID):
            info["tries"] += 1
            if info["tries"] > 15:                    # Battle.net no llegó a arrancar
                self._direct_fail(game_id, _("Battle.net no se abrió: no se pudo pedir la actualización."))
                return
            GLib.timeout_add_seconds(2, lambda: (self._direct_request(game_id), False)[1])
            return
        if self._agent is None or self._agent.prefix_path != bnet.path:
            self._agent = agent.AgentClient(bnet.path)
        client = self._agent

        def work():
            ok = client.request_update(g.product)

            def done():
                info["tries"] += 1
                if ok is None and info["tries"] <= 45:        # el Agent aún no está listo: se reintenta
                    GLib.timeout_add_seconds(2, lambda: (self._direct_request(game_id), False)[1])
                elif ok:
                    # el seguimiento lo hace _poll_agent; si en 40 s no se ve ninguna descarga, no empezó
                    GLib.timeout_add_seconds(40, lambda: (self._direct_started(game_id), False)[1])
                else:
                    self._direct_fail(game_id, _("El Agent de Battle.net no aceptó la actualización de «{0}»: "
                                                 "hazla desde Battle.net.").format(g.name))
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _direct_started(self, game_id: str) -> None:
        info = self._direct.get(game_id)
        if info is not None and not info["seen"] and game_id not in self.update_progress:
            self.check_updates()                       # tal vez ya estaba al día o terminó al instante
            self._direct.pop(game_id, None)
            self._finishing.pop(game_id, None)
            self.emit("updates", [game_id])

    def _direct_finished(self, game_id: str, ok: bool) -> None:
        """La descarga acabó. Si Umbral abrió Battle.net solo para esto y no hay nada más en curso, lo cierra."""
        info = self._direct.pop(game_id, None)
        if info is None:
            return
        busy = bool(self.update_progress or self._direct) or any(
            self.is_running(x.id) for x in self.cfg.games if x.kind == "blizzard")
        if ok and info["opened"] and not busy:
            self.emit("toast", _("Actualización terminada: cerrando Battle.net."))
            self.stop(BATTLENET_ID)
        elif not ok:
            self.emit("toast", _("La actualización no terminó: revisa Battle.net."))

    def _poll_agent(self) -> bool:
        """Mientras Battle.net está abierto, pregunta al Agent cuánto lleva cada actualización."""
        running = self.is_running(BATTLENET_ID)
        if running != self._bnet_was_running:
            self._bnet_was_running = running
            if not running:
                self._agent = None                    # el Agent se va con Battle.net
                if self.update_progress:
                    gone = list(self.update_progress)
                    self.update_progress.clear()
                    self.emit("progress", gone)
                self.check_updates()
        bnet = self.cfg.battlenet_prefix()
        games = self._blizzard_games()
        if not running or bnet is None or not games or self._poll_busy:
            return True
        if self._agent is None or self._agent.prefix_path != bnet.path:
            self._agent = agent.AgentClient(bnet.path)
        client, self._poll_busy = self._agent, True

        def work():
            now = {g.id: p for g in games if (p := client.progress(g.product)) is not None}

            def done():
                self._poll_busy = False
                before, self.update_progress = self.update_progress, now
                for gid in now:
                    if gid in self._direct:
                        self._direct[gid]["seen"] = True
                changed = [gid for gid in {*before, *now} if before.get(gid) != now.get(gid)]
                if changed:
                    self.emit("progress", changed)
                finished = set(before) - set(now)           # terminó (o se canceló): se vuelve a comprobar
                if finished:
                    for gid in finished:
                        self._finishing[gid] = 6            # hasta ~1 min esperando a que figure al día
                    GLib.timeout_add_seconds(2, lambda: (self.check_updates(), False)[1])
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _others_in_prefix(self, key: str, prefix_path: str) -> bool:
        """¿Hay otro programa de Umbral en marcha en el mismo prefijo (p. ej. Battle.net con el juego)?"""
        return bool(prefix_path) and any(k != key and q.running() and q.prefix_path == prefix_path
                                         for k, q in self.procs.items())

    def stop(self, key: str) -> bool:
        """Cierra un juego en marcha (botón Detener, `umbral --stop`). False si no estaba abierto."""
        p = self.procs.get(key)
        g = self.cfg.game(key)
        if p and p.running():
            threading.Thread(target=p.stop, kwargs={"exe": g.exe if g else "",
                                                    "shared": self._others_in_prefix(key, p.prefix_path)},
                             daemon=True).start()
            return True
        entry = next((e for e in self._running_entries() if e["id"] == key), None)
        if entry is None:
            return False

        def work():
            running.stop_game(entry)
            self._adopted.pop(key, None)
            GLib.idle_add(lambda: (self._refresh_running(), self.emit("library"), False)[-1])
        threading.Thread(target=work, daemon=True).start()
        return True

    # ------------------------------------------------------------ juegos en marcha (running.json)
    def _running_entries(self) -> list[dict]:
        out: list[dict] = []
        for key, p in self.procs.items():
            if not (p.running() and p.proc):
                continue
            g = self.cfg.game(key)
            pref = self.cfg.prefix(g.prefix_id) if g else None
            tree = p.pids()
            exe = g.exe if g and g.exe else ""
            if p.plan.runner.kind == "native":     # ScummVM, emuladores: sin Proton ni prefijo
                out.append(running.entry(key, g.name if g else key, g.kind if g else "", "umbral", p.proc.pid,
                                         [p.proc.pid], exe, "", "", "", p.started, engine=p.plan.runner.name))
                continue
            out.append(running.entry(
                key, g.name if g else key, g.kind if g else "", "umbral", p.proc.pid,
                running.exe_pids(tree, Path(exe).name) if exe else [], exe, p.plan.runner.name,
                str(p.plan.runner.path) if p.plan.runner.kind == "proton" else "",
                pref.path if pref else p.prefix_path, p.started))
        for key in self.external:
            g = self.cfg.game(key)
            pref = self.cfg.prefix(g.prefix_id) if g else None
            r = runners.resolve(pref.runner, self.runners) if pref else None
            pids = self._ext_pids.get(key, [])
            if g and pids:
                out.append(running.entry(key, g.name, g.kind, "battlenet", pids[0], pids, g.exe,
                                         r.name if r else "", str(r.path) if r and r.kind == "proton" else "",
                                         pref.path if pref else "", self._ext_started.get(key, time.time())))
        have = {e["id"] for e in out}
        for key, e in list(self._adopted.items()):
            if key in have:
                continue
            if running.read_alive(e):
                out.append(e)
            else:
                del self._adopted[key]
        return out

    def _refresh_running(self) -> bool:
        """Mantiene running.json al día (solo se reescribe si algo cambió)."""
        try:
            entries = self._running_entries()
            key = repr([(e["id"], e["pid"], [x["pid"] for x in e["game_pids"]]) for e in entries])
            if key != self._running_cache:
                running.write(entries)
                self._running_cache = key
        except OSError as e:
            log.warning("No se pudo escribir running.json: %s", e)
        return True

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
        if g.kind == engines.EMULATOR:      # ROMs: a Juegos/<sistema>/<juego>
            return Path(g.exe).is_file() and not engines.is_organized(g.exe, self.games_root(), g.system)
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
                if g.kind == engines.EMULATOR:
                    new = engines.organize_rom(g.exe, self.games_root(), g.system, g.name)
                else:
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
        if sgdb.get_key() and not installers.is_installer(exe):
            self.fetch_cover(g.id, quiet=True)
        return g

    def add_native_game(self, c: "engines.Candidate", name: str) -> Game:
        """Añade un juego de ScummVM, de emulador o de máquina virtual (sin prefijo de Wine)."""
        path = c.path
        root = self.games_root()
        if c.engine == engines.SCUMMVM and Path(path).parent.resolve() == root.resolve():
            # disco recién extraído: como las ROMs, agrupado → Juegos/ScummVM/<juego>
            base = root / "ScummVM"
            clean = engines.folder_name(name) or Path(path).name
            dest, n = base / clean, 2
            while dest.exists():
                dest, n = base / f"{clean} ({n})", n + 1
            try:
                base.mkdir(parents=True, exist_ok=True)
                Path(path).rename(dest)
                path = str(dest)
            except OSError as e:
                log.warning("No se pudo mover %s a %s: %s", path, dest, e)
        g = Game(Config.new_id(), name, c.engine, "", path, system=c.system, target=c.target,
                 cdrom=c.extra.get("cdrom", ""), search_name=c.title if c.title != name else "")
        self.cfg.games.append(g)
        self.save()
        self.emit("library")
        if sgdb.get_key():
            self.fetch_cover(g.id, quiet=True)
        return g

    def add_roms(self, cands: list, organize: bool) -> list[Game]:
        """Añade muchas ROMs de golpe (las que ya están en la biblioteca se saltan). Guardarlas en
        su carpeta y buscar sus portadas se hace después, de una en una, en segundo plano."""
        have = set()
        for g in self.cfg.games:
            try:
                have.add(Path(g.exe).resolve())
            except OSError:
                pass
        added = []
        for c in cands:
            if Path(c.path).resolve() in have:
                continue
            g = Game(Config.new_id(), c.title, engines.EMULATOR, "", c.path, system=c.system)
            self.cfg.games.append(g)
            added.append(g)
        self.save()
        self.emit("library")
        if not added:
            return added
        root, key = self.games_root(), sgdb.get_key()
        for g in added:
            self.moving.add(g.id)

        def work():
            for g in added:
                if organize and not engines.is_organized(g.exe, root, g.system):
                    try:
                        new = engines.organize_rom(g.exe, root, g.system, g.name)
                        GLib.idle_add(lambda g=g, new=new: (setattr(g, "exe", new), False)[1])
                    except OSError as e:
                        log.warning("No se pudo guardar %s en su carpeta: %s", g.exe, e)
                GLib.idle_add(lambda g=g: (self.moving.discard(g.id), False)[1])
                if key:
                    try:
                        path = sgdb.best_cover([g.name])
                        if path:
                            GLib.idle_add(lambda g=g, path=path: (self.set_cover(g.id, str(path)), False)[1])
                    except sgdb.SGDBError as e:
                        log.info("SteamGridDB sin portada para %s: %s", g.name, e)

            def done():
                self.save()
                self.emit("library")
                self.emit("toast", _("{0} juegos añadidos").format(len(added)))
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()
        return added

    # ------------------------------------------------------------ emuladores
    def install_emulator(self, key: str) -> None:
        """Instala un emulador sin sudo (Flathub para tu usuario o AppImage oficial), en segundo plano."""
        if key in self.installing:
            return
        self.installing.add(key)
        name = engines.EMULATORS[key].name
        log_key = "emuladores"
        self.logs.setdefault(log_key, [])
        self.emit("system")

        def work():
            try:
                engines.install(key, lambda line: self.append_log(log_key, line))
                self.emit("toast", _("{0} instalado").format(name))
            except RuntimeError as e:
                self.append_log(log_key, f"⚠ {e}")
                self.error(_("No se pudo instalar {0}: {1}").format(name, e))
            GLib.idle_add(lambda: (self.installing.discard(key), self.emit("system"), self.emit("library"), False)[-1])
        threading.Thread(target=work, daemon=True).start()

    def uninstall_emulator(self, key: str) -> None:
        name = engines.EMULATORS[key].name

        def work():
            try:
                engines.uninstall(key)
                self.emit("toast", _("{0} desinstalado").format(name))
            except (RuntimeError, OSError) as e:
                self.error(_("No se pudo desinstalar {0}: {1}").format(name, e))
            GLib.idle_add(lambda: (self.emit("system"), self.emit("library"), False)[-1])
        threading.Thread(target=work, daemon=True).start()

    def extract_disc(self, iso: str, name: str, on_done: Callable[[Path | None, list], None]) -> None:
        """Extrae un CD/DVD de PC a la carpeta de juegos y lo identifica (en segundo plano).
        on_done(carpeta, candidatos) en el hilo principal; (None, []) si falla."""
        root = self.games_root()
        base = engines.folder_name(name) or Path(iso).stem   # como al mover juegos: «Nombre (2)»
        dest, n = root / base, 2
        while dest.exists():
            dest, n = root / f"{base} ({n})", n + 1

        def work():
            try:
                engines.extract_disc(Path(iso), dest)
                found = [c for c in engines.detect(dest) if c.engine != "extract"]
            except OSError as e:
                self.error(_("No se pudo extraer el disco: {0}").format(e))
                GLib.idle_add(lambda: (on_done(None, []), False)[1])
                return
            GLib.idle_add(lambda: (on_done(dest, found), False)[1])
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ SteamGridDB
    def fetch_cover(self, game_id: str, quiet: bool = False) -> None:
        """Busca en SteamGridDB la portada del juego (por su nombre) y la aplica."""
        g = self.cfg.game(game_id)
        if g is None:
            return

        def work():
            try:
                path = sgdb.best_cover([g.name, g.search_name])
            except sgdb.SGDBError as e:
                if not quiet:
                    self.error(str(e))
                log.info("SteamGridDB sin portada para %s: %s", g.name, e)
                return
            if path is None:
                if not quiet:
                    self.emit("toast", _("SteamGridDB no tiene una portada clara para «{0}»: elígela en ⚙ → "
                                         "Buscar en SteamGridDB.").format(g.name))
                return
            GLib.idle_add(lambda: (self.set_cover(game_id, str(path)), False)[1])
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ copias de WTF (WoW)
    def has_wtf(self, g: Game | None) -> bool:
        return bool(g and g.kind == "blizzard" and g.exe and wowconfig.wtf_dir(g.exe).is_dir())

    def backup_wtf(self, game_id: str, reason: str = "", quiet: bool = False) -> None:
        g = self.cfg.game(game_id)
        if not self.has_wtf(g):
            if not quiet:
                self.error(_("El juego aún no tiene carpeta WTF (ábrelo una vez)."))
            return

        def work():
            try:
                dest = wowconfig.backup_wtf(g.exe, game_id, reason)
            except (OSError, FileNotFoundError) as e:
                self.error(_("No se pudo copiar la configuración: {0}").format(e))
                return
            self.append_log(game_id, _("# Copia de la configuración (WTF): {0}").format(dest))
            if not quiet:
                self.emit("toast", _("Copia de la configuración guardada"))
        threading.Thread(target=work, daemon=True).start()

    def _auto_wtf_backup(self, key: str) -> None:
        """Al cerrar WoW: copia de WTF si la última tiene más de 12 horas."""
        g = self.cfg.game(key)
        if self.has_wtf(g) and wowconfig.needs_auto_backup(key):
            self.backup_wtf(key, "auto", quiet=True)

    def restore_wtf(self, game_id: str, archive: Path) -> None:
        g = self.cfg.game(game_id)
        if g is None:
            return
        if self.is_running(game_id):
            self.error(_("Cierra el juego antes de restaurar su configuración."))
            return
        try:
            wowconfig.restore_wtf(g.exe, game_id, archive)
        except OSError as e:
            self.error(_("No se pudo restaurar la configuración: {0}").format(e))
            return
        self.emit("toast", _("Configuración restaurada (la anterior quedó guardada como copia)"))

    # ------------------------------------------------------------ tiempo de juego
    def _tracks_time(self, key: str) -> bool:
        g = self.cfg.game(key)
        return bool(g and g.kind in ("blizzard", "custom", *engines.NATIVE_KINDS))

    def _session_start(self, key: str) -> None:
        if self._tracks_time(key) and key not in self._sessions:
            self._sessions[key] = time.time()
            g = self.cfg.game(key)
            g.last_played = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.save()

    def _add_played(self, key: str, until: float) -> None:
        start = self._sessions.get(key)
        g = self.cfg.game(key)
        if start is None or g is None:
            return
        g.playtime += max(0, int(until - start))
        g.last_played = time.strftime("%Y-%m-%dT%H:%M:%S")

    def _session_end(self, key: str) -> None:
        if key in self._sessions:
            self._add_played(key, time.time())
            del self._sessions[key]
            self.save()
            self.emit("played", [key])       # solo cambia su tarjeta (tiempo y «hoy»)

    def _tick_playtime(self) -> bool:
        """Cada minuto se suma lo jugado: nada se pierde si Umbral se cierra de golpe."""
        if self._sessions:
            now = time.time()
            for key in list(self._sessions):
                self._add_played(key, now)
                self._sessions[key] = now
            self.save()
            self.emit("played", list(self._sessions))
        return True

    def flush_playtime(self) -> None:
        """Al salir de Umbral: guarda lo jugado hasta ahora (el juego sigue abierto)."""
        if self._sessions:
            self._tick_playtime()

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
