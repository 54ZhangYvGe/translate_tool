import os
import importlib.util
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if importlib.util.find_spec("PySide6"):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit
    from resident_app import ModernResultWindow


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 未安装")
class ModernResultWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        config = {
            "provider": "mock",
            "save_dir": os.getcwd(),
            "hotkey": "Alt+T",
            "manual_input_hotkey": "Ctrl+I",
            "screenshot_enabled": True,
            "screenshot_hotkey": "Ctrl+Alt+T",
            "auto_read_aloud": False,
            "auto_read_max_chars": 50,
        }
        with patch("resident_app.load_config", return_value=config), patch("resident_app.load_environment"):
            self.window = ModernResultWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def test_translation_card_keeps_geometry_when_rail_opens(self):
        card = self.window.card.geometry()
        self.window.toggle_rail()
        QTest.qWait(300)
        self.assertEqual(self.window.card.geometry(), card)
        self.assertTrue(self.window.rail.isVisible())
        self.window.toggle_rail()
        QTest.qWait(300)
        self.assertEqual(self.window.card.geometry(), card)
        self.assertFalse(self.window.rail.isVisible())

    def test_settings_share_rail_and_have_masked_api_secret(self):
        self.window.toggle_rail()
        QTest.qWait(300)
        self.window.open_settings()
        QTest.qWait(500)
        self.assertEqual(self.window._stage, "settings")
        self.assertEqual(self.window.rail.geometry(), self.window.RAIL_RECT)
        self.assertTrue(self.window.screenshot_checkbox.isChecked())
        self.assertTrue(self.window.screenshot_hotkey_edit.isEnabled())
        self.window.screenshot_checkbox.setChecked(False)
        self.assertFalse(self.window.screenshot_hotkey_edit.isEnabled())
        self.assertEqual(self.window.api_secret_edit.echoMode(), QLineEdit.Password)
        self.assertFalse(self.window.clear_secret_checkbox.isChecked())
        self.assertIsInstance(self.window.result_font_edit, QLineEdit)
        self.assertIsInstance(self.window.card_width_edit, QLineEdit)
        self.assertIsInstance(self.window.card_height_edit, QLineEdit)
        self.window.back_stage()
        QTest.qWait(450)
        self.assertEqual(self.window._stage, "home")

    def test_settings_submit_reaches_config_writer_without_exposing_secret(self):
        self.window.toggle_rail()
        QTest.qWait(300)
        self.window.open_settings()
        QTest.qWait(500)
        self.window.screenshot_checkbox.setChecked(False)
        self.window.api_key_edit.setText("sample-app")
        self.window.api_secret_edit.setText("sample-secret")
        with patch("resident_app.save_result_settings", return_value=dict(self.window.config)) as save:
            self.window.save_settings()
        self.assertFalse(save.call_args.args[3])
        self.assertEqual(save.call_args.args[5:], ("sample-app", "sample-secret", False, 14, 378, 360))
        self.assertEqual(self.window.api_secret_edit.text(), "")
        self.assertEqual(self.window.tts_api_secret_edit.echoMode(), QLineEdit.Password)
        self.assertEqual(save.call_args.kwargs, {"tts_app_key": "", "tts_app_secret": "", "clear_tts_secret": False})

    def test_result_font_and_card_size_follow_saved_settings(self):
        self.window.config = dict(self.window.config, result_font_size=18,
                                  result_card_width=500, result_card_height=430)
        self.window.apply_config_to_ui()
        self.assertEqual(self.window.editor.font().pointSize(), 18)
        self.assertEqual(self.window.card.size().width(), 500)
        self.assertEqual(self.window.card.size().height(), 430)
        self.assertEqual(self.window.rail.height(), 430)
        self.assertEqual(self.window.editor.width(), 444)
        self.assertEqual(self.window.editor.height(), 298)

    def test_shared_transition_can_reverse_mid_motion(self):
        self.window.toggle_rail()
        QTest.qWait(300)
        self.window.open_settings()
        QTest.qWait(150)
        self.window.back_stage()
        QTest.qWait(500)
        self.assertEqual(self.window._stage, "home")
        self.assertEqual(self.window.rail.surface_color, self.window.RAIL_DARK)
        self.assertFalse(self.window.ghost.isVisible())


if __name__ == "__main__":
    unittest.main()
