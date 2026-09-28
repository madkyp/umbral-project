"""Detección de GPU, diagnóstico por fabricante y perfiles de entorno.

Las funciones parse_* y analyze() son puras (se prueban con salidas
simuladas); gather() es la única que toca el sistema.

Fuentes de cada variable:
- __NV_PRIME_RENDER_OFFLOAD, __VK_LAYER_NV_optimus, __GLX_VENDOR_LIBRARY_NAME:
  README de NVIDIA, capítulo "PRIME Render Offload".
- DRI_PRIME=pci-XXXX_XX_XX_X, MESA_VK_DEVICE_SELECT=vid:did: docs.mesa3d.org/envvars.
- DXVK_FILTER_DEVICE_NAME / VKD3D_FILTER_DEVICE_NAME: README de DXVK y vkd3d-proton.
- Caché de shaders: PROTON_LOCAL_SHADER_CACHE + STEAM_COMPAT_SHADER_PATH, que
  GE-Proton traduce a las variables propias de NVIDIA o Mesa
  (protonfixes/utilities.py: setup_local_shader_cache).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from .i18n import _

VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
VENDOR_LABEL = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel", "other": "Otra"}

# vkd3d-proton (README): mínimo NVIDIA 535; RADV Mesa 22.0.
NVIDIA_MIN_DRIVER = (535, 0)
MESA_MIN = (22, 0)

PKGS = {
    "common": ["vulkan-icd-loader", "lib32-vulkan-icd-loader"],
    "nvidia": ["nvidia-utils", "lib32-nvidia-utils"],
    "amd": ["vulkan-radeon", "lib32-vulkan-radeon", "mesa", "lib32-mesa"],
    "intel": ["vulkan-intel", "lib32-vulkan-intel", "mesa", "lib32-mesa"],
    "conflict_amd": ["amdvlk", "lib32-amdvlk"],
}


@dataclass
class Gpu:
    slot: str               # "0000:01:00.0"
    vendor: str             # nvidia | amd | intel | other
    vendor_id: str
    device_id: str
    name: str
    driver: str = ""        # driver del kernel en uso
    boot_vga: bool = False  # GPU que muestra el escritorio

    @property
    def pci_id(self) -> str:
        return f"{self.vendor_id}:{self.device_id}"

    @property
    def dri_prime(self) -> str:
        return "pci-" + re.sub(r"[:.]", "_", self.slot)


@dataclass
class VkDevice:
    name: str
    vendor_id: str
    device_id: str
    device_type: str
    driver_name: str = ""
    driver_info: str = ""
    api_version: str = ""


@dataclass
class Issue:
    level: str              # "error" | "warning" | "info"
    message: str
    fix: str = ""           # comando sugerido (nunca se ejecuta solo)


@dataclass
class GpuReport:
    gpus: list[Gpu] = field(default_factory=list)
    vk: list[VkDevice] = field(default_factory=list)
    packages: dict[str, str] = field(default_factory=dict)   # nombre -> versión
    nvidia_modeset: bool | None = None
    nvidia_fbdev: bool | None = None
    mesa_version: str = ""
    issues: list[Issue] = field(default_factory=list)

    def vk_for(self, gpu: Gpu) -> VkDevice | None:
        return next((v for v in self.vk if v.vendor_id == gpu.vendor_id
                     and v.device_id == gpu.device_id), None)

    def default_gpu(self) -> Gpu | None:
        return pick_default(self.gpus, self.vk)

    def summary(self) -> str:
        out = []
        for g in self.gpus:
            v = self.vk_for(g)
            drv = v.driver_info if v else _('sin Vulkan')
            out.append(f"{VENDOR_LABEL[g.vendor]} {g.name} · {g.driver or '?'} · {drv}")
        return "\n".join(out) or _('No se detectó ninguna GPU')


# ---------------------------------------------------------------- parseo

_LSPCI_DEV = re.compile(
    r"^(?P<slot>[0-9a-f:.]+)\s+(?P<cls>VGA compatible controller|3D controller|Display controller)"
    r"\s+\[03(?:00|02|80)\]:\s+(?P<name>.*?)\s+\[(?P<vid>[0-9a-f]{4}):(?P<did>[0-9a-f]{4})\]",
    re.I)


def parse_lspci(text: str) -> list[Gpu]:
    """Parsea `lspci -Dnnk` (o `lspci -nnk`, sin dominio)."""
    gpus: list[Gpu] = []
    cur: Gpu | None = None
    for line in text.splitlines():
        m = _LSPCI_DEV.match(line)
        if m:
            slot = m["slot"]
            if slot.count(":") == 1:
                slot = "0000:" + slot
            vid = m["vid"].lower()
            name = re.sub(r"^(NVIDIA Corporation|Advanced Micro Devices, Inc\. \[AMD/ATI\]|Intel Corporation)\s+",
                          "", m["name"])
            cur = Gpu(slot, VENDORS.get(vid, "other"), vid, m["did"].lower(), name)
            gpus.append(cur)
        elif cur and line.startswith("\t"):
            if 'Kernel driver in use:' in line:
                cur.driver = line.split(":", 1)[1].strip()
        elif line and not line.startswith("\t"):
            cur = None
    return gpus


def parse_vulkaninfo(text: str) -> list[VkDevice]:
    """Parsea `vulkaninfo --summary`."""
    devs: list[VkDevice] = []
    block: dict[str, str] | None = None
    for line in text.splitlines():
        if re.match(r"^GPU\d+:", line.strip()):
            block = {}
            devs.append(block)  # type: ignore[arg-type]
            continue
        if block is not None and "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            block[k] = v
    out = []
    for b in devs:
        if not b:
            continue
        out.append(VkDevice(
            name=b.get("deviceName", "?"),
            vendor_id=b.get("vendorID", "0x0").lower().removeprefix("0x").zfill(4),
            device_id=b.get("deviceID", "0x0").lower().removeprefix("0x").zfill(4),
            device_type=b.get("deviceType", ""),
            driver_name=b.get("driverName", ""),
            driver_info=b.get("driverInfo", ""),
            api_version=b.get("apiVersion", ""),
        ))
    return out


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3]) or (0,)


def pick_default(gpus: list[Gpu], vk: list[VkDevice]) -> Gpu | None:
    """GPU preferida para jugar: la dedicada con Vulkan; si no, la primera."""
    if not gpus:
        return None

    def score(g: Gpu) -> tuple:
        v = next((d for d in vk if d.vendor_id == g.vendor_id and d.device_id == g.device_id), None)
        discrete = bool(v and "DISCRETE" in v.device_type)
        vendor_rank = {"nvidia": 2, "amd": 1}.get(g.vendor, 0)
        return (v is not None, discrete, vendor_rank)

    return max(gpus, key=score)


# ---------------------------------------------------------------- diagnóstico

def pacman_cmd(pkgs: list[str]) -> str:
    return 'sudo pacman -S --needed ' + " ".join(pkgs)


def analyze(r: GpuReport) -> list[Issue]:
    issues: list[Issue] = []
    if not r.gpus:
        return [Issue("error", _('No se ha detectado ninguna GPU con lspci.'))]
    vendors = {g.vendor for g in r.gpus}

    missing = [p for p in PKGS["common"] if p not in r.packages]
    for v in vendors & {"nvidia", "amd", "intel"}:
        missing += [p for p in PKGS[v] if p not in r.packages and p not in missing]
    if missing:
        issues.append(Issue("error", _('Faltan paquetes gráficos (Vulkan de 32/64 bits): ')
                            + ", ".join(missing), pacman_cmd(missing)))

    for g in r.gpus:
        v = r.vk_for(g)
        label = f"{VENDOR_LABEL[g.vendor]} {g.name}"
        if g.vendor == "nvidia":
            if g.driver == "nouveau":
                issues.append(Issue("error", _('{0} usa nouveau: DXVK/VKD3D necesitan el driver propietario o nvidia-open de NVIDIA.').format(label),
                                    _('Instala el módulo NVIDIA de tu kernel (p. ej. nvidia-open-dkms)')))
            if v and _ver(v.driver_info) < NVIDIA_MIN_DRIVER:
                issues.append(Issue("warning", _('Driver NVIDIA {0} < 535: vkd3d-proton (juegos D3D12 como WoW) requiere 535 o superior.').format(v.driver_info)))
            if r.nvidia_modeset is False:
                issues.append(Issue("error", _('nvidia_drm.modeset no está activo; Hyprland lo necesita.'),
                                    _('Añade nvidia_drm.modeset=1 a la línea del kernel')))
            if r.nvidia_fbdev is False:
                issues.append(Issue("info", _('nvidia_drm.fbdev=0: recomendado activarlo (driver ≥ 545).'),
                                    _('Añade nvidia_drm.fbdev=1 a la línea del kernel')))
        elif g.vendor == "amd":
            if g.driver and g.driver != "amdgpu":
                issues.append(Issue("error", _("{0} usa el driver '{1}'; Vulkan (RADV) requiere amdgpu.").format(label, g.driver),
                                    _('Parámetros: radeon.si_support=0 amdgpu.si_support=1 (o cik_support según la generación)')))
            conflicts = [p for p in PKGS["conflict_amd"] if p in r.packages]
            if conflicts:
                issues.append(Issue("warning", _('AMDVLK instalado junto a RADV; Proton puede elegir el ICD equivocado. Recomendado usar solo RADV.'),
                                    'sudo pacman -Rns ' + " ".join(conflicts)))
            if r.mesa_version and _ver(r.mesa_version) < MESA_MIN:
                issues.append(Issue("warning", _('Mesa {0} < 22.0: insuficiente para vkd3d-proton.').format(r.mesa_version)))
        if v is None and g.vendor in ("nvidia", "amd", "intel"):
            issues.append(Issue("error", _('{0} no aparece en Vulkan: sin Vulkan no funcionan DXVK ni VKD3D-Proton.').format(label),
                                pacman_cmd(PKGS["common"] + PKGS[g.vendor])))
    if len(r.gpus) > 1:
        d = r.default_gpu()
        issues.append(Issue("info", _('Sistema multi-GPU: por defecto se usará {0} {1}. Puedes cambiarlo por juego.').format(VENDOR_LABEL[d.vendor], d.name)))
    return issues


# ---------------------------------------------------------------- entorno

def gpu_env(r: GpuReport, choice: str | None) -> tuple[dict[str, str], Gpu | None]:
    """Variables para dirigir el juego a una GPU. choice: None/"auto" o "vid:did"."""
    target = None
    if choice and choice != "auto":
        target = next((g for g in r.gpus if g.pci_id == choice), None)
    target = target or r.default_gpu()
    env: dict[str, str] = {}
    if target is None or len(r.gpus) < 2:
        return env, target  # una sola GPU: nada que elegir, el driver sabe qué hacer

    primary = next((g for g in r.gpus if g.boot_vga), None)
    vk = r.vk_for(target)
    if target.vendor == "nvidia":
        if primary is None or primary.slot != target.slot:
            env.update({"__NV_PRIME_RENDER_OFFLOAD": "1",
                        "__VK_LAYER_NV_optimus": "NVIDIA_only",
                        "__GLX_VENDOR_LIBRARY_NAME": "nvidia"})
    else:
        env["DRI_PRIME"] = target.dri_prime
        env["MESA_VK_DEVICE_SELECT"] = target.pci_id
        if any(g.vendor == "nvidia" for g in r.gpus):
            env["__VK_LAYER_NV_optimus"] = "non_NVIDIA_only"
    if vk:
        # Filtro por nombre, neutral respecto al fabricante (DXVK y vkd3d-proton).
        env["DXVK_FILTER_DEVICE_NAME"] = vk.name
        env["VKD3D_FILTER_DEVICE_NAME"] = vk.name
    return env, target


# ---------------------------------------------------------------- sistema

def _run(cmd: list[str], timeout: float = 10) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_bool(p: str) -> bool | None:
    try:
        return Path(p).read_text().strip() in ("Y", "1")
    except OSError:
        return None


def installed_packages(names: list[str]) -> dict[str, str]:
    out = _run(["pacman", "-Q", *names])
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def gather() -> GpuReport:
    r = GpuReport()
    r.gpus = parse_lspci(_run(["lspci", "-Dnnk"]))
    for g in r.gpus:
        g.boot_vga = _read_bool(f"/sys/bus/pci/devices/{g.slot}/boot_vga") or False
    r.vk = parse_vulkaninfo(_run(["vulkaninfo", "--summary"], timeout=20))
    all_pkgs = sorted({p for v in PKGS.values() for p in v})
    r.packages = installed_packages(all_pkgs)
    if any(g.vendor == "nvidia" for g in r.gpus):
        r.nvidia_modeset = _read_bool("/sys/module/nvidia_drm/parameters/modeset")
        r.nvidia_fbdev = _read_bool("/sys/module/nvidia_drm/parameters/fbdev")
    r.mesa_version = r.packages.get("mesa", "").split(":")[-1]
    r.issues = analyze(r)
    return r


def gpus_in_use(pids: list[int], r: GpuReport) -> list[Gpu]:
    """GPU realmente abiertas por los procesos (lee /proc/<pid>/fd)."""
    used: set[str] = set()
    for pid in pids:
        try:
            fds = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = str(fd.readlink())
            except OSError:
                continue
            m = re.match(r"/dev/dri/(renderD\d+|card\d+)$", target)
            if m:
                try:
                    used.add(Path(f"/sys/class/drm/{m[1]}/device").resolve().name)
                except OSError:
                    pass
            elif re.match(r"/dev/nvidia\d+$", target):
                used.update(g.slot for g in r.gpus if g.vendor == "nvidia")
    return [g for g in r.gpus if g.slot in used]
