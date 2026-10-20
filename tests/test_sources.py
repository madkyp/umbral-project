import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "umbral"


class TestSources(unittest.TestCase):
    def test_every_module_has_code_and_compiles(self):
        """Un .py vacío pasa ruff y los tests sin GTK pero rompe la app al abrirla (pasó con system_page.py)."""
        for f in ROOT.rglob("*.py"):
            if f.name != "__init__.py":
                self.assertGreater(f.stat().st_size, 0, f"{f} está vacío")
            compile(f.read_text(), str(f), "exec")
