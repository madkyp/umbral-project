"""Traducción mínima: los textos se escriben en español y _() devuelve la versión
inglesa de locale/en.json cuando el idioma es inglés. Si falta una traducción se
muestra el texto en español (nunca rompe la interfaz).

El idioma se decide al importar el módulo (antes que la interfaz) leyendo
settings.language de la configuración: "es", "en" o "" (= el del sistema).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

LANGUAGES = {"": "Sistema", "es": "Español", "en": "English"}
_EN: dict[str, str] = {}


def system_language() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var, "")
        if v:
            return "es" if v.lower().startswith("es") else "en"
    return "es"


def configured_language() -> str:
    try:
        return json.loads(paths.CONFIG_FILE.read_text()).get("settings", {}).get("language", "")
    except (OSError, ValueError):
        return ""


def resolve(lang: str) -> str:
    return lang if lang in ("es", "en") else system_language()


LANG = resolve(configured_language())
if LANG == "en":
    try:
        _EN = json.loads((Path(__file__).parent / "locale" / "en.json").read_text())
    except (OSError, ValueError):
        _EN = {}


def _(text: str) -> str:
    return _EN.get(text, text) if LANG == "en" else text
