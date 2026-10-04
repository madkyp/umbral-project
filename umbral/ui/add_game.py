"""Diálogo «Añadir»: un .exe de Windows, una ROM o imagen de consola, un juego de ScummVM
o un disco de PC. Umbral identifica lo elegido y propone con qué abrirlo."""
from __future__ import annotations

import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .. import engines, installers, library
from ..config import BATTLENET_ID
from ..controller import Controller
from ..i18n import _

NEW_PREFIX = _('Prefijo propio (nuevo, recomendado)')
SUFFIXES = sorted({e.lstrip(".") for e in (engines.WINDOWS_EXTS | engines.DISC_EXTS | engines.VM_EXTS
                                          | set(engines.ROM_EXTS))})


class AddGameDialog(Adw.Dialog):
    def __init__(self, ctl: Controller, on_added):
        super().__init__(title=_('Añadir juego, ROM o programa'), content_width=580)
        self.ctl, self.on_added = ctl, on_added
        self.exe: str = ""
        self.candidates: list[engines.Candidate] = []
        self._auto_name = ""          # nombre propuesto (se sustituye si no lo has cambiado tú)
        self._busy = False

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        # Botones abajo: Cancelar a la izquierda, Añadir a la derecha
        bar = Gtk.Box(spacing=12, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        cancel = Gtk.Button(label=_('Cancelar'), css_classes=["pill"])
        cancel.connect("clicked", lambda *_a: self.close())
        bar.append(cancel)
        bar.append(Gtk.Box(hexpand=True))
        self.add_btn = Gtk.Button(label=_('Añadir'), css_classes=["suggested-action", "pill"], sensitive=False)
        self.add_btn.connect("clicked", self._add)
        bar.append(self.add_btn)
        tv.add_bottom_bar(bar)

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        self.exe_row = Adw.ActionRow(use_markup=False, title=_('Juego'), subtitle=_('Ninguno elegido'))
        pick = Gtk.Button(label=_('Archivo…'), valign=Gtk.Align.CENTER)
        pick.connect("clicked", self._pick)
        folder = Gtk.Button(label=_('Carpeta…'), valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text=_('Una carpeta llena de ROMs (se añaden todas) o un juego de ScummVM'))
        folder.connect("clicked", self._pick_folder)
        self.exe_row.add_suffix(folder)
        self.exe_row.add_suffix(pick)
        g.add(self.exe_row)
        # Con qué se abre (tras identificar lo elegido), justo debajo
        dg = g
        self.engine_row = Adw.ComboRow(title=_('Se abrirá con'), visible=False, use_subtitle=False)
        self.engine_row.connect("notify::selected", lambda *_a: self._sync())
        dg.add(self.engine_row)
        self.busy_row = Adw.ActionRow(use_markup=False, title=_('Identificando…'), visible=False)
        self.busy_row.add_prefix(Adw.Spinner())
        dg.add(self.busy_row)
        self.status_row = Adw.ActionRow(use_markup=False, visible=False, subtitle_selectable=True)
        dg.add(self.status_row)
        self.extract_row = Adw.ActionRow(use_markup=False, visible=False)
        self.extract_btn = Gtk.Button(label=_('Extraer'), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.extract_btn.connect("clicked", self._extract)
        self.extract_row.add_suffix(self.extract_btn)
        dg.add(self.extract_row)
        self.name_row = Adw.EntryRow(title=_('Nombre'))
        self.name_row.connect("changed", lambda *_a: self._name_changed())
        g.add(self.name_row)
        page.add(g)

        # Qué se puede añadir (hasta que eliges algo): Umbral decide con qué abrirlo
        self.kinds_group = Adw.PreferencesGroup(
            title=_('Qué puedes añadir'),
            description=_('Elige el archivo o la carpeta: Umbral reconoce qué es y lo abre con lo que necesite.'))
        for icon, title, sub in (
                ("application-x-executable-symbolic", _('Juegos y programas de Windows'),
                 _('.exe para jugar · .msi o setup.exe para instalar (luego te propone añadir el juego) · .bat')),
                ("input-gaming-symbolic", _('ROMs y discos de consola'),
                 _('Nintendo (NES a Wii, DS, 3DS) · PlayStation 1, 2 y PSP · Sega (Master System a Dreamcast) · recreativas y Neo Geo (MAME) · Atari')),
                ("media-optical-symbolic", _('CD o DVD de PC'),
                 _('.iso: se extrae a la carpeta de juegos y se identifica solo')),
                ("folder-symbolic", _('Aventuras clásicas (ScummVM)'),
                 _('La carpeta del juego, con «Carpeta…» (Monkey Island, La Pantera Rosa, Broken Sword…)'))):
            row = Adw.ActionRow(use_markup=False, title=title, subtitle=sub)
            row.add_prefix(Gtk.Image(icon_name=icon))
            self.kinds_group.add(row)
        page.add(self.kinds_group)

        self.hint = Adw.ActionRow(use_markup=False, visible=False, css_classes=["dim-label"],
                                  title=_('Parece un instalador: cuando termine, Umbral te propondrá añadir el juego que haya instalado.'))
        self.wine_group = Adw.PreferencesGroup()
        self.wine_group.add(self.hint)
        self.move_row = Adw.SwitchRow(use_markup=False, visible=False, active=True,
                                      title=_("Mover a la carpeta de juegos de Umbral"))
        self.wine_group.add(self.move_row)
        page.add(self.wine_group)

        self.prefix_group = Adw.PreferencesGroup(
            title=_('Prefijo de Wine'),
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
        self.prefix_group.add(self.prefix_row)
        names = library.runner_names([r for r in ctl.runners if r.kind == "proton"])
        self._runner_names = names or ["GE-Proton"]
        self.runner_row = Adw.ComboRow(title=_('Proton para el prefijo nuevo'),
                                       model=Gtk.StringList.new(self._runner_names))
        if ctl.cfg.settings.default_runner in self._runner_names:
            self.runner_row.set_selected(self._runner_names.index(ctl.cfg.settings.default_runner))
        self.prefix_group.add(self.runner_row)
        page.add(self.prefix_group)

        tv.set_content(page)
        self.set_child(tv)
        self._sync()

    # ---------------------------------------------------------------- elección
    def _initial_folder(self) -> Gio.File:
        downloads = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or Path.home())
        return Gio.File.new_for_path(str(downloads))

    def _pick(self, *_a):
        dlg = Gtk.FileDialog(title=_('Elegir juego, ROM o ejecutable'))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name=_('Juegos y ROMs compatibles'))
        for s in SUFFIXES:
            f.add_suffix(s)
        filters.append(f)
        allf = Gtk.FileFilter(name=_('Todos los archivos'))
        allf.add_pattern("*")
        filters.append(allf)
        dlg.set_filters(filters)
        dlg.set_default_filter(f)
        dlg.set_initial_folder(self._initial_folder())

        def done(d, res):
            try:
                self._chosen(d.open_finish(res).get_path())
            except GLib.Error:
                pass
        dlg.open(self.get_root(), None, done)

    def _pick_folder(self, *_a):
        dlg = Gtk.FileDialog(title=_('Elegir la carpeta del juego'))
        dlg.set_initial_folder(self._initial_folder())

        def done(d, res):
            try:
                self._chosen(d.select_folder_finish(res).get_path())
            except GLib.Error:
                pass
        dlg.select_folder(self.get_root(), None, done)

    def _chosen(self, path: str):
        self.exe = path
        self.kinds_group.set_visible(False)
        self.exe_row.set_subtitle(path)
        self._identify(path)

    def _identify(self, path: str):
        """Identifica en segundo plano (ScummVM puede tardar un poco)."""
        self._set_busy(True, _('Identificando…'))

        def work():
            found = engines.detect(path)
            GLib.idle_add(lambda: (self._show(found), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        self.busy_row.set_visible(busy)
        if text:
            self.busy_row.set_title(text)
        self.extract_btn.set_sensitive(not busy)
        self._sync()

    def _show(self, found: list[engines.Candidate], note: str = ""):
        self._set_busy(False)
        self.candidates = found
        self.engine_row.set_model(Gtk.StringList.new([c.label for c in found]))
        self.engine_row.set_visible(bool(found))
        self.engine_row.set_selected(0)
        if not found:
            self.status_row.set_title(note or _('No se reconoce: elige un .exe, una ROM, una imagen de disco '
                                                'o la carpeta de un juego de ScummVM.'))
            self.status_row.set_subtitle("")
            self.status_row.set_visible(True)
        self._sync()

    def _name_changed(self):
        c = self._current()
        if c is not None and c.engine == engines.EMULATOR:
            self._sync_rom(c)

    def _current(self) -> engines.Candidate | None:
        i = self.engine_row.get_selected()
        return self.candidates[i] if self.candidates and 0 <= i < len(self.candidates) else None

    def _sync(self):
        c = self._current()
        wine = c is not None and c.engine == "wine"
        self.engine_row.set_subtitle((c.detail or (engines.system_name(c.system) if c.engine == engines.EMULATOR else ""))
                                     if c else "")
        # Nombre propuesto: el que detectó ScummVM, el de la ROM sin etiquetas…
        if c and c.title and self.name_row.get_text().strip() in ("", self._auto_name):
            self._auto_name = c.title
            self.name_row.set_text(c.title)
        rom = c is not None and c.engine == engines.EMULATOR
        bulk = c is not None and c.engine == "bulk"
        self.name_row.set_visible(not bulk)          # cada ROM toma su propio nombre
        self.wine_group.set_visible(wine or rom or bulk)
        self.prefix_group.set_visible(wine)
        new = self._prefix_ids[self.prefix_row.get_selected()] == "__new__"
        self.runner_row.set_visible(new)
        if wine:
            self.hint.set_visible(installers.is_installer(c.path))
            self._sync_move(c.path)
        elif rom:
            self.hint.set_visible(False)
            self._sync_rom(c)
        elif bulk:
            self.hint.set_visible(False)
            self.move_row.set_visible(True)
            self.move_row.set_title(_("Guardar cada una en su carpeta"))
            self.move_row.set_subtitle(_("En {0}/<sistema>/<juego>, como al añadirlas de una en una.")
                                       .format(self.ctl.games_root()))
        # Disco de PC: hay que extraerlo antes de saber qué es
        extract = c is not None and c.engine == "extract"
        self.extract_row.set_visible(extract and not self._busy)
        if extract:
            size = installers.human_size(engines.disc_size(Path(c.path)))
            self.extract_row.set_title(_('Extraer el disco ({0}) a la carpeta de juegos').format(size))
            self.extract_row.set_subtitle(c.detail)
        # Programa que lo abre: instalado o cómo instalarlo
        status = ""
        if c is not None and c.engine in engines.NATIVE_KINDS:
            from ..config import Game
            prog, install = engines.engine_status(Game("", "", c.engine, "", c.path, system=c.system))
            if install:
                status = _('{0} no está instalado. Puedes añadirlo igualmente e instalarlo después: {1}') \
                    .format(prog, install)
            if c.engine == engines.EMULATOR and c.system in engines.SYSTEMS and engines.SYSTEMS[c.system].bios:
                status = (status + "\n" if status else "") + engines.SYSTEMS[c.system].bios
            if c.engine == engines.VM:
                status = _('Las máquinas virtuales aún no se pueden lanzar desde Umbral (próximamente).')
        if bulk:
            missing = []
            for sid in dict.fromkeys(x.system for x in c.extra["roms"]):
                key, base = engines.emulator_for(sid)
                if not base and engines.EMULATORS[key].name not in missing:
                    missing.append(engines.EMULATORS[key].name)
            if missing:
                status = _("Faltan emuladores: {0}. Puedes añadir las ROMs igualmente e instalarlos en "
                           "Sistema → Emuladores.").format(", ".join(missing))
        if c is not None:
            self.status_row.set_title(status)
            self.status_row.set_visible(bool(status))
        self.add_btn.set_sensitive(c is not None and not extract and not self._busy and c.engine != engines.VM)
        self.add_btn.set_label(_("Añadir {0} juegos").format(len(c.extra["roms"])) if bulk else _("Añadir"))

    def _sync_rom(self, c: engines.Candidate):
        """ROMs ordenadas por sistema: Juegos/GBA/Pokémon Zafiro/…"""
        root = self.ctl.games_root()
        ok = Path(c.path).is_file() and not engines.is_organized(c.path, root, c.system)
        self.move_row.set_visible(ok)
        if not ok:
            return
        name = self.name_row.get_text().strip() or c.title
        files = engines.rom_files(Path(c.path))
        size = installers.human_size(sum(installers.size_of(f) for f in files))
        what = _("«{0}»").format(files[0].name) if len(files) == 1 else \
            _("«{0}» y {1} archivos más").format(files[0].name, len(files) - 1)
        dest = engines.rom_folder(root, c.system, name)
        self.move_row.set_title(_("Guardar en su carpeta: {0}").format(dest.relative_to(root)))
        self.move_row.set_subtitle(_("Se moverá {0} ({1}) a {2}. Cada juego en su carpeta, agrupados por sistema.")
                                   .format(what, size, dest))

    def _sync_move(self, exe: str):
        """Explica qué se moverá (la carpeta del juego o solo el archivo) y cuánto ocupa."""
        root = self.ctl.games_root()
        self.move_row.set_title(_("Mover a la carpeta de juegos de Umbral"))
        ok = installers.can_move(exe, root, [p.path for p in self.ctl.cfg.prefixes])
        self.move_row.set_visible(ok)
        if not ok:
            return
        src, is_dir = installers.game_source(exe)
        size = installers.human_size(installers.size_of(src))
        what = _("la carpeta «{0}»").format(src.name) if is_dir else _("el archivo «{0}»").format(src.name)
        self.move_row.set_subtitle(_("Se moverá {0} ({1}) a {2}, para no perderlo si borras la carpeta original.")
                                   .format(what, size, root))

    def _extract(self, *_a):
        c = self._current()
        if c is None:
            return
        name = self.name_row.get_text().strip() or c.title
        self._set_busy(True, _('Extrayendo el disco…'))

        def done(dest, found):
            if dest is None:
                self._show([])
                return
            self.exe = str(dest)
            self.exe_row.set_subtitle(str(dest))
            self._show(found, _('Extraído en {0}, pero no se reconoce el juego: elige su .exe o instalador '
                                'con «Archivo…».').format(dest))
        self.ctl.extract_disc(c.path, name, done)

    def _add(self, *_a):
        c = self._current()
        if c is None:
            return
        if c.engine == "bulk":
            self.ctl.add_roms(c.extra["roms"], self.move_row.get_active())
            self.close()
            return
        name = self.name_row.get_text().strip() or c.title or Path(c.path).stem
        if c.engine in engines.NATIVE_KINDS:
            g = self.ctl.add_native_game(c, name)
            if c.engine == engines.EMULATOR and self.move_row.get_visible() and self.move_row.get_active():
                self.ctl.move_game_files(g.id)
            self.close()
            self.on_added(g)
            return
        pid = self._prefix_ids[self.prefix_row.get_selected()]
        if pid == "__new__":
            runner = self._runner_names[self.runner_row.get_selected()]
            pid = self.ctl.create_prefix(name, runner).id
        g = self.ctl.add_custom_game(name, c.path, pid)
        if self.move_row.get_visible() and self.move_row.get_active():
            self.ctl.move_game_files(g.id)
        self.close()
        self.on_added(g)
