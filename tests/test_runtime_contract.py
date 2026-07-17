import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeContractTests(unittest.TestCase):
    def test_resident_versions_match(self):
        ahk = (ROOT / "translator.ahk").read_text(encoding="utf-8")
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        ahk_version = re.search(r'RESIDENT_VERSION := "([^"]+)"', ahk).group(1)
        python_version = re.search(r'RESIDENT_VERSION = "([^"]+)"', python).group(1)
        self.assertEqual(ahk_version, python_version)

    def test_resident_is_not_started_hidden(self):
        ahk = (ROOT / "translator.ahk").read_text(encoding="utf-8")
        self.assertNotIn('Run cmd, BASE_DIR, "Hide"', ahk)
        self.assertIn("SetTimer ActivateResultWindow", ahk)


if __name__ == "__main__":
    unittest.main()
