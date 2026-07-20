import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QLockFile,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QTextBlockFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
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
PROCESSING_DIR = DATA_DIR / "processing"
PROCESSED_DIR = DATA_DIR / "processed"
STATE_PATH = DATA_DIR / "app_state.json"
LOCK_PATH = DATA_DIR / "resident.lock"
RESIDENT_LOG_PATH = DATA_DIR / "resident.log"
RESIDENT_VERSION = "2026-07-19-startup-prewarm-1"
POLL_INTERVAL_MS = 150
HEARTBEAT_INTERVAL_MS = 2000
STALE_REQUEST_SECONDS = 300


def append_resident_log(message):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(RESIDENT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class TranslationWorker(QRunnable):
    def __init__(self, source_text, source_label, request_id, request_path=None):
        super().__init__()
        self.source_text = source_text
        self.source_label = source_label
        self.request_id = request_id
        self.request_path = request_path
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        started_at = time.perf_counter()
        try:
            config = load_config()
            translation = translate_text(self.source_text, config)
            saved_file = save_record(self.source_text, translation, config)
            self.signals.finished.emit(
                {
                    "source_text": self.source_text,
                    "source_label": self.source_label,
                    "request_id": self.request_id,
                    "request_path": self.request_path,
                    "translation": translation,
                    "saved_file": saved_file,
                    "config": config,
                    "elapsed_ms": (time.perf_counter() - started_at) * 1000,
                }
            )
        except Exception as exc:
            self.signals.failed.emit(
                {
                    "source_label": self.source_label,
                    "request_id": self.request_id,
                    "request_path": self.request_path,
                    "error": str(exc) or exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                }
            )


class ResultWindow(QWidget):
    manual_translation_requested = Signal(str)

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
        self.setMinimumSize(460, 320)
        self.setWindowIcon(app.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.setStyleSheet(
            """
            QWidget {
                background: #f5f5f7;
                color: #1d1d1f;
                font-family: 'Segoe UI', 'Microsoft YaHei UI';
                font-size: 12px;
            }
            QFrame#HeaderCard {
                background: rgba(255, 255, 255, 242);
                border: 1px solid #e1e1e6;
                border-radius: 16px;
            }
            QFrame#HeaderCard QLabel {
                background: transparent;
            }
            QFrame#HeaderCard QLabel#HeaderIcon {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #38a0ff, stop: 1 #0068e8
                );
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 115);
                border-radius: 11px;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#TitleLabel {
                color: #1d1d1f;
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#SubtitleLabel, QLabel#PathLabel {
                background: transparent;
                color: #6e6e73;
                font-size: 11px;
            }
            QLabel#StatusLabel {
                background: #e8f2ff;
                color: #0066cc;
                border-radius: 8px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#StatusLabel[status="loading"] {
                background: #fff4dc;
                color: #9a6700;
            }
            QLabel#StatusLabel[status="success"] {
                background: #e9f7ed;
                color: #237a3b;
            }
            QTextEdit {
                background: #ffffff;
                color: #1d1d1f;
                font-size: 14px;
                border: 1px solid #e2e2e7;
                border-radius: 16px;
                padding: 16px;
                selection-background-color: #b8d9ff;
                selection-color: #1d1d1f;
            }
            QPushButton {
                background: #e8e8ed;
                color: #1d1d1f;
                border: none;
                border-radius: 10px;
                padding: 9px 15px;
                min-width: 82px;
                font-weight: 500;
            }
            QPushButton:hover { background: #dddde3; }
            QPushButton:pressed { background: #d1d1d6; }
            QPushButton:focus {
                border: 2px solid #80bfff;
                padding: 7px 13px;
            }
            QPushButton:disabled {
                background: #ededf0;
                color: #a1a1a6;
            }
            QPushButton#PrimaryButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #198cff, stop: 1 #0071e3
                );
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton#PrimaryButton:hover { background: #0875df; }
            QPushButton#PrimaryButton:pressed { background: #0062cc; }
            QPushButton#CloseButton {
                background: transparent;
                color: #6e6e73;
            }
            QPushButton#CloseButton:hover { background: #e8e8ed; }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        header_card = QFrame()
        header_card.setObjectName("HeaderCard")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(15, 13, 15, 13)
        header_layout.setSpacing(12)

        header_icon = QLabel("译")
        header_icon.setObjectName("HeaderIcon")
        header_icon.setAlignment(Qt.AlignCenter)
        header_icon.setFixedSize(38, 38)

        header_text_layout = QVBoxLayout()
        header_text_layout.setContentsMargins(0, 0, 0, 0)
        header_text_layout.setSpacing(2)

        title_label = QLabel("翻译结果")
        title_label.setObjectName("TitleLabel")
        hotkey_text = str(self.config.get("hotkey", "Alt+T")).replace("+", " + ")
        subtitle_label = QLabel(f"{hotkey_text} 翻译 · Enter / Esc 隐藏")
        subtitle_label.setObjectName("SubtitleLabel")

        header_text_layout.addWidget(title_label)
        header_text_layout.addWidget(subtitle_label)
        header_layout.addWidget(header_icon)
        header_layout.addLayout(header_text_layout)
        header_layout.addStretch(1)
        layout.addWidget(header_card)

        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(190)
        editor_font = QFont(app.font())
        editor_font.setPointSize(14)
        self.editor.setFont(editor_font)
        self.editor.setAccessibleName("翻译结果正文")
        editor_shadow = QGraphicsDropShadowEffect(self.editor)
        editor_shadow.setBlurRadius(24)
        editor_shadow.setOffset(0, 5)
        editor_shadow.setColor(QColor(20, 20, 30, 28))
        self.editor.setGraphicsEffect(editor_shadow)
        layout.addWidget(self.editor)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self.status_label = QLabel("等待翻译")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setProperty("status", "idle")
        self.path_label = QLabel("")
        self.path_label.setObjectName("PathLabel")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        meta_row.addWidget(self.status_label)
        meta_row.addWidget(self.path_label, 1)
        layout.addLayout(meta_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self.open_btn = QPushButton("打开记录")
        self.copy_btn = QPushButton("复制译文")
        manual_btn = QPushButton("手动输入")
        close_btn = QPushButton("关闭")
        self.copy_btn.setObjectName("PrimaryButton")
        close_btn.setObjectName("CloseButton")
        close_btn.setDefault(True)
        close_btn.setAutoDefault(True)
        self.open_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)

        for button in (self.open_btn, self.copy_btn, manual_btn, close_btn):
            button.setCursor(Qt.PointingHandCursor)

        self.open_btn.clicked.connect(self.open_saved_file)
        self.copy_btn.clicked.connect(self.copy_translation)
        manual_btn.clicked.connect(self.manual_translate)
        close_btn.clicked.connect(self.close)

        button_row.addWidget(self.open_btn)
        button_row.addWidget(manual_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        button_row.addWidget(self.copy_btn)
        layout.addLayout(button_row)
        self.resize(600, 400)

    def set_status(self, text, status="idle"):
        self.status_label.setText(text)
        self.status_label.setProperty("status", status)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def format_editor_text(self):
        cursor = QTextCursor(self.editor.document())
        cursor.select(QTextCursor.Document)
        block_format = QTextBlockFormat()
        block_format.setLineHeight(140, QTextBlockFormat.ProportionalHeight.value)
        block_format.setBottomMargin(6)
        cursor.mergeBlockFormat(block_format)
        cursor.clearSelection()
        self.editor.setTextCursor(cursor)
        self.editor.moveCursor(QTextCursor.Start)

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
            self.set_status("✓ 已复制", "success")

    def manual_translate(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "手动输入翻译", "请输入要翻译的英文/文本：", ""
        )
        if not ok:
            return
        source = text.strip()
        if not source:
            QMessageBox.information(self, "translate_tool", "输入内容为空。")
            return
        self.manual_translation_requested.emit(source)

    def show_loading(self):
        self.editor.setPlainText("正在翻译，请稍候……")
        self.format_editor_text()
        self.path_label.setText("")
        self.open_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.set_status("● 正在翻译", "loading")
        self.bring_to_front()

    def update_result(self, source_text, translation, saved_file):
        self.current_source = source_text
        self.current_translation = translation
        self.current_saved_file = Path(saved_file)
        self.editor.setPlainText(translation.strip())
        self.format_editor_text()
        self.path_label.setText(f"保存至  {self.current_saved_file.name}")
        self.path_label.setToolTip(str(self.current_saved_file))
        self.open_btn.setEnabled(True)
        self.copy_btn.setEnabled(bool(translation.strip()))
        self.set_status("✓ 已保存", "success")
        self.bring_to_front()

    def bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()
        QApplication.alert(self, 0)
        append_resident_log(
            f"result window shown | visible={self.isVisible()} | win_id={int(self.winId())}"
        )


class ResidentApp(QObject):
    def __init__(self, instance_lock, app):
        super().__init__()
        self.instance_lock = instance_lock
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.active_worker = None
        self.active_request = None
        self.app = app
        self.app.setQuitOnLastWindowClosed(False)
        for directory in (REQUEST_DIR, PROCESSING_DIR, PROCESSED_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        self.recover_interrupted_requests()

        window_started_at = time.perf_counter()
        self.window = ResultWindow()
        self.window.manual_translation_requested.connect(self.process_manual_request)
        # 隐藏状态下提前创建原生窗口并完成首帧渲染，避免第一次翻译时支付 Qt 冷启动成本。
        self.window.ensurePolished()
        self.window.winId()
        self.window.grab()
        append_resident_log(
            f"result window prewarmed | elapsed_ms="
            f"{(time.perf_counter() - window_started_at) * 1000:.0f}"
        )
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(1)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.process_pending_requests)
        self.poll_timer.start(POLL_INTERVAL_MS)

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.write_state)
        self.heartbeat_timer.start(HEARTBEAT_INTERVAL_MS)

        self.app.aboutToQuit.connect(self.cleanup)
        self.write_state()
        append_resident_log(f"resident app started | pid={os.getpid()} | version={RESIDENT_VERSION}")

    def recover_interrupted_requests(self):
        for processing_path in PROCESSING_DIR.glob("request_*.json"):
            target = REQUEST_DIR / processing_path.name
            if target.exists():
                target = REQUEST_DIR / (
                    f"{processing_path.stem}_{int(time.time() * 1000)}{processing_path.suffix}"
                )
            try:
                processing_path.replace(target)
                append_resident_log(f"recovered interrupted request: {processing_path.name}")
            except OSError:
                append_resident_log(
                    f"recover request failed: {processing_path.name}\n{traceback.format_exc()}"
                )

    def write_state(self):
        state = {
            "pid": os.getpid(),
            "version": RESIDENT_VERSION,
            "started_at": self.started_at,
            "heartbeat_ts": time.time(),
        }
        temp_path = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, STATE_PATH)

    def process_pending_requests(self):
        if self.active_worker is not None:
            return

        request_files = list(REQUEST_DIR.glob("request_*.json"))
        request_files.sort(key=lambda path: path.stat().st_mtime if path.exists() else float("inf"))
        for request_file in request_files:
            claimed_path = PROCESSING_DIR / request_file.name
            try:
                request_file.replace(claimed_path)
            except FileNotFoundError:
                continue
            except OSError:
                append_resident_log(
                    f"claim request failed: {request_file.name}\n{traceback.format_exc()}"
                )
                continue

            try:
                with open(claimed_path, "r", encoding="utf-8-sig") as f:
                    payload = json.load(f)
                text = str(payload.get("text", "")).strip()
                source = str(payload.get("source", "selection")).strip() or "selection"
                request_id = str(payload.get("id", claimed_path.stem)).strip() or claimed_path.stem
                if not text:
                    raise RuntimeError("请求内容为空")
                if time.time() - claimed_path.stat().st_mtime > STALE_REQUEST_SECONDS:
                    raise RuntimeError("请求等待时间过长，已停止处理")
            except Exception as exc:
                append_resident_log(
                    f"invalid request: {claimed_path.name}\n{traceback.format_exc()}"
                )
                self.archive_failed_request(claimed_path)
                QMessageBox.critical(self.window, "翻译失败", str(exc))
                continue

            self.start_translation(text, source, request_id, claimed_path)
            return

    @Slot(str)
    def process_manual_request(self, text):
        if self.active_worker is not None:
            QMessageBox.information(self.window, "translate_tool", "已有翻译任务正在进行，请稍候。")
            return
        request_id = f"manual_{int(time.time() * 1000)}"
        self.start_translation(text, "manual", request_id)

    def start_translation(self, text, source, request_id, request_path=None):
        self.active_request = request_path
        worker = TranslationWorker(text, source, request_id, request_path)
        worker.signals.finished.connect(self.translation_finished)
        worker.signals.failed.connect(self.translation_failed)
        self.active_worker = worker
        # 翻译先进入后台线程；即使首次显示窗口较慢，也不会阻塞网络请求。
        self.thread_pool.start(worker)
        self.window.show_loading()

    @Slot(object)
    def translation_finished(self, result):
        self.window.config = result["config"]
        self.window.update_result(
            result["source_text"], result["translation"], result["saved_file"]
        )
        request_path = result["request_path"]
        if request_path:
            self.finalize_successful_request(
                Path(request_path), bool(result["config"].get("keep_processed_requests", False))
            )
        append_perf_log(
            "resident_request",
            f"id={result['request_id']} | source={result['source_label']} | "
            f"total={result['elapsed_ms']:.0f}ms",
        )
        self.finish_active_job()

    @Slot(object)
    def translation_failed(self, result):
        append_resident_log(
            f"process request failed: {result['request_id']}\n{result['traceback']}"
        )
        if result["request_path"]:
            self.archive_failed_request(Path(result["request_path"]))
        QMessageBox.critical(self.window, "翻译失败", result["error"])
        self.finish_active_job()

    def finalize_successful_request(self, request_path, keep_processed):
        try:
            if keep_processed:
                target = self.unique_archive_path(request_path.name)
                request_path.replace(target)
            else:
                request_path.unlink(missing_ok=True)
        except Exception:
            append_resident_log(
                f"finalize request failed: {request_path.name}\n{traceback.format_exc()}"
            )

    def archive_failed_request(self, request_path):
        try:
            target = self.unique_archive_path(f"failed_{request_path.name}")
            request_path.replace(target)
        except Exception:
            append_resident_log(
                f"archive failed request failed: {request_path.name}\n{traceback.format_exc()}"
            )

    @staticmethod
    def unique_archive_path(file_name):
        target = PROCESSED_DIR / file_name
        if not target.exists():
            return target
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return PROCESSED_DIR / f"{target.stem}_{timestamp}{target.suffix}"

    def finish_active_job(self):
        self.active_worker = None
        self.active_request = None
        QTimer.singleShot(0, self.process_pending_requests)

    def cleanup(self):
        try:
            if STATE_PATH.exists():
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if state.get("pid") == os.getpid():
                    STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        self.instance_lock.unlock()

    def run(self):
        return self.app.exec()


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    instance_lock = QLockFile(str(LOCK_PATH))
    instance_lock.setStaleLockTime(10000)
    if not instance_lock.tryLock(0):
        append_resident_log("resident app start skipped: another instance owns the lock")
        return 0

    try:
        resident = ResidentApp(instance_lock, app)
        return resident.run()
    except Exception:
        append_resident_log(f"resident app startup failed\n{traceback.format_exc()}")
        instance_lock.unlock()
        raise


if __name__ == "__main__":
    sys.exit(main())
