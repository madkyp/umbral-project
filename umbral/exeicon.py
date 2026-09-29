"""Extrae el icono de mayor tamaño de un ejecutable de Windows (PE).

Lee solo las cabeceras y los recursos RT_GROUP_ICON/RT_ICON, sin dependencias
externas salvo Pillow para convertir iconos BMP antiguos. El resultado se
guarda en caché como PNG; nunca se descarga nada.
"""
from __future__ import annotations

import hashlib
import io
import logging
import mmap
import struct
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

RT_ICON, RT_GROUP_ICON = 3, 14
ICON_CACHE = paths.CACHE_DIR / "icons"
ASSETS = Path(__file__).resolve().parent / "assets"

# Iconos incluidos para productos cuyo .exe no trae uno bueno (uid -> archivo).
# Se comparan por prefijo: "wow_forever" cubre también el nombre final del producto.
PRODUCT_ICONS = {"wow_classic_beta": "wow-forever.png", "wow_forever": "wow-forever.png"}


def product_icon(uid: str) -> Path | None:
    for prefix, name in PRODUCT_ICONS.items():
        if uid.startswith(prefix) and (ASSETS / name).exists():
            return ASSETS / name
    return None


class PEError(Exception):
    pass


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


class _PE:
    def __init__(self, data):
        self.d = data
        if data[:2] != b"MZ":
            raise PEError("no es un ejecutable MZ")
        pe = _u32(data, 0x3C)
        if data[pe:pe + 4] != b"PE\0\0":
            raise PEError("firma PE ausente")
        coff = pe + 4
        nsec, optsize = _u16(data, coff + 2), _u16(data, coff + 16)
        opt = coff + 20
        magic = _u16(data, opt)
        ddir = opt + (96 if magic == 0x10B else 112)
        self.res_rva = _u32(data, ddir + 2 * 8)
        if not self.res_rva:
            raise PEError("sin sección de recursos")
        self.sections = []
        s = opt + optsize
        for i in range(nsec):
            o = s + i * 40
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
            self.sections.append((va, max(vsize, rawsize), rawptr))

    def off(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return raw + rva - va
        raise PEError(f"RVA {rva:#x} fuera de las secciones")

    def _entries(self, dir_off):
        d = self.d
        n = _u16(d, dir_off + 12) + _u16(d, dir_off + 14)
        for i in range(n):
            name, target = struct.unpack_from("<II", d, dir_off + 16 + i * 8)
            yield name, target

    def resources(self, rtype):
        """{id: bytes} de un tipo de recurso (primer idioma de cada id)."""
        base = self.off(self.res_rva)
        out = {}
        for tid, tt in self._entries(base):
            if tid != rtype or not tt & 0x80000000:
                continue
            for rid, rt in self._entries(base + (tt & 0x7FFFFFFF)):
                if not rt & 0x80000000:
                    continue
                for _lang, lt in self._entries(base + (rt & 0x7FFFFFFF)):
                    if lt & 0x80000000:
                        continue
                    rva, size = struct.unpack_from("<II", self.d, base + lt)
                    o = self.off(rva)
                    out[rid] = bytes(self.d[o:o + size])
                    break
        return out


def extract_largest_icon(exe: Path) -> bytes:
    """PNG del icono más grande del .exe."""
    with open(exe, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pe = _PE(mm)
        groups = pe.resources(RT_GROUP_ICON)
        if not groups:
            raise PEError("el ejecutable no tiene iconos")
        icons = pe.resources(RT_ICON)
    best = None  # (tamaño, bits, id)
    for grp in groups.values():
        count = _u16(grp, 4)
        for i in range(count):
            w, h, _c, _r, _p, bits, _sz, nid = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
            size = w or 256
            if nid in icons and (best is None or (size, bits) > best[:2]):
                best = (size, bits, nid)
    if best is None:
        raise PEError("grupo de iconos sin imágenes")
    img = icons[best[2]]
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return img
    # Icono BMP clásico: se envuelve en un .ico de una imagen y lo convierte Pillow
    from PIL import Image
    size = best[0] % 256
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", size, size, 0, 0, 1, best[1],
                                                      len(img), 22) + img
    out = io.BytesIO()
    Image.open(io.BytesIO(ico)).save(out, "PNG")
    return out.getvalue()


def cached_icon(exe: str) -> Path | None:
    """Ruta del PNG en caché (se regenera si cambia el .exe). None si no hay icono."""
    p = Path(exe)
    try:
        st = p.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    target = ICON_CACHE / f"{key}.png"
    if target.exists():
        return target
    try:
        png = extract_largest_icon(p)
    except (PEError, OSError, ValueError, struct.error) as e:
        log.info("Sin icono para %s: %s", p.name, e)
        return None
    except Exception as e:  # Pillow u otros formatos raros: nunca debe romper la biblioteca
        log.warning("No se pudo convertir el icono de %s: %s", p.name, e)
        return None
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return target


def icon_candidates(exe: str, kind: str) -> list[Path]:
    """Ejecutables de los que sacar el icono, en orden de preferencia.

    En juegos de Blizzard el lanzador de la carpeta raíz lleva el icono del
    producto; el cliente beta de WoW (WowB.exe) solo tiene uno genérico «TEST».
    """
    p = Path(exe)
    out: list[Path] = []
    if kind == "blizzard":
        for root in (p.parent, *p.parents[:3]):
            if (root / ".build.info").exists():
                out += sorted(root.glob("*Launcher.exe"))
                break
    out.append(p)
    return out


def best_icon(exe: str, kind: str) -> Path | None:
    """El icono más grande entre los candidatos (empate: el primero)."""
    from PIL import Image
    best, best_size = None, 0
    for cand in icon_candidates(exe, kind):
        png = cached_icon(str(cand))
        if png is None:
            continue
        try:
            size = Image.open(png).size[0]
        except OSError:
            continue
        if size > best_size:
            best, best_size = png, size
    return best


def dominant_color(png: Path) -> str:
    """Color medio de los píxeles opacos y saturados, para teñir la portada."""
    from PIL import Image
    img = Image.open(png).convert("RGBA").resize((32, 32))
    px = [(r, g, b) for r, g, b, a in img.getdata() if a > 200 and max(r, g, b) - min(r, g, b) > 40]
    if not px:
        return "#2a3350"
    r, g, b = (sum(c[i] for c in px) // len(px) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def ball_thumbnail(image: str, crop: bool, size: int = 256, radius: int = 44) -> Path | None:
    """Miniatura cuadrada para el hueco del logo de la tarjeta (se cachea).

    crop=True (portadas/fotos): recorte centrado a cuadrado y esquinas redondeadas.
    crop=False (iconos): se encaja entero respetando la transparencia.
    """
    from PIL import Image, ImageDraw, ImageOps
    p = Path(image)
    try:
        st = p.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}|{crop}|{size}".encode()).hexdigest()[:16]
    target = ICON_CACHE / f"ball-{key}.png"
    if target.exists():
        return target
    try:
        img = Image.open(p).convert("RGBA")
    except (OSError, ValueError) as e:
        log.warning("Imagen no válida %s: %s", p, e)
        return None
    if crop:
        img = ImageOps.fit(img, (size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        img.putalpha(Image.composite(img.getchannel("A"), mask, mask))
    else:
        img = ImageOps.contain(img, (size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
        img = canvas
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target
