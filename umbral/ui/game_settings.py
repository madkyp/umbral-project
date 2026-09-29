"""Ajustes por juego (o por prefijo, en la entrada de Battle.net)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from .. import gpu as gpumod
from .. import library, wowconfig
from ..config import BATTLENET_ID, Game, LaunchOptions
from ..controller import Controller
from ..launcher import (GS_FILTERS, GS_MODES, GS_RESOLUTIONS, GS_SCALERS, MANGOHUD_POSITIONS, MANGOHUD_PRESETS,
                        effective_options, monitor_resolution)
from ..i18n import _

# (campo, título, descripción/variable)
SWITCHES = [
    ("writecopy", _('Simular copy-on-write'), _('WINE_SIMULATE_WRITECOPY · evita la interfaz en blanco de Battle.net')),
    ("wayland", _('Driver Wayland de Wine'), _('PROTON_ENABLE_WAYLAND · experimental; desactivado = XWayland')),
    ("use_wined3d", _('Usar WineD3D (OpenGL)'), _('PROTON_USE_WINED3D · solo para diagnosticar; D3D12 no lo usa')),
    ("gpu_shader_cache", _('Caché de shaders por prefijo'), _('PROTON_LOCAL_SHADER_CACHE · carpeta shadercache del prefijo')),
    ("no_esync", _('Desactivar esync'), "PROTON_NO_ESYNC"),
    ("no_fsync", _('Desactivar fsync'), "PROTON_NO_FSYNC"),
    ("no_ntsync", _('Desactivar ntsync'), _('PROTON_NO_NTSYNC · solo GE-Proton')),
]
TOOLS = [
    ("gamemode", "GameMode", _('gamemoderun · ajustes de CPU durante la partida')),
]


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
            env[k.strip()] = v.strip()
    return env


class GameSettings(Adw.PreferencesDialog):
    def __init__(self, ctl: Controller, game: Game):
        super().__init__(title=_('Ajustes · {0}').format(game.name), search_enabled=False)
        self.ctl, self.game = ctl, game
        self.prefix = ctl.cfg.prefix(game.prefix_id)
        # En Battle.net se editan las opciones del prefijo (afectan a todo lo que corre en él)
        self.target: LaunchOptions = self.prefix.options if game.id == BATTLENET_ID else game.options
        self.eff = effective_options(ctl.cfg, self.prefix, None if game.id == BATTLENET_ID else game)
        self.add(self._general())
        self.add(self._performance())
        self.add(self._gpu())
        self.connect("closed", self._save)

    # ---------------------------------------------------------------- general
    def _general(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="general", title=_('General'), icon_name="applications-games-symbolic")
        g = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title=_('Nombre'), text=self.game.name)
        g.add(self.name_row)
        if self.game.kind == "custom":
            self._exe_initial = self.game.exe
            self.exe_row = Adw.EntryRow(title=_("Ejecutable (.exe)"), text=self.game.exe)
            pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
            pick.connect("clicked", self._pick_exe)
            self.exe_row.add_suffix(pick)
            g.add(self.exe_row)
        elif self.game.kind == "blizzard":
            g.add(Adw.ActionRow(use_markup=False, title=_('Al pulsar «Jugar»'),
                                subtitle=_('Comprueba la versión con Blizzard: si coincide lanza el juego directamente; si no, abre Battle.net para actualizar.')))
            g.add(Adw.ActionRow(use_markup=False, title=_('Producto'), subtitle=f"{self.game.product} · {self.game.exe}",
                                subtitle_selectable=True))
        self.api_row = None
        if self.game.kind == "blizzard" and self.game.product.startswith("wow"):
            self._wtf = wowconfig.config_path(self.game.exe)
            self._api_cur = (wowconfig.get_cvar(self._wtf, "gxApi") or "").upper()
            keys = list(wowconfig.GX_APIS)
            if self._api_cur not in keys:
                keys.append(self._api_cur)
            self._api_keys = keys
            self.api_row = Adw.ComboRow(
                title=_('API gráfica'),
                subtitle=_('gxApi en Config.wtf. Se aplica con el juego cerrado; en DX11 funcionan las opciones de DXVK.'),
                model=Gtk.StringList.new([wowconfig.GX_APIS.get(k, k) for k in keys]),
                selected=keys.index(self._api_cur))
            g.add(self.api_row)
        self.args_row = Adw.EntryRow(title=_('Argumentos de lanzamiento'), text=self.target.args)
        g.add(self.args_row)
        page.add(g)
        if self.game.id != BATTLENET_ID:
            page.add(self._look_group())

        if self.game.kind == "blizzard":
            rg = Adw.PreferencesGroup(title=_('Runner'))
            rg.add(Adw.ActionRow(use_markup=False, title=_('Proton del prefijo de Battle.net: {0}').format(self.prefix.runner),
                                 subtitle=_('Los juegos de Blizzard comparten prefijo con Battle.net: cámbialo en ⚙ de Battle.net para que todo use la misma versión.')))
            page.add(rg)
            self.runner_row = None
            return self._env_group(page)
        desc = (_('Cambiar de Proton actualiza o degrada el prefijo: Umbral guarda antes una copia (sin juegos) que puedes restaurar desde Reparar.')) if self.game.id == BATTLENET_ID else \
            _('Por defecto se usa el Proton del prefijo.')
        rg = Adw.PreferencesGroup(title=_('Runner'), description=desc)
        names = [_('(el del prefijo: ') + self.prefix.runner + ")"] if self.game.id != BATTLENET_ID else []
        names += library.runner_names(self.ctl.runners)
        self._runner_names = names
        self.runner_row = Adw.ComboRow(title=_('Proton / Wine'), model=Gtk.StringList.new(names))
        current = self.target.runner if self.game.id != BATTLENET_ID else self.prefix.runner
        if current in names:
            self.runner_row.set_selected(names.index(current))
        rg.add(self.runner_row)
        page.add(rg)
        return self._env_group(page)

    def _look_group(self) -> Adw.PreferencesGroup:
        lg = Adw.PreferencesGroup(title=_('Portada e icono'))
        self._look_rows = {}
        for field, title, empty in (("cover", _("Portada"), _('Automática (icono sobre su color)')),
                                    ("icon", _("Icono"), _('El del propio juego'))):
            row = Adw.ActionRow(use_markup=False, title=title)
            pick = Gtk.Button(label=_('Elegir…'), valign=Gtk.Align.CENTER)
            pick.connect("clicked", lambda _b, f=field: self._pick_image(f))
            clear = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text=_('Volver a la original'))
            clear.connect("clicked", lambda _b, f=field: self._set_image(f, None))
            row.add_suffix(clear)
            row.add_suffix(pick)
            self._look_rows[field] = (row, clear, empty)
            lg.add(row)
        sg = Adw.ActionRow(use_markup=False, title=_("Buscar en SteamGridDB…"), activatable=True,
                           subtitle=_("Portadas y logos de la comunidad (steamgriddb.com)"))
        sg.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        sg.connect("activated", lambda *_a: self._open_sgdb())
        lg.add(sg)
        self._refresh_look()
        return lg

    def _open_sgdb(self):
        from .sgdb_picker import SGDBPicker
        SGDBPicker(self.ctl, self.game.id, on_done=self._refresh_look).present(self)

    def _refresh_look(self):
        for field, (row, clear, empty) in self._look_rows.items():
            val = getattr(self.game, field)
            row.set_subtitle(Path(val).name if val else empty)
            clear.set_visible(bool(val))

    def _set_image(self, field: str, path: str | None):
        self.ctl.set_cover(self.game.id, path, field)
        self._refresh_look()

    def _pick_image(self, field: str):
        dlg = Gtk.FileDialog(title=_('Elegir icono (PNG con transparencia)') if field == "icon"
                             else _('Elegir imagen de portada'))
        f = Gtk.FileFilter(name=_("Imágenes"))
        for suf in ("png", "jpg", "jpeg", "webp"):
            f.add_suffix(suf)
        dlg.set_default_filter(f)

        def done(d, res):
            try:
                self._set_image(field, d.open_finish(res).get_path())
            except GLib.Error:
                pass
        dlg.open(self.get_root(), None, done)

    def _env_group(self, page: Adw.PreferencesPage) -> Adw.PreferencesPage:
        eg = Adw.PreferencesGroup(title=_('Variables de entorno'),
                                  description=_('Una por línea, NOMBRE=valor. Tienen prioridad sobre todo lo demás.'))
        self.env_view = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=10,
                                     right_margin=10, css_classes=["card"], height_request=110)
        self.env_view.get_buffer().set_text("\n".join(f"{k}={v}" for k, v in self.target.env.items()))
        eg.add(self.env_view)
        page.add(eg)
        return page

    def _pick_exe(self, *_a):
        dlg = Gtk.FileDialog(title=_('Elegir ejecutable'))
        f = Gtk.FileFilter(name=_('Ejecutables de Windows'))
        f.add_suffix("exe")
        f.add_suffix("bat")
        f.add_suffix("msi")
        dlg.set_default_filter(f)

        def done(d, res):
            try:
                file = d.open_finish(res)
                self.exe_row.set_text(file.get_path())
            except Exception:
                pass
        dlg.open(self.get_root(), None, done)

    # ---------------------------------------------------------------- rendimiento
    def _performance(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="perf", title=_('Rendimiento'), icon_name="preferences-system-symbolic")
        self.switches: dict[str, Adw.SwitchRow] = {}
        g = Adw.PreferencesGroup(title="Proton")
        for field, title, sub in SWITCHES:
            row = Adw.SwitchRow(title=title, subtitle=sub, active=bool(getattr(self.eff, field)))
            self.switches[field] = row
            g.add(row)

        # MangoHud: desplegable con su propio interruptor (arriba del todo: es lo más usado)
        m = Adw.PreferencesGroup(title=_('Superposición'))
        self.mh_row = Adw.ExpanderRow(title="MangoHud", show_enable_switch=True,
                                      subtitle=_('FPS, temperaturas y uso de CPU/GPU en pantalla · Shift derecho + F12 lo oculta en partida'),
                                      enable_expansion=bool(self.eff.mangohud), expanded=bool(self.eff.mangohud))
        self._mh_presets = list(MANGOHUD_PRESETS)
        self._mh_positions = list(MANGOHUD_POSITIONS)
        self.mh_preset = Adw.ComboRow(title=_('Qué mostrar'), model=Gtk.StringList.new(
            [v[0] for v in MANGOHUD_PRESETS.values()]))
        self.mh_preset.set_selected(self._mh_presets.index(self.eff.mangohud_preset)
                                    if self.eff.mangohud_preset in self._mh_presets else 1)
        self.mh_pos = Adw.ComboRow(title=_('Posición'), model=Gtk.StringList.new(list(MANGOHUD_POSITIONS.values())))
        self.mh_pos.set_selected(self._mh_positions.index(self.eff.mangohud_position)
                                 if self.eff.mangohud_position in self._mh_positions else 0)
        self.mh_row.add_row(self.mh_preset)
        self.mh_row.add_row(self.mh_pos)
        if not shutil.which("mangohud"):
            self.mh_row.set_subtitle(_('No instalado: sudo pacman -S mangohud lib32-mangohud'))
            self.mh_row.set_sensitive(False)
        m.add(self.mh_row)
        page.add(m)
        page.add(self._gamescope_group())
        page.add(g)

        t = Adw.PreferencesGroup(title=_('Herramientas'))
        for field, title, sub in TOOLS:
            row = Adw.SwitchRow(title=title, subtitle=sub, active=bool(getattr(self.eff, field)))
            self.switches[field] = row
            t.add(row)
        page.add(t)
        return page

    def _gamescope_group(self) -> Adw.PreferencesGroup:
        """Resolución y escalado con gamescope, en opciones legibles."""
        grp = Adw.PreferencesGroup(title=_("Pantalla"))
        self.gs_row = Adw.ExpanderRow(title=_("Resolución y escalado (gamescope)"), show_enable_switch=True,
                                      subtitle=_("Agranda juegos de baja resolución (p. ej. RPG Maker a 640×480) "
                                                 "a toda tu pantalla"),
                                      enable_expansion=bool(self.eff.gamescope), expanded=bool(self.eff.gamescope))
        mon = monitor_resolution()
        screen = f"{mon[0]}×{mon[1]}" if mon else "?"
        self._gs_res = [""] + GS_RESOLUTIONS
        cur = (self.eff.gs_resolution or "").lower()
        if cur and cur not in self._gs_res:
            self._gs_res.append(cur)
        labels = [_("La de la pantalla ({0})").format(screen)] + [r.replace("x", "×") for r in self._gs_res[1:]]
        self.gs_res_row = Adw.ComboRow(title=_("Resolución del juego"),
                                       subtitle=_("A la que dibuja el juego; gamescope la escala a tu pantalla"),
                                       model=Gtk.StringList.new(labels), selected=self._gs_res.index(cur))
        self._gs_combos = {}
        rows = [self.gs_res_row]
        for field, title, options, default in (("gs_mode", _("Ventana"), GS_MODES, "fullscreen"),
                                               ("gs_scaler", _("Escalado"), GS_SCALERS, "fit"),
                                               ("gs_filter", _("Filtro"), GS_FILTERS, "linear")):
            keys = list(options)
            val = getattr(self.eff, field) or default
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(list(options.values())),
                               selected=keys.index(val) if val in keys else 0)
            self._gs_combos[field] = (row, keys, default)
            rows.append(row)
        self.gs_args = Adw.EntryRow(title=_("Argumentos extra de gamescope"), text=self.eff.gamescope_args or "")
        rows.append(self.gs_args)
        for r in rows:
            self.gs_row.add_row(r)
        if not shutil.which("gamescope"):
            self.gs_row.set_subtitle(_("No instalado: sudo pacman -S gamescope"))
            self.gs_row.set_sensitive(False)
        grp.add(self.gs_row)
        return grp

    # ---------------------------------------------------------------- gpu
    def _gpu(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="gpu", title="GPU", icon_name="video-display-symbolic")
        rep = self.ctl.report
        g = Adw.PreferencesGroup(title=_('Tarjeta gráfica'))
        if rep is None or not rep.gpus:
            g.add(Adw.ActionRow(title=_('Detectando GPU…') if rep is None else _('No se detectó ninguna GPU')))
            page.add(g)
            self._gpu_ids = ["auto"]
            self.gpu_row = None
            return page
        default = rep.default_gpu()
        labels = [_('Automática ({0} {1})').format(gpumod.VENDOR_LABEL[default.vendor], default.name)]
        self._gpu_ids = ["auto"]
        for gg in rep.gpus:
            labels.append(f"{gpumod.VENDOR_LABEL[gg.vendor]} {gg.name}")
            self._gpu_ids.append(gg.pci_id)
        self.gpu_row = Adw.ComboRow(title=_('Usar'), model=Gtk.StringList.new(labels),
                                    sensitive=len(rep.gpus) > 1,
                                    subtitle=_('Solo hay una GPU') if len(rep.gpus) == 1 else
                                             _('PRIME offload / DRI_PRIME / filtro de dispositivo Vulkan'))
        cur = self.eff.gpu or "auto"
        if cur in self._gpu_ids:
            self.gpu_row.set_selected(self._gpu_ids.index(cur))
        g.add(self.gpu_row)
        for gg in rep.gpus:
            vk = rep.vk_for(gg)
            row = Adw.ActionRow(use_markup=False, title=f"{gpumod.VENDOR_LABEL[gg.vendor]} {gg.name}",
                                subtitle=_('Driver kernel: {0} · Vulkan: {1} · PCI {2}').format(gg.driver or '?', vk.driver_name + ' ' + vk.driver_info if vk else 'no disponible', gg.slot) + (_(' · pantalla principal') if gg.boot_vga else ""))
            g.add(row)
        page.add(g)

        e = Adw.PreferencesGroup(title=_('Variables que aplicará Umbral'),
                                 description=_('Según la GPU elegida. Revisadas contra la documentación de NVIDIA, Mesa, DXVK y vkd3d-proton.'))
        self.gpu_env_label = Gtk.Label(xalign=0, selectable=True, wrap=True, css_classes=["mono", "card"],
                                       margin_top=4)
        self.gpu_env_label.set_margin_start(4)
        e.add(self.gpu_env_label)
        page.add(e)
        self.gpu_row.connect("notify::selected", lambda *_a: self._update_gpu_env())
        self._update_gpu_env()

        if rep.issues:
            ig = Adw.PreferencesGroup(title=_('Avisos del sistema'))
            for i in rep.issues:
                ig.add(Adw.ActionRow(use_markup=False, title=i.message, subtitle=i.fix, subtitle_selectable=True,
                                     css_classes=[f"issue-{i.level}"]))
            page.add(ig)
        return page

    def _update_gpu_env(self):
        choice = self._gpu_ids[self.gpu_row.get_selected()]
        env, target = gpumod.gpu_env(self.ctl.report, choice)
        txt = "\n".join(f"{k}={v}" for k, v in env.items()) or \
            _('(ninguna: con una sola GPU el driver ya la usa)')
        self.gpu_env_label.set_label(txt)

    # ---------------------------------------------------------------- guardar
    def _save(self, *_a):
        t, g = self.target, self.game
        name = self.name_row.get_text().strip()
        if name:
            g.name = name
        if g.kind == "custom":
            # Solo si lo has editado tú: el juego puede haberse movido mientras el diálogo
            # estaba abierto y no hay que pisar la ruta nueva con la antigua.
            new_exe = self.exe_row.get_text().strip()
            if new_exe and new_exe != self._exe_initial:
                g.exe = new_exe
        t.args = self.args_row.get_text().strip()
        if self.runner_row is not None:
            sel = self._runner_names[self.runner_row.get_selected()]
            if g.id == BATTLENET_ID:
                self.prefix.runner = sel
            else:
                t.runner = None if self.runner_row.get_selected() == 0 else sel   # 0 = «el del prefijo»
        if self.api_row is not None:
            new = self._api_keys[self.api_row.get_selected()]
            if new != self._api_cur:
                if self.ctl.is_running(g.id):
                    self.ctl.error(_('Cierra el juego para cambiar la API gráfica (WoW reescribe Config.wtf al salir).'))
                else:
                    try:
                        wowconfig.set_cvar(self._wtf, "gxApi", new or None)
                        self.ctl.emit("toast", _('API gráfica: {0}').format(wowconfig.GX_APIS.get(new, new)))
                    except OSError as e:
                        self.ctl.error(_('No se pudo escribir Config.wtf: {0}').format(e))
        buf = self.env_view.get_buffer()
        t.env = parse_env(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        # Solo se guardan como override los valores que difieren del heredado
        for field, row in self.switches.items():
            val = row.get_active()
            if val != bool(getattr(self.eff, field)) or getattr(t, field) is not None:
                setattr(t, field, val)
        mh = self.mh_row.get_enable_expansion()
        if mh != bool(self.eff.mangohud) or t.mangohud is not None:
            t.mangohud = mh
        preset = self._mh_presets[self.mh_preset.get_selected()]
        if preset != (self.eff.mangohud_preset or "basic") or t.mangohud_preset is not None:
            t.mangohud_preset = preset
        pos = self._mh_positions[self.mh_pos.get_selected()]
        if pos != (self.eff.mangohud_position or "top-left") or t.mangohud_position is not None:
            t.mangohud_position = pos
        on = self.gs_row.get_enable_expansion()
        if on != bool(self.eff.gamescope) or t.gamescope is not None:
            t.gamescope = on
        res = self._gs_res[self.gs_res_row.get_selected()]
        if res != (self.eff.gs_resolution or "") or t.gs_resolution is not None:
            t.gs_resolution = res
        for field, (row, keys, default) in self._gs_combos.items():
            val = keys[row.get_selected()]
            if val != (getattr(self.eff, field) or default) or getattr(t, field) is not None:
                setattr(t, field, val)
        gs = self.gs_args.get_text().strip()
        if gs != (self.eff.gamescope_args or "") or t.gamescope_args is not None:
            t.gamescope_args = gs
        if self.gpu_row is not None:
            choice = self._gpu_ids[self.gpu_row.get_selected()]
            if choice != (self.eff.gpu or "auto") or t.gpu is not None:
                t.gpu = choice
        self.ctl.save()
        self.ctl.emit("library")
