"""Modelo de datos y persistencia en ~/.config/umbral/config.json."""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

BATTLENET_ID = "battlenet"


@dataclass
class LaunchOptions:
    """Ajustes de lanzamiento. None = heredar del prefijo."""
    env: dict[str, str] = field(default_factory=dict)
    args: str = ""
    runner: str | None = None          # nombre de runner; None = el del prefijo
    use_wined3d: bool | None = None     # PROTON_USE_WINED3D (OpenGL en vez de DXVK)
    no_esync: bool | None = None        # PROTON_NO_ESYNC
    no_fsync: bool | None = None        # PROTON_NO_FSYNC
    no_ntsync: bool | None = None       # PROTON_NO_NTSYNC (solo GE-Proton)
    wayland: bool | None = None         # PROTON_ENABLE_WAYLAND
    writecopy: bool | None = None       # WINE_SIMULATE_WRITECOPY
    gpu_shader_cache: bool | None = None
    gamemode: bool | None = None
    mangohud: bool | None = None
    mangohud_preset: str | None = None  # "fps" | "basic" | "full"
    mangohud_position: str | None = None
    fps_limit: int | None = None        # 0 = sin límite
    gamescope: bool | None = None
    gamescope_args: str | None = None      # argumentos extra
    gs_resolution: str | None = None       # resolución del juego «WxH»; "" = la de la pantalla
    gs_mode: str | None = None             # fullscreen | borderless | window
    gs_scaler: str | None = None           # fit | integer | stretch
    gs_filter: str | None = None           # linear | nearest | fsr | pixel
    gpu: str | None = None              # "auto" o "vvvv:dddd" (ids PCI)
    fullscreen: bool | None = None      # ScummVM y emuladores: pantalla completa

    @classmethod
    def from_dict(cls, d: dict) -> "LaunchOptions":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


# Valores base del prefijo de Battle.net. Verificados: son los que usa la
# instalación funcional existente (Faugus) y ambos los lee el script `proton`
# de GE-Proton 11 y Proton-CachyOS 11.
BATTLENET_DEFAULTS = LaunchOptions(
    use_wined3d=False, no_esync=False, no_fsync=False, no_ntsync=False,
    wayland=False, writecopy=True, gpu_shader_cache=True,
    gamemode=False, mangohud=False, gamescope=False, gamescope_args="",
    gpu="auto",
)


@dataclass
class Prefix:
    id: str
    name: str
    path: str
    runner: str
    options: LaunchOptions = field(default_factory=LaunchOptions)
    imported: bool = False   # prefijo externo (p. ej. de Faugus): nunca se recrea
    last_runner: str = ""    # último Proton con el que se abrió (para detectar cambios de versión)

    @classmethod
    def from_dict(cls, d: dict) -> "Prefix":
        d = dict(d)
        d["options"] = LaunchOptions.from_dict(d.get("options", {}))
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


@dataclass
class Game:
    id: str
    name: str
    kind: str                 # "battlenet" | "blizzard" | "custom" (Wine) | "scummvm" | "emulator" | "vm"
    prefix_id: str            # "" en los que no van por Wine
    exe: str                  # ruta Linux absoluta al .exe (o carpeta de ScummVM, ROM, disco de la VM)
    options: LaunchOptions = field(default_factory=LaunchOptions)
    product: str = ""         # uid de Battle.net (p. ej. wow_classic_beta)
    auto: bool = False        # generado por el detector; se refresca solo
    hidden: bool = False
    cover: str = ""           # imagen elegida por el usuario (copia en ~/.local/share/umbral/covers)
    icon: str = ""            # icono elegido por el usuario (sustituye al extraído del .exe)
    playtime: int = 0         # segundos jugados en total
    last_played: str = ""     # ISO 8601 de la última partida
    system: str = ""          # emuladores: gba, ps1, ps2, gc… · VM: win9x
    target: str = ""          # ScummVM: id del juego (p. ej. pink:peril)
    cdrom: str = ""           # VM: imagen del CD
    search_name: str = ""     # título original detectado (ScummVM, ROM…) para buscar portadas

    @classmethod
    def from_dict(cls, d: dict) -> "Game":
        d = dict(d)
        d["options"] = LaunchOptions.from_dict(d.get("options", {}))
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


@dataclass
class Settings:
    accent: str = ""          # "" = acento del sistema/tema GTK
    default_runner: str = "GE-Proton"
    prefix_root: str = str(paths.DEFAULT_PREFIX_ROOT)
    games_root: str = str(paths.DEFAULT_PREFIX_ROOT / "games")   # donde se mueven los juegos añadidos
    run_in_tray: bool = True
    minimize_on_launch: bool = True   # ocultar/minimizar Umbral al lanzar un juego
    language: str = ""           # "" = el del sistema, "es" o "en" (se aplica al reiniciar)     # al cerrar la ventana, seguir en segundo plano con icono en la bandeja
    float_window: bool = True    # Hyprland: ventana flotante y centrada, como un diálogo
    window_width: int = 1180
    window_height: int = 680
    first_run_done: bool = False
    library_filter: str = "all"  # filtro de «Mis juegos»: all, windows, scummvm, sys:<sistema>, other
    library_sort: str = "name"   # orden de «Mis juegos»: name, recent, playtime


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    prefixes: list[Prefix] = field(default_factory=list)
    games: list[Game] = field(default_factory=list)

    # -- consultas --
    def prefix(self, pid: str) -> Prefix | None:
        return next((p for p in self.prefixes if p.id == pid), None)

    def game(self, gid: str) -> Game | None:
        return next((g for g in self.games if g.id == gid), None)

    def battlenet_prefix(self) -> Prefix | None:
        return self.prefix(BATTLENET_ID)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:10]

    # -- persistencia --
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        s = Settings(**{k: v for k, v in d.get("settings", {}).items()
                        if k in {f.name for f in fields(Settings)}})
        return cls(
            settings=s,
            prefixes=[Prefix.from_dict(p) for p in d.get("prefixes", [])],
            games=[Game.from_dict(g) for g in d.get("games", [])],
        )

    @classmethod
    def load(cls, path: Path = paths.CONFIG_FILE) -> "Config":
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError) as e:
            backup = path.with_suffix(".json.bad")
            log.error("Configuración ilegible (%s); se guarda copia en %s", e, backup)
            try:
                path.replace(backup)
            except OSError:
                pass
            return cls()

    def save(self, path: Path = paths.CONFIG_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        os.replace(tmp, path)


def merged(*layers: LaunchOptions) -> LaunchOptions:
    """Combina capas de opciones; las posteriores ganan si no son None."""
    out = LaunchOptions()
    for layer in layers:
        for f in fields(LaunchOptions):
            v = getattr(layer, f.name)
            if f.name == "env":
                out.env = {**out.env, **v}
            elif f.name == "args":
                out.args = v or out.args
            elif v is not None:
                setattr(out, f.name, v)
    return out
