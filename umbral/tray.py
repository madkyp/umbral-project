"""Icono en la bandeja del sistema (StatusNotifierItem + menú dbusmenu) con Gio.

GTK4 no tiene API de bandeja; se implementa el protocolo freedesktop/KDE que
usan Waybar, KDE, etc.: se exporta org.kde.StatusNotifierItem y un menú
com.canonical.dbusmenu, y se registra en org.kde.StatusNotifierWatcher.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
    <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
  </interface>
</node>
"""

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"


class Tray:
    """items: lista de (etiqueta, callback) o None para separador. Se puede cambiar
    con set_items(); on_activate: clic izquierdo."""

    def __init__(self, app_id: str, title: str, icon_name: str, icon_path: str,
                 on_activate: Callable[[], None]):
        self.app_id, self.title = app_id, title
        self.icon_name, self.icon_path = icon_name, icon_path
        self.on_activate = on_activate
        self.items: list[tuple[str, Callable[[], None]] | None] = []
        self.revision = 1
        self.registered = False
        self.bus: Gio.DBusConnection | None = None
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._ids: list[int] = []
        self._watch_id = 0

    # ---------------------------------------------------------------- arranque
    def start(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            log.warning("Sin bus de sesión, no hay bandeja: %s", e)
            return
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._ids.append(self.bus.register_object(SNI_PATH, sni, self._sni_call, self._sni_prop, None))
        self._ids.append(self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None))
        Gio.bus_own_name_on_connection(self.bus, self.service, Gio.BusNameOwnerFlags.NONE, None, None)
        # Registrarse ahora y cada vez que (re)aparezca la barra (p. ej. al reiniciar Waybar)
        self._watch_id = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, self._watcher_down)

    def _watcher_up(self, conn, name, owner):
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  self._registered_cb)

    def _registered_cb(self, conn, res):
        try:
            conn.call_finish(res)
            self.registered = True
            log.info("Icono de bandeja registrado (%s)", self.service)
        except GLib.Error as e:
            self.registered = False
            log.warning("No se pudo registrar el icono de bandeja: %s", e)

    def _watcher_down(self, conn, name):
        self.registered = False

    # ---------------------------------------------------------------- StatusNotifierItem
    def _sni_prop(self, conn, sender, path, iface, prop):
        tooltip = ("", [], self.title, "")
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_path),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", tooltip),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        invocation.return_value(None)

    # ---------------------------------------------------------------- dbusmenu
    def set_items(self, items: list[tuple[str, Callable[[], None]] | None]) -> None:
        self.items = items
        self.revision += 1
        if self.bus:
            self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))

    def _item_props(self, idx: int) -> dict:
        it = self.items[idx]
        if it is None:
            return {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", True)}
        return {"label": GLib.Variant("s", it[0]), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i + 1, self._item_props(i), []))
                    for i in range(len(self.items))]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = [(i, self._item_props(i - 1)) for i in ids if 1 <= i <= len(self.items)]
            if 0 in ids:
                out.insert(0, (0, {"children-display": GLib.Variant("s", "submenu")}))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            props = self._item_props(item_id - 1) if 1 <= item_id <= len(self.items) else {}
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            item_id, event = params.unpack()[:2]
            self._fire(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in params.unpack()[0]:
                self._fire(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _fire(self, item_id: int, event: str) -> None:
        if event != "clicked" or not 1 <= item_id <= len(self.items):
            return
        it = self.items[item_id - 1]
        if it is not None:
            GLib.idle_add(lambda: (it[1](), False)[1])
