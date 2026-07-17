import tempfile
import unittest
from pathlib import Path

from translate import save_record, translate_text, truncate_for_youdao_sign


class TranslateTests(unittest.TestCase):
    def test_youdao_sign_input_keeps_short_text(self):
        self.assertEqual(truncate_for_youdao_sign("short text"), "short text")

    def test_youdao_sign_input_truncates_long_text(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        self.assertEqual(truncate_for_youdao_sign(text), "abcdefghij26qrstuvwxyz")

    def test_mock_translation(self):
        result = translate_text("hello", {"provider": "mock"})
        self.assertIn("hello", result)

    def test_empty_translation_is_rejected(self):
        with self.assertRaises(ValueError):
            translate_text("  ", {"provider": "mock"})

    def test_save_record_uses_configured_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_file = save_record("source", "translation", {"save_dir": temp_dir})
            self.assertEqual(saved_file.parent, Path(temp_dir))
            self.assertIn("source", saved_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
