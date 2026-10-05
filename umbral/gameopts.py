"""Opciones de un juego desde fuera de la interfaz: `umbral --get/--set` (p. ej. Gaming Deck).

Claves públicas y estables (independientes de los nombres internos de LaunchOptions):

    gamemode, mangohud, wayland, writecopy, wined3d, shader_cache, gamescope   on|off|default
    esync, fsync, ntsync                                                       on|off|default
    mangohud_preset   fps|basic|full          mangohud_position  top-left|top-right|bottom-left|bottom-right
    fps_limit         0 (sin límite)…1000
    gamescope_resolution  WxH|screen          gamescope_mode     fullscreen|borderless|window
    gamescope_scaler  fit|integer|stretch     gamescope_filter   linear|nearest|fsr|pixel
    gamescope_args, args                      texto libre
    fullscreen        on|off|default         (ScummVM y emuladores)
    gpu               auto|vvvv:dddd (ids PCI)
    runner            nombre de un Proton instalado o GE-Proton
    env.NOMBRE=valor  variable de entorno (env.NOMBRE= la quita)

«default» (o un valor vacío) vuelve a heredar del prefijo. En Battle.net las opciones son las del
prefijo, como en su ⚙; los juegos de Blizzard usan el Proton de ese prefijo.
"""
from __future__ import annotations

import re
from dataclasses import fields

from .config import BATTLENET_ID, Config, Game, LaunchOptions
from .i18n import _
from .launcher import GS_FILTERS, GS_MODES, GS_SCALERS, MANGOHUD_POSITIONS, MANGOHUD_PRESETS, effective_options

TRUE = {"on", "true", "1", "yes", "si", "sí"}
FALSE = {"off", "false", "0", "no"}
DEFAULT = {"default", "inherit", ""}

# clave pública -> (campo interno, tipo, invertido)
BOOLS = {"gamemode": "gamemode", "mangohud": "mangohud", "wayland": "wayland", "writecopy": "writecopy",
         "wined3d": "use_wined3d", "shader_cache": "gpu_shader_cache", "gamescope": "gamescope",
         "fullscreen": "fullscreen"}
INVERTED = {"esync": "no_esync", "fsync": "no_fsync", "ntsync": "no_ntsync"}
ENUMS = {"mangohud_preset": ("mangohud_preset", list(MANGOHUD_PRESETS)),
         "mangohud_position": ("mangohud_position", list(MANGOHUD_POSITIONS)),
         "gamescope_mode": ("gs_mode", list(GS_MODES)),
         "gamescope_scaler": ("gs_scaler", list(GS_SCALERS)),
         "gamescope_filter": ("gs_filter", list(GS_FILTERS))}
TEXTS = {"gamescope_args": "gamescope_args", "args": "args"}
SPECIAL = ("fps_limit", "gamescope_resolution", "gpu", "runner")
KEYS = sorted([*BOOLS, *INVERTED, *ENUMS, *TEXTS, *SPECIAL]) + ["env.NOMBRE"]


class OptionError(ValueError):
    pass


def _bool(key: str, value: str) -> bool | None:
    v = value.strip().lower()
    if v in DEFAULT:
        return None
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    raise OptionError(_("«{0}» espera on, off o default (no «{1}»).").format(key, value))


