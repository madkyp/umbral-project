"""Progreso de las actualizaciones de Battle.net, leído del Agent (solo lectura).

Battle.net no descarga nada por sí mismo: lo hace el Agent (`Agent.exe`), un programa aparte que
escucha en un puerto local y al que Battle.net le manda órdenes por HTTP. Mientras Battle.net
actualiza un juego, `GET /update/<producto>` da el progreso. Se autentica con la cabecera
`Authorization: <clave>` (sin «Bearer»); la clave es de la sesión y el propio Agent la escribe en
su registro (`Agent/Agent.<versión>/Logs/Agent-*.log`). Verificado el 2026-10-12 con la
actualización de WoW Forever (de 6 % a 100 %, con velocidad y tiempo estimado).

El progreso se lee solo con GET. La única orden que Umbral manda es `POST /update` (la misma que
Battle.net envía al pulsar «Actualizar»), y únicamente cuando el usuario elige «Actualizar desde
Umbral» en un juego que ya tiene una versión nueva.

No se puede arrancar el Agent sin Battle.net: la clave de sesión no se escribe en la salida estándar,
ni en un archivo, ni en el registro de Wine (comprobado el 2026-10-12), y sin clientes conectados el
Agent se apaga solo a los ~20 s. Por eso Umbral abre Battle.net y habla con el Agent que este lanza.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import playtime
from .i18n import _

log = logging.getLogger(__name__)

UPDATING = 1003          # estado del Agent mientras descarga/aplica un parche (con download_complete=false)
_AUTH = re.compile(r'"authorization":\s*"(\d{6,})"')
TIMEOUT = 1.5


@dataclass
class Progress:
    fraction: float          # 0..1
    rate: int = 0            # bytes/s (0 mientras prepara o aplica)
    remaining: int = 0       # bytes por descargar
    eta: int = 0             # segundos (0 = desconocido)

    @property
    def percent(self) -> int:
        return max(0, min(100, int(self.fraction * 100)))


def parse_update(data: dict) -> Progress | None:
    """Respuesta de GET /update/<producto> → progreso, o None si no hay una actualización en curso.
    En curso = estado 1003 y descarga sin completar; con la descarga completa el Agent repite
    estados (1000, 1003, 1004, 1008) que no son una actualización."""
    if not isinstance(data, dict) or data.get("download_complete") or data.get("state") != UPDATING:
        return None
    rate = int(data.get("download_rate") or 0)
    eta = int(data.get("estimated_time_in_sec") or 0)
    return Progress(float(data.get("progress") or 0.0), rate, int(data.get("download_remaining") or 0),
                    eta if rate > 0 else 0)          # sin velocidad el tiempo estimado no significa nada


def describe(p: Progress) -> str:
    """Texto bajo la barra: «Actualizando 42 % · 12 MB/s · 8 min», «Preparando…» o «Aplicando…»."""
    if p.fraction <= 0 and p.rate <= 0:
        return _("Preparando la actualización…")
    if p.rate <= 0:
        return _("Aplicando la actualización… {0} %").format(p.percent)
    parts = [_("Actualizando {0} %").format(p.percent), f"{p.rate / 1e6:.1f} MB/s".replace(".0 ", " ")]
    if p.eta > 0:
        parts.append(playtime.format_duration(p.eta))
    return " · ".join(parts)


def agent_dir(prefix_path: str | Path) -> Path:
    return Path(prefix_path) / "drive_c/ProgramData/Battle.net/Agent"


def read_auth(prefix_path: str | Path) -> str:
    """Clave de la sesión del Agent: la del registro más reciente. «» si aún no está (el Agent la
    escribe cuando Battle.net le hace su primera petición, unos segundos después de arrancar). Nunca
    se usa la de una sesión anterior: ya no vale."""
    try:
        newest = max(agent_dir(prefix_path).glob("Agent.*/Logs/Agent-*.log"), key=lambda p: p.stat().st_mtime)
        with open(newest, errors="replace") as fh:
            m = _AUTH.search(fh.read(65536))
    except (OSError, ValueError):
        return ""
    return m.group(1) if m else ""


def _get(port: int, path: str, auth: str | None = None, payload: dict | None = None) -> tuple[int, bytes]:
    """GET (o POST con JSON si hay `payload`) al Agent: (código HTTP, cuerpo); (0, b"") si no responde."""
    headers = {"Authorization": auth} if auth else {}
    data = None
    if payload is not None:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b""
    except (OSError, ValueError):
        return 0, b""


def listening_ports(owner: str = "wineserver") -> list[int]:
    """Puertos TCP en escucha en 127.0.0.1 cuyo dueño es el proceso `owner` (Wine abre los
    sockets de Windows a nombre de wineserver). Así la clave nunca se manda a otros programas."""
    inodes: dict[str, int] = {}
    try:
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and f[1].startswith("0100007F:"):     # LISTEN en 127.0.0.1
                inodes[f[9]] = int(f[1].split(":")[1], 16)
    except OSError:
        return []
    found: list[int] = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if (d / "comm").read_text().strip() != owner:
                continue
            for fd in (d / "fd").iterdir():
                m = re.fullmatch(r"socket:\[(\d+)\]", str(fd.readlink()))
                if m and m.group(1) in inodes:
                    found.append(inodes[m.group(1)])
        except OSError:
            continue
    return sorted(set(found))


def find_port(auth: str, candidates: list[int]) -> int | None:
    """El puerto del Agent: sin credenciales responde 401 y con la clave `GET /agent` da 200.
    Battle.net, en otro puerto, responde 404, así que se descarta sin mandarle la clave."""
    for port in candidates:
        if _get(port, "/gamesession")[0] != 401:
            continue
        code, body = _get(port, "/agent", auth)
        if code == 200 and b"authorization" in body:
            return port
    return None


class AgentClient:
    """Lectura del progreso: recuerda el puerto mientras responda y lo vuelve a buscar si no."""

    def __init__(self, prefix_path: str, ports=listening_ports):
        self.prefix_path = str(prefix_path)
        self._ports = ports
        self._auth = ""
        self._port: int | None = None

    def _connect(self) -> bool:
        self._auth = read_auth(self.prefix_path)
        self._port = find_port(self._auth, self._ports()) if self._auth else None
        return self._port is not None

    def progress(self, product: str) -> Progress | None:
        """Progreso de la actualización de ese producto; None si no hay (o no se puede saber)."""
        if self._port is None and not self._connect():
            return None
        code, body = _get(self._port, f"/update/{product}", self._auth)
        if code in (0, 401):                     # el Agent se reinició: otra clave y otro puerto
            self._port = None
            if not self._connect():
                return None
            code, body = _get(self._port, f"/update/{product}", self._auth)
        if code != 200:
            return None
        try:
            return parse_update(json.loads(body))
        except ValueError:
            return None

    def request_update(self, product: str) -> bool | None:
        """Pide al Agent que actualice el juego (lo mismo que hace Battle.net al pulsar «Actualizar»).
        True si lo aceptó, False si lo rechazó, None si todavía no se puede hablar con él."""
        if self._port is None and not self._connect():
            return None
        body = {"uid": product, "priority": {"insert_at_head": True, "value": 699}}
        code, resp = _get(self._port, "/update", self._auth, body)
        if code in (0, 401):
            self._port = None
            return None
        try:
            return code == 200 and "response_uri" in json.loads(resp)
        except ValueError:
            return False

    def reset(self) -> None:
        self._port, self._auth = None, ""
