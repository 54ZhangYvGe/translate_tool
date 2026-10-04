import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 未安装")
class ScreenshotPipelineTests(unittest.TestCase):
    def test_ocr_only_skips_api_history_and_translation_loading(self):
        from resident_app import TranslationWorker

        worker = TranslationWorker("", "screenshot", "ocr-only", image_bytes=b"png")
        finished, ready = [], []
        worker.signals.finished.connect(finished.append)
        worker.signals.ocr_ready.connect(ready.append)
        with (
            patch("resident_app.load_config", return_value={"screenshot_auto_translate": False}),
            patch("screen_ocr.extract_text_from_png", return_value="Hello OCR"),
            patch("resident_app.translate_text") as translate,
            patch("resident_app.save_record") as save,
        ):
            worker.run()
        translate.assert_not_called()
        save.assert_not_called()
        self.assertEqual(ready, [])
        self.assertTrue(finished[0]["ocr_only"])
        self.assertEqual(finished[0]["source_text"], "Hello OCR")

    def test_ocr_only_does_not_change_manual_translation(self):
        from resident_app import TranslationWorker

        worker = TranslationWorker("Hello", "manual", "manual")
        finished = []
        worker.signals.finished.connect(finished.append)
        config = {"screenshot_auto_translate": False}
        with (
            patch("resident_app.load_config", return_value=config),
            patch("resident_app.translate_text", return_value="你好") as translate,
            patch("resident_app.save_record", return_value=Path("history.txt")),
        ):
            worker.run()
        translate.assert_called_once_with("Hello", config)
        self.assertEqual(finished[0]["translation"], "你好")

    def test_ocr_only_result_reaches_window_and_finishes_request(self):
        from resident_app import ResidentApp

        window = SimpleNamespace(apply_config_to_ui=Mock(), show_extracted_text=Mock(), update_result=Mock())
        app = SimpleNamespace(window=window, finalize_successful_request=Mock(), finish_active_job=Mock())
        result = {"ocr_only": True, "source_text": "Hello", "source_label": "screenshot",
                  "config": {}, "request_id": "ocr-only", "request_path": Path("request.json"),
                  "timings": {}, "elapsed_ms": 1}
        with patch("resident_app.append_perf_log"):
            ResidentApp.translation_finished(app, result)
        window.show_extracted_text.assert_called_once_with("Hello")
        window.update_result.assert_not_called()
        app.finalize_successful_request.assert_called_once_with(Path("request.json"), False)
        app.finish_active_job.assert_called_once()

    def test_worker_sends_only_ocr_text_to_translation(self):
        from resident_app import TranslationWorker

        with tempfile.TemporaryDirectory() as directory:
            saved_file = Path(directory) / "history.txt"
            worker = TranslationWorker("", "screenshot", "test", image_bytes=b"png")
            finished = []
            ocr_ready = []
            worker.signals.finished.connect(finished.append)
            worker.signals.ocr_ready.connect(ocr_ready.append)
            with (
                patch("resident_app.load_config", return_value={"provider": "mock"}),
                patch("screen_ocr.extract_text_from_png", return_value="Hello") as ocr,
                patch("resident_app.translate_text", return_value="你好") as translate,
                patch("resident_app.save_record", return_value=saved_file) as save,
            ):
                worker.run()

            ocr.assert_called_once()
            self.assertEqual(ocr.call_args.args, (b"png", finished[0]["timings"]))
            translate.assert_called_once_with("Hello", {"provider": "mock"})
            save.assert_called_once_with("Hello", "你好", {"provider": "mock"})
            self.assertEqual(finished[0]["source_text"], "Hello")
            self.assertEqual(ocr_ready, ["test"])
            self.assertIn("translate_ms", finished[0]["timings"])
            self.assertIn("save_ms", finished[0]["timings"])

    def test_worker_reports_stage_timings_on_ocr_failure(self):
        from resident_app import TranslationWorker

        worker = TranslationWorker("", "screenshot", "test-error", image_bytes=b"png")
        failed = []
        worker.signals.failed.connect(failed.append)
        with (
            patch("resident_app.load_config", return_value={"provider": "mock"}),
            patch("screen_ocr.extract_text_from_png", side_effect=RuntimeError("OCR failed")),
        ):
            worker.run()

        self.assertEqual(failed[0]["error"], "处理失败，请稍后重试或检查日志")
        self.assertEqual(failed[0]["diagnostic"], "RuntimeError")
        self.assertIn("ocr_module_import_ms", failed[0]["timings"])
        self.assertGreaterEqual(failed[0]["elapsed_ms"], 0)

    def test_empty_screenshot_skips_translation_without_error(self):
        from resident_app import TranslationWorker

        worker = TranslationWorker("", "screenshot", "empty", image_bytes=b"png")
        empty, ready, failed = [], [], []
        worker.signals.empty.connect(empty.append)
        worker.signals.ocr_ready.connect(ready.append)
        worker.signals.failed.connect(failed.append)
        with (
            patch("resident_app.load_config", return_value={"provider": "mock"}),
            patch("screen_ocr.extract_text_from_png", return_value="  "),
            patch("resident_app.translate_text") as translate,
            patch("resident_app.save_record") as save,
        ):
            worker.run()

        self.assertEqual(empty[0]["request_id"], "empty")
        self.assertEqual(ready, [])
        self.assertEqual(failed, [])
        translate.assert_not_called()
        save.assert_not_called()

    def test_empty_screenshot_is_finalized_without_showing_window(self):
        from resident_app import ResidentApp

        fake_app = SimpleNamespace(
            active_worker=SimpleNamespace(request_id="empty"),
            window=SimpleNamespace(show_loading=Mock()),
            finalize_successful_request=Mock(),
            finish_active_job=Mock(),
        )
        request_path = Path("request_empty.json")
        result = {
            "request_id": "empty",
            "request_path": request_path,
            "timings": {"ocr_infer_ms": 1.0},
            "elapsed_ms": 2.0,
        }
        with patch("resident_app.append_perf_log"):
            ResidentApp.screenshot_empty(fake_app, result)

        fake_app.window.show_loading.assert_not_called()
        fake_app.finalize_successful_request.assert_called_once_with(request_path, False)
        fake_app.finish_active_job.assert_called_once()
