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
        gs = plan.argv[:plan.argv.index("--") + 1]
        self.assertEqual(gs[0], "gamescope")
        self.assertEqual(gs.count("-f"), 1)                      # el «-f» antiguo no se duplica
        self.assertIn("-W", gs)                                  # los argumentos extra se conservan
        self.assertIn("--mangoapp", gs)
        self.assertNotIn("MANGOHUD", plan.env)
        self.assertIn("gpu_temp", plan.env["MANGOHUD_CONFIG"])   # mangoapp también lo lee
        self.assertTrue(any("gamemode" in w for w in plan.warnings))

    def test_gamescope_command(self):
        o = LaunchOptions(gs_resolution="640x480", gs_mode="fullscreen", gs_scaler="integer", gs_filter="nearest")
        self.assertEqual(launcher.gamescope_command(o, (1920, 1080)),
                         ["-w", "640", "-h", "480", "-W", "1920", "-H", "1080", "-f", "-S", "integer", "-F", "nearest"])
        o = LaunchOptions(gs_resolution="640x480", gs_mode="window")      # ventana: mayor múltiplo que cabe
        self.assertEqual(launcher.gamescope_command(o, (1920, 1080))[:10],
                         ["-w", "640", "-h", "480", "-W", "1280", "-H", "960", "-S", "fit"])
        o = LaunchOptions(gs_mode="borderless", gamescope_args="--force-grab-cursor", mangohud=True)
        self.assertEqual(launcher.gamescope_command(o, None),
                         ["-b", "-S", "fit", "-F", "linear", "--force-grab-cursor", "--mangoapp"])

    def test_fps_limit(self):
        self.bnet.options = LaunchOptions(fps_limit=120)
        with mock.patch("shutil.which", side_effect=lambda c: f"/usr/bin/{c}" if c in ("umu-run", "mangohud") else None):
            plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertEqual(plan.env["MANGOHUD"], "1")
        self.assertEqual(plan.env["MANGOHUD_CONFIG"], "no_display,fps_limit=120")   # límite sin superposición
        self.bnet.options = LaunchOptions(fps_limit=60, mangohud=True, mangohud_preset="fps")
        with mock.patch("shutil.which", side_effect=lambda c: f"/usr/bin/{c}" if c in ("umu-run", "mangohud") else None):
            plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertTrue(plan.env["MANGOHUD_CONFIG"].startswith("fps_only,position=") and
                        plan.env["MANGOHUD_CONFIG"].endswith(",fps_limit=60"))
        self.bnet.options = LaunchOptions(fps_limit=60)                     # sin MangoHud: variables de Proton
        plan = launcher.build(self.cfg, self.bnet, "a.exe", runners=[self.r])
        self.assertEqual((plan.env["DXVK_FRAME_RATE"], plan.env["VKD3D_FRAME_RATE"]), ("60", "60"))
        self.assertTrue(plan.warnings)
        self.assertIn("-r", launcher.gamescope_command(LaunchOptions(fps_limit=60), None))   # con gamescope

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


class TestDeckHook(unittest.TestCase):
    """Hook de Control Deck: variables de los shaders y TEMPS para los juegos que lanza Umbral."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.r = fake_runner(self.t)
        self.cfg = Config()
        self.pfx = Prefix("p", "P", str(self.t / "pfx"), self.r.name)
        from umbral.config import Game
        self.game = Game("story", "Story", "custom", "p", str(self.t / "Story.exe"))

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, deck_out: str | None, user_env: dict | None = None):
        tools = {"umu-run", "control-deck"} if deck_out is not None else {"umu-run"}
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=deck_out or ""))
        if user_env:
            self.game.options.env = user_env
        with mock.patch("shutil.which", side_effect=lambda c: f"/usr/bin/{c}" if c in tools else None), \
             mock.patch("umbral.launcher.subprocess.run", run):
            plan = launcher.build(self.cfg, self.pfx, self.game.exe, game=self.game, runners=[self.r])
        return plan, run

    def test_no_control_deck(self):
        plan, run = self._build(None)
        self.assertNotIn("WINEDLLOVERRIDES", plan.env)
        self.assertFalse(plan.overlay)
        run.assert_not_called()

    def test_reshade_env_and_temps(self):
        plan, run = self._build('{"env":{"WINEDLLOVERRIDES":"d3dcompiler_47=n;dxgi=n,b"},"overlay":true}')
        self.assertEqual(run.call_args[0][0], ["/usr/bin/control-deck", "hook", "umbral:story"])
        self.assertEqual(plan.env["WINEDLLOVERRIDES"], "d3dcompiler_47=n;dxgi=n,b")
        self.assertTrue(plan.overlay)

    def test_user_env_wins(self):
        plan, _run = self._build('{"env":{"ENABLE_VKBASALT":"1"},"overlay":false}', {"ENABLE_VKBASALT": "0"})
        self.assertEqual(plan.env["ENABLE_VKBASALT"], "0")

    def test_broken_hook_is_ignored(self):
        plan, _run = self._build("not json")
        self.assertFalse(plan.overlay)
        self.assertNotIn("WINEDLLOVERRIDES", plan.env)
