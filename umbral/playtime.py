"""Tiempo de juego: formato legible y «última partida»."""
from __future__ import annotations

import datetime as dt

from .i18n import _


def format_duration(seconds: int) -> str:
    """3 h 20 min · 45 min · menos de 1 min."""
    minutes = int(seconds) // 60
    if minutes < 1:
        return _("menos de 1 min")
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return _("{0} min").format(minutes)
    if minutes == 0:
        return _("{0} h").format(hours)
    return _("{0} h {1} min").format(hours, minutes)


def format_ago(iso: str, now: dt.datetime | None = None) -> str:
    """hoy · ayer · hace 3 días · 12/08/2026."""
    if not iso:
        return ""
    try:
        when = dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    now = now or dt.datetime.now()
    days = (now.date() - when.date()).days
    if days <= 0:
        return _("hoy")
    if days == 1:
        return _("ayer")
    if days < 7:
        return _("hace {0} días").format(days)
    return when.strftime("%d/%m/%Y")


def summary(seconds: int, last_played: str, now: dt.datetime | None = None) -> str:
    """Texto de la tarjeta: «3 h 20 min · hoy» ('' si nunca se ha jugado)."""
    if seconds <= 0 and not last_played:
        return ""
    parts = [format_duration(seconds)]
    ago = format_ago(last_played, now)
    if ago:
        parts.append(ago)
    return " · ".join(parts)
