"""Local OCR for a captured screen region; imported only for screenshot requests."""

import threading
import time


_engine = None
_engine_lock = threading.Lock()


def initialize_engine(timings=None):
    """Initialize once; the background warm-up and a screenshot may race."""
    global _engine
    if _engine is not None:
        return _engine
    waiting_at = time.perf_counter()
    with _engine_lock:
        if timings is not None:
            timings["ocr_init_wait_ms"] = (time.perf_counter() - waiting_at) * 1000
        if _engine is None:
            importing_at = time.perf_counter()
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "截图翻译需要本地 OCR 依赖，请运行：pip install -r requirements-ocr.txt"
                ) from exc
            if timings is not None:
                timings["ocr_import_ms"] = (time.perf_counter() - importing_at) * 1000
            initializing_at = time.perf_counter()
            _engine = RapidOCR()
            if timings is not None:
                timings["ocr_model_init_ms"] = (time.perf_counter() - initializing_at) * 1000
    return _engine


def extract_text_from_png(image_bytes, timings=None):
    """Recognize text without sending the image to the translation provider."""
    engine = initialize_engine(timings)
    recognizing_at = time.perf_counter()
    result = engine(image_bytes)
    if timings is not None:
        timings["ocr_infer_ms"] = (time.perf_counter() - recognizing_at) * 1000
    lines = getattr(result, "txts", None)
    if lines is None:
        lines = ()
    text = "\n".join(str(line).strip() for line in lines if str(line).strip())
    return text
