"""Punto de entrada: aplicación GTK de instancia única y modo --check."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, paths  # noqa: E402
from .i18n import _  # noqa: E402

HERE = Path(__file__).resolve().parent
log = logging.getLogger("umbral")


def setup_logging(debug: bool) -> Path:
    paths.ensure_dirs()
    logfile = paths.LOG_DIR / "umbral.log"
    handlers: list[logging.Handler] = [logging.FileHandler(logfile, mode="w")]
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return logfile


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="umbral", description=_('{0}: lanzador de Battle.net para Linux').format(APP_NAME))
    ap.add_argument("--debug", action="store_true", help=_('registro detallado (GPU, entorno, PROTON_LOG)'))
    ap.add_argument("--launch", metavar="ID", help=_('lanzar un juego de la biblioteca (p. ej. battlenet)'))
    ap.add_argument("--check", action="store_true", help=_('diagnóstico en terminal, sin interfaz'))
    ap.add_argument("--stop", metavar="ID", help=_("cerrar un juego en marcha (sin abrir la ventana)"))
    ap.add_argument("--set", nargs="+", metavar="ID CLAVE=VALOR",
                    help=_("cambiar opciones de un juego (p. ej. --set <id> gamemode=on fps_limit=120)"))
    ap.add_argument("--get", metavar="ID", help=_("mostrar en JSON las opciones de un juego"))
    ap.add_argument("--list", action="store_true", help=_("mostrar en JSON los juegos de la biblioteca"))
    ap.add_argument("--running", action="store_true",
                    help=_("mostrar en JSON los juegos en marcha (el contenido de running.json)"))
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    return ap.parse_args(argv)


def check() -> int:
    """Diagnóstico sin GUI: GPU, runners, prefijo y juegos detectados."""
    from . import battlenet, gpu, integration, launcher, library, prefixes, runners
    from .config import BATTLENET_ID, Config

    cfg = Config.load()
    rep = gpu.gather()
    print(_('== {0} {1}\n== GPU\n{2}').format(APP_NAME, VERSION, rep.summary()))
    if rep.nvidia_modeset is not None:
        print(_('nvidia_drm modeset={0} fbdev={1}').format(rep.nvidia_modeset, rep.nvidia_fbdev))
    for i in rep.issues:
        print(f"[{i.level}] {i.message}" + (f"\n        → {i.fix}" if i.fix else ""))
    rs = runners.discover()
    print("== Runners\n" + "\n".join(f"  {r.name}  ({r.path})" for r in rs))
    pfx = cfg.battlenet_prefix()
    importable = prefixes.find_importable()
    print("== Battle.net")
    if pfx:
        p = Path(pfx.path)
        print(_('  prefijo: {0} ({1}) runner: {2}').format(p, _('importado') if pfx.imported else _('propio'), pfx.runner))
        print(_('  cliente: {0}').format(battlenet.client_exe(p) or _('NO instalado')))
        print(_('  salud: {0}').format(prefixes.health(p) or 'OK'))
        try:
            plan = launcher.build(cfg, pfx, "Battle.net.exe", report=rep, runners=rs)
            print(_('  entorno: {0}').format(plan.describe()))
        except (LookupError, FileNotFoundError) as e:
            print(_('  ERROR: {0}').format(e))
    else:
        print(_('  sin configurar') + (_('; importable: {0}').format(', '.join(map(str, importable))) if importable else ""))
    print(_('== Juegos detectados'))
    for p in [Path(x.path) for x in cfg.prefixes] or importable:
        for g in battlenet.detect_games(p):
            print(_('  {0} [{1}] {2} v{3}\n      {4}').format(g.name, g.uid, _('confirmado') if g.confirmed else _('sin confirmar'), g.version, g.exe))
    library.sync_detected(cfg) if pfx else None
    ok, _out = integration.verify_lua_snippet(integration.hyprland_snippet(rep))
    print(_('== Fragmento Hyprland: {0} (Hyprland --verify-config)').format(_('válido') if ok else _('NO válido')))
    return 0 if not any(i.level == "error" for i in rep.issues) and cfg.game(BATTLENET_ID) is not None else 1


class UmbralApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.ctl = None
        self.win = None
        self._accent_css = Gtk.CssProvider()

    def do_command_line(self, cmdline: Gio.ApplicationCommandLine) -> int:
        args = parse_args(cmdline.get_arguments()[1:])
        if self.ctl is None:
            self._startup(args.debug)
        if args.set:
            code, out = set_options(self.ctl.cfg, args.set, self.ctl.runners)
            if code == 0:
                self.ctl.save()
                self.ctl.emit("library")
                cmdline.print_literal(out + "\n")
            else:
                cmdline.printerr_literal(out + "\n")
            return code
        if args.stop:
            # Viene de otra instancia (p. ej. el botón DETENER de Gaming Deck): sin ventana
            if self.ctl.stop(args.stop):
                cmdline.print_literal(_("Cerrando {0}…").format(args.stop) + "\n")
                return 0
            cmdline.printerr_literal(_("«{0}» no está en marcha.").format(args.stop) + "\n")
            return 1
        self.activate()
        if args.launch:
            GLib.idle_add(lambda: (self.ctl.launch_game(args.launch), False)[1])
        return 0

    def _startup(self, debug: bool):
        from .controller import Controller
        logfile = setup_logging(debug)
        log.info("%s %s (debug=%s), registro en %s", APP_NAME, VERSION, debug, logfile)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "ui" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Por encima de ~/.config/gtk-4.0/gtk.css (prioridad USER), que define su propio acento
        Gtk.StyleContext.add_provider_for_display(display, self._accent_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        icons = Gtk.IconTheme.get_for_display(display)
        for d in (HERE.parent / "data", Path("/usr/share/umbral")):
            if d.is_dir():
                icons.add_search_path(str(d))
        self.ctl = Controller(debug=debug)
        self.connect("shutdown", lambda *_a: self.ctl.flush_playtime())
        self.set_accent(self.ctl.cfg.settings.accent)
        self._start_tray()
        self.ctl.refresh_system()
        self.ctl.sync_library()

    # ------------------------------------------------------------ bandeja
    def _start_tray(self):
        from .tray import Tray
        installed = Path("/usr/share/icons/hicolor/scalable/apps") / f"{APP_ID}.svg"
        icon_path = "" if installed.exists() else str(HERE.parent / "data")
        self.tray = Tray(self.get_application_id() or APP_ID, APP_NAME, APP_ID, icon_path, self.toggle_window)
        self.tray.start()
        self._update_tray_menu()
        self.ctl.connect(lambda ev, *a: ev in ("library", "state") and self._update_tray_menu())

    def _update_tray_menu(self):
        from .config import BATTLENET_ID
        items = [(_('Abrir Umbral'), self.show_window)]
        if self.ctl.battlenet_ready():
            items.append((_('Abrir Battle.net'), lambda: self.ctl.launch_game(BATTLENET_ID)))
        for g in self.ctl.cfg.games:
            if g.id != BATTLENET_ID and not g.hidden:
                items.append((_('Jugar: {0}').format(g.name), lambda gid=g.id: self.ctl.launch_game(gid)))
        items += [None, (_('Salir de Umbral'), self.quit)]
        self.tray.set_items(items)

    def restart(self):
        """Reinicia Umbral (p. ej. para aplicar el idioma). Los juegos abiertos no se cierran."""
        import os
        self.ctl.save()
        os.execv(sys.executable, [sys.executable, "-m", "umbral"])

    def show_window(self):
        self.activate()

    def toggle_window(self):
        if self.win and self.win.is_visible() and self.win.is_active():
            self.win.set_visible(False)
        else:
            self.activate()

    def in_tray(self) -> bool:
        return bool(self.ctl.cfg.settings.run_in_tray and getattr(self, "tray", None) and self.tray.registered)

    def set_accent(self, color: str):
        """color vacío = respetar el acento del sistema/tema."""
        if not color:
            self._accent_css.load_from_string("")
            return
        self._accent_css.load_from_string(
            f'@define-color accent_bg_color {color};\n@define-color accent_color {color};\n:root {{ --accent-bg-color: {color}; --accent-color: color-mix(in srgb, {color} 80%, white); }}')

    def do_activate(self):
        from . import integration
        from .ui.window import MainWindow
        if self.win is None:
            st = self.ctl.cfg.settings
            if st.float_window:
                # Antes de mapear la ventana, para que nazca ya flotante (sin parpadeo)
                ok, out = integration.hyprland_float_rule(self.get_application_id(), st.window_width,
                                                          st.window_height)
                log.info("Regla de ventana flotante en Hyprland: %s (%s)", ok, out)
            self.win = MainWindow(self, self.ctl, self.set_accent)
            self.win.set_default_size(st.window_width, st.window_height)
            self.win.set_icon_name(APP_ID)
        self.win.present()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    args = parse_args(argv[1:])
    if args.check:
        setup_logging(args.debug)
        return check()
    if args.list or args.get:
        import json

        from . import gameopts
        from .config import Config
        cfg = Config.load()
        if args.list:
            print(json.dumps(gameopts.listing(cfg), indent=1, ensure_ascii=False))
            return 0
        g = cfg.game(args.get)
        if g is None:
            print(_("No existe el juego '{0}'.").format(args.get), file=sys.stderr)
            return 1
        print(json.dumps(gameopts.describe(cfg, g), indent=1, ensure_ascii=False))
        return 0
    if args.set:
        return set_from_cli(args.set, argv)
    if args.running:
        import json

        from . import running
        print(json.dumps(running.read(), indent=1, ensure_ascii=False))
        return 0
    if args.stop:
        return stop_from_cli(args.stop, argv)
    return UmbralApp().run(argv)


def set_options(cfg, values: list[str], runner_list) -> tuple[int, str]:
    """Aplica `--set <id> clave=valor…` sobre cfg. (código, mensaje): 0 bien, 1 sin juego, 2 error."""
    import json

    from . import gameopts
    from .library import runner_names
    game_id, pairs = values[0], values[1:]
    g = cfg.game(game_id)
    if g is None:
        return 1, _("No existe el juego '{0}'.").format(game_id)
    if not pairs:
        return 2, _("Falta al menos una clave=valor. Claves: {0}.").format(", ".join(gameopts.KEYS))
    try:
        changed = gameopts.apply(cfg, g, gameopts.parse(pairs), runner_names(runner_list))
    except gameopts.OptionError as e:
        return 2, str(e)
    desc = gameopts.describe(cfg, g)
    return 0, json.dumps({"id": g.id, "changed": changed, "options": desc["options"], "env": desc["env"],
                          "effective": desc["effective"]}, ensure_ascii=False)


def set_from_cli(values: list[str], argv: list[str]) -> int:
    """`umbral --set`: si Umbral está abierto se lo pide a él (si no, al guardar pisaría el
    cambio); si no, edita la configuración directamente."""
    app = UmbralApp()
    try:
        app.register(None)
    except GLib.Error:
        pass
    if app.get_is_remote():
        return app.run(argv)
    from . import runners
    from .config import Config
    cfg = Config.load()
    code, out = set_options(cfg, values, runners.discover())
    if code == 0:
        cfg.save()
    print(out, file=sys.stdout if code == 0 else sys.stderr)
    return code


def stop_from_cli(game_id: str, argv: list[str]) -> int:
    """`umbral --stop <id>`: si Umbral está abierto se lo pide a él (sin mostrar la ventana);
    si no, cierra el juego con los datos de running.json."""
    app = UmbralApp()
    try:
        app.register(None)
    except GLib.Error:
        pass
    if app.get_is_remote():
        return app.run(argv)
    from . import running
    entry = next((g for g in running.read() if g.get("id") == game_id), None)
    if entry is None:
        print(_("«{0}» no está en marcha.").format(game_id), file=sys.stderr)
        return 1
    print(_("Cerrando {0}…").format(game_id))
    return 0 if running.stop_game(entry) else 1
