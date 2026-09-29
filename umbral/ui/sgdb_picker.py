"""Galería de SteamGridDB: buscar el juego y elegir portada o logo."""
from __future__ import annotations

import threading
import urllib.request

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import sgdb
from ..controller import Controller
from ..i18n import _

KINDS = [("grids", _("Portadas"), "cover"), ("logos", _("Logos (para el hueco del icono)"), "icon")]


class SGDBPicker(Adw.Dialog):
    def __init__(self, ctl: Controller, game_id: str, on_done=None):
        super().__init__(title=_("Buscar en SteamGridDB"), content_width=760, content_height=620)
        self.ctl, self.game_id, self.on_done = ctl, game_id, on_done
        self.games: list[sgdb.SGDBGame] = []
        self._gen = 0   # descarta resultados de búsquedas anteriores

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16, margin_end=16,
                      margin_top=6, margin_bottom=12)
        row = Gtk.Box(spacing=8)
        g = ctl.cfg.game(game_id)
        self.entry = Gtk.SearchEntry(text=g.name if g else "", hexpand=True,
                                     placeholder_text=_("Nombre del juego"))
        self.entry.connect("activate", lambda *_a: self._search())
        row.append(self.entry)
        self.game_dd = Gtk.DropDown(model=Gtk.StringList.new([]), sensitive=False)
        self.game_dd.connect("notify::selected", lambda *_a: self._load_images())
        row.append(self.game_dd)
        box.append(row)

        self.kind_dd = Gtk.DropDown(model=Gtk.StringList.new([k[1] for k in KINDS]), halign=Gtk.Align.START)
        self.kind_dd.connect("notify::selected", lambda *_a: self._load_images())
        box.append(self.kind_dd)

        self.status = Gtk.Label(xalign=0, css_classes=["dim-label"], wrap=True)
        box.append(self.status)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=2,
                                max_children_per_line=4, column_spacing=10, row_spacing=10,
                                homogeneous=True, valign=Gtk.Align.START)
        box.append(Gtk.ScrolledWindow(child=self.flow, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        if not sgdb.get_key():
            self.status.set_label(_("Falta la clave de SteamGridDB (Sistema → SteamGridDB)."))
        else:
            GLib.idle_add(lambda: (self._search(), False)[1])

    # ---------------------------------------------------------------- búsqueda
    def _search(self):
        term = self.entry.get_text().strip()
        if not term:
            return
        self.status.set_label(_("Buscando «{0}»…").format(term))
        self._clear()

        def work():
            try:
                found = sgdb.search(term)
                err = ""
            except sgdb.SGDBError as e:
                found, err = [], str(e)

            def done():
                self.games = found
                names = [f"{x.name} ({x.year})" if x.year else x.name for x in found]
                self.game_dd.set_model(Gtk.StringList.new(names))
                self.game_dd.set_sensitive(bool(found))
                if err:
                    self.status.set_label(err)
                elif not found:
                    self.status.set_label(_("Sin resultados para «{0}».").format(term))
                else:
                    self.game_dd.set_selected(0)
                    self._load_images()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _clear(self):
        while (c := self.flow.get_first_child()) is not None:
            self.flow.remove(c)

    def _load_images(self):
        i = self.game_dd.get_selected()
        if not (0 <= i < len(self.games)):
            return
        game = self.games[i]
        kind, label, field = KINDS[self.kind_dd.get_selected()]
        self._gen += 1
        gen = self._gen
        self._clear()
        self.status.set_label(_("Cargando {0} de «{1}»…").format(label.lower(), game.name))

        def work():
            try:
                imgs = sgdb.images(game.id, kind)
                err = ""
            except sgdb.SGDBError as e:
                imgs, err = [], str(e)
            GLib.idle_add(lambda: (self._show(gen, imgs, err, field), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _show(self, gen, imgs, err, field):
        if gen != self._gen:
            return
        if err or not imgs:
            self.status.set_label(err or _("No hay imágenes de este tipo para este juego."))
            return
        self.status.set_label(_("Pulsa una imagen para usarla."))
        for img in imgs[:40]:
            btn = Gtk.Button(css_classes=["flat"], tooltip_text=f"{img.width}×{img.height} · {img.style}")
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
            pic.set_size_request(160, 110)
            btn.set_child(pic)
            btn.connect("clicked", lambda _b, im=img: self._choose(im, field))
            self.flow.append(btn)
            threading.Thread(target=self._thumb, args=(gen, img.thumb, pic), daemon=True).start()

    def _thumb(self, gen, url, pic):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "umbral-launcher"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
        except OSError:
            return

        def put():
            if gen == self._gen:
                try:
                    pic.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                except GLib.Error:
                    pass
            return False
        GLib.idle_add(put)

    def _choose(self, img: sgdb.SGDBImage, field: str):
        self.status.set_label(_("Descargando…"))

        def work():
            try:
                path = sgdb.download(img.url, f"{self.game_id}-{field}")
            except sgdb.SGDBError as e:
                msg = str(e)   # «e» deja de existir al salir del except
                GLib.idle_add(lambda: (self.status.set_label(msg), False)[1])
                return

            def done():
                self.ctl.set_cover(self.game_id, str(path), field)
                self.ctl.emit("toast", _("Portada aplicada") if field == "cover" else _("Icono aplicado"))
                if self.on_done:
                    self.on_done()
                self.close()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()
