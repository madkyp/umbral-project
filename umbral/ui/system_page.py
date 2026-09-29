"""Página Sistema: diagnóstico de GPU, runners, Hyprland y apariencia."""
from __future__ import annotations

from pathlib import Path

import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import gpu as gpumod
from .. import integration, paths, runners
from ..controller import Controller
from ..i18n import _

ISSUE_ICON = {"error": "dialog-error-symbolic", "warning": "dialog-warning-symbolic",
              "info": "dialog-information-symbolic"}


def copy_text(widget: Gtk.Widget, text: str) -> None:
    Gdk.Display.get_default().get_clipboard().set(text)


class SystemPage(Adw.PreferencesPage):
    def __init__(self, ctl: Controller, on_accent):
        super().__init__()
        self.ctl = ctl
        self.on_accent = on_accent
        self._dynamic: list[Adw.PreferencesGroup] = []
        self._build_static()
        self.rebuild()
        ctl.connect(self._on_event)

    def _on_event(self, ev, *a):
        if ev == "system":
            self.rebuild()

    # ---------------------------------------------------------------- fijo
    def _build_static(self):
        hy = Adw.PreferencesGroup(title="Hyprland",
                                  description=_('Umbral no modifica tu configuración: genera un fragmento para que lo revises y lo pegues tú.'))
        row = Adw.ActionRow(use_markup=False, title=_('Fragmento sugerido (Lua)'),
                            subtitle=_('Reglas de ventana para Umbral, Battle.net y WoW + atajo SUPER+G'),
                            activatable=True)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_a: self._show_snippet())
        hy.add(row)
        self._static_hypr = hy

        ap = Adw.PreferencesGroup(title=_('Apariencia'))
        accent = Adw.ActionRow(use_markup=False, title=_('Color de acento'))
        accent.set_subtitle(_("Personalizado") if self.ctl.cfg.settings.accent else _('El del sistema (tema GTK)'))
        rgba = Gdk.RGBA()
        rgba.parse(self.ctl.cfg.settings.accent or "#5b8cff")
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text=_('Usar el acento del sistema'))
        reset.connect("clicked", lambda *_a: self._set_accent(""))
        accent.add_suffix(reset)
        btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), rgba=rgba,
                                    valign=Gtk.Align.CENTER)
        btn.connect("notify::rgba", self._accent_changed)
        accent.add_suffix(btn)
        self._accent_row = accent
        ap.add(accent)
        fl = Adw.SwitchRow(title=_('Ventana flotante (Hyprland)'),
                           subtitle=_('Flotante y centrada sobre las demás, como un diálogo. Se aplica al reiniciar Umbral; no modifica tu configuración de Hyprland.'),
                           active=self.ctl.cfg.settings.float_window)

        def on_float(row, *_a):
            self.ctl.cfg.settings.float_window = row.get_active()
            self.ctl.save()
        fl.connect("notify::active", on_float)
        ap.add(fl)
        tr = Adw.SwitchRow(title=_('Seguir en la bandeja al cerrar'),
                           subtitle=_('Cerrar la ventana deja Umbral en segundo plano con su icono en la barra; «Salir» en el menú del icono lo cierra del todo.'),
                           active=self.ctl.cfg.settings.run_in_tray)

        def on_tray(row, *_a):
            self.ctl.cfg.settings.run_in_tray = row.get_active()
            self.ctl.save()
        tr.connect("notify::active", on_tray)
        ap.add(tr)

        from .. import i18n
        codes = list(i18n.LANGUAGES)
        sys_name = i18n.LANGUAGES["es" if i18n.system_language() == "es" else "en"]
        lang = Adw.ComboRow(title=_("Idioma"), subtitle=_("Se aplica al reiniciar Umbral."),
                            model=Gtk.StringList.new([_("Sistema ({0})").format(sys_name), "Español", "English"]))
        cur = self.ctl.cfg.settings.language
        lang.set_selected(codes.index(cur) if cur in codes else 0)

        def on_lang(row, *_a):
            code = codes[row.get_selected()]
            if code == self.ctl.cfg.settings.language:
                return
            self.ctl.cfg.settings.language = code
            self.ctl.save()
            toast = Adw.Toast(title=_("Idioma cambiado: reinicia Umbral para aplicarlo"),
                              button_label=_("Reiniciar ahora"), timeout=0)
            toast.connect("button-clicked", lambda *_a: self.get_root().get_application().restart())
            self.get_root().toasts.add_toast(toast)
        lang.connect("notify::selected", on_lang)
        ap.add(lang)
        self._static_look = ap

        pg = Adw.PreferencesGroup(title=_('Rutas'))
        for title, p in ((_("Configuración"), paths.CONFIG_FILE), (_("Registros"), paths.LOG_DIR),
                         (_('Runners descargados'), paths.RUNNER_INSTALL_DIR),
                         (_("Carpeta de juegos"), Path(self.ctl.cfg.settings.games_root))):
            r = Adw.ActionRow(use_markup=False, title=title, subtitle=str(p), subtitle_selectable=True)
            b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text=_('Abrir carpeta'))
            b.connect("clicked", lambda *_a, p=p: self._open(p if p.is_dir() else p.parent))
            r.add_suffix(b)
            pg.add(r)
        self._static_paths = pg

    def _accent_changed(self, btn, *_a):
        c = btn.get_rgba()
        hexc = f"#{int(c.red * 255):02x}{int(c.green * 255):02x}{int(c.blue * 255):02x}"
        self._set_accent(hexc)

    def _set_accent(self, hexc: str):
        self.ctl.cfg.settings.accent = hexc
        self.ctl.save()
        self.on_accent(hexc)
        self._accent_row.set_subtitle(_("Personalizado") if hexc else _('El del sistema (tema GTK)'))

    def _open(self, p):
        p.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher(file=Gio.File.new_for_path(str(p))).launch(self.get_root(), None, None)

    # ---------------------------------------------------------------- dinámico
    def rebuild(self):
        for g in self._dynamic + [self._static_hypr, self._static_look, self._static_paths]:
            if g.get_parent():
                self.remove(g)
        self._dynamic = [self._gpu_group(), self._runner_group()]
        for g in self._dynamic:
            self.add(g)
        for g in (self._static_hypr, self._static_look, self._static_paths):
            self.add(g)

    def _gpu_group(self) -> Adw.PreferencesGroup:
        rep = self.ctl.report
        g = Adw.PreferencesGroup(title=_('GPU y drivers'))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], tooltip_text=_('Volver a detectar'))
        refresh.connect("clicked", lambda *_a: self.ctl.refresh_system())
        g.set_header_suffix(refresh)
        if rep is None:
            row = Adw.ActionRow(use_markup=False, title=_('Detectando…'))
            row.add_prefix(Adw.Spinner())
            g.add(row)
            return g
        for gg in rep.gpus:
            vk = rep.vk_for(gg)
            row = Adw.ActionRow(use_markup=False, title=f"{gpumod.VENDOR_LABEL[gg.vendor]} {gg.name}",
                                subtitle=f"{gg.driver or '?'} · "
                                         f"{(vk.driver_name + ' ' + vk.driver_info + ' · Vulkan ' + vk.api_version) if vk else 'sin Vulkan'}")
            row.add_prefix(Gtk.Image(icon_name="video-display-symbolic"))
            if gg == rep.default_gpu():
                row.add_suffix(Gtk.Label(label=_('Juegos'), css_classes=["status-chip", "running"],
                                         valign=Gtk.Align.CENTER))
            g.add(row)
        if rep.nvidia_modeset is not None:
            g.add(Adw.ActionRow(use_markup=False, title="nvidia_drm", subtitle=f"modeset={'Y' if rep.nvidia_modeset else 'N'} · "
                                                             f"fbdev={'Y' if rep.nvidia_fbdev else 'N'}"))
        if not rep.issues:
            ok = Adw.ActionRow(use_markup=False, title=_('Sin problemas detectados'),
                               subtitle=_('Paquetes Vulkan de 32 y 64 bits, driver y módulos correctos'))
            ok.add_prefix(Gtk.Image(icon_name="object-select-symbolic", css_classes=["success"]))
            g.add(ok)
        for i in rep.issues:
            row = Adw.ActionRow(use_markup=False, title=i.message, subtitle=i.fix, subtitle_selectable=True)
            row.add_prefix(Gtk.Image(icon_name=ISSUE_ICON[i.level], css_classes=[f"issue-{i.level}"]))
            if i.fix:
                b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text=_('Copiar comando (Umbral nunca ejecuta sudo por ti)'))
                b.connect("clicked", lambda _b, t=i.fix: copy_text(_b, t))
                row.add_suffix(b)
            g.add(row)
        return g

    def _runner_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=_('Runners'),
                                 description=_('Se descargan de sus GitHub oficiales y se verifican con SHA-512. UMU-Proton (base Valve) es la alternativa estable si notas tirones con GE o CachyOS.'))
        box = Gtk.Box(spacing=4)
        self.dl_buttons = []
        for source in runners.SOURCES:
            b = Gtk.Button(label=source, css_classes=["flat"], tooltip_text=_('Descargar el último {0}').format(source))
            b.connect("clicked", lambda _b, s=source: self._download(s))
            box.append(b)
            self.dl_buttons.append(b)
        g.set_header_suffix(box)
        self.dl_progress = Gtk.ProgressBar(visible=False, margin_bottom=6)
        g.add(self.dl_progress)
        for r in self.ctl.runners:
            row = Adw.ActionRow(use_markup=False, title=_("Wine del sistema") if r.kind == "wine" else r.name,
                                subtitle=str(r.path))
            row.add_prefix(Gtk.Image(icon_name="application-x-executable-symbolic"))
            if r.is_ge:
                row.add_suffix(Gtk.Label(label="GE", css_classes=["status-chip"], valign=Gtk.Align.CENTER))
            g.add(row)
        return g

    def _download(self, source: str):
        for b in self.dl_buttons:
            b.set_sensitive(False)
        self.dl_progress.set_visible(True)
        self.dl_progress.set_text(f"{source}: consultando…")
        self.dl_progress.set_show_text(True)

        def prog(f):
            GLib.idle_add(lambda: (self.dl_progress.set_visible(True), self.dl_progress.set_fraction(f), False)[-1])

        def work():
            try:
                rel = runners.releases(source, 1)[0]
                installed = {r.name.removesuffix("-x86_64") for r in self.ctl.runners}
                if rel["tag"] in installed:
                    self.ctl.emit("toast", _('{0} ya está instalado (es la última versión)').format(rel['tag']))
                else:
                    GLib.idle_add(lambda: (self.dl_progress.set_text(_('Descargando {0}…').format(rel['tag'])), False)[1])
                    runners.install_release(rel, prog)
                    self.ctl.emit("toast", _('{0} instalado y verificado').format(rel['tag']))
            except (runners.RunnerError, IndexError) as e:
                self.ctl.error(_('No se pudo descargar {0}: {1}').format(source, e))
            self.ctl.runners = runners.discover()
            self.ctl.emit("system")
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- snippet
    def _show_snippet(self):
        text = integration.hyprland_snippet(self.ctl.report)
        dlg = Adw.Dialog(title=_('Fragmento para Hyprland'), content_width=720, content_height=620)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        copy = Gtk.Button(label=_('Copiar'), css_classes=["suggested-action"])
        copy.connect("clicked", lambda b: (copy_text(b, text), b.set_label(_('Copiado'))))
        hb.pack_end(copy)
        verify = Gtk.Button(label=_('Validar con Hyprland'))
        status = Gtk.Label(css_classes=["caption"], wrap=True, xalign=0)

        def do_verify(*_a):
            ok, out = integration.verify_lua_snippet(text)
            status.set_label((_('✔ Hyprland acepta el fragmento') if ok else "✖ " + out.splitlines()[-1])
                             + _(' (validado sin aplicarlo)'))
        verify.connect("clicked", do_verify)
        hb.pack_start(verify)
        tv.add_top_bar(hb)
        view = Gtk.TextView(editable=False, monospace=True, left_margin=16, top_margin=12, right_margin=16,
                            bottom_margin=12)
        view.get_buffer().set_text(text)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Gtk.ScrolledWindow(child=view, vexpand=True))
        status.set_margin_start(16)
        status.set_margin_bottom(10)
        box.append(status)
        tv.set_content(box)
        dlg.set_child(tv)
        dlg.present(self.get_root())
