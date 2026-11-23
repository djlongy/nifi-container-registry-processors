"""Checks that run without NiFi: python3 -m unittest discover -s python/tests"""
import ast
import os
import subprocess
import sys
import unittest

HERE = os.path.join(os.path.dirname(__file__), "..", "container-registry")
sys.path.insert(0, HERE)


class Discovery(unittest.TestCase):
    """NiFi reads properties from the source, without running it: each must be a literal PropertyDescriptor(name=...)."""
    def properties(self, module):
        tree = ast.parse(open(os.path.join(HERE, module + ".py")).read())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == module)
        return [kw.value.value for n in cls.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", None) == "PropertyDescriptor" for kw in n.value.keywords if kw.arg == "name"]

    def test_plan(self):
        self.assertIn("Allowed Repositories", self.properties("PlanRegistryTransfer"))

    def test_route(self):
        self.assertIn("Registry URL", self.properties("RouteRegistryTransfer"))


class Isolation(unittest.TestCase):
    """NiFi loads each processor file on its own and never preloads another processor file, so none may import one."""
    def test_no_processor_imports_another(self):
        names = {"PlanRegistryTransfer", "RouteRegistryTransfer"}
        for name in names:
            tree = ast.parse(open(os.path.join(HERE, name + ".py")).read())
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertEqual(imported & names, set(), name)

    def test_each_processor_loads_with_the_others_unimportable(self):
        """NiFi puts a processor's directory on sys.path and loads its file; another processor module is not there."""
        names = {"PlanRegistryTransfer", "RouteRegistryTransfer"}
        for name in sorted(names):
            others = sorted(names - {name})
            code = (
                "import sys\n"
                f"sys.path[:0] = [{os.path.dirname(__file__)!r}, {HERE!r}]\n"
                "import nifi_stub\n"
                "class Block:\n"
                f"    def find_spec(self, fullname, path=None, target=None):\n"
                f"        if fullname in {others!r}:\n"
                "            raise ModuleNotFoundError(fullname)\n"
                "sys.meta_path.insert(0, Block())\n"
                f"import {name}\n")
            with self.subTest(name):
                result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
