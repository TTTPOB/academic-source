"""Smoke-test a built wheel in an isolated interpreter outside the checkout.

CI runs this file with the wheel environment's Python, from a temporary directory.
It deliberately uses only the standard library so pytest and the source checkout
cannot make a missing wheel file appear to work.
"""

import importlib.metadata
import importlib.resources
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

# This standalone CI smoke must not run under the checkout's regular pytest suite.
__test__ = __name__ == "__main__"


class WheelInstallationTest(unittest.TestCase):
    check_cdp = "--cdp" in sys.argv

    def test_installed_entry_point_and_modules(self):
        root = Path(__file__).resolve().parents[2]
        self.assertNotIn(str(root / "src"), sys.path)

        from academic_source import app
        from academic_source.interfaces import cli, mcp
        from academic_source.services import application
        from scansci_pdf import browser_backend, sources

        for module in (app, cli, mcp, application, browser_backend, sources):
            self.assertFalse(Path(module.__file__).resolve().is_relative_to(root))

        entry_points = importlib.metadata.entry_points(group="console_scripts")
        entry = next(item for item in entry_points if item.name == "academic-source")
        result = subprocess.run(
            [str(Path(sys.executable).parent / "academic-source"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(entry.value, "academic_source.interfaces.cli:main")
        self.assertIn("serve", result.stdout)
        self.assertIn("batch", result.stdout)

        catalog = importlib.resources.files("scansci_pdf").joinpath(
            "data/publisher_access_catalog.json"
        )
        self.assertIsInstance(json.loads(catalog.read_text(encoding="utf-8")), dict)

    def test_cdp_extra_import(self):
        if not self.check_cdp:
            self.skipTest("Only the CI CDP extra smoke requests this check")
        self.assertIsNotNone(importlib.util.find_spec("playwright"))
        from playwright.sync_api import sync_playwright

        from scansci_pdf.browser_backend import BACKEND_CDP, connect_cdp

        self.assertTrue(callable(sync_playwright))
        self.assertTrue(callable(connect_cdp))
        self.assertEqual(BACKEND_CDP, "cdp")


if __name__ == "__main__":
    sys.argv = [sys.argv[0]]
    unittest.main()
