import unittest
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
import sys
from unittest.mock import patch

import screen_ocr


class ScreenOcrTests(unittest.TestCase):
    def tearDown(self):
        screen_ocr._engine = None

    def test_joins_recognized_lines(self):
        class Result:
            txts = (" Hello ", "", " world ")

        with patch.object(screen_ocr, "_engine", lambda _: Result()):
            self.assertEqual(screen_ocr.extract_text_from_png(b"png"), "Hello\nworld")

    def test_empty_result_returns_empty_text(self):
        class Result:
            txts = None

        with patch.object(screen_ocr, "_engine", lambda _: Result()):
            self.assertEqual(screen_ocr.extract_text_from_png(b"png"), "")

    def test_reports_inference_time_without_reinitializing(self):
        class Result:
            txts = ("Hello",)

        timings = {}
        with patch.object(screen_ocr, "_engine", lambda _: Result()):
            self.assertEqual(screen_ocr.extract_text_from_png(b"png", timings), "Hello")
        self.assertIn("ocr_infer_ms", timings)
        self.assertNotIn("ocr_model_init_ms", timings)

    def test_parallel_warmup_initializes_model_once(self):
        class FakeEngine:
            created = 0

            def __init__(self):
                FakeEngine.created += 1

        fake_rapidocr = ModuleType("rapidocr")
        fake_rapidocr.RapidOCR = FakeEngine
        with patch.dict(sys.modules, {"rapidocr": fake_rapidocr}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                engines = list(pool.map(lambda _: screen_ocr.initialize_engine(), range(4)))
        self.assertEqual(FakeEngine.created, 1)
        self.assertTrue(all(engine is engines[0] for engine in engines))
