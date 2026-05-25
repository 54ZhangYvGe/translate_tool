import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from translate import BASE_DIR, append_perf_log, load_config, save_record, translate_text


DATA_DIR = BASE_DIR / "data"
REQUEST_DIR = DATA_DIR / "requests"
PROCESSED_DIR = DATA_DIR / "processed"
STATE_PATH = DATA_DIR / "app_state.json"
RESIDENT_LOG_PATH = DATA_DIR / "resident.log"
RESIDENT_VERSION = "2026-05-25-resident-keepalive-1"
POLL_INTERVAL_MS = 150
HEARTBEAT_INTERVAL_MS = 2000
STALE_REQUEST_SECONDS = 300


def append_resident_log(message):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(RESIDENT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


class ResultWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.current_source = ""
        self.current_translation = ""
        self.current_saved_file = None
        self.setup_ui()

    def setup_ui(self):
        app = QApplication.instance()
        self.setWindowTitle("翻译结果")
        self.setMinimumWidth(420)
        self.setWindowIcon(app.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.setStyleSheet(
            """
            QWidget {
                background: #f7f3ea;
                color: #2b2b2b;
                font-family: 'Microsoft YaHei UI';
            }
            QFrame#HeaderCard {
                background: #efe7d8;
                border: 1px solid #ddd2bf;
                border-radius: 12px;
            }
            QTextEdit {
                background: #fffdf8;
                border: 1px solid #d8d0c2;
                border-radius: 10px;
                padding: 12px;
            }
            QPushButton {
                background: #e7dfd1;
                border: 1px solid #c8baa2;
                border-radius: 8px;
                padding: 8px 14px;
                min-width: 88px;
            }
            QPushButton:hover {
                background: #ddd1bc;
            }
            QPushButton:pressed {
                background: #d2c1a4;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header_card = QFrame()
        header_card.setObjectName("HeaderCard")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(12, 10, 12, 10)
        header_layout.setSpacing(10)

        header_icon = QLabel("✨")
        header_icon.setFont(QFont("Segoe UI Emoji", 18))
        header_icon.setAlignment(Qt.AlignCenter)
        header_icon.setFixedWidth(28)

        header_text_layout = QVBoxLayout()
        header_text_layout.setContentsMargins(0, 0, 0, 0)
        header_text_layout.setSpacing(2)

        title_label = QLabel("翻译结果")
        title_label.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        subtitle_label = QLabel("常驻模式已启用 · Enter / Esc 可直接关闭")
        subtitle_label.setStyleSheet("color: #7a7468; font-size: 11px;")

        header_text_layout.addWidget(title_label)
        header_text_layout.addWidget(subtitle_label)
        header_layout.addWidget(header_icon)
        header_layout.addLayout(header_text_layout)
        header_layout.addStretch(1)
        layout.addWidget(header_card)

        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(170)
        self.editor.setFont(QFont("Microsoft YaHei UI", 13))
        layout.addWidget(self.editor)

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color: #7a7468; font-size: 11px;")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.path_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        open_btn = QPushButton("打开保存文件")
        copy_btn = QPushButton("复制译文")
        manual_btn = QPushButton("手动输入")
        close_btn = QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.setAutoDefault(True)

        open_btn.clicked.connect(self.open_saved_file)
        copy_btn.clicked.connect(self.copy_translation)
        manual_btn.clicked.connect(self.manual_translate)
        close_btn.clicked.connect(self.close)

        button_row.addWidget(open_btn)
        button_row.addWidget(copy_btn)
        button_row.addWidget(manual_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self.resize(560, 340)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.hide()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def open_saved_file(self):
        if not self.current_saved_file:
            return
        target = Path(self.current_saved_file)
        if not target.exists():
            QMessageBox.warning(self, "translate_tool", f"保存文件不存在：\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def copy_translation(self):
        if self.current_translation:
            QGuiApplication.clipboard().setText(self.current_translation.strip())

    def manual_translate(self):
        text, ok = QInputDialog.getMultiLineText(
            self,
            "手动输入翻译",
            "请输入要翻译的英文/文本：",
            "",
        )
        if not ok:
            return

        source = text.strip()
        if not source:
            QMessageBox.information(self, "translate_tool", "输入内容为空。")
            return

        self.config = load_config()
        try:
            translation = translate_text(source, self.config)
            saved_file = save_record(source, translation, self.config)
        except Exception:
            QMessageBox.critical(self, "翻译失败", traceback.format_exc())
            return

        self.update_result(source, translation, saved_file)

    def update_result(self, source_text, translation, saved_file):
        self.current_source = source_text
        self.current_translation = translation
        self.current_saved_file = Path(saved_file)
        self.editor.setPlainText(translation.strip())
        self.path_label.setText(str(saved_file))
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()


class ResidentApp:
    def __init__(self):
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        self.window = ResultWindow()

        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.process_pending_requests)
        self.poll_timer.start(POLL_INTERVAL_MS)

        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.write_state)
        self.heartbeat_timer.start(HEARTBEAT_INTERVAL_MS)

        self.write_state()
        append_resident_log("resident app started")

    def write_state(self):
        state = {
            "pid": os.getpid(),
            "version": RESIDENT_VERSION,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat_ts": time.time(),
        }
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def process_pending_requests(self):
        request_files = sorted(REQUEST_DIR.glob("request_*.json"), key=lambda p: p.stat().st_mtime)
        for request_file in request_files:
            self.process_request(request_file)

    def process_request(self, request_file):
        started_at = time.perf_counter()
        try:
            with open(request_file, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)

            text = str(payload.get("text", "")).strip()
            source = str(payload.get("source", "selection")).strip() or "selection"
            request_id = str(payload.get("id", request_file.stem)).strip() or request_file.stem

            if not text:
                raise RuntimeError("请求内容为空")

            if time.time() - request_file.stat().st_mtime > STALE_REQUEST_SECONDS:
                append_resident_log(f"skip stale request: {request_file.name}")
                request_file.unlink(missing_ok=True)
                return

            config = load_config()
            translation = translate_text(text, config)
            saved_file = save_record(text, translation, config)
            self.window.config = config
            self.window.update_result(text, translation, saved_file)

            archive_path = PROCESSED_DIR / request_file.name
            if archive_path.exists():
                archive_path.unlink()
            request_file.replace(archive_path)

            total_elapsed_ms = (time.perf_counter() - started_at) * 1000
            append_perf_log(
                "resident_request",
                f"id={request_id} | source={source} | total={total_elapsed_ms:.0f}ms",
            )
        except json.JSONDecodeError:
            append_resident_log(
                f"process request failed (json decode / possible BOM issue): {request_file.name}\n"
                f"{traceback.format_exc()}"
            )
            try:
                bad_path = PROCESSED_DIR / f"failed_{request_file.name}"
                if bad_path.exists():
                    bad_path.unlink()
                request_file.replace(bad_path)
            except Exception:
                pass
        except Exception:
            append_resident_log(f"process request failed: {request_file.name}\n{traceback.format_exc()}")
            try:
                bad_path = PROCESSED_DIR / f"failed_{request_file.name}"
                if bad_path.exists():
                    bad_path.unlink()
                request_file.replace(bad_path)
            except Exception:
                pass

    def run(self):
        return self.app.exec()


def main():
    resident = ResidentApp()
    sys.exit(resident.run())


if __name__ == "__main__":
    main()