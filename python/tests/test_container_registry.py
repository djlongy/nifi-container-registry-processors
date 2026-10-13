"""Checks that run without NiFi: python3 -m unittest discover -s python/tests"""
import ast
import io
import os
import sys
import unittest

HERE = os.path.join(os.path.dirname(__file__), "..", "container_registry")
sys.path.insert(0, HERE)
from container_registry_client import RegistryError, _Hashing, rebase  # noqa: E402


class Client(unittest.TestCase):
    def test_rebase(self):
        self.assertEqual(rebase("low/team/app", "high"), "high/team/app")
        self.assertEqual(rebase("low/team/app", strip=True), "team/app")
        self.assertEqual(rebase("team/app"), "team/app")
        with self.assertRaises(RegistryError):
            rebase("low/app", "high", strip=True)
        with self.assertRaises(RegistryError):
            rebase("app", strip=True)

    def test_hashing_read_zero(self):
        stream = _Hashing(io.BytesIO(b"abc"))
        self.assertEqual(stream.read(0), b"")
        self.assertEqual(stream.read(), b"abc")
        self.assertEqual(stream.count, 3)


class Discovery(unittest.TestCase):
    """NiFi reads properties from the source, without running it: each must be a literal PropertyDescriptor(name=...)."""
    def properties(self, module):
        tree = ast.parse(open(os.path.join(HERE, module + ".py")).read())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == module)
        return [kw.value.value for n in cls.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", None) == "PropertyDescriptor" for kw in n.value.keywords if kw.arg == "name"]

    def test_pull(self):
        self.assertIn("Output Directory", self.properties("PullContainerImages"))
        self.assertEqual(len(self.properties("PullContainerImages")), 20)

    def test_push(self):
        self.assertIn("Tar File", self.properties("PushContainerImages"))
        self.assertEqual(len(self.properties("PushContainerImages")), 11)


if __name__ == "__main__":
    unittest.main()