def parse(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if "=" not in p:
            raise OptionError(_("«{0}» no tiene el formato clave=valor.").format(p))
        k, v = p.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def apply(cfg: Config, game: Game, pairs: list[tuple[str, str]], runner_names: list[str]) -> list[str]:
    """Valida todo y, solo si todo es correcto, lo aplica. Devuelve las claves cambiadas."""
    prefix = cfg.prefix(game.prefix_id)
    target = prefix.options if game.id == BATTLENET_ID and prefix else game.options
    staged: list[tuple] = []          # (tipo, campo, valor)
    for key, value in pairs:
        if key.startswith("env."):
            name = key[4:]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise OptionError(_("Nombre de variable no válido: «{0}».").format(name))
            staged.append(("env", name, value))
        elif key in BOOLS:
            staged.append(("field", BOOLS[key], _bool(key, value)))
        elif key in INVERTED:
            b = _bool(key, value)
            staged.append(("field", INVERTED[key], None if b is None else not b))
        elif key in ENUMS:
            fld, allowed = ENUMS[key]
            v = value.strip().lower()
            if v in DEFAULT:
                staged.append(("field", fld, None))
            elif v in allowed:
                staged.append(("field", fld, v))
            else:
                raise OptionError(_("«{0}» admite: {1}.").format(key, ", ".join(allowed)))
        elif key in TEXTS:
            staged.append(("field", TEXTS[key], None if value.lower() == "default" else value))
        elif key == "fps_limit":
            if value.lower() in DEFAULT:
                staged.append(("field", "fps_limit", None))
            elif value.isdigit() and int(value) <= 1000:
                staged.append(("field", "fps_limit", int(value)))
            else:
                raise OptionError(_("«fps_limit» espera un número de 0 (sin límite) a 1000."))
        elif key == "gamescope_resolution":
            v = value.lower().replace("×", "x")
            if v in DEFAULT:
                staged.append(("field", "gs_resolution", None))
            elif v == "screen":
                staged.append(("field", "gs_resolution", ""))
            elif re.fullmatch(r"\d{3,5}x\d{3,5}", v):
                staged.append(("field", "gs_resolution", v))
            else:
                raise OptionError(_("«gamescope_resolution» espera ANCHOxALTO (p. ej. 640x480) o screen."))
        elif key == "gpu":
            v = value.lower()
            if v in DEFAULT:
                staged.append(("field", "gpu", None))
            elif v == "auto" or re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", v):
                staged.append(("field", "gpu", v))
            else:
                raise OptionError(_("«gpu» espera auto o los ids PCI vvvv:dddd (p. ej. 10de:1f07)."))
        elif key == "runner":
            if not game.prefix_id:
                raise OptionError(_("«{0}» no usa Wine: no tiene Proton que cambiar.").format(game.name))
            if game.kind == "blizzard":
                raise OptionError(_("Los juegos de Blizzard usan el Proton de Battle.net: "
                                    "cámbialo con «--set battlenet runner=…»."))
            if value.lower() in DEFAULT:
                if game.id == BATTLENET_ID:
                    raise OptionError(_("Battle.net necesita un Proton concreto."))
                staged.append(("field", "runner", None))
            elif value in runner_names:
                staged.append(("prefix_runner" if game.id == BATTLENET_ID else "field", "runner", value))
            else:
                raise OptionError(_("Proton no instalado: «{0}». Disponibles: {1}.")
                                  .format(value, ", ".join(runner_names)))
        else:
            raise OptionError(_("Clave desconocida «{0}». Claves válidas: {1}.").format(key, ", ".join(KEYS)))
    changed = []
    for kind, fld, value in staged:
        if kind == "env":
            if value == "":
                target.env.pop(fld, None)
            else:
                target.env[fld] = value
            changed.append(f"env.{fld}")
        elif kind == "prefix_runner":
            prefix.runner = value
            changed.append("runner")
        else:
            setattr(target, fld, value)
            changed.append(fld)
    return changed


def _public(opts: LaunchOptions) -> dict:
    """LaunchOptions -> claves públicas (None = hereda)."""
    out: dict = {k: getattr(opts, f) for k, f in BOOLS.items()}
    for k, f in INVERTED.items():
        v = getattr(opts, f)
        out[k] = None if v is None else not v
    for k, (f, _allowed) in ENUMS.items():
        out[k] = getattr(opts, f)
    for k, f in TEXTS.items():
        out[k] = getattr(opts, f) or None          # texto vacío = sin ajuste
    out["fps_limit"] = opts.fps_limit
    out["gamescope_resolution"] = (None if opts.gs_resolution is None
                                   else (opts.gs_resolution or "screen"))
    out["gpu"] = opts.gpu
    out["runner"] = opts.runner
    return out


def describe(cfg: Config, game: Game) -> dict:
    """Para `umbral --get`: opciones puestas a mano y las efectivas al lanzar."""
    prefix = cfg.prefix(game.prefix_id)
    own = prefix.options if game.id == BATTLENET_ID and prefix else game.options
    eff = effective_options(cfg, prefix, None if game.id == BATTLENET_ID else game)
    effective = _public(eff)
    effective["runner"] = eff.runner or (prefix.runner if prefix else None)
    for k in ("esync", "fsync", "ntsync"):                 # sin ajuste explícito, Proton los usa
        if effective[k] is None:
            effective[k] = True
    return {"id": game.id, "name": game.name, "kind": game.kind,
            "prefix": {"id": prefix.id, "path": prefix.path, "runner": prefix.runner} if prefix else None,
            "options": _public(own), "env": dict(own.env),
            "effective": effective, "effective_env": dict(eff.env),
            "keys": KEYS}


def listing(cfg: Config) -> list[dict]:
    return [{"id": g.id, "name": g.name, "kind": g.kind, "prefix": g.prefix_id, "exe": g.exe}
            for g in cfg.games if not g.hidden]


def known_fields() -> set[str]:
    return {f.name for f in fields(LaunchOptions)}
