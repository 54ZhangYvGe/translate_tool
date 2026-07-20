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

    def test_resident_is_prewarmed_without_startup_error_popup(self):
        ahk = (ROOT / "translator.ahk").read_text(encoding="utf-8")
        self.assertIn("SetTimer PrewarmResidentApp, -100", ahk)
        self.assertIn("EnsureResidentApp(false)", ahk)
        self.assertIn("RESIDENT_START_TIMEOUT_MS := 15000", ahk)

    def test_worker_starts_before_loading_window_is_rendered(self):
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        start_method = python.split("def start_translation", 1)[1].split(
            "def translation_finished", 1
        )[0]
        self.assertLess(
            start_method.index("self.thread_pool.start(worker)"),
            start_method.index("self.window.show_loading()"),
        )
        self.assertIn("self.window.grab()", python)

    def test_ui_stays_dependency_free_and_animation_free(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        self.assertNotIn("qt-material", requirements.lower())
        self.assertNotIn("QPropertyAnimation", python)
        self.assertNotIn("QGraphicsBlurEffect", python)
        self.assertIn("QGraphicsDropShadowEffect", python)
        self.assertIn("font-size: 14px", python)
        self.assertIn("editor_font.setPointSize(14)", python)
        self.assertIn('setObjectName("PrimaryButton")', python)


if __name__ == "__main__":
    unittest.main()
