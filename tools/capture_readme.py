"""Render the actual UI with public demonstration text, without APIs/history."""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from resident_app import ModernResultWindow

def main():
    app = QApplication.instance() or QApplication([])
    app.setWindowIcon(QIcon(str(ROOT / "assets/transeasy-icon.ico")))
    config = {"provider": "mock", "save_dir": str(ROOT / "build/readme-demo"),
              "hotkey": "Alt+T", "manual_input_hotkey": "Ctrl+I",
              "screenshot_enabled": True, "screenshot_hotkey": "Ctrl+Alt+T",
              "auto_read_aloud": False, "auto_read_max_chars": 50,
              "result_font_size": 18, "result_card_width": 500, "result_card_height": 560}
    records = [{"source": "Stay focused. Translate what you need.", "translation": "保持专注，翻译眼前所需。", "time": "2026-09-26 10:30", "file": ROOT / "build/readme-demo/demo.txt"},
               {"source": "Capture a region and recognize its text.", "translation": "框选区域，识别其中的文字。", "time": "2026-09-26 10:25", "file": ROOT / "build/readme-demo/demo.txt"}]
    with patch("resident_app.load_config", return_value=config), patch("resident_app.load_environment"), patch("resident_app.has_secret", return_value=False), patch("resident_app.load_recent_records", return_value=records):
        window = ModernResultWindow()
        window.current_source = records[0]["source"]
        window.current_translation = records[0]["translation"]
        window.editor.setPlainText("保持专注，\n翻译眼前所需。\n\n划词、截图、OCR 与朗读，\n在需要时出现。")
        window.format_editor_text()
        window.copy_btn.setEnabled(True)
        window.read_btn.setEnabled(True)
        window.set_status("演示译文", "success")
        window.show()
        window.toggle_rail()
        QTest.qWait(500)
        def capture(name):
            app.processEvents()
            image = QImage(window.width() + 40, window.height() + 40, QImage.Format_ARGB32)
            image.fill(QColor("#f6f8fa"))
            painter = QPainter(image)
            window.render(painter, QPoint(20, 20))
            painter.end()
            if not image.save(str(ROOT / "assets" / name)):
                raise RuntimeError("Could not save documentation screenshot")
        capture("transeasy-main.png")
        window.open_saved_file()
        QTest.qWait(600)
        capture("transeasy-history.png")
        window.back_stage()
        QTest.qWait(600)
        window.open_settings()
        QTest.qWait(600)
        from PySide6.QtWidgets import QScrollArea
        scroll = window._pages["settings"].findChild(QScrollArea)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        QTest.qWait(100)
        capture("transeasy-settings.png")
        window.hide()
    print("Generated actual-UI screenshots using public demo data only.")

if __name__ == "__main__":
    main()
