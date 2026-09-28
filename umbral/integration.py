"""Integración con el escritorio: .desktop, notificaciones y fragmento de Hyprland.

Nunca modifica la configuración de Hyprland: solo genera texto sugerido.
Los nombres de propiedades Lua (match/float/idle_inhibit/immediate/content...)
se validan con `Hyprland --verify-config`.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_ID, paths
from .gpu import GpuReport
from .i18n import _

# Clases XWayland de Wine/Proton. umu fija SteamGameId=umu-default -> steam_app_default;
# Wine sin Proton usa el nombre del .exe en minúsculas.
BNET_MATCH = r"^(battle\.net\.exe|steam_app_default)$"
BNET_TITLE = r"(^|.* )Battle\.net$"  # "Battle.net", "Inicio de sesión en Battle.net"...
WOW_TITLE = r"^World of Warcraft$"


def notify(summary: str, body: str = "", icon: str = APP_ID, urgency: str = "normal") -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Umbral", "-i", icon, "-u", urgency, summary, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _lq(rx: str) -> str:
    """Regex -> cadena Lua (la barra invertida debe duplicarse)."""
    return rx.replace("\\", "\\\\")


def hyprland_snippet(report: GpuReport | None, lua: bool = True) -> str:
    nvidia = bool(report and any(g.vendor == "nvidia" for g in report.gpus))
    if lua:
        s = f'''-- Umbral: fragmento sugerido para ~/.config/hypr/hyprland.lua
-- (revísalo antes de pegarlo; Umbral no toca tu configuración)

-- La propia app: ventana flotante centrada, tamaño cómodo
hl.window_rule({{
    name = "umbral",
    match = {{ class = "{_lq("^" + re.escape(APP_ID) + "$")}" }},
    float = true,
    center = true,
    size = "1100 720",
}})

-- Battle.net (XWayland): flotante y sin desenfoque (su interfaz CEF no lo necesita)
hl.window_rule({{
    name = "umbral-battlenet",
    match = {{ title = "{_lq(BNET_TITLE)}" }},
    float = true,
    center = true,
    no_blur = true,
}})

-- World of Warcraft: opaco, sin suspensión y marcado como juego
hl.window_rule({{
    name = "umbral-wow",
    match = {{ title = "{_lq(WOW_TITLE)}" }},
    opaque = true,
    no_blur = true,
    idle_inhibit = "fullscreen",
    content = "game",
}})

-- Atajo: SUPER + G abre Umbral
hl.bind("SUPER + G", hl.dsp.exec_cmd("umbral"), {{
    description = "[Utilities] Umbral (Battle.net)",
}})
'''
    else:
        s = f'''# Umbral: fragmento sugerido para hyprland.conf
windowrule = match:class ^{re.escape(APP_ID)}$, float on, center on, size 1100 720
windowrule = match:title {BNET_TITLE}, float on, center on, no_blur on
windowrule = match:title {WOW_TITLE}, opaque on, no_blur on, idle_inhibit fullscreen, content game
bind = SUPER, G, exec, umbral
'''
    if nvidia:
        s += ('\n-- NVIDIA: HyDE ya exporta LIBVA_DRIVER_NAME=nvidia, __GLX_VENDOR_LIBRARY_NAME=nvidia\n'
              '-- y NVD_BACKEND=direct. No hace falta repetirlas. Si NO usas HyDE, añade:\n'
              '-- hl.env("LIBVA_DRIVER_NAME", "nvidia")\n'
              '-- hl.env("__GLX_VENDOR_LIBRARY_NAME", "nvidia")\n') if lua else (
              '\n# NVIDIA (solo si tu config no las define ya):\n'
              'env = LIBVA_DRIVER_NAME,nvidia\nenv = __GLX_VENDOR_LIBRARY_NAME,nvidia\n')
    return s


def verify_lua_snippet(snippet: str) -> tuple[bool, str]:
    """Valida el fragmento con el propio Hyprland (no arranca el compositor)."""
    if not shutil.which("Hyprland"):
        return False, _('Hyprland no está en el PATH')
    tmp = paths.CACHE_DIR / "snippet-check.lua"
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(snippet)
    try:
        r = subprocess.run(["Hyprland", "--verify-config", "-c", str(tmp)],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and 'config ok' in out, out


def _exec_line() -> str:
    if shutil.which("umbral"):
        return "umbral"
    # Ejecutando desde el código fuente, sin instalar
    repo = Path(__file__).resolve().parent.parent
    return f'env PYTHONPATH={shlex.quote(str(repo))} {shlex.quote(sys.executable)} -m umbral'


def write_game_desktop(game_id: str, name: str) -> Path:
    paths.APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", game_id)
    f = paths.APPLICATIONS_DIR / f"umbral-{safe}.desktop"
    f.write_text(
        f'[Desktop Entry]\nType=Application\nName={name}\nComment=Lanzar con Umbral\nExec={_exec_line()} --launch {shlex.quote(game_id)}\nIcon={APP_ID}\nCategories=Game;\nTerminal=false\n')
    return f


def hyprland_float_rule(app_id: str, width: int, height: int) -> tuple[bool, str]:
    """Pide a Hyprland (en tiempo de ejecución, sin tocar la configuración) que la
    ventana de Umbral sea flotante y centrada. La regla vive hasta que se recargue
    Hyprland; Umbral la vuelve a añadir en cada arranque."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return False, _('no es Hyprland')
    rx = _lq("^" + re.escape(app_id) + "$")
    lua = (f'hl.window_rule({{ name = "umbral-window", match = {{ class = "{rx}" }}, '
           f'float = true, center = true, size = "{int(width)} {int(height)}" }})')
    try:
        r = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and out == "ok", out
