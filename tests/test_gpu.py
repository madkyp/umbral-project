import unittest
from pathlib import Path

from umbral import gpu

FX = Path(__file__).parent / "fixtures"
NV_PKGS = {p: "1" for p in gpu.PKGS["common"] + gpu.PKGS["nvidia"]}
AMD_PKGS = {p: "1" for p in gpu.PKGS["common"] + gpu.PKGS["amd"]}
INTEL_PKGS = {p: "1" for p in gpu.PKGS["common"] + gpu.PKGS["intel"]}


def report(lspci, vk, pkgs, boot=None, **kw):
    r = gpu.GpuReport(gpus=gpu.parse_lspci((FX / lspci).read_text()),
                      vk=gpu.parse_vulkaninfo((FX / vk).read_text()), packages=dict(pkgs), **kw)
    for g in r.gpus:
        g.boot_vga = g.slot == boot
    r.issues = gpu.analyze(r)
    return r


def levels(r):
    return [i.level for i in r.issues]


class TestNvidiaReal(unittest.TestCase):
    def setUp(self):
        self.r = report("lspci_nvidia_real.txt", "vk_nvidia_real.txt", NV_PKGS,
                        boot="0000:01:00.0", nvidia_modeset=True, nvidia_fbdev=True)

    def test_parse(self):
        self.assertEqual(len(self.r.gpus), 1)
        g = self.r.gpus[0]
        self.assertEqual((g.vendor, g.pci_id, g.driver), ("nvidia", "10de:1f07", "nvidia"))
        self.assertEqual(self.r.vk_for(g).driver_info, "615.71.09")

    def test_no_issues_and_no_env_single_gpu(self):
        self.assertEqual(self.r.issues, [])
        env, target = gpu.gpu_env(self.r, "auto")
        self.assertEqual(env, {})
        self.assertEqual(target.vendor, "nvidia")

    def test_missing_lib32_nvidia(self):
        pk = dict(NV_PKGS)
        del pk["lib32-nvidia-utils"]
        r = report("lspci_nvidia_real.txt", "vk_nvidia_real.txt", pk, nvidia_modeset=True)
        err = [i for i in r.issues if i.level == "error"]
        self.assertTrue(err and "lib32-nvidia-utils" in err[0].fix)
        self.assertTrue(err[0].fix.startswith("sudo pacman -S --needed"))

    def test_modeset_off(self):
        r = report("lspci_nvidia_real.txt", "vk_nvidia_real.txt", NV_PKGS, nvidia_modeset=False)
        self.assertIn("modeset", " ".join(i.message for i in r.issues))

    def test_old_driver(self):
        r = report("lspci_nvidia_real.txt", "vk_nvidia_real.txt", NV_PKGS, nvidia_modeset=True)
        r.vk[0].driver_info = "470.256.02"
        issues = gpu.analyze(r)
        self.assertTrue(any(i.level == "warning" and "535" in i.message for i in issues))

    def test_no_vulkan(self):
        r = report("lspci_nvidia_real.txt", "vk_empty.txt", NV_PKGS, nvidia_modeset=True)
        self.assertTrue(any("Vulkan" in i.message and i.level == "error" for i in r.issues))


class TestAmd(unittest.TestCase):
    def test_amd_profile(self):
        r = report("lspci_amd.txt", "vk_amd.txt", AMD_PKGS, boot="0000:03:00.0")
        self.assertEqual([g.vendor for g in r.gpus], ["amd"])  # el audio HDMI no cuenta
        self.assertEqual(r.issues, [])
        self.assertEqual(r.default_gpu().pci_id, "1002:744c")
        # llvmpipe (CPU) no debe confundirse con la GPU
        self.assertEqual(r.vk_for(r.gpus[0]).driver_name, "radv")

    def test_amd_missing_lib32_radeon_and_amdvlk(self):
        pk = {**AMD_PKGS, "amdvlk": "1"}
        del pk["lib32-vulkan-radeon"]
        r = report("lspci_amd.txt", "vk_amd.txt", pk)
        text = " ".join(i.message + i.fix for i in r.issues)
        self.assertIn("lib32-vulkan-radeon", text)
        self.assertIn("pacman -Rns amdvlk", text)
        self.assertNotIn("nvidia", text.lower())  # nada de NVIDIA en un equipo AMD

    def test_amd_radeon_driver(self):
        r = report("lspci_amd.txt", "vk_amd.txt", AMD_PKGS)
        r.gpus[0].driver = "radeon"
        self.assertTrue(any("amdgpu" in i.message for i in gpu.analyze(r)))


class TestHybrid(unittest.TestCase):
    def test_intel_nvidia_laptop_prime(self):
        pk = {**NV_PKGS, **INTEL_PKGS}
        r = report("lspci_hybrid_intel_nvidia.txt", "vk_hybrid_intel_nvidia.txt", pk,
                   boot="0000:00:02.0", nvidia_modeset=True, nvidia_fbdev=True)
        self.assertEqual(r.default_gpu().vendor, "nvidia")
        env, t = gpu.gpu_env(r, "auto")
        self.assertEqual(env["__NV_PRIME_RENDER_OFFLOAD"], "1")
        self.assertEqual(env["__VK_LAYER_NV_optimus"], "NVIDIA_only")
        self.assertEqual(env["DXVK_FILTER_DEVICE_NAME"], "NVIDIA GeForce RTX 3050 Laptop GPU")
        self.assertNotIn("DRI_PRIME", env)
        # elegir la iGPU a mano
        env, t = gpu.gpu_env(r, "8086:46a6")
        self.assertEqual(t.vendor, "intel")
        self.assertEqual(env["DRI_PRIME"], "pci-0000_00_02_0")
        self.assertEqual(env["MESA_VK_DEVICE_SELECT"], "8086:46a6")
        self.assertEqual(env["__VK_LAYER_NV_optimus"], "non_NVIDIA_only")
        self.assertNotIn("__NV_PRIME_RENDER_OFFLOAD", env)
        self.assertIn("info", levels(r))

    def test_amd_apu_plus_dgpu(self):
        r = report("lspci_hybrid_amd_amd.txt", "vk_hybrid_amd_amd.txt", AMD_PKGS, boot="0000:65:00.0")
        self.assertEqual(r.default_gpu().pci_id, "1002:7480")  # la dedicada
        env, _ = gpu.gpu_env(r, "auto")
        self.assertEqual(env["DRI_PRIME"], "pci-0000_03_00_0")
        self.assertEqual(env["VKD3D_FILTER_DEVICE_NAME"], "AMD Radeon RX 7700S (RADV NAVI33)")
        self.assertFalse(any(k.startswith("__NV") or k.startswith("__VK_LAYER_NV") for k in env))


class TestIntel(unittest.TestCase):
    def test_intel_only_no_domain(self):
        r = report("lspci_intel.txt", "vk_empty.txt", INTEL_PKGS)
        self.assertEqual(r.gpus[0].slot, "0000:00:02.0")
        self.assertEqual(r.gpus[0].vendor, "intel")
        self.assertTrue(any("vulkan-intel" in i.fix for i in r.issues))

    def test_no_gpu(self):
        r = gpu.GpuReport()
        self.assertEqual(gpu.analyze(r)[0].level, "error")


if __name__ == "__main__":
    unittest.main()
