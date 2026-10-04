"""Ventana principal: biblioteca, sistema y consola de registros."""
from __future__ import annotations

import threading
import zlib
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import APP_NAME, battlenet, engines, exeicon, installers, integration, playtime, prefixes
from ..config import BATTLENET_ID, Game
from ..controller import Controller
from ..launcher import State
from .add_game import AddGameDialog
from .game_settings import GameSettings
from .system_page import SystemPage
from .wizard import SetupWizard
from ..i18n import _

def _fold(text: str) -> str:
    """Minúsculas y sin tildes, para buscar «piramide» y encontrar «Pirámide»."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


CARD_WIDTH = 276   # ancho fijo de todas las tarjetas (el que tenía la de WoW Forever)

CHIP = {State.RUNNING: "running", State.STARTING: "starting", State.STOPPING: "starting",
        State.ERROR: "error", _('Comprobando versión'): "starting", _("Moviendo…"): "starting"}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, ctl: Controller, on_accent):
        super().__init__(application=app, title=APP_NAME, default_width=1180, default_height=780)
        self.add_css_class("umbral")
        self.ctl = ctl
        self.console_key = BATTLENET_ID
        self._keys: list[str] = []
        self.narrow = False
        self._tints: dict[str, str] = {}
        self._search_text = ""           # búsqueda por título en «Mis juegos y programas»
        self._search_open = False
        self._search_entry: Gtk.SearchEntry | None = None
        self._tint_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self._tint_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)

        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        # Con icono + texto: algunos temas (Tela) no pintan list-add-symbolic suelto
        add = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label=_('Añadir')),
                         tooltip_text=_('Añadir un juego o programa de Windows'))
        add.connect("clicked", self._add_game)
        header.pack_start(add)
        self.console_btn = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic",
                                            tooltip_text=_('Registro (Ctrl+L)'))
        header.pack_end(self.console_btn)
        refresh = Gtk.Button(child=Adw.ButtonContent(icon_name="view-refresh-symbolic", label=_('Actualizar')),
                             tooltip_text=_('Volver a detectar juegos instalados, runners y GPU (F5)'))
        refresh.connect("clicked", lambda *_a: self._refresh())
        header.pack_end(refresh)
        tv.add_top_bar(header)

        self.library_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26,
                                   margin_top=24, margin_bottom=32, margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=1240, child=self.library_box)
        self.stack.add_titled_with_icon(Gtk.ScrolledWindow(child=clamp, vexpand=True),
                                        "library", _('Biblioteca'), "applications-games-symbolic")
        self.system_page = SystemPage(ctl, on_accent)
        self.stack.add_titled_with_icon(self.system_page, "system", _('Sistema'), "preferences-system-symbolic")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.stack)
        body.append(self._console())
        tv.set_content(body)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        # Ventanas estrechas (tiling): el banner apila los botones bajo el texto
        bp = Adw.Breakpoint(condition=Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.connect("apply", lambda *_a: self._set_narrow(True))
        bp.connect("unapply", lambda *_a: self._set_narrow(False))
        self.add_breakpoint(bp)

        self._actions()
        ctl.connect(self._on_event)
        # Cerrar la ventana = seguir en segundo plano con el icono de la bandeja
        self.connect("close-request", self._on_close)
        self.rebuild_library()
        GLib.timeout_add_seconds(20, self._periodic)
        # Al volver a la ventana (p. ej. tras instalar o borrar un juego) se revisa la biblioteca
        self.connect("notify::is-active", lambda *_a: self.is_active() and self.ctl.sync_library())
        GLib.timeout_add_seconds(3, self._scan_external)

    # ---------------------------------------------------------------- eventos
    def _on_event(self, ev: str, *a):
        if ev in ("library", "system", "icons"):
            self.rebuild_library()
        elif ev == "state":
            self.rebuild_library()
            if a[1] == State.ERROR:
                self._show_console(a[0])
        elif ev == "log":
            key, line = a
            if key == self.console_key:
                self._append_console(line)
            if key not in self._keys:
                self._refresh_keys()
        elif ev == "error":
            self.toasts.add_toast(Adw.Toast(title=a[0], timeout=6, priority=Adw.ToastPriority.HIGH))
        elif ev == "toast":
            self.toasts.add_toast(Adw.Toast(title=a[0]))
        elif ev == "show_log":
            self._show_console(a[0])
        elif ev == "game_started":
            self._minimize_for_game()
        elif ev == "installed_candidates":
            self._installed_dialog(*a)


    def _set_narrow(self, narrow: bool):
        if narrow != self.narrow:
            self.narrow = narrow
            self.rebuild_library()

    def _scan_external(self):
        if self.ctl.scan_external():
            self.rebuild_library()
        return True

    def _minimize_for_game(self):
        """Al lanzar un juego: a la bandeja si la hay (Hyprland no tiene «minimizar»);
        si no, se pide al escritorio que minimice la ventana."""
        if not self.ctl.cfg.settings.minimize_on_launch or not self.is_visible():
            return
        if self.get_application().in_tray():
            self.set_visible(False)
        else:
            self.minimize()

    def _on_close(self, *_a):
        app = self.get_application()
        if app.in_tray():
            self.set_visible(False)
            if not getattr(self, "_tray_hint_shown", False):
                from .. import integration
                integration.notify(_('Umbral sigue en segundo plano'),
                                   _('Ábrelo desde su icono de la bandeja; «Salir» en su menú lo cierra del todo.'))
                self._tray_hint_shown = True
            return True   # no destruir: la app sigue viva
        return False

    def _refresh(self):
        self.ctl.refresh_all()
        self.toasts.add_toast(Adw.Toast(title=_('Biblioteca actualizada'), timeout=2))

    def _periodic(self):
        if self.is_visible():
            self.ctl.sync_library()
        return True

    # ---------------------------------------------------------------- biblioteca
    def rebuild_library(self):
        while (c := self.library_box.get_first_child()) is not None:
            self.library_box.remove(c)
        self._search_entry = None            # se recrea con «Mis juegos y programas»
        if not self.ctl.battlenet_ready() and not self.ctl.is_running(BATTLENET_ID):
            self.library_box.append(self._setup_prompt())
        else:
            self.library_box.append(self._hero())
        games = [g for g in self.ctl.cfg.games if g.id != BATTLENET_ID and not g.hidden]
        blizzard = sorted((g for g in games if g.kind == "blizzard"),
                          key=lambda g: (not g.product.startswith("wow"), g.name.lower()))
        mine = self._sorted([g for g in games if g.kind != "blizzard"])
        # Cada biblioteca solo aparece si tiene algo
        self._section(_('Biblioteca Battle.net'), blizzard)
        self._section(_('Mis juegos y programas'), mine, filters=True)

    SORTS = {"name": _("Nombre"), "recent": _("Jugado recientemente"), "playtime": _("Más horas")}

    def _sorted(self, games: list[Game]) -> list[Game]:
        how = self.ctl.cfg.settings.library_sort
        by_name = sorted(games, key=lambda g: g.name.lower())
        if how == "recent":     # los nunca jugados, al final y por nombre
            return sorted(by_name, key=lambda g: g.last_played or "", reverse=True)
        if how == "playtime":
            return sorted(by_name, key=lambda g: g.playtime, reverse=True)
        return by_name

    @staticmethod
    def _category(g: Game) -> str:
        """Filtro al que pertenece un juego: windows, scummvm, sys:<sistema> u other."""
        if g.kind in ("custom", "battlenet"):
            return "windows"
        if g.kind == engines.SCUMMVM:
            return "scummvm"
        if g.kind == engines.EMULATOR and g.system in engines.SYSTEMS:
            return f"sys:{g.system}"
        return "other"

    def _filters(self, games: list[Game]) -> list[tuple[str, str, int]]:
        """(clave, etiqueta, nº de juegos) de los filtros con algo, en orden fijo."""
        counts: dict[str, int] = {}
        for g in games:
            counts[self._category(g)] = counts.get(self._category(g), 0) + 1
        order = [("windows", "Windows"), ("scummvm", "ScummVM")]
        order += [(f"sys:{sid}", s.short) for sid, s in engines.SYSTEMS.items()]
        order.append(("other", _("Otros")))
        return [("all", _("Todos"), len(games))] + [(k, label, counts[k]) for k, label in order if k in counts]

    def _section(self, title: str, games: list[Game], filters: bool = False):
        if not games:
            return
        if not filters:
            self.library_box.append(Gtk.Label(label=title, xalign=0, css_classes=["title-2"]))
        else:
            # Título con lupa: la búsqueda por nombre aparece a su lado
            head = Gtk.Box(spacing=12)
            head.append(Gtk.Label(label=title, xalign=0, css_classes=["title-2"]))
            lupa = Gtk.ToggleButton(icon_name="system-search-symbolic", css_classes=["flat", "circular"],
                                    valign=Gtk.Align.CENTER, active=self._search_open,
                                    tooltip_text=_("Buscar por título (Ctrl+F)"))
            entry = Gtk.SearchEntry(placeholder_text=_("Buscar por título…"), width_request=280,
                                    valign=Gtk.Align.CENTER, text=self._search_text)
            rev = Gtk.Revealer(child=entry, reveal_child=self._search_open, transition_duration=180,
                               transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT)
            head.append(lupa)
            head.append(rev)
            self.library_box.append(head)
            self._search_entry, self._search_toggle, self._search_rev = entry, lupa, rev
            lupa.connect("toggled", lambda b: self._show_search(b.get_active()))
            entry.connect("stop-search", lambda *_a: self._show_search(False))
        if filters:
            options = self._filters(games)
            current = self.ctl.cfg.settings.library_filter
            if current not in {k for k, _l, _n in options}:
                current = "all"
            tools = Gtk.Box(spacing=12)
            if len(options) > 2:          # con una sola categoría no hace falta filtrar
                bar = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                                  max_children_per_line=30, halign=Gtk.Align.START, hexpand=True,
                                  valign=Gtk.Align.CENTER)
                first = None
                for key, label, n in options:
                    b = Gtk.ToggleButton(css_classes=["filter-chip"], active=key == current, group=first)
                    first = first or b
                    row = Gtk.Box(spacing=6)
                    row.append(Gtk.Label(label=label))
                    row.append(Gtk.Label(label=str(n), css_classes=["filter-count"]))
                    b.set_child(row)
                    b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_filter(k))
                    bar.append(b)
                tools.append(bar)
            else:
                tools.append(Gtk.Box(hexpand=True))
            keys = list(self.SORTS)
            sort = Gtk.DropDown(model=Gtk.StringList.new(list(self.SORTS.values())), valign=Gtk.Align.CENTER,
                                selected=keys.index(self.ctl.cfg.settings.library_sort)
                                if self.ctl.cfg.settings.library_sort in keys else 0,
                                tooltip_text=_("Ordenar"), css_classes=["flat"])
            sort.connect("notify::selected", lambda d, *_a: self._set_sort(keys[d.get_selected()]))
            sort_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            sort_box.append(Gtk.Image(icon_name="view-sort-descending-symbolic", css_classes=["dim-label"]))
            sort_box.append(sort)
            tools.append(sort_box)
            self.library_box.append(tools)
            if current != "all":
                games = [g for g in games if self._category(g) == current]
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, halign=Gtk.Align.START,
                           column_spacing=18, row_spacing=18, min_children_per_line=1,
                           max_children_per_line=6)
        for g in games:
            flow.append(self._card(g))
            flow.get_last_child().set_name(_fold(g.name))     # para la búsqueda
        self.library_box.append(flow)
        if filters:
            self._search_flow = flow
            self._no_match = Gtk.Label(css_classes=["dim-label"], xalign=0, visible=False)
            self.library_box.append(self._no_match)
            flow.set_filter_func(lambda child: _fold(self._search_text) in child.get_name())
            self._search_entry.connect("search-changed", self._on_search)
            self._update_no_match()
            if self._search_open:
                GLib.idle_add(lambda: (self._search_entry.grab_focus(),
                                       self._search_entry.set_position(-1), False)[-1])

    def _show_search(self, show: bool):
        """Lupa: abre el buscador junto al título; al cerrarlo se borra la búsqueda."""
        self._search_open = show
        if self._search_toggle.get_active() != show:
            self._search_toggle.set_active(show)
        self._search_rev.set_reveal_child(show)
        if show:
            self._search_entry.grab_focus()
        elif self._search_entry.get_text():
            self._search_entry.set_text("")          # dispara search-changed y vuelve a mostrar todo

    def _on_search(self, entry: Gtk.SearchEntry):
        self._search_text = entry.get_text().strip()
        self._search_flow.invalidate_filter()
        self._update_no_match()

    def _update_no_match(self):
        q = _fold(self._search_text)
        any_match = False
        child = self._search_flow.get_first_child()
        while child is not None:
            any_match = any_match or q in child.get_name()
            child = child.get_next_sibling()
        self._no_match.set_label(_("Ningún juego coincide con «{0}».").format(self._search_text))
        self._no_match.set_visible(bool(q) and not any_match)

    def _set_sort(self, key: str):
        if self.ctl.cfg.settings.library_sort == key:
            return
        self.ctl.cfg.settings.library_sort = key
        self._set_filter(self.ctl.cfg.settings.library_filter, force=True)

    def _set_filter(self, key: str, force: bool = False):
        if self.ctl.cfg.settings.library_filter == key and not force:
            return
        self.ctl.cfg.settings.library_filter = key
        self.ctl.save()
        adj = self.library_box.get_ancestor(Gtk.ScrolledWindow).get_vadjustment()
        pos = adj.get_value()

        def rebuild():                       # fuera del manejador del botón, sin saltar arriba
            self.rebuild_library()
            GLib.idle_add(lambda: (adj.set_value(min(pos, adj.get_upper() - adj.get_page_size())), False)[1])
            return False
        GLib.idle_add(rebuild)

    def _hero_box(self) -> tuple[Gtk.Box, Gtk.Box]:
        """(banner, fila superior). En estrecho los botones van en una segunda fila."""
        hero = Gtk.Box(spacing=24, css_classes=["hero"],
                       orientation=Gtk.Orientation.VERTICAL if self.narrow else Gtk.Orientation.HORIZONTAL)
        top = Gtk.Box(spacing=20 if self.narrow else 24, hexpand=True)
        hero.append(top)
        return hero, top

    def _tint_class(self, color: str) -> str:
        """Clase CSS con un degradado del color del icono hacia el fondo oscuro."""
        cls = "tint-" + color.lstrip("#")
        if cls not in self._tints:
            self._tints[cls] = (
                f".cover.{cls} {{ background-image: radial-gradient(circle at 50% 38%, "
                f"alpha({color}, 0.55), transparent 62%), linear-gradient(160deg, "
                f"color-mix(in srgb, {color} 35%, #10131c), #0b0e15); }}")
            self._tint_css.load_from_string("\n".join(self._tints.values()))
        return cls

    def _cover_class(self, image: str, color: str) -> str:
        """Clase CSS con la imagen como fondo encajado (contain) sobre su color."""
        uri = Gio.File.new_for_path(image).get_uri()
        cls = "cvr-" + format(zlib.crc32(f"{uri}|{color}".encode()), "x")
        if cls not in self._tints:
            self._tints[cls] = (
                f'.cover.{cls} {{ background-image: url("{uri}"), radial-gradient(circle at 50% 50%, '
                f"alpha({color}, 0.45), transparent 70%), linear-gradient(160deg, "
                f"color-mix(in srgb, {color} 35%, #10131c), #0b0e15); "
                "background-size: contain, auto, auto; background-repeat: no-repeat; "
                "background-position: center; }")
            self._tint_css.load_from_string("\n".join(self._tints.values()))
        return cls

    def _cover(self, g: Game) -> Gtk.Widget:
        """Portada: la imagen elegida (portada o icono) o la del juego, siempre en el hueco
        del logo y sobre su color, para que todas las tarjetas midan lo mismo."""
        cover = Gtk.Overlay(css_classes=["cover"], overflow=Gtk.Overflow.HIDDEN)
        if g.cover and Path(g.cover).exists():
            # Portada entera (sin recorte ni zoom) de borde a borde: fondo CSS con «contain»,
            # que no cambia el tamaño de la tarjeta; los huecos llevan el color de la imagen.
            try:
                color = exeicon.dominant_color(Path(g.cover))
            except OSError:
                color = "#2a3350"
            cover.add_css_class(self._cover_class(g.cover, color))
            cover.set_child(Gtk.Box())
            return cover
        icon = self.ctl.game_icon(g)
        if icon and icon[0]:
            png, color = icon
            cover.add_css_class(self._tint_class(color))
            # Gtk.Image con pixel_size: tamaño fijo, no se estira con la tarjeta
            img = Gtk.Image.new_from_file(str(png))
            img.set_pixel_size(120)
            img.set_halign(Gtk.Align.CENTER)
            img.set_valign(Gtk.Align.CENTER)
            cover.set_child(img)
        else:
            tone = "tone-wow" if g.product.startswith("wow") else f"tone-{zlib.crc32(g.id.encode()) % 5}"
            cover.add_css_class(tone)
            name = {"blizzard": "applications-games-symbolic", engines.EMULATOR: "input-gaming-symbolic",
                    engines.SCUMMVM: "media-optical-symbolic", engines.VM: "computer-symbolic"}.get(
                        g.kind, "application-x-executable-symbolic")
            cover.set_child(Gtk.Image(icon_name=name, pixel_size=72, css_classes=["cover-icon"],
                                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER))
        return cover

    def _setup_prompt(self) -> Gtk.Widget:
        # Adw.StatusPage lleva su propio scroll y se colapsa dentro de otro: caja propia.
        hero, top = self._hero_box()
        top.append(Gtk.Image(icon_name="system-software-install-symbolic", pixel_size=64,
                              css_classes=["hero-icon"], valign=Gtk.Align.CENTER))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        text.append(Gtk.Label(label=_('Configura Battle.net'), xalign=0, wrap=True, css_classes=["hero-title"]))
        text.append(Gtk.Label(label=_('Instala el cliente oficial en un prefijo de Proton o importa uno que ya tengas (Faugus, Lutris…).'), xalign=0, wrap=True, css_classes=["hero-sub"]))
        top.append(text)
        b = Gtk.Button(label=_('Empezar'), css_classes=["suggested-action", "pill-play"], valign=Gtk.Align.CENTER,
                       halign=Gtk.Align.END)
        b.connect("clicked", lambda *_a: SetupWizard(self.ctl).present(self))
        hero.append(b)
        return hero

    def _hero(self) -> Gtk.Widget:
        pfx = self.ctl.cfg.battlenet_prefix()
        running = self.ctl.is_running(BATTLENET_ID)
        st = self.ctl.state(BATTLENET_ID)
        hero, top = self._hero_box()
        client = battlenet.client_exe(Path(pfx.path))
        icon = self.ctl.icon(str(client), "battlenet") if client else None
        if icon and icon[0]:
            logo = Gtk.Image.new_from_file(str(icon[0]))
            logo.set_pixel_size(80)
            logo.set_valign(Gtk.Align.CENTER)
            top.append(logo)
        else:
            top.append(Gtk.Image(icon_name="network-server-symbolic", pixel_size=64,
                                  css_classes=["hero-icon"], valign=Gtk.Align.CENTER))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        row = Gtk.Box(spacing=10)
        row.append(Gtk.Label(label="Battle.net", xalign=0, css_classes=["hero-title"]))
        if st and st != State.EXITED:
            row.append(Gtk.Label(label=st, css_classes=["status-chip", CHIP.get(st, "")], valign=Gtk.Align.CENTER))
        text.append(row)
        sub = f"{pfx.runner} · {pfx.path}" + (" · importado" if pfx.imported else "")
        text.append(Gtk.Label(label=sub, xalign=0, css_classes=["hero-sub"], ellipsize=3))
        top.append(text)

        btns = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        main = Gtk.Button(label=_('Cerrar Battle.net') if running else _('Abrir Battle.net'),
                          css_classes=["pill-play"] + (["destructive-action"] if running else ["suggested-action"]))
        main.connect("clicked", lambda *_a: self.ctl.stop(BATTLENET_ID) if running
                     else self.ctl.launch_game(BATTLENET_ID))
        btns.append(main)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text=_('Ajustes del prefijo'),
                          css_classes=["circular"], valign=Gtk.Align.CENTER)
        gear.connect("clicked", lambda *_a: GameSettings(self.ctl, self.ctl.cfg.game(BATTLENET_ID)).present(self))
        btns.append(gear)
        btns.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self._repair_menu(pfx.imported),
                                   tooltip_text=_('Reparar y herramientas'), css_classes=["circular"],
                                   valign=Gtk.Align.CENTER))
        hero.append(btns)
        return hero

    def _repair_menu(self, imported: bool) -> Gio.Menu:
        m = Gio.Menu()
        s1 = Gio.Menu()
        s1.append(_('Ver registro'), "win.bnet-log")
        s1.append(_('Abrir carpeta del prefijo'), "win.bnet-folder")
        s1.append(_('Configuración de Wine (winecfg)'), "win.bnet-winecfg")
        s1.append(_('Crear acceso directo (.desktop)'), "win.shortcut::battlenet")
        m.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append(_('Limpiar caché del cliente'), "win.bnet-clear-cache")
        s2.append(_('Reiniciar Agent (error «se ha dormido»)'), "win.bnet-reset-agent")
        s2.append(_('Relanzar instalador de Battle.net'), "win.bnet-reinstall")
        s2.append(_('Instalar dependencias opcionales (winetricks)'), "win.bnet-deps")
        s2.append(_('Forzar cierre del prefijo (wineserver -k)'), "win.bnet-kill")
        s2.append(_('Restaurar la última copia del prefijo…'), "win.bnet-restore")
        m.append_section(_("Reparar"), s2)
        if not imported:
            s3 = Gio.Menu()
            s3.append(_('Recrear prefijo (conserva los juegos)…'), "win.bnet-recreate")
            m.append_section(None, s3)
        return m

    def _card(self, g: Game) -> Gtk.Widget:
        running = self.ctl.is_running(g.id)
        st = self.ctl.state(g.id)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["game-card"], width_request=CARD_WIDTH)
        cover = self._cover(g)
        badge_txt = {"blizzard": "Battle.net"}.get(g.kind, "")
        if g.kind == "custom":
            badge_txt = _('Prefijo Battle.net') if g.prefix_id == BATTLENET_ID else \
                (_("Instalador") if installers.is_installer(g.exe) else _('Prefijo propio'))
        if g.kind in engines.NATIVE_KINDS:
            badge_txt = engines.short_label(g)
        if g.product == "wow_classic_beta":
            badge_txt = "Beta"
        if badge_txt:
            cover.add_overlay(Gtk.Label(label=badge_txt, css_classes=["cover-badge"],
                                        halign=Gtk.Align.START, valign=Gtk.Align.START))
        card.append(cover)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=14,
                       margin_start=14, margin_end=14)
        # max_width_chars=1 + ellipsize: el nombre no ensancha la tarjeta (todas miden CARD_WIDTH)
        info.append(Gtk.Label(label=g.name, xalign=0, css_classes=["card-title"], ellipsize=3,
                              max_width_chars=1, hexpand=True, tooltip_text=g.name))
        meta = Gtk.Box(spacing=6)
        prefix = self.ctl.cfg.prefix(g.prefix_id)
        runner = g.options.runner or (prefix.runner if prefix else "?")
        if g.kind in engines.NATIVE_KINDS:
            prog, install = engines.engine_status(g)
            runner = prog if not install else _("{0} (no instalado)").format(prog)
        played = playtime.summary(g.playtime, g.last_played)
        meta.append(Gtk.Label(label=played or runner, xalign=0, tooltip_text=runner,
                              css_classes=["caption", "dim-label"], ellipsize=3, hexpand=True))
        if st and st != State.EXITED:
            meta.append(Gtk.Label(label=st, css_classes=["status-chip", CHIP.get(st, "")]))
        info.append(meta)

        actions = Gtk.Box(spacing=6)
        play = Gtk.Button(css_classes=["pill-play"] + (["destructive-action"] if running else ["suggested-action"]),
                          hexpand=True)
        play.set_child(Adw.ButtonContent(icon_name="media-playback-stop-symbolic" if running
                                         else "media-playback-start-symbolic",
                                         label=_("Detener") if running else _("Jugar")))
        if g.id in self.ctl.external:
            play.set_tooltip_text(_("Abierto a través de Battle.net: «Detener» cierra solo el juego"))
        play.connect("clicked", lambda *_a: self.ctl.stop(g.id) if running else self.ctl.launch_game(g.id))
        actions.append(play)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", css_classes=["flat", "circular"],
                          valign=Gtk.Align.CENTER, tooltip_text=_('Ajustes'))
        gear.connect("clicked", lambda *_a: GameSettings(self.ctl, g).present(self))
        actions.append(gear)
        m = Gio.Menu()
        m.append(_("Abrir carpeta del juego"), f"win.game-folder::{g.id}")
        m.append(_('Ver registro'), f"win.game-log::{g.id}")
        m.append(_('Crear acceso directo'), f"win.shortcut::{g.id}")
        m.append(_('Cambiar imagen de portada…'), f"win.cover-pick::{g.id}")
        if g.cover:
            m.append(_('Quitar imagen de portada'), f"win.cover-clear::{g.id}")
        m.append(_('Cambiar icono…'), f"win.icon-pick::{g.id}")
        if g.icon:
            m.append(_('Restaurar icono original'), f"win.icon-clear::{g.id}")
        if self.ctl.can_move(g):
            m.append(_("Mover a la carpeta de juegos"), f"win.game-move::{g.id}")
        if self.ctl.has_wtf(g):
            m.append(_("Copia de la configuración (WTF)"), f"win.wtf-backup::{g.id}")
            m.append(_("Restaurar configuración (WTF)…"), f"win.wtf-restore::{g.id}")
        m.append(_('Quitar de la biblioteca'), f"win.game-remove::{g.id}")
        actions.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=m,
                                      css_classes=["flat", "circular"], valign=Gtk.Align.CENTER))
        info.append(actions)
        card.append(info)
        return card


    # ---------------------------------------------------------------- consola
    def _console(self) -> Gtk.Widget:
        self.console_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, css_classes=["console-bar"])
        bar.append(Gtk.Image(icon_name="utilities-terminal-symbolic"))
        self.console_dd = Gtk.DropDown(model=Gtk.StringList.new([]))
        self.console_dd.connect("notify::selected", self._console_selected)
        bar.append(self.console_dd)
        bar.append(Gtk.Box(hexpand=True))
        for icon, tip, cb in (("edit-copy-symbolic", _("Copiar"), self._copy_console),
                              ("document-save-symbolic", _('Abrir archivo de registro'), self._open_logfile),
                              ("user-trash-symbolic", _("Limpiar"), self._clear_console)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", cb)
            bar.append(b)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Cerrar el registro (Esc)"),
                           css_classes=["flat", "circular"])
        close.connect("clicked", lambda *_a: self.console_btn.set_active(False))
        bar.append(close)
        box.append(bar)
        self.console_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                         wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["console"])
        self.console_scroll = Gtk.ScrolledWindow(child=self.console_view, height_request=230)
        box.append(self.console_scroll)
        self.console_rev.set_child(box)
        self.console_btn.bind_property("active", self.console_rev, "reveal-child",
                                       GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        return self.console_rev

    def _refresh_keys(self):
        self._keys = sorted(self.ctl.logs.keys(), key=lambda k: (k != self.console_key, k))
        names = []
        for k in self._keys:
            g = self.ctl.cfg.game(k)
            names.append(g.name if g else k)
        self.console_dd.set_model(Gtk.StringList.new(names))
        if self.console_key in self._keys:
            self.console_dd.set_selected(self._keys.index(self.console_key))

    def _console_selected(self, *_a):
        i = self.console_dd.get_selected()
        if 0 <= i < len(self._keys):
            self.console_key = self._keys[i]
            self.console_view.get_buffer().set_text("\n".join(self.ctl.logs.get(self.console_key, [])))
            self._scroll_end()

    def _append_console(self, line: str):
        buf = self.console_view.get_buffer()
        buf.insert(buf.get_end_iter(), ("\n" if buf.get_char_count() else "") + line)
        self._scroll_end()

    def _scroll_end(self):
        adj = self.console_scroll.get_vadjustment()
        GLib.idle_add(lambda: (adj.set_value(adj.get_upper()), False)[1])

    def _toggle_console(self):
        if self.console_btn.get_active():
            self.console_btn.set_active(False)
        else:
            self._show_console(self.console_key)

    def _esc_console(self, *_a):
        """Esc cierra el registro si está abierto (si no, deja pasar la tecla)."""
        if self.console_btn.get_active():
            self.console_btn.set_active(False)
            return True
        if self._search_open and self._search_entry is not None:
            self._show_search(False)
            return True
        return False

    def _show_console(self, key: str):
        self.console_key = key
        self._refresh_keys()
        self._console_selected()
        self.console_btn.set_active(True)

    def _copy_console(self, *_a):
        Gdk.Display.get_default().get_clipboard().set("\n".join(self.ctl.logs.get(self.console_key, [])))
        self.toasts.add_toast(Adw.Toast(title=_('Registro copiado')))

    def _open_logfile(self, *_a):
        p = self.ctl.procs.get(self.console_key)
        if p and p.log_path.exists():
            Gtk.FileLauncher(file=Gio.File.new_for_path(str(p.log_path))).launch(self, None, None)

    def _clear_console(self, *_a):
        self.ctl.logs[self.console_key] = []
        self.console_view.get_buffer().set_text("")

    # ---------------------------------------------------------------- acciones
    def _actions(self):
        def act(name, cb, param=False):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s") if param else None)
            a.connect("activate", lambda _a, p: cb(p.get_string() if p else None))
            self.add_action(a)
        pfx = lambda: self.ctl.cfg.battlenet_prefix()  # noqa: E731
        act("bnet-log", lambda _a: self._show_console(BATTLENET_ID))
        act("toggle-log", lambda _a: self._toggle_console())
        act("game-log", self._show_console, True)
        act("bnet-folder", lambda _a: Gtk.FileLauncher(file=Gio.File.new_for_path(pfx().path)).launch(self, None, None))
        act("bnet-winecfg", lambda _a: self.ctl.run("winecfg", BATTLENET_ID, "winecfg", notify_user=False)
            and self._show_console("winecfg"))
        act("bnet-clear-cache", self._clear_cache)
        act("bnet-reset-agent", self._reset_agent)
        act("bnet-reinstall", self._reinstall)
        act("bnet-deps", self._deps)
        act("bnet-kill", lambda _a: (self.ctl.kill_prefix(BATTLENET_ID),
                                    self.toasts.add_toast(Adw.Toast(title=_('wineserver -k enviado')))))
        act("bnet-recreate", self._recreate)
        act("bnet-restore", self._restore)
        act("shortcut", self._shortcut, True)
        act("game-remove", self._remove_game, True)
        act("game-move", self._move_game, True)
        act("game-folder", self._open_game_folder, True)
        act("wtf-backup", lambda gid: self.ctl.backup_wtf(gid), True)
        act("wtf-restore", self._restore_wtf, True)
        act("refresh", lambda _a: self._refresh())
        self.get_application().set_accels_for_action("win.refresh", ["F5"])
        act("cover-pick", self._pick_cover, True)
        act("cover-clear", lambda gid: self.ctl.set_cover(gid, None), True)
        act("icon-pick", lambda gid: self._pick_cover(gid, "icon"), True)
        act("icon-clear", lambda gid: self.ctl.set_cover(gid, None, "icon"), True)
        self.get_application().set_accels_for_action("win.toggle-log", ["<Control>l"])
        act("search", lambda _a: self._search_entry is not None and self._show_search(True))
        self.get_application().set_accels_for_action("win.search", ["<Control>f"])
        esc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        esc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                      action=Gtk.CallbackAction.new(self._esc_console)))
        self.add_controller(esc)

    def _need_stopped(self) -> bool:
        if self.ctl.running_in_prefix(BATTLENET_ID):
            self.ctl.error(_('Cierra Battle.net y los juegos antes de reparar el prefijo.'))
            return False
        return True

    def _clear_cache(self, _a):
        if self._need_stopped():
            removed = prefixes.clear_client_cache(Path(self.ctl.cfg.battlenet_prefix().path))
            self.toasts.add_toast(Adw.Toast(title=_('Caché limpiada ({0} carpetas)').format(len(removed)) if removed
                                            else _('No había caché que limpiar')))

    def _reset_agent(self, _a):
        if self._need_stopped():
            b = prefixes.reset_agent(Path(self.ctl.cfg.battlenet_prefix().path))
            self.toasts.add_toast(Adw.Toast(title=_('Agent reiniciado (copia en {0})').format(b.name) if b
                                            else _('No existe la carpeta Agent')))

    def _reinstall(self, _a):
        if not self._need_stopped():
            return

        def work():
            try:
                setup = battlenet.download_installer()
            except battlenet.BattleNetError as e:
                return self.ctl.error(str(e))
            GLib.idle_add(lambda: (self.ctl.run(BATTLENET_ID, BATTLENET_ID, str(setup)), False)[1])
        threading.Thread(target=work, daemon=True).start()
        self._show_console(BATTLENET_ID)

    def _deps(self, _a):
        dlg = Adw.AlertDialog(heading=_('Dependencias opcionales'),
                              body=_('Battle.net funciona sin ellas con Proton (verificado). Instálalas solo si ves fuentes rotas o errores de runtime de Visual C++.\n\nSe ejecutará: umu-run winetricks -q corefonts vcrun2022'))
        dlg.add_response("cancel", _('Cancelar'))
        dlg.add_response("go", _('Instalar'))
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "go" and self._need_stopped()
                    and self.ctl.run_winetricks(BATTLENET_ID, ["corefonts", "vcrun2022"]))
        dlg.present(self)

    def _recreate(self, _a):
        if not self._need_stopped():
            return
        pfx = self.ctl.cfg.battlenet_prefix()
        dlg = Adw.AlertDialog(heading=_('¿Recrear el prefijo?'),
                              body=_('Se apartarán los juegos instalados en {0}, se borrará el resto del prefijo (incluida la sesión de Battle.net) y se volverá a instalar el cliente. Los juegos se devuelven a su sitio antes de abrir Battle.net.').format(pfx.path))
        dlg.add_response("cancel", _('Cancelar'))
        dlg.add_response("go", _('Recrear'))
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r != "go":
                return
            try:
                stash = prefixes.delete_for_recreate(Path(pfx.path), pfx.imported)
            except (prefixes.PrefixError, OSError) as e:
                return self.ctl.error(str(e))
            SetupWizard(self.ctl, recreate_stash=stash).present(self)
        dlg.connect("response", resp)
        dlg.present(self)

    def _restore(self, _a):
        if not self._need_stopped():
            return
        pfx = self.ctl.cfg.battlenet_prefix()
        path = Path(pfx.path)
        archive = prefixes.latest_backup(path)
        if archive is None:
            self.ctl.error(_('Aún no hay copias: se crean solas al cambiar el Proton del prefijo.'))
            return
        dlg = Adw.AlertDialog(heading=_('¿Restaurar la copia del prefijo?'),
                              body=_('Se restaurará «{0}» (Windows, registro y Battle.net; los juegos no están en la copia y no se tocan). Umbral volverá a usar el Proton de esa copia.').format(archive.name))
        dlg.add_response("cancel", _('Cancelar'))
        dlg.add_response("go", _('Restaurar'))
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r != "go":
                return

            def work():
                try:
                    prefixes.restore_backup(path, archive)
                except prefixes.PrefixError as e:
                    return self.ctl.error(str(e))
                runner = prefixes.matching_runner(path, self.ctl.runners)
                if runner:
                    pfx.runner = pfx.last_runner = runner.name
                self.ctl.save()
                self.ctl.emit("toast", _('Copia restaurada ({0})').format(prefixes.proton_version(path)))
                self.ctl.emit("library")
            threading.Thread(target=work, daemon=True).start()
        dlg.connect("response", resp)
        dlg.present(self)

    def _shortcut(self, gid: str):
        g = self.ctl.cfg.game(gid)
        if g:
            f = integration.write_game_desktop(g.id, g.name)
            self.toasts.add_toast(Adw.Toast(title=_('Acceso directo creado: {0}').format(f.name)))

    def _open_game_folder(self, gid: str):
        """Abre en el gestor de archivos la carpeta del juego (en WoW, la del cliente:
        Interface/AddOns y WTF están ahí)."""
        g = self.ctl.cfg.game(gid)
        if g is None or not g.exe:
            return
        folder = Path(g.exe) if Path(g.exe).is_dir() else installers.game_folder(g.exe)
        if not folder.is_dir():
            self.ctl.error(_("La carpeta del juego no existe: {0}").format(folder))
            return
        Gtk.FileLauncher(file=Gio.File.new_for_path(str(folder))).launch(self, None, None)

    def _restore_wtf(self, gid: str):
        """Lista las copias de WTF del juego y restaura la elegida."""
        from .. import wowconfig
        g = self.ctl.cfg.game(gid)
        backups = wowconfig.list_wtf_backups(gid)
        if g is None:
            return
        if not backups:
            self.ctl.error(_("Aún no hay copias. Se crean solas al cerrar el juego, o desde «Copia de la configuración»."))
            return
        dlg = Adw.Dialog(title=_("Restaurar configuración de {0}").format(g.name), content_width=520,
                         content_height=460)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(description=_("Se sustituye la carpeta WTF (ajustes, macros, barras y datos de "
                                                 "addons). La configuración actual se guarda antes como copia."))
        for b in backups:
            stamp = b.name.split(".")[0]
            date = f"{stamp[6:8]}/{stamp[4:6]}/{stamp[0:4]} {stamp[9:11]}:{stamp[11:13]}"
            kind = _("automática") if "-auto" in b.name else (_("antes de restaurar") if "antes" in b.name
                                                              else _("manual"))
            row = Adw.ActionRow(use_markup=False, title=date,
                                subtitle=f"{kind} · {installers.human_size(b.stat().st_size)}")
            btn = Gtk.Button(label=_('Restaurar'), valign=Gtk.Align.CENTER)

            def do(_b, arch=b):
                self.ctl.restore_wtf(gid, arch)
                dlg.close()
            btn.connect("clicked", do)
            row.add_suffix(btn)
            grp.add(row)
        page.add(grp)
        tv.set_content(page)
        dlg.set_child(tv)
        dlg.present(self)

    def _move_game(self, gid: str):
        g = self.ctl.cfg.game(gid)
        if g is None:
            return
        src, is_dir = installers.game_source(g.exe)
        size = installers.human_size(installers.size_of(src))
        dlg = Adw.AlertDialog(heading=_("¿Mover «{0}»?").format(g.name),
                              body=_("Se moverá {0} ({1}) a {2}. Umbral actualizará la ruta del juego.")
                              .format(src if is_dir else src.name, size, self.ctl.games_root()))
        dlg.add_response("cancel", _('Cancelar'))
        dlg.add_response("move", _("Mover"))
        dlg.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda _d, r: r == "move" and self.ctl.move_game_files(gid))
        dlg.present(self)

    def _remove_game(self, gid: str):
        g = self.ctl.cfg.game(gid)
        if g is None:
            return
        p = self.ctl.cfg.prefix(g.prefix_id)
        exclusive = (p is not None and p.id != BATTLENET_ID and not p.imported
                     and [x.id for x in self.ctl.prefix_users(p.id)] == [gid] and Path(p.path).exists())
        files = None
        if self.ctl.owns_files(g):
            if g.kind == engines.SCUMMVM:
                files = Path(g.exe)          # la carpeta del disco extraído
            elif g.kind == engines.EMULATOR and engines.is_organized(g.exe, self.ctl.games_root(), g.system):
                files = Path(g.exe).parent   # Juegos/<sistema>/<juego>: su carpeta propia
            elif g.kind == "custom":
                src, is_dir = installers.game_source(g.exe)
                files = src if is_dir else Path(g.exe).parent   # en la carpeta de juegos siempre hay una carpeta propia
        if files is not None and (files.resolve() == self.ctl.games_root().resolve()
                                  or files.parent.resolve() == self.ctl.games_root().resolve()
                                  and g.kind == engines.EMULATOR):
            files = None                     # nunca la carpeta de juegos entera ni la de un sistema
        if not exclusive and files is None:
            self.ctl.remove_game(gid)
            return
        parts = []
        if exclusive:
            parts.append(_("su prefijo propio ({0})").format(p.path))
        if files is not None:
            parts.append(_("sus archivos en la carpeta de juegos ({0}, {1})")
                         .format(files, installers.human_size(installers.size_of(files))))
        dlg = Adw.AlertDialog(heading=_("¿Quitar «{0}»?").format(g.name),
                              body=_("Este juego tiene {0}. Puedes conservarlo todo (por ejemplo, por las "
                                     "partidas guardadas) o borrarlo para liberar espacio.")
                              .format(_(" y ").join(parts)))
        dlg.add_response("cancel", _('Cancelar'))
        dlg.add_response("keep", _("Quitar y conservar sus archivos"))
        dlg.add_response("delete", _("Quitar y borrar sus archivos"))
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "cancel":
                return
            if self.ctl.is_running(gid):
                self.ctl.error(_('Cierra el juego antes de quitarlo.'))
                return
            self.ctl.remove_game(gid)
            if r == "delete":
                import shutil
                targets = []
                if exclusive:
                    self.ctl.cfg.prefixes.remove(p)
                    targets.append(p.path)
                if files is not None:
                    targets.append(str(files))
                self.ctl.save()
                for t in targets:
                    threading.Thread(target=shutil.rmtree, args=(t,), kwargs={"ignore_errors": True},
                                     daemon=True).start()
                self.toasts.add_toast(Adw.Toast(title=_("Archivos borrados")))
        dlg.connect("response", resp)
        dlg.present(self)

    def _pick_cover(self, gid: str, field: str = "cover"):
        dlg = Gtk.FileDialog(title=_('Elegir icono (PNG con transparencia)') if field == "icon"
                             else _('Elegir imagen de portada'))
        f = Gtk.FileFilter(name=_("Imágenes"))
        for suf in ("png", "jpg", "jpeg", "webp"):
            f.add_suffix(suf)
        dlg.set_default_filter(f)

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            self.ctl.set_cover(gid, file.get_path(), field)
        dlg.open(self, None, done)

    def _add_game(self, *_a):
        AddGameDialog(self.ctl, lambda g: GameSettings(self.ctl, g).present(self)).present(self)

    def _installed_dialog(self, installer_id: str, found: list):
        inst = self.ctl.cfg.game(installer_id)
        if inst is None:
            return
        dlg = Adw.Dialog(title=_('Instalación terminada'), content_width=560)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        add = Gtk.Button(label=_('Añadir'), css_classes=["suggested-action"])
        hb.pack_end(add)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            title=_('¿Qué quieres añadir a la biblioteca?') if found else _('No se encontraron ejecutables nuevos'),
            description=(_('Ejecutables nuevos en el prefijo tras ejecutar «') + inst.name + _('». El primero suele ser el juego.')) if found else
            _('Si el programa se instaló, añádelo con «Añadir» eligiendo su .exe.'))
        checks = []
        for i, exe in enumerate(found):
            row = Adw.SwitchRow(use_markup=False, title=f"{installers.pretty_name(exe)} · {exe.name}",
                                subtitle=str(exe.parent), active=(i == 0))
            checks.append((row, exe))
            g.add(row)
        page.add(g)
        og = Adw.PreferencesGroup()
        drop = Adw.SwitchRow(title=_('Quitar el instalador de la biblioteca'), active=bool(found))
        og.add(drop)
        page.add(og)
        tv.set_content(page)
        dlg.set_child(tv)

        def do_add(*_a):
            chosen = [exe for row, exe in checks if row.get_active()]
            for exe in chosen:
                name = installers.pretty_name(exe)
                if len(chosen) > 1:
                    name = f"{name} ({exe.stem})"
                self.ctl.add_custom_game(name, str(exe), inst.prefix_id)
            if drop.get_active():
                self.ctl.remove_game(inst.id)
            dlg.close()
        add.connect("clicked", do_add)
        dlg.present(self)

