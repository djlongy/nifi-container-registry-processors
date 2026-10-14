"""The property tables in the READMEs are generated from the descriptors. After changing a descriptor, run
python3 python/tests/test_readme_tables.py --write"""
import os
import re
import sys
import unittest

import nifi_stub  # noqa: F401

import ExportContainerImages
import PullContainerImages
import PushContainerImages

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
READMES = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "python", "container_registry", "README.md")]
PROCESSORS = {m.__name__: getattr(m, m.__name__) for m in (PullContainerImages, ExportContainerImages, PushContainerImages)}
BLOCK = re.compile(r"(<!-- properties:(\w+) -->\n).*?(<!-- /properties -->)", re.S)


def table(cls):
    rows = ["| Name | Required | Default | Description |", "|---|---|---|---|"]
    for d in cls.PROPERTIES:
        default = f"`{d.default_value}`" if d.default_value is not None else "none"
        description = d.description.replace("|", "\\|")
        rows.append(f"| {d.name} | {'Yes' if d.required else 'No'} | {default} | {description} |")
    return "\n".join(rows) + "\n"


def render(text):
    return BLOCK.sub(lambda m: m.group(1) + table(PROCESSORS[m.group(2)]) + m.group(3), text)


class ReadmeTables(unittest.TestCase):
    def test_tables_match_descriptors(self):
        for path in READMES:
            text = open(path).read()
            self.assertEqual({m[1] for m in BLOCK.findall(text)}, set(PROCESSORS), path)
            self.assertEqual(render(text), text, f"{path} is stale: python3 python/tests/test_readme_tables.py --write")

    def test_descriptions_are_short(self):
        for name, cls in PROCESSORS.items():
            for d in cls.PROPERTIES:
                self.assertLessEqual(len(d.description), 300, f"{name}: {d.name}")


if __name__ == "__main__":
    if "--write" in sys.argv:
        for path in READMES:
            text = open(path).read()
            open(path, "w").write(render(text))
    else:
        unittest.main()
