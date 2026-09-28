"""Rutas XDG de la aplicación."""
import os
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or HOME / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "umbral"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "umbral"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "umbral"
DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
LOG_DIR = STATE_DIR / "logs"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PREFIX_ROOT = HOME / "Games" / "umbral"
RUNNER_INSTALL_DIR = DATA_HOME / "umbral" / "runners"
COVERS_DIR = DATA_HOME / "umbral" / "covers"
APPLICATIONS_DIR = DATA_HOME / "applications"

# Directorios donde otras herramientas instalan Proton; se leen, no se tocan.
PROTON_SEARCH_DIRS = [
    RUNNER_INSTALL_DIR,
    DATA_HOME / "Steam" / "compatibilitytools.d",
    HOME / ".steam" / "root" / "compatibilitytools.d",
    Path("/usr/share/steam/compatibilitytools.d"),
    DATA_HOME / "umu",
]


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, LOG_DIR, CACHE_DIR, RUNNER_INSTALL_DIR):
        d.mkdir(parents=True, exist_ok=True)
