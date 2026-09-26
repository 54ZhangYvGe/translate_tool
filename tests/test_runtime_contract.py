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

    def test_ui_stays_dependency_free_and_uses_bounded_qt_animation(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        self.assertNotIn("qt-material", requirements.lower())
        self.assertIn("QVariantAnimation", python)
        self.assertNotIn("QGraphicsBlurEffect", python)
        self.assertIn("QGraphicsDropShadowEffect", python)
        self.assertIn("font-size: 14px", python)
        self.assertIn("editor_font.setPointSize(14)", python)
        self.assertIn('setObjectName("PrimaryButton")', python)

    def test_result_window_and_settings_expose_new_controls(self):
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        self.assertIn("self.setFixedSize(card_width + 216, card_height + 20)", python)
        self.assertIn('self.settings_btn.setObjectName("SettingsButton")', python)
        self.assertIn('self.settings_btn = self._action("设置"', python)
        self.assertIn('config.get("manual_input_hotkey", "Ctrl+I")', python)
        self.assertIn("QKeySequenceEdit", python)
        self.assertIn("class ModernResultWindow(ResultWindow)", python)
        self.assertIn("self.window = ModernResultWindow()", python)
        self.assertIn("self.screenshot_hotkey_edit", python)
        self.assertIn("self.api_secret_edit.setEchoMode(QLineEdit.Password)", python)

    def test_dedicated_tts_is_synthesized_before_local_playback(self):
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        self.assertIn("synthesize_speech_to_temp", python)
        self.assertIn("count_tts_words", python)
        self.assertIn("QUrl.fromLocalFile(str(self.current_audio_file))", python)
        self.assertNotIn("translation_speak_url", python)

    def test_screenshot_hotkey_has_separate_capture_request(self):
        ahk = (ROOT / "translator.ahk").read_text(encoding="utf-8")
        python = (ROOT / "resident_app.py").read_text(encoding="utf-8")
        self.assertIn('SCREENSHOT_HOTKEY := "Ctrl+Alt+T"', ahk)
        self.assertIn("SetTimer SyncScreenshotHotkey, 1000", ahk)
        self.assertIn('WriteTranslateRequest(requestId, "", "screenshot", "capture")', ahk)
        self.assertIn('if action == "capture":', python)
        self.assertIn("class ScreenCaptureOverlay", python)
        self.assertIn("from screen_ocr import extract_text_from_png", python)


if __name__ == "__main__":
    unittest.main()
