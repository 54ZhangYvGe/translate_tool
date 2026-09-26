import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import translate
import secret_store
from translate import (
    TTS_MAX_WORDS,
    count_tts_words,
    save_read_aloud_settings,
    save_result_settings,
    load_recent_records,
    save_record,
    synthesize_speech_to_temp,
    translate_text,
    truncate_for_youdao_sign,
)


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

    def test_read_aloud_settings_are_saved_without_losing_other_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                '{"provider": "mock", "save_dir": "data"}', encoding="utf-8"
            )
            with patch.object(translate, "CONFIG_PATH", config_path):
                result = save_read_aloud_settings(True, 40, "Ctrl+M")

            self.assertTrue(result["auto_read_aloud"])
            self.assertEqual(result["auto_read_max_chars"], 40)
            self.assertEqual(result["manual_input_hotkey"], "Ctrl+M")
            self.assertEqual(result["provider"], "mock")

    def test_tts_word_count_treats_chinese_chars_and_english_words_as_units(self):
        self.assertEqual(count_tts_words("你好 hello world"), 4)
        self.assertEqual(count_tts_words("don't stop"), 2)

    def test_read_aloud_limit_cannot_exceed_service_cap(self):
        with self.assertRaisesRegex(ValueError, str(TTS_MAX_WORDS)):
            save_read_aloud_settings(True, TTS_MAX_WORDS + 1)

    def test_result_settings_keep_credentials_out_of_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            env_path = Path(temp_dir) / ".env.local"
            config_path.write_text('{"provider":"mock","save_dir":"data"}', encoding="utf-8")
            env_path.write_text("OTHER_SETTING=keep\n", encoding="utf-8")
            with (
                patch.object(translate, "CONFIG_PATH", config_path),
                patch.object(translate, "ENV_LOCAL_PATH", env_path),
                patch.object(translate, "ENV_PATH", Path(temp_dir) / ".env"),
                patch.object(secret_store, "secret_path", return_value=Path(temp_dir) / "secret.dpapi"),
            ):
                result = save_result_settings(False, 30, "Ctrl+I", False, "Ctrl+Alt+F8", "example-id", "example-secret")
                self.assertEqual(result["screenshot_hotkey"], "Ctrl+Alt+F8")
                self.assertFalse(result["screenshot_enabled"])
                self.assertEqual(secret_store.read_secret(), "example-secret")
                self.assertEqual(translate.load_config()["youdao_app_key"], "example-id")
            config_text = config_path.read_text(encoding="utf-8")
            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("example-secret", config_text)
            self.assertIn("OTHER_SETTING=keep", env_text)
            self.assertNotIn("example-secret", env_text)

    def test_result_font_and_card_size_persist_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"provider":"mock","save_dir":"data"}', encoding="utf-8")
            with (
                patch.object(translate, "CONFIG_PATH", config_path),
                patch.object(translate, "ENV_LOCAL_PATH", Path(temp_dir) / ".env.local"),
                patch.object(translate, "ENV_PATH", Path(temp_dir) / ".env"),
            ):
                result = save_result_settings(False, 30, "Ctrl+I", True, "Ctrl+Alt+T",
                                              result_font_size=18, result_card_width=520,
                                              result_card_height=440)
                self.assertEqual((result["result_font_size"], result["result_card_width"],
                                  result["result_card_height"]), (18, 520, 440))
                self.assertEqual(result["provider"], "mock")
                with self.assertRaisesRegex(ValueError, "result_card_height"):
                    save_result_settings(False, 30, "Ctrl+I", True, "Ctrl+Alt+T",
                                         result_card_height=900)

    def test_recent_records_are_newest_first_and_limited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {"save_dir": temp_dir}
            save_record("first", "一", config)
            save_record("second", "二", config)
            records = load_recent_records(config, limit=1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "second")

    def test_dedicated_tts_saves_mp3_and_sends_signed_form(self):
        class FakeAudioResponse:
            status_code = 200
            content = b"ID3" + b"\x00" * 64
            headers = {"Content-Type": "audio/mp3"}

        config = {
            "youdao_tts_api_url": "https://openapi.youdao.com/ttsapi",
            "youdao_tts_voice_name": "youxiaoqin",
            "youdao_tts_app_key": "tts-app-key",
        }
        with (
            patch("translate.load_environment"),
            patch("translate.migrate_legacy_credentials"),
            patch("translate.read_secret", return_value="tts-app-secret"),
            patch.dict(
                "os.environ",
                {
                    "YOUDAO_TTS_APP_KEY": "tts-app-key",
                },
                clear=False,
            ),
            patch(
                "translate.requests.post", return_value=FakeAudioResponse()
            ) as post_mock,
        ):
            audio_path = synthesize_speech_to_temp("你好", config)
        try:
            self.assertEqual(audio_path.suffix, ".mp3")
            self.assertEqual(audio_path.read_bytes(), FakeAudioResponse.content)
            request_data = post_mock.call_args.kwargs["data"]
            self.assertEqual(request_data["appKey"], "tts-app-key")
            self.assertEqual(request_data["voiceName"], "youxiaoqin")
            self.assertEqual(request_data["format"], "mp3")
            self.assertNotIn("tts-app-secret", request_data.values())
        finally:
            audio_path.unlink(missing_ok=True)

    def test_tts_over_50_words_does_not_call_service(self):
        config = {
            "youdao_tts_api_url": "https://openapi.youdao.com/ttsapi",
            "youdao_tts_voice_name": "youxiaoqin",
            "youdao_tts_app_key": "tts-app-key",
        }
        text = " ".join(f"word{i}" for i in range(TTS_MAX_WORDS + 1))
        with (
            patch("translate.requests.post") as post_mock,
            self.assertRaisesRegex(ValueError, str(TTS_MAX_WORDS)),
        ):
            synthesize_speech_to_temp(text, config)
        post_mock.assert_not_called()

    def test_tts_json_error_is_not_saved_as_an_audio_file(self):
        class FakeErrorResponse:
            status_code = 200
            content = b'{"errorCode":"110"}'
            headers = {"Content-Type": "application/json"}

            @staticmethod
            def json():
                return {"errorCode": "110"}

        config = {
            "youdao_tts_api_url": "https://openapi.youdao.com/ttsapi",
            "youdao_tts_voice_name": "youxiaoqin",
            "youdao_tts_app_key": "tts-app-key",
        }
        with (
            patch("translate.load_environment"),
            patch("translate.migrate_legacy_credentials"),
            patch("translate.read_secret", return_value="tts-app-secret"),
            patch.dict(
                "os.environ",
                {
                    "YOUDAO_TTS_APP_KEY": "tts-app-key",
                },
                clear=False,
            ),
            patch("translate.requests.post", return_value=FakeErrorResponse()),
            self.assertRaisesRegex(RuntimeError, "110"),
        ):
            synthesize_speech_to_temp("你好", config)


if __name__ == "__main__":
    unittest.main()
