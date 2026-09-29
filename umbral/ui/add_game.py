"""Diálogo «Añadir»: cualquier .exe/.msi/.bat, en un prefijo propio o en el de Battle.net."""
from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .. import installers, library
from ..config import BATTLENET_ID
from ..controller import Controller
from ..i18n import _

NEW_PREFIX = _('Prefijo propio (nuevo, recomendado)')


class AddGameDialog(Adw.Dialog):
    def __init__(self, ctl: Controller, on_added):
        super().__init__(title=_('Añadir juego o programa'), content_width=560)
        self.ctl, self.on_added = ctl, on_added
        self.exe: str = ""

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.add_btn = Gtk.Button(label=_('Añadir'), css_classes=["suggested-action"], sensitive=False)
        self.add_btn.connect("clicked", self._add)
        hb.pack_end(self.add_btn)
        tv.add_top_bar(hb)

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description=_('Ejecutables .exe, instaladores .msi o scripts .bat de Windows.'))
        self.exe_row = Adw.ActionRow(use_markup=False, title=_('Ejecutable'), subtitle=_('Ninguno elegido'),
                                     activatable=True)
        pick = Gtk.Button(label=_('Elegir…'), valign=Gtk.Align.CENTER)
        pick.connect("clicked", self._pick)
        self.exe_row.add_suffix(pick)
        self.exe_row.connect("activated", self._pick)
        g.add(self.exe_row)
        self.name_row = Adw.EntryRow(title=_('Nombre'))
        g.add(self.name_row)
        page.add(g)
        self.hint = Adw.ActionRow(use_markup=False, visible=False, css_classes=["dim-label"],
                                  title=_('Parece un instalador: cuando termine, Umbral te propondrá añadir el juego que haya instalado.'))
        hg = Adw.PreferencesGroup()
        hg.add(self.hint)
        self.move_row = Adw.SwitchRow(use_markup=False, visible=False, active=True,
                                      title=_("Mover a la carpeta de juegos de Umbral"))
        hg.add(self.move_row)
        page.add(hg)

        pg = Adw.PreferencesGroup(title=_('Prefijo de Wine'),
                                  description=_('Un prefijo propio evita mezclar librerías y versiones de Proton con Battle.net.'))
        self._prefix_ids = ["__new__"]
        labels = [NEW_PREFIX]
        for p in ctl.cfg.prefixes:
            if p.id == BATTLENET_ID and not ctl.battlenet_ready():
                continue
            self._prefix_ids.append(p.id)
            labels.append("Battle.net (compartido)" if p.id == BATTLENET_ID else f"{p.name} (existente)")
        self.prefix_row = Adw.ComboRow(title=_('Instalar/ejecutar en'), model=Gtk.StringList.new(labels))
        self.prefix_row.connect("notify::selected", lambda *_a: self._sync())
        pg.add(self.prefix_row)
        names = library.runner_names([r for r in ctl.runners if r.kind == "proton"])
        self._runner_names = names or ["GE-Proton"]
        self.runner_row = Adw.ComboRow(title=_('Proton para el prefijo nuevo'),
                                       model=Gtk.StringList.new(self._runner_names))
        if ctl.cfg.settings.default_runner in self._runner_names:
            self.runner_row.set_selected(self._runner_names.index(ctl.cfg.settings.default_runner))
        pg.add(self.runner_row)
        page.add(pg)


        tv.set_content(page)
        self.set_child(tv)
        self._sync()

    def _sync(self):
        new = self._prefix_ids[self.prefix_row.get_selected()] == "__new__"
        self.runner_row.set_visible(new)

    def _pick(self, *_a):
        dlg = Gtk.FileDialog(title=_('Elegir ejecutable de Windows'))
        f = Gtk.FileFilter(name=_('Ejecutables de Windows (.exe, .msi, .bat)'))
        for s in ("exe", "EXE", "msi", "MSI", "bat", "BAT", "cmd"):
            f.add_suffix(s)
        dlg.set_default_filter(f)
        downloads = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or Path.home())
        dlg.set_initial_folder(Gio.File.new_for_path(str(downloads)))

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            self.exe = file.get_path()
            self.exe_row.set_subtitle(self.exe)
            if not self.name_row.get_text().strip():
                self.name_row.set_text(Path(self.exe).stem)
            self.hint.set_visible(installers.is_installer(self.exe))
            self._sync_move()
            self.add_btn.set_sensitive(True)
        dlg.open(self.get_root(), None, done)

    def _sync_move(self):
        """Explica qué se moverá (la carpeta del juego o solo el archivo) y cuánto ocupa."""
        root = self.ctl.games_root()
        ok = installers.can_move(self.exe, root, [p.path for p in self.ctl.cfg.prefixes])
        self.move_row.set_visible(ok)
        if not ok:
            return
        src, is_dir = installers.game_source(self.exe)
        size = installers.human_size(installers.size_of(src))
        what = _("la carpeta «{0}»").format(src.name) if is_dir else _("el archivo «{0}»").format(src.name)
        self.move_row.set_subtitle(_("Se moverá {0} ({1}) a {2}, para no perderlo si borras la carpeta original.")
                                   .format(what, size, root))

    def _add(self, *_a):
        name = self.name_row.get_text().strip() or Path(self.exe).stem
        pid = self._prefix_ids[self.prefix_row.get_selected()]
        if pid == "__new__":
            runner = self._runner_names[self.runner_row.get_selected()]
            pid = self.ctl.create_prefix(name, runner).id
        g = self.ctl.add_custom_game(name, self.exe, pid)
        if self.move_row.get_visible() and self.move_row.get_active():
            self.ctl.move_game_files(g.id)
        self.close()
        self.on_added(g)
