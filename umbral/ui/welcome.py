"""Ventana de bienvenida (primer arranque) y aplicación del tema claro/oscuro."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from .. import APP_ID
from ..controller import Controller
from ..i18n import _

THEMES = ("", "dark", "light")


def apply_theme(theme: str) -> None:
    """Tema de la aplicación: «light» claro, «dark» oscuro y vacío el del sistema (oscuro si no dice nada)."""
    Adw.StyleManager.get_default().set_color_scheme(
        {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(
            theme, Adw.ColorScheme.PREFER_DARK))


class WelcomeDialog(Adw.Dialog):
    def __init__(self, ctl: Controller):
        super().__init__(title=_('Bienvenida'), content_width=520, content_height=600, can_close=True)
        self.ctl = ctl
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=6, margin_bottom=28,
                      margin_start=32, margin_end=32, valign=Gtk.Align.CENTER)
        logo = Gtk.Image(icon_name=APP_ID, pixel_size=112, halign=Gtk.Align.CENTER)
        box.append(logo)
        title = Gtk.Label(label=_('Bienvenido a Umbral'), css_classes=["title-1"])
        box.append(title)
        about = Gtk.Label(
            label=_('Umbral es un lanzador personal para tus juegos de Windows en Linux. Instala el cliente '
                    'oficial de Battle.net y lanza World of Warcraft con un clic, avisándote de las '
                    'actualizaciones. También abre tus propios .exe, juegos de ScummVM y de consola.'),
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["body"])
        box.append(about)
        box.append(Gtk.Label(label=_('Elige el tema'), css_classes=["heading"], margin_top=8))

        self._dark = Gtk.ToggleButton(label=_('Oscuro'), css_classes=["pill"])
        self._light = Gtk.ToggleButton(label=_('Claro'), css_classes=["pill"], group=self._dark)
        # Sin elección guardada se sigue el sistema; el botón marcado solo refleja cómo se ve ahora
        st = ctl.cfg.settings
        light = (st.theme == "light") if st.theme_chosen else not Adw.StyleManager.get_default().get_dark()
        (self._light if light else self._dark).set_active(True)
        self._dark.connect("toggled", self._on_toggle)
        self._light.connect("toggled", self._on_toggle)
        row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, homogeneous=True)
        row.append(self._dark)
        row.append(self._light)
        box.append(row)
        box.append(Gtk.Label(label=_('Podrás cambiarlo después en Sistema → Apariencia.'),
                             css_classes=["dim-label", "caption"]))

        go = Gtk.Button(label=_('Empezar'), css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER,
                        margin_top=12)
        go.set_size_request(180, -1)
        go.connect("clicked", lambda *_a: self.close())
        box.append(go)
        tv.set_content(box)
        self.set_child(tv)
        self.set_default_widget(go)
        self.connect("closed", self._on_closed)

    def _on_toggle(self, btn):
        if not btn.get_active():
            return
        theme = "light" if btn is self._light else "dark"
        self.ctl.cfg.settings.theme = theme
        self.ctl.cfg.settings.theme_chosen = True
        apply_theme(theme)             # se ve al momento

    def _on_closed(self, *_a):
        self.ctl.cfg.settings.welcome_done = True
        self.ctl.save()
