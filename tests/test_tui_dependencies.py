"""Minimal, exact dependency contract for the independently rebuilt TUI."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"textual": "8.2.8", "rich": "15.0.0"}


class TUIDependencyTest(unittest.TestCase):
    def test_requirement_file_has_only_exact_direct_ui_pins(self) -> None:
        requirements = {}
        for raw in (ROOT / "requirements-tui.txt").read_text(
            encoding="utf-8"
        ).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.assertEqual(line.count("=="), 1, line)
            name, pinned = line.split("==", 1)
            requirements[name.casefold()] = pinned
        self.assertEqual(requirements, EXPECTED)

    def test_active_test_environment_matches_ui_pins(self) -> None:
        for distribution, pinned in EXPECTED.items():
            with self.subTest(distribution=distribution):
                self.assertEqual(version(distribution), pinned)

    def test_launcher_and_docs_install_from_the_same_file(self) -> None:
        for relative in ("stammtisch", "README.md", "tui/README.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("--require-hashes requirements-tui.lock", text)

    def test_transitive_lock_is_exact_hashed_and_ui_only(self) -> None:
        text = (ROOT / "requirements-tui.lock").read_text(encoding="utf-8")
        self.assertIn("--python-version 3.11", text)
        self.assertIn("--python-platform x86_64-unknown-linux-gnu", text)
        records = re.findall(r"(?m)^([a-z0-9-]+)==([^ \\\n]+) \\\n", text)
        self.assertGreater(len(records), len(EXPECTED))
        names = {name for name, _ in records}
        self.assertTrue(EXPECTED.keys() <= names)
        self.assertNotIn("quantkit", names)
        self.assertNotIn("pandas", names)
        self.assertNotIn("numpy", names)
        self.assertEqual(text.count("--hash=sha256:"), len(records) * 2)


if __name__ == "__main__":
    unittest.main()
