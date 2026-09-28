"""Asistente de primera instalación de Battle.net."""
from __future__ import annotations

import threading
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from .. import battlenet, library, prefixes, runners
from ..config import BATTLENET_ID
from ..controller import Controller
from ..i18n import _

DOWNLOAD_GE = _('Descargar el GE-Proton más reciente')


class SetupWizard(Adw.Dialog):
    def __init__(self, ctl: Controller, recreate_stash: Path | None = None):
        super().__init__(title=_('Configurar Battle.net'), content_width=560, content_height=560)
        self.ctl = ctl
        self.stash = recreate_stash  # juegos apartados al recrear el prefijo
        self._cancel = False
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.nav.add(self._choice_page())
        self.connect("closed", lambda *_a: setattr(self, "_cancel", True))

    # ---------------------------------------------------------------- páginas
    def _page(self, title: str, child: Gtk.Widget) -> Adw.NavigationPage:
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(child)
        return Adw.NavigationPage(title=title, child=tv)

    def _choice_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12,
                      margin_bottom=24, margin_start=24, margin_end=24)
        status = Adw.StatusPage(icon_name="system-software-install-symbolic",
                                title=_('Battle.net en Linux'),
                                description=_('Umbral instala el cliente oficial de Blizzard en un prefijo de Proton. Después, World of Warcraft se instala y actualiza desde el propio Battle.net.'))
        status.add_css_class("compact")
        box.append(status)

        group = Adw.PreferencesGroup()
        new_row = Adw.ActionRow(use_markup=False, title=_('Instalar en un prefijo nuevo'),
                                subtitle=str(Path(self.ctl.cfg.settings.prefix_root) / "battlenet"),
                                activatable=True)
        new_row.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        new_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        new_row.connect("activated", lambda *_a: self.nav.push(self._install_page()))
        group.add(new_row)

        if self.stash is None:
            for p in prefixes.find_importable():
                r = prefixes.matching_runner(p, self.ctl.runners)
                row = Adw.ActionRow(use_markup=False, title=_('Usar un prefijo existente'), activatable=True,
                                    subtitle=f"{p} · {prefixes.proton_version(p) or '?'}"
                                             f" → {r.name if r else 'sin runner equivalente'}")
                row.add_prefix(Gtk.Image(icon_name="folder-open-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, p=p, r=r: self._import(p, r))
                group.add(row)
        box.append(group)
        note = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"],
                         label=_('Importar no copia ni modifica el prefijo: Umbral lo usa tal cual y con el mismo Proton que lo creó, para no actualizarlo sin querer.'))
        box.append(note)
        return self._page(_('Configurar Battle.net'), Gtk.ScrolledWindow(child=box, vexpand=True))

    def _install_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12,
                      margin_bottom=24, margin_start=24, margin_end=24)
        group = Adw.PreferencesGroup(title=_('Opciones'))
        names = library.runner_names([r for r in self.ctl.runners if r.kind == "proton"])
        if not any(n.startswith("GE-Proton") for n in names):
            names.insert(0, DOWNLOAD_GE)
        else:
            names.append(DOWNLOAD_GE)
        self.runner_row = Adw.ComboRow(title=_('Runner'), subtitle=_('GE-Proton recomendado'),
                                       model=Gtk.StringList.new(names))
        group.add(self.runner_row)
        self.path_row = Adw.EntryRow(title=_('Carpeta del prefijo'),
                                     text=str(Path(self.ctl.cfg.settings.prefix_root) / "battlenet"))
        group.add(self.path_row)
        box.append(group)

        self.steps = Adw.PreferencesGroup(title=_('Progreso'))
        self.step_rows = {}
        for key, title in (("runner", _('Runner Proton')), ("prefix", _('Crear prefijo')),
                           ("download", _('Descargar instalador oficial')),
                           ("install", _('Instalar Battle.net'))):
            row = Adw.ActionRow(use_markup=False, title=title, css_classes=["wizard-step"])
            icon = Gtk.Image(icon_name="content-loading-symbolic", opacity=0.3)
            row.add_prefix(icon)
            self.steps.add(row)
            self.step_rows[key] = (row, icon)
        box.append(self.steps)
        self.progress = Gtk.ProgressBar(visible=False)
        box.append(self.progress)

        self.start_btn = Gtk.Button(label=_('Instalar'), css_classes=["suggested-action", "pill-play"],
                                    halign=Gtk.Align.CENTER)
        self.start_btn.connect("clicked", self._start)
        box.append(self.start_btn)
        return self._page(_("Instalar"), Gtk.ScrolledWindow(child=box, vexpand=True))

    def _done_page(self, imported: bool = False) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_bottom=24)
        status = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title=_('Battle.net listo') if not imported else _('Prefijo importado'),
            description=(_('Inicia sesión en Battle.net y, en «World of Warcraft», elige la versión <b>World of Warcraft: Forever</b> en el selector e instálala. Umbral detectará el juego automáticamente y añadirá su acceso directo.\n\nUmbral no descarga el juego: de eso se encarga Battle.net.')))
        box.append(status)
        btn = Gtk.Button(label=_('Ir a la biblioteca'), css_classes=["suggested-action", "pill-play"],
                         halign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_a: self.close())
        box.append(btn)
        return self._page(_("Listo"), box)

    # ---------------------------------------------------------------- importar
    def _import(self, path: Path, runner: runners.Runner | None) -> None:
        if runner is None:
            self.ctl.error(_("No hay un Proton instalado equivalente a '{0}'. Instálalo antes de importar para no alterar el prefijo.").format(prefixes.proton_version(path)))
            return
        library.ensure_battlenet_prefix(self.ctl.cfg, path, runner.name, imported=True)
        self.ctl.cfg.settings.first_run_done = True
        self.ctl.save()
        self.ctl.sync_library()
        self.ctl.emit("library")
        self.nav.push(self._done_page(imported=True))

    # ---------------------------------------------------------------- instalar
    def _mark(self, key: str, state: str, subtitle: str = "") -> None:
        def do():
            row, icon = self.step_rows[key]
            icon.set_opacity(1)
            icon.set_from_icon_name({"run": "content-loading-symbolic", "ok": "object-select-symbolic",
                                     "err": "dialog-error-symbolic"}[state])
            if subtitle:
                row.set_subtitle(subtitle)
            return False
        GLib.idle_add(do)

    def _progress(self, frac: float | None) -> None:
        def do():
            self.progress.set_visible(frac is not None)
            if frac is not None:
                self.progress.set_fraction(frac)
            return False
        GLib.idle_add(do)

    def _start(self, *_a):
        self.start_btn.set_sensitive(False)
        self.runner_row.set_sensitive(False)
        self.path_row.set_sensitive(False)
        choice = self.runner_row.get_selected_item().get_string()
        path = Path(self.path_row.get_text()).expanduser()
        threading.Thread(target=self._work, args=(choice, path), daemon=True).start()

    def _fail(self, key: str, msg: str) -> None:
        self._mark(key, "err", msg)
        self._progress(None)
        self.ctl.error(msg)
        GLib.idle_add(lambda: (self.start_btn.set_sensitive(True), self.start_btn.set_label(_('Reintentar')),
                               False)[-1])

    def _work(self, choice: str, path: Path) -> None:
        ctl = self.ctl
        # 1. runner
        self._mark("runner", "run")
        try:
            if choice == DOWNLOAD_GE:
                rel = runners.ge_releases(1)
                if not rel:
                    raise runners.RunnerError(_('GitHub no devolvió ninguna release de GE-Proton'))
                self._mark("runner", "run", _('Descargando {0}…').format(rel[0]['tag']))
                r = runners.install_ge(rel[0], self._progress, lambda: self._cancel)
                ctl.runners = runners.discover()
                runner_name = r.name
            else:
                runner_name = choice
            runner = runners.resolve(runner_name, ctl.runners)
            if runner is None:
                raise runners.RunnerError(_("Runner '{0}' no encontrado").format(runner_name))
        except runners.RunnerError as e:
            return self._fail("runner", str(e))
        self._progress(None)
        self._mark("runner", "ok", runner.name)

        # 2. prefijo
        self._mark("prefix", "run", str(path))
        library.ensure_battlenet_prefix(ctl.cfg, path, runner_name)
        ctl.save()
        if not prefixes.exists(path):
            path.mkdir(parents=True, exist_ok=True)
            ok = self._run_blocking("prefix", "wineboot", ["-u"])
            if not ok or not prefixes.exists(path):
                return self._fail("prefix", _('No se pudo crear el prefijo. Revisa el registro.'))
        if self.stash is not None:
            prefixes.restore_games(path, self.stash)
            self.stash = None
        self._mark("prefix", "ok", prefixes.proton_version(path))

        # 3. instalador
        self._mark("download", "run", "battle.net (oficial)")
        try:
            setup = battlenet.download_installer(self._progress)
        except battlenet.BattleNetError as e:
            return self._fail("download", str(e))
        self._progress(None)
        self._mark("download", "ok", _('{0} KiB · cabecera PE verificada').format(setup.stat().st_size // 1024))

        # 4. instalar: el instalador acaba abriendo Battle.net, así que se espera
        #    a que aparezca el ejecutable en lugar de a que termine el proceso.
        self._mark("install", "run", _('Completa el instalador en su ventana…'))
        if self._cancel:
            return
        proc = ctl.run(BATTLENET_ID, BATTLENET_ID, str(setup), notify_user=False)
        if proc is None:
            return self._fail("install", _('No se pudo lanzar el instalador.'))
        while proc.running() and not battlenet.is_installed(path) and not self._cancel:
            GLib.usleep(2_000_000)
        if not battlenet.is_installed(path):
            return self._fail("install", _('El instalador terminó sin instalar Battle.net. Mira el registro (⚠ consejos) y reinténtalo.'))
        self._mark("install", "ok", _('Battle.net instalado'))
        ctl.cfg.settings.first_run_done = True
        ctl.save()
        ctl.emit("library")
        GLib.idle_add(lambda: (self.nav.push(self._done_page()), False)[1])

    def _run_blocking(self, step: str, exe: str, args: list[str]) -> bool:
        done = threading.Event()
        result = {}

        def on_exit(code):
            result["code"] = code
            done.set()
        GLib.idle_add(lambda: (self.ctl.run(f"setup-{step}", BATTLENET_ID, exe, args,
                                            on_exit=on_exit, notify_user=False) or done.set(), False)[1])
        done.wait(timeout=600)
        return result.get("code") == 0
