import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umbral import gpu, launcher, prefixes
from umbral.config import Config, LaunchOptions, Prefix
from umbral.runners import Runner, runner_for_prefix_version


def fake_runner(tmp: Path, name="GE-Proton11-7-x86_64") -> Runner:
    d = tmp / name
    (d / "files/bin").mkdir(parents=True)
    (d / "proton").write_text("")
    (d / "files/bin/wineserver").write_text("")
    return Runner(name, d, "proton")


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.r = fake_runner(self.t)
        self.cfg = Config()
        self.bnet = Prefix("battlenet", "Battle.net", str(self.t / "pfx"), self.r.name)
        self.which = mock.patch("shutil.which", side_effect=lambda c: f"/usr/bin/{c}"
                                if c in ("umu-run", "gamescope") else None)
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.tmp.cleanup()

    def test_battlenet_defaults(self):
        plan = launcher.build(self.cfg, self.bnet, "C:/x/Battle.net.exe", runners=[self.r])
        e = plan.env
        self.assertEqual(plan.argv[:1], ["umu-run"])
        self.assertEqual(e["WINE_SIMULATE_WRITECOPY"], "1")
        self.assertEqual(e["PROTON_ENABLE_WAYLAND"], "0")
        self.assertEqual(e["GAMEID"], "umu-default")
        self.assertEqual(e["PROTONPATH"], str(self.r.path))
        self.assertEqual(e["PROTON_LOCAL_SHADER_CACHE"], "1")
        self.assertNotIn("PROTON_LOG", e)

    def test_debug_and_user_env_wins(self):
        self.bnet.options = LaunchOptions(env={"WINE_SIMULATE_WRITECOPY": "0", "FOO": "1"})
        plan = launcher.build(self.cfg, self.bnet, "a.exe", debug=True, runners=[self.r])
        self.assertEqual(plan.env["WINE_SIMULATE_WRITECOPY"], "0")
        self.assertEqual(plan.env["PROTON_LOG"], "1")

    def test_wrappers_and_missing_tools(self):
        self.bnet.options = LaunchOptions(gamescope=True, gamescope_args="-f -W 1920", mangohud=True,
                                          gamemode=True)
        plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertEqual(plan.argv[:6], ["gamescope", "-f", "-W", "1920", "--mangoapp", "--"])
        self.assertNotIn("MANGOHUD", plan.env)
        self.assertIn("gpu_temp", plan.env["MANGOHUD_CONFIG"])   # mangoapp también lo lee
        self.assertTrue(any("gamemode" in w for w in plan.warnings))

    def test_mangohud_presets(self):
        self.bnet.options = LaunchOptions(mangohud=True, mangohud_preset="fps", mangohud_position="top-right")
        plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertEqual(plan.env["MANGOHUD"], "1")
        self.assertEqual(plan.env["MANGOHUD_CONFIG"], "fps_only,position=top-right")
        self.bnet.options = LaunchOptions(mangohud=False)
        plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertNotIn("MANGOHUD", plan.env)
        self.assertNotIn("MANGOHUD_CONFIG", plan.env)

    def test_ntsync_only_ge(self):
        cachy = fake_runner(self.t, "Proton-CachyOS Latest")
        self.bnet.runner = cachy.name
        self.bnet.options = LaunchOptions(no_ntsync=True)
        plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[cachy])
        self.assertNotIn("PROTON_NO_NTSYNC", plan.env)
        self.assertTrue(plan.warnings)

    def test_missing_runner(self):
        self.bnet.runner = "No-Existe"
        with self.assertRaises(LookupError):
            launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])

    def test_gpu_env_applied(self):
        fx = Path(__file__).parent / "fixtures"
        rep = gpu.GpuReport(gpus=gpu.parse_lspci((fx / "lspci_hybrid_intel_nvidia.txt").read_text()),
                            vk=gpu.parse_vulkaninfo((fx / "vk_hybrid_intel_nvidia.txt").read_text()))
        rep.gpus[0].boot_vga = True
        plan = launcher.build(self.cfg, self.bnet, "a.exe", report=rep, runners=[self.r])
        self.assertEqual(plan.env["__NV_PRIME_RENDER_OFFLOAD"], "1")
        self.assertEqual(plan.gpu.vendor, "nvidia")


class TestPrefixes(unittest.TestCase):
    def test_runner_match_from_version_file(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            ge = fake_runner(t, "GE-Proton11-7-x86_64")
            cachy = fake_runner(t, "Proton-CachyOS Latest")
            (cachy.path / "version").write_text("1784681208 cachyos-11.0-20260703-slr\n")
            self.assertEqual(runner_for_prefix_version("GE-Proton11-7", [ge, cachy]), ge)
            self.assertEqual(runner_for_prefix_version("CachyOS-11.0-100", [ge, cachy]), cachy)

    def test_recreate_keeps_games(self):
        from test_battlenet import BNET, DB_FOREVER, WOW, make_prefix
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET, f"{WOW}/.build.info", f"{WOW}/_classic_beta_/WowB.exe"])
            with self.assertRaises(prefixes.PrefixError):
                prefixes.delete_for_recreate(p, imported=True)
            stash = prefixes.delete_for_recreate(p, imported=False)
            self.assertFalse(p.exists())
            (p / "drive_c/Program Files (x86)").mkdir(parents=True)
            prefixes.restore_games(p, stash)
            self.assertTrue((p / "drive_c" / WOW / "_classic_beta_/WowB.exe").exists())
            self.assertTrue((p / "drive_c/ProgramData/Battle.net/Agent/product.db").exists())
            self.assertFalse(stash.exists())

    def test_reset_agent_keeps_product_db(self):
        from test_battlenet import BNET, DB_FOREVER, make_prefix
        with tempfile.TemporaryDirectory() as d:
            p = make_prefix(Path(d), DB_FOREVER, [BNET])
            backup = prefixes.reset_agent(p)
            self.assertTrue(backup.exists())
            self.assertEqual((p / "drive_c/ProgramData/Battle.net/Agent/product.db").read_bytes(), DB_FOREVER)


if __name__ == "__main__":
    unittest.main()
