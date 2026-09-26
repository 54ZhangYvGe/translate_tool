import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import secret_store
import translate


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class SecretStoreTests(unittest.TestCase):
    def test_independent_api_settings_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(translate, "CONFIG_PATH", root / "config.json"),
                  patch.object(translate, "ENV_LOCAL_PATH", root / ".env.local"),
                  patch.object(translate, "ENV_PATH", root / ".env"),
                  patch.object(secret_store, "secret_path", side_effect=lambda kind="translation": root / f"{kind}.dpapi")):
                def save(**values):
                    return translate.save_result_settings(False, 50, "Ctrl+I", True, "Ctrl+Alt+T", **values)
                config = save(app_key="synthetic-text-id", app_secret="synthetic-text-secret", tts_app_key="synthetic-tts-id", tts_app_secret="synthetic-tts-secret")
                self.assertEqual(config["youdao_app_key"], "synthetic-text-id")
                self.assertEqual(config["youdao_tts_app_key"], "synthetic-tts-id")
                self.assertNotIn("synthetic-text-secret", (root / "config.json").read_text())
                self.assertNotIn("synthetic-tts-secret", (root / "config.json").read_text())
                save(clear_app_secret=True)
                self.assertEqual(secret_store.read_secret(), "")
                self.assertEqual(secret_store.read_secret("tts"), "synthetic-tts-secret")
                save(app_secret="synthetic-text-new", tts_app_secret="synthetic-tts-new")
                self.assertEqual(secret_store.read_secret(), "synthetic-text-new")
                self.assertEqual(secret_store.read_secret("tts"), "synthetic-tts-new")
                save(clear_tts_secret=True)
                translate.migrate_legacy_credentials()
                self.assertEqual(secret_store.read_secret(), "synthetic-text-new")
                self.assertEqual(secret_store.read_secret("tts"), "")

    def test_failed_tts_migration_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text("YOUDAO_TTS_APP_KEY=synthetic-id\nYOUDAO_TTS_APP_SECRET=synthetic-secret\n")
            with (patch.object(translate, "CONFIG_PATH", root / "config.json"),
                  patch.object(translate, "ENV_PATH", path),
                  patch.object(translate, "ENV_LOCAL_PATH", root / ".env.local"),
                  patch("translate.has_secret", return_value=False),
                  patch("translate.save_secret", side_effect=secret_store.CredentialError("synthetic-failure"))):
                with self.assertRaises(secret_store.CredentialError):
                    translate.migrate_legacy_credentials()
            self.assertIn("YOUDAO_TTS_APP_SECRET=synthetic-secret", path.read_text())

    def test_tts_secret_migrates_separately_and_keeps_non_sensitive_app_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / ".env.local"
            local.write_text("YOUDAO_TTS_APP_KEY=synthetic-tts-id\nYOUDAO_TTS_APP_SECRET=synthetic-tts-secret\nOTHER=keep\n", encoding="utf-8")
            with (patch.object(translate, "CONFIG_PATH", root / "config.json"),
                  patch.object(translate, "ENV_LOCAL_PATH", local),
                  patch.object(translate, "ENV_PATH", root / ".env"),
                  patch.object(secret_store, "secret_path", side_effect=lambda kind="translation": root / f"{kind}.dpapi")):
                translate.migrate_legacy_credentials()
                self.assertEqual(secret_store.read_secret("tts"), "synthetic-tts-secret")
                self.assertEqual(secret_store.read_secret(), "")
                self.assertNotIn("YOUDAO_TTS_APP_SECRET", local.read_text(encoding="utf-8"))
                self.assertIn("YOUDAO_TTS_APP_KEY=synthetic-tts-id", local.read_text(encoding="utf-8"))
                self.assertEqual(translate.load_config()["youdao_tts_app_key"], "synthetic-tts-id")
                secret_store.clear_secret("tts")
                self.assertFalse(secret_store.has_secret("tts"))

    def test_save_replace_restart_clear_and_corrupt_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youdao_secret.dpapi"
            with patch.object(secret_store, "secret_path", return_value=path):
                secret_store.save_secret("synthetic-1")
                self.assertNotIn(b"synthetic-1", path.read_bytes())
                self.assertEqual(secret_store.read_secret(), "synthetic-1")
                secret_store.save_secret("synthetic-2")
                self.assertEqual(secret_store.read_secret(), "synthetic-2")
                path.write_bytes(b"not-a-dpapi-blob")
                with self.assertRaises(secret_store.CredentialError):
                    secret_store.read_secret()
                secret_store.clear_secret()
                self.assertEqual(secret_store.read_secret(), "")

    def test_legacy_migration_preserves_other_fields_and_clear_does_not_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            local = root / ".env.local"
            old = root / ".env"
            path = root / "youdao_secret.dpapi"
            local.write_text("# keep\nYOUDAO_APP_KEY=synthetic-id\nYOUDAO_APP_SECRET=synthetic-secret\nOTHER=keep\n", encoding="utf-8")
            old.write_text("YOUDAO_TTS_APP_KEY=tts-only\n", encoding="utf-8")
            with (patch.object(translate, "CONFIG_PATH", config),
                  patch.object(translate, "ENV_LOCAL_PATH", local),
                  patch.object(translate, "ENV_PATH", old),
                  patch.object(secret_store, "secret_path", return_value=path)):
                translate.migrate_legacy_credentials()
                self.assertEqual(secret_store.read_secret(), "synthetic-secret")
                self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["youdao_app_key"], "synthetic-id")
                self.assertEqual(local.read_text(encoding="utf-8"), "# keep\nOTHER=keep\n")
                self.assertIn("YOUDAO_TTS_APP_KEY", old.read_text(encoding="utf-8"))
                translate.save_result_settings(False, 50, "Ctrl+I", True, "Ctrl+Alt+T", clear_app_secret=True)
                self.assertFalse(path.exists())
                translate.migrate_legacy_credentials()
                self.assertFalse(path.exists())

    def test_failed_migration_keeps_plaintext_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / ".env.local"
            local.write_text("YOUDAO_APP_SECRET=synthetic-secret\nOTHER=keep\n", encoding="utf-8")
            with (patch.object(translate, "CONFIG_PATH", root / "config.json"),
                  patch.object(translate, "ENV_LOCAL_PATH", local),
                  patch.object(translate, "ENV_PATH", root / ".env"),
                  patch.object(secret_store, "secret_path", return_value=root / "secret.dpapi"),
                  patch("translate.save_secret", side_effect=secret_store.CredentialError("save failed"))):
                with self.assertRaises(secret_store.CredentialError):
                    translate.migrate_legacy_credentials()
            self.assertIn("YOUDAO_APP_SECRET=synthetic-secret", local.read_text(encoding="utf-8"))

    def test_settings_can_replace_a_corrupt_blob_and_remove_legacy_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / ".env.local"
            local.write_text("YOUDAO_APP_KEY=synthetic-id\nYOUDAO_APP_SECRET=old-synthetic\n", encoding="utf-8")
            path = root / "secret.dpapi"
            path.write_bytes(b"corrupt")
            with (patch.object(translate, "CONFIG_PATH", root / "config.json"),
                  patch.object(translate, "ENV_LOCAL_PATH", local),
                  patch.object(translate, "ENV_PATH", root / ".env"),
                  patch.object(secret_store, "secret_path", return_value=path)):
                translate.save_result_settings(False, 50, "Ctrl+I", True, "Ctrl+Alt+T", app_secret="new-synthetic")
                self.assertEqual(secret_store.read_secret(), "new-synthetic")
                self.assertNotIn("YOUDAO_APP_SECRET", local.read_text(encoding="utf-8"))
                self.assertEqual(translate.load_config()["youdao_app_key"], "synthetic-id")


