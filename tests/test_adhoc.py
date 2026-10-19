import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from umbral import app, integration
    from umbral.config import Config
    from umbral.controller import Controller
except ImportError as e:           # el CI no tiene PyGObject (GTK)
    raise unittest.SkipTest(f"sin PyGObject: {e}") from e


def controller() -> Controller:
    """Un Controller sin arrancar (sin hilos ni GLib): solo la lógica de run_adhoc."""
    ctl = object.__new__(Controller)
    ctl.cfg = Config()
    ctl.cfg.settings.prefix_root = tempfile.mkdtemp()      # que no dependa de los prefijos reales del equipo
    ctl.errors = []
    ctl.error = ctl.errors.append
    ctl.is_running = lambda key: False
    ctl.save = lambda: None
    ctl.run = mock.Mock(return_value=object())
    return ctl


class TestArgs(unittest.TestCase):
    def test_run_and_open_with(self):
        a = app.parse_args(["--run", "/x/a.exe", "/y/b.msi"])
        self.assertEqual((a.run, a.files), ("/x/a.exe", ["/y/b.msi"]))
        self.assertEqual(app.parse_args(["/y/b.bat"]).files, ["/y/b.bat"])
        self.assertEqual(app.parse_args(["--launch", "battlenet"]).files, [])


class TestRunAdhoc(unittest.TestCase):
    def test_runs_in_the_shared_prefix_without_adding_a_game(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "Programa.EXE")
            exe.write_bytes(b"MZ")
            ctl = controller()
            self.assertTrue(ctl.run_adhoc(str(exe)))
            self.assertEqual(ctl.cfg.games, [])                      # no entra en la biblioteca
            key, prefix_id, path = ctl.run.call_args.args[:3]
            self.assertEqual((key, path), ("adhoc:Programa.EXE", str(exe)))
            self.assertEqual([(p.id, p.name) for p in ctl.cfg.prefixes], [(prefix_id, Controller.ADHOC_NAME)])
            ctl.run_adhoc(str(exe))                                  # el prefijo se reutiliza
            self.assertEqual(len(ctl.cfg.prefixes), 1)

    def test_rejects_other_files_and_missing_ones(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = controller()
            Path(d, "notas.txt").write_text("x")
            self.assertFalse(ctl.run_adhoc(str(Path(d, "notas.txt"))))
            self.assertFalse(ctl.run_adhoc(str(Path(d, "no-existe.exe"))))
            self.assertEqual(len(ctl.errors), 2)
            ctl.run.assert_not_called()


class HideWindowsTests(unittest.TestCase):
    def test_hide_rule_matches_class_and_title(self):
        done = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "x"}), \
                mock.patch("shutil.which", return_value="/bin/hyprctl"), \
                mock.patch("subprocess.run", return_value=done) as run:
            ok, _out = integration.hyprland_hide_windows(r"^steam_app_default$", r"^Battle\.net$", 90)
        lua = run.call_args[0][0][2]
        self.assertTrue(ok)
        self.assertIn(r'class = "^steam_app_default$"', lua)
        self.assertIn(r'title = "^Battle\\.net$"', lua)
        self.assertIn("special:umbral silent", lua)
        self.assertIn("timeout = 90000", lua)

    def test_not_hyprland_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("subprocess.run") as run:
            self.assertFalse(integration.hyprland_hide_windows("a", "b")[0])
        run.assert_not_called()
