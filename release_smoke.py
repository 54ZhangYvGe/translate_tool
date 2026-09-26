"""Packaged smoke checks, permitted only in an isolated mock test directory."""

import ctypes
import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QPainter, QPixmap
from PySide6.QtTest import QTest

from secret_store import has_secret, read_secret, save_secret
from translate import BASE_DIR, APP_DIR


def _memory_mb():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "peak_ws", "ws", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.ws / 1024**2, 1)


def run_smoke(resident, app, overlay_class, phase):
    report = {"phase": phase, "checks": [], "ok": False}
    isolated_data = Path(os.environ.get("LOCALAPPDATA", "")).resolve()
    if os.environ.get("SCREENTRANS_RELEASE_TEST") != "1" or not isolated_data.is_relative_to(APP_DIR) or resident.window.config.get("provider") != "mock":
        raise RuntimeError("Release smoke tests require isolated LOCALAPPDATA and mock config")
    window = resident.window

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)

    def wait_for_job():
        deadline = time.monotonic() + 90
        while resident.active_worker is not None and time.monotonic() < deadline:
            QTest.qWait(25)
        check("worker completed", resident.active_worker is None)

    try:
        window.show_loading()
        QTest.qWait(50)
        check("GUI visible", window.isVisible())
        check("application icon", not window.windowIcon().isNull())
        report["memory_before_ocr_mb"] = _memory_mb()
        if phase == "write":
            window.toggle_rail()
            QTest.qWait(300)
            window.open_settings()
            QTest.qWait(500)
            window.result_font_edit.setText("17")
            window.card_width_edit.setText("470")
            window.card_height_edit.setText("420")
            window.api_key_edit.setText("synthetic-release-id")
            window.api_secret_edit.setText("synthetic-release-secret-1")
            window.tts_api_key_edit.setText("synthetic-release-tts-id")
            window.tts_api_secret_edit.setText("synthetic-tts-release")
            window.save_settings()
            QTest.qWait(500)
            check("settings saved", window.editor.font().pointSize() == 17 and window.card.width() == 470)
            check("DPAPI saved", read_secret() == "synthetic-release-secret-1")
            check("TTS settings saved independently", read_secret("tts") == "synthetic-tts-release" and window.config["youdao_tts_app_key"] == "synthetic-release-tts-id")
            resident.start_translation("Hello release test", "manual", "smoke-manual")
            wait_for_job()
            check("mock translation/history saved", "Hello release test" in window.current_translation and window.current_saved_file.exists())
            screenshot = app.primaryScreen().grabWindow(window.winId())
            check("screen capture available", not screenshot.isNull())
            picture = QPixmap(700, 150)
            picture.fill(Qt.white)
            painter = QPainter(picture)
            painter.setPen(Qt.black)
            painter.setFont(QFont("Segoe UI", 28))
            painter.drawText(25, 85, "Hello ScreenTrans OCR")
            painter.end()
            overlay = overlay_class(app.primaryScreen(), picture)
            overlay.resize(700, 150)
            captures = []
            overlay.captured.connect(lambda png, elapsed: captures.append(png))
            overlay.show()
            QTest.qWait(50)
            QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(5, 5))
            QTest.mouseMove(overlay, QPoint(695, 145))
            QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(695, 145))
            overlay.close()
            overlay.deleteLater()
            check("selection produced PNG", bool(captures))
            for index in range(3):
                resident.start_translation("", "screenshot", f"smoke-ocr-{index}", image_bytes=captures[0])
                wait_for_job()
                check(f"OCR/mock translation round {index + 1}", "ScreenTrans" in window.current_source)
            window.open_saved_file()
            QTest.qWait(500)
            check("history page", window._stage == "history" and window.history_layout.count() > 1)
            window.history_layout.itemAt(0).widget().click()
            QTest.qWait(500)
            check("history detail", window._stage == "source" and bool(window.current_translation))
            window.open_settings()
            QTest.qWait(500)
            window.screenshot_checkbox.setChecked(False)
            window.save_settings()
            QTest.qWait(450)
            check("screenshot disabled", not window.config["screenshot_enabled"])
            window.open_settings()
            QTest.qWait(500)
            window.screenshot_checkbox.setChecked(True)
            window.screenshot_hotkey_edit.setKeySequence("Ctrl+Alt+F11")
            window.save_settings()
            QTest.qWait(450)
            check("hotkey and toggle saved", window.config["screenshot_hotkey"] == "Ctrl+Alt+F11" and window.config["screenshot_enabled"])
        elif phase == "restart":
            check("DPAPI read after process restart", read_secret() == "synthetic-release-secret-1")
            check("TTS DPAPI read after restart", read_secret("tts") == "synthetic-tts-release")
            window.api_secret_edit.setText("synthetic-release-secret-2")
            window.save_settings()
            check("DPAPI replacement", read_secret() == "synthetic-release-secret-2")
            check("translation replacement preserves TTS", read_secret("tts") == "synthetic-tts-release")
            window.clear_secret_checkbox.setChecked(True)
            window.save_settings()
            check("DPAPI cleared", not has_secret())
            check("translation clear preserves TTS", read_secret("tts") == "synthetic-tts-release")
            window.api_secret_edit.setText("synthetic-release-secret-3")
            window.tts_api_secret_edit.setText("synthetic-tts-replaced")
            window.save_settings()
            check("TTS replacement", read_secret("tts") == "synthetic-tts-replaced")
            window.clear_tts_secret_checkbox.setChecked(True)
            window.save_settings()
            check("TTS clear preserves translation", not has_secret("tts") and read_secret() == "synthetic-release-secret-3")
            window.clear_secret_checkbox.setChecked(True)
            window.save_settings()
        elif phase == "cleared":
            check("cleared secrets stay cleared after restart", not has_secret() and not has_secret("tts"))
            check("ordinary settings retained", window.config["result_font_size"] == 17)
        else:
            raise ValueError("Unknown smoke phase")
        for _ in range(3):
            window.hide()
            QTest.qWait(20)
            window.show()
            QTest.qWait(20)
        check("window open/close", window.isVisible())
        report["memory_end_mb"] = _memory_mb()
        window.grab().save(str(BASE_DIR / f"smoke-ui-{phase}.png"))
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.hide()
        resident.thread_pool.waitForDone(15000)
        resident.cleanup()
        (BASE_DIR / f"smoke-report-{phase}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