class ApiFailureTests(unittest.TestCase):
    CONFIG = {"youdao_app_key": "synthetic-id", "youdao_api_url": "https://example.invalid/api",
              "source_language": "auto", "target_language": "zh-CHS"}

    def call_with(self, response=None, exception=None, secret="synthetic-secret"):
        with (patch("translate.migrate_legacy_credentials"),
              patch("translate.read_secret", return_value=secret),
              patch("translate.requests.post", return_value=response, side_effect=exception) as post):
            try:
                result = translate.translate_by_youdao("hello", self.CONFIG)
            except translate.TranslationServiceError as error:
                return error, post
            return result, post

    def test_missing_id_and_secret(self):
        with patch("translate.migrate_legacy_credentials"), patch("translate.read_secret", return_value=""):
            with self.assertRaisesRegex(translate.TranslationServiceError, "密钥"):
                translate.translate_by_youdao("hello", self.CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            with (patch("translate.migrate_legacy_credentials"),
                  patch("translate.read_secret", return_value="synthetic"),
                  patch.object(translate, "CONFIG_PATH", Path(directory) / "absent.json")):
                with self.assertRaisesRegex(translate.TranslationServiceError, "应用 ID"):
                    translate.translate_by_youdao("hello", {"youdao_api_url": "https://example.invalid/api"})

    def test_timeout_dns_and_network_failure(self):
        for exception, diagnostic in ((requests.Timeout(), "network_timeout"),
                                      (requests.ConnectionError("secret must not appear"), "connection_failed")):
            error, post = self.call_with(exception=exception)
            self.assertEqual(error.diagnostic, diagnostic)
            self.assertNotIn("secret", str(error))
            self.assertEqual(post.call_args.kwargs["timeout"], (3, 10))

    def test_http_and_business_errors(self):
        class Response:
            def __init__(self, status, payload):
                self.status_code = status
                self.payload = payload

            def json(self):
                return self.payload

        for status, expected in ((503, "http_503"), (429, "http_429"), (401, "http_401"), (504, "http_504")):
            error, _ = self.call_with(Response(status, {}))
            self.assertEqual(error.diagnostic, expected)
        for code, expected in (("108", "密钥无效"), ("202", "密钥无效"),
                               ("401", "额度不足"), ("411", "频繁"), ("412", "频繁")):
            error, _ = self.call_with(Response(200, {"errorCode": code, "msg": "synthetic-secret"}))
            self.assertIn(expected, str(error))
            self.assertNotIn("synthetic-secret", str(error))

    def test_invalid_and_missing_response_fields(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                if isinstance(self.payload, Exception):
                    raise self.payload
                return self.payload

        for payload in (ValueError("invalid"), [], {}, {"errorCode": "0"},
                        {"errorCode": "0", "translation": [None]}):
            error, _ = self.call_with(Response(payload))
            self.assertIsInstance(error, translate.TranslationServiceError)
        result, _ = self.call_with(Response({"errorCode": "0", "translation": ["你好"]}))
        self.assertEqual(result, "你好")

    def test_request_can_succeed_after_timeout_without_restart(self):
        error, _ = self.call_with(exception=requests.Timeout())
        self.assertEqual(error.diagnostic, "network_timeout")

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"errorCode": "0", "translation": ["重试成功"]}

        result, _ = self.call_with(Response())
        self.assertEqual(result, "重试成功")


if __name__ == "__main__":
    unittest.main()
