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

from . import battlenet, engines, exeicon, gpu, installers, integration, library, paths, prefixes, runners, running, sgdb, updates, wowconfig
from .config import BATTLENET_ID, Config, Game, Prefix
from .launcher import GameProcess, State, build, build_native, kill_wineserver, start_deck_session
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
        self._ext_pids: dict[str, list[int]] = {}       # sus PIDs exactos (el .exe)
        self._ext_started: dict[str, float] = {}
        # Juegos que seguían abiertos de una sesión anterior de Umbral (siguen en running.json)
        self._adopted: dict[str, dict] = {g["id"]: g for g in running.read() if g.get("id")}
        self._running_cache = ""
        GLib.timeout_add_seconds(5, self._refresh_running)
        self._icons: dict[str, tuple[Path | None, str]] = {}   # exe -> (png, color)
        self._icon_jobs: set[str] = set()
        self.checking: set[str] = set()   # juegos comprobando versión
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
                build(self.cfg, prefix, exe, args, game, self.report, self.debug, self.runners)
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

    def stop(self, key: str) -> bool:
        """Cierra un juego en marcha (botón Detener, `umbral --stop`). False si no estaba abierto."""
        p = self.procs.get(key)
        g = self.cfg.game(key)
        if p and p.running():
            threading.Thread(target=p.stop, kwargs={"exe": g.exe if g else ""}, daemon=True).start()
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
            clean = name.replace("/", "-").strip() or Path(path).name
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
                 cdrom=c.extra.get("cdrom", ""))
        self.cfg.games.append(g)
        self.save()
        self.emit("library")
        if sgdb.get_key():
            self.fetch_cover(g.id, quiet=True)
        return g

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
        base = name.replace("/", "-").strip() or Path(iso).stem   # como al mover juegos: «Nombre (2)»
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
                path = sgdb.best_cover(g.name)
            except sgdb.SGDBError as e:
                if not quiet:
                    self.error(str(e))
                log.info("SteamGridDB sin portada para %s: %s", g.name, e)
                return
            if path is None:
                if not quiet:
                    self.emit("toast", _("SteamGridDB no tiene portada para «{0}».").format(g.name))
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
            self.emit("library")

    def _tick_playtime(self) -> bool:
        """Cada minuto se suma lo jugado: nada se pierde si Umbral se cierra de golpe."""
        if self._sessions:
            now = time.time()
            for key in list(self._sessions):
                self._add_played(key, now)
                self._sessions[key] = now
            self.save()
            self.emit("library")
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
