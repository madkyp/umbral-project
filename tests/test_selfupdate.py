import unittest

from umbral import selfupdate


class TestSelfUpdate(unittest.TestCase):
    def test_parse_version(self):
        pk = "pkgname=umbral\npkgver=0.16.1\npkgrel=2\npkgdesc=x\n"
        self.assertEqual(selfupdate.parse_version(pk), ("0.16.1", "2"))
        self.assertIsNone(selfupdate.parse_version("nada"))

    def test_is_newer(self):
        self.assertTrue(selfupdate.is_newer("0.16.2", "0.16.1"))
        self.assertTrue(selfupdate.is_newer("0.17.0", "0.16.9"))
        self.assertTrue(selfupdate.is_newer("0.10.0", "0.9.5"))      # numérica, no de texto
        self.assertFalse(selfupdate.is_newer("0.16.1", "0.16.1"))
        self.assertFalse(selfupdate.is_newer("0.15.9", "0.16.0"))

    def test_source_checkout_is_not_installed(self):
        self.assertFalse(selfupdate.installed())     # se ejecuta desde el repo, no desde /usr/lib/umbral
