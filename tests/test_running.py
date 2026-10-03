import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from umbral import running


def spawn_launcher_with_game():
    """Imita umu-run: un lanzador cuyo hijo aparece como «C:\\Games\\Game.exe»."""
    launcher = subprocess.Popen(
        ["bash", "-c", "exec -a 'C:\\Games\\Game.exe' sleep 60 & wait"], start_new_session=True)
    for _i in range(50):
        tree = running.process_tree(launcher.pid)
        if running.exe_pids(tree, "Game.exe"):
            return launcher
        time.sleep(0.05)
    raise AssertionError("el proceso del juego no apareció")


class TestRunning(unittest.TestCase):
    def test_starttime_and_reused_pid(self):
        st = running.starttime(os.getpid())
        self.assertIsInstance(st, int)
        self.assertTrue(running.alive(os.getpid(), st))
        self.assertFalse(running.alive(os.getpid(), st + 1))      # mismo PID, otro proceso: no vale
        self.assertFalse(running.alive(2 ** 22 + 7, None))          # PID inexistente

    def test_tree_and_exact_game_pids(self):
        launcher = spawn_launcher_with_game()
        try:
            tree = running.process_tree(launcher.pid)
            game = running.exe_pids(tree, "game.exe")                # sin distinguir mayúsculas
            self.assertEqual(len(game), 1)
            self.assertIn(launcher.pid, tree)
            self.assertEqual(running.argv0_name(game[0]), "Game.exe")
            # Otro «Game.exe» ajeno a este árbol no se cuenta
            other = subprocess.Popen(["bash", "-c", "exec -a 'C:\\Otro\\Game.exe' sleep 60"])
            time.sleep(0.2)
            self.assertNotIn(other.pid, running.exe_pids(running.process_tree(launcher.pid), "Game.exe"))
            other.kill()
            other.wait()
        finally:
            os.killpg(launcher.pid, signal.SIGKILL)     # el lanzador y su «juego»
            launcher.wait()

    def test_write_read_and_stop(self):
        launcher = spawn_launcher_with_game()
        tree = running.process_tree(launcher.pid)
        game = running.exe_pids(tree, "Game.exe")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "running.json"
            e = running.entry("1484d426be", "Pokemon Iberia", "custom", "umbral", launcher.pid, game,
                              "/g/Pokémon Iberia V2.03/Game.exe", "GE-Proton", "", "/pfx", time.time())
            dead = dict(e, id="viejo", pid=2 ** 22 + 9, pid_starttime=1, game_pids=[])
            running.write([e, dead], path)
            games = running.read(path)
            self.assertEqual([g["id"] for g in games], ["1484d426be"])    # la entrada muerta se descarta
            self.assertEqual(games[0]["exe_name"], "Game.exe")
            self.assertEqual(games[0]["game_pids"][0]["pid"], game[0])
            self.assertTrue(running.stop_game(games[0], timeout=3))
            launcher.wait(timeout=5)
            self.assertEqual(running.read(path), [])


if __name__ == "__main__":
    unittest.main()
