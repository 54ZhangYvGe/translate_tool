import json
import ctypes
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QLockFile,
    QObject,
    QIODevice,
    QEvent,
    QRect,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    QUrl,
    QEasingCurve,
    QVariantAnimation,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QIntValidator,
    QKeySequence,
    QPainter,
    QPen,
    QRegion,
    QShortcut,
    QTextBlockFormat,
    QTextCursor,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from translate import (
    BASE_DIR,
    APP_DIR,
    TTS_MAX_WORDS,
    append_perf_log,
    count_tts_words,
    load_config,
    load_environment,
    load_recent_records,
    migrate_legacy_credentials,
    save_result_settings,
    save_read_aloud_settings,
    save_record,
    synthesize_speech_to_temp,
    translate_text,
    TranslationServiceError,
)
from secret_store import CredentialError, has_secret


DATA_DIR = BASE_DIR / "data"
REQUEST_DIR = DATA_DIR / "requests"
PROCESSING_DIR = DATA_DIR / "processing"
PROCESSED_DIR = DATA_DIR / "processed"
STATE_PATH = DATA_DIR / "app_state.json"
SHUTDOWN_PATH = DATA_DIR / "shutdown.request"
LOCK_PATH = DATA_DIR / "resident.lock"
RESIDENT_LOG_PATH = DATA_DIR / "resident.log"
RESIDENT_VERSION = "0.1.0-rc1"
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
    ocr_ready = Signal(str)
    empty = Signal(object)


class ScreenCaptureOverlay(QWidget):
    captured = Signal(bytes, float)
    cancelled = Signal()

    def __init__(self, screen, snapshot):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.snapshot = snapshot
        self.start_point = None
        self.selection = QRect()
        self.setWindowTitle("截图翻译")
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.snapshot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 65))
        if not self.selection.isNull():
            # Keep the chosen pixels clear while the rest of the screen stays dimmed.
            painter.save()
            painter.setClipRect(self.selection)
            painter.drawPixmap(self.rect(), self.snapshot)
            painter.restore()
            painter.fillRect(self.selection, QColor(70, 145, 240, 20))
            painter.setPen(QPen(QColor("#8ec4ff"), 2))
            painter.drawRect(self.selection)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(20, 32, "拖动框选文字区域 · Esc 取消")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_point = event.position().toPoint()
            self.selection = QRect(self.start_point, self.start_point)
            self.update()

    def mouseMoveEvent(self, event):
        if self.start_point is not None:
            self.selection = QRect(
                self.start_point, event.position().toPoint()
            ).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.start_point is None:
            return
        self.selection = QRect(
            self.start_point, event.position().toPoint()
        ).normalized().intersected(self.rect())
        self.start_point = None
        if self.selection.width() < 5 or self.selection.height() < 5:
            self.selection = QRect()
            self.update()
            return
        # Mouse positions are logical pixels; screen grabs can have a different DPR.
        x_scale = self.snapshot.width() / self.width()
        y_scale = self.snapshot.height() / self.height()
        source = QRect(
            round(self.selection.x() * x_scale),
            round(self.selection.y() * y_scale),
            round(self.selection.width() * x_scale),
            round(self.selection.height() * y_scale),
        )
        cropped = self.snapshot.copy(source)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        encoding_at = time.perf_counter()
        if not cropped.save(buffer, "PNG"):
            self.cancelled.emit()
            return
        self.captured.emit(bytes(buffer.data()), (time.perf_counter() - encoding_at) * 1000)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(event)


class TranslationWorker(QRunnable):
    def __init__(self, source_text, source_label, request_id, request_path=None, image_bytes=None):
        super().__init__()
        self.source_text = source_text
        self.source_label = source_label
        self.request_id = request_id
        self.request_path = request_path
        self.image_bytes = image_bytes
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        started_at = time.perf_counter()
        timings = {}
        source_text = self.source_text
        try:
            config = load_config()
            timings["config_ms"] = (time.perf_counter() - started_at) * 1000
            if self.image_bytes is not None:
                importing_at = time.perf_counter()
                from screen_ocr import extract_text_from_png

                timings["ocr_module_import_ms"] = (time.perf_counter() - importing_at) * 1000
                source_text = extract_text_from_png(self.image_bytes, timings)
                if not source_text.strip():
                    self.signals.empty.emit(
                        {
                            "request_id": self.request_id,
                            "request_path": self.request_path,
                            "timings": timings,
                            "elapsed_ms": (time.perf_counter() - started_at) * 1000,
                        }
                    )
                    return
                self.signals.ocr_ready.emit(self.request_id)
            translating_at = time.perf_counter()
            translation = translate_text(source_text, config)
            timings["translate_ms"] = (time.perf_counter() - translating_at) * 1000
            saving_at = time.perf_counter()
            saved_file = save_record(source_text, translation, config)
            timings["save_ms"] = (time.perf_counter() - saving_at) * 1000
            self.signals.finished.emit(
                {
                    "source_text": source_text,
                    "source_label": self.source_label,
                    "request_id": self.request_id,
                    "request_path": self.request_path,
                    "translation": translation,
                    "saved_file": saved_file,
                    "config": config,
                    "timings": timings,
                    "elapsed_ms": (time.perf_counter() - started_at) * 1000,
                }
            )
        except Exception as exc:
            public_error = str(exc) if isinstance(exc, (TranslationServiceError, CredentialError)) else "处理失败，请稍后重试或检查日志"
            self.signals.failed.emit(
                {
                    "source_label": self.source_label,
                    "request_id": self.request_id,
                    "request_path": self.request_path,
                    "timings": timings,
                    "elapsed_ms": (time.perf_counter() - started_at) * 1000,
                    "error": public_error,
                    "diagnostic": getattr(exc, "diagnostic", type(exc).__name__),
                    "source_text": source_text,
                }
            )


class AudioDownloadWorker(QRunnable):
    def __init__(self, tts_text, config):
        super().__init__()
        self.tts_text = tts_text
        self.config = config
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            audio_path = synthesize_speech_to_temp(self.tts_text, self.config)
            self.signals.finished.emit(
                {"tts_text": self.tts_text, "audio_path": audio_path}
            )
        except Exception as exc:
            self.signals.failed.emit(
                {
                    "tts_text": self.tts_text,
                    "error": str(exc) or exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                }
            )


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("翻译设置")
        self.setModal(True)
        self.setMinimumSize(520, 420)
        self.global_hotkey = QKeySequence(str(config.get("hotkey", "Alt+T")))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        title = QLabel("设置")
        title.setObjectName("DialogTitle")
        subtitle = QLabel("调整译文朗读规则和结果窗口快捷键")
        subtitle.setObjectName("DialogSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        audio_section = QFrame()
        audio_section.setObjectName("SettingsSection")
        audio_layout = QVBoxLayout(audio_section)
        audio_layout.setContentsMargins(18, 16, 18, 16)
        audio_layout.setSpacing(12)
        audio_title = QLabel("朗读")
        audio_title.setObjectName("SectionTitle")
        audio_layout.addWidget(audio_title)

        self.auto_read_checkbox = QCheckBox("翻译完成后自动朗读")
        self.auto_read_checkbox.setChecked(bool(config.get("auto_read_aloud", False)))
        audio_layout.addWidget(self.auto_read_checkbox)

        form_layout = QFormLayout()
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(10)
        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setRange(1, TTS_MAX_WORDS)
        self.max_chars_spin.setValue(
            min(int(config.get("auto_read_max_chars", TTS_MAX_WORDS)), TTS_MAX_WORDS)
        )
        self.max_chars_spin.setSuffix(" 个词")
        form_layout.addRow("自动朗读上限：", self.max_chars_spin)
        audio_layout.addLayout(form_layout)

        hint = QLabel(
            "语音合成最多 50 个词；中文按单字、英文按单词计数。"
            "自动朗读还会遵守上方的自定义上限。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("SettingHint")
        audio_layout.addWidget(hint)
        layout.addWidget(audio_section)

        shortcut_section = QFrame()
        shortcut_section.setObjectName("SettingsSection")
        shortcut_layout = QVBoxLayout(shortcut_section)
        shortcut_layout.setContentsMargins(18, 16, 18, 16)
        shortcut_layout.setSpacing(12)
        shortcut_title = QLabel("快捷键")
        shortcut_title.setObjectName("SectionTitle")
        shortcut_layout.addWidget(shortcut_title)

        shortcut_form = QFormLayout()
        shortcut_form.setHorizontalSpacing(12)
        self.manual_hotkey_edit = QKeySequenceEdit(
            QKeySequence(str(config.get("manual_input_hotkey", "Ctrl+I")))
        )
        self.manual_hotkey_edit.setMaximumSequenceLength(1)
        self.manual_hotkey_edit.setClearButtonEnabled(True)
        shortcut_form.addRow("手动输入：", self.manual_hotkey_edit)
        shortcut_layout.addLayout(shortcut_form)
        shortcut_hint = QLabel("快捷键在翻译结果窗口处于活动状态时生效，默认是 Ctrl + I。")
        shortcut_hint.setWordWrap(True)
        shortcut_hint.setObjectName("SettingHint")
        shortcut_layout.addWidget(shortcut_hint)
        layout.addWidget(shortcut_section)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(540, 450)

    def accept(self):
        shortcut = self.manual_hotkey_edit.keySequence().toString(
            QKeySequence.PortableText
        )
        if not shortcut:
            QMessageBox.information(self, "快捷键不能为空", "请为“手动输入”设置一个快捷键。")
            return
        if shortcut in {"Enter", "Return", "Esc", "Escape"}:
            QMessageBox.information(
                self, "快捷键不可用", "Enter 和 Esc 已用于隐藏结果窗口，请选择其他快捷键。"
            )
            return
        if shortcut == self.global_hotkey.toString(QKeySequence.PortableText):
            QMessageBox.information(
                self, "快捷键冲突", "手动输入快捷键不能与全局翻译快捷键相同。"
            )
            return
        self.manual_input_hotkey = shortcut
        super().accept()


class ResultWindow(QWidget):
    manual_translation_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.current_source = ""
        self.current_translation = ""
        self.current_saved_file = None
        self.current_tts_text = None
        self.current_audio_file = None
        self.current_audio_text = None
        self.active_audio_worker = None
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.9)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.errorOccurred.connect(self.audio_error)
        self.media_player.mediaStatusChanged.connect(self.audio_status_changed)
        self.setup_ui()
        self.manual_input_shortcut = QShortcut(QKeySequence(), self)
        self.manual_input_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.manual_input_shortcut.activated.connect(self.manual_translate)
        self.apply_config_to_ui()

    def setup_ui(self):
        app = QApplication.instance()
        self.setWindowTitle("翻译结果")
        self.setMinimumSize(640, 400)
        self.setWindowIcon(app.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.setStyleSheet(
            """
            QWidget {
                background: #edf2f8;
                color: #1d1d1f;
                font-family: 'Segoe UI', 'Microsoft YaHei UI';
                font-size: 12px;
            }
            QFrame#HeaderCard {
                background: rgba(255, 255, 255, 222);
                border: 1px solid rgba(255, 255, 255, 245);
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
                background: rgba(255, 255, 255, 238);
                color: #1d1d1f;
                font-size: 14px;
                border: 1px solid rgba(255, 255, 255, 250);
                border-radius: 16px;
                padding: 16px;
                selection-background-color: #b8d9ff;
                selection-color: #1d1d1f;
            }
            QPushButton {
                background: rgba(255, 255, 255, 190);
                color: #1d1d1f;
                border: 1px solid rgba(255, 255, 255, 235);
                border-radius: 10px;
                padding: 9px 15px;
                min-width: 82px;
                font-weight: 500;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 235); }
            QPushButton:pressed { background: #dce7f4; }
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
            QToolButton {
                background: rgba(255, 255, 255, 190);
                border: 1px solid rgba(255, 255, 255, 235);
                border-radius: 10px;
            }
            QToolButton:hover { background: rgba(255, 255, 255, 235); }
            QToolButton:pressed { background: #dce7f4; }
            QToolButton:disabled { background: #ededf0; }
            QToolButton#SettingsButton {
                background: transparent;
                color: #6e6e73;
                font-size: 19px;
            }
            QToolButton#SettingsButton:hover { background: #e8e8ed; }
            QDialog#SettingsDialog {
                background: #edf2f8;
            }
            QLabel#DialogTitle {
                font-size: 22px;
                font-weight: 600;
                color: #1d1d1f;
            }
            QLabel#DialogSubtitle, QLabel#SettingHint {
                color: #6e6e73;
                font-size: 11px;
            }
            QFrame#SettingsSection {
                background: rgba(255, 255, 255, 225);
                border: 1px solid rgba(255, 255, 255, 245);
                border-radius: 14px;
            }
            QFrame#SettingsSection QLabel,
            QFrame#SettingsSection QCheckBox {
                background: transparent;
            }
            QLabel#SectionTitle {
                color: #1d1d1f;
                font-size: 14px;
                font-weight: 600;
            }
            QSpinBox, QKeySequenceEdit {
                background: #f5f5f7;
                color: #1d1d1f;
                border: 1px solid #d9d9df;
                border-radius: 8px;
                padding: 7px 9px;
                min-width: 150px;
                selection-background-color: #b8d9ff;
            }
            QSpinBox:focus, QKeySequenceEdit:focus {
                border: 1px solid #3b8eea;
            }
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
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("SubtitleLabel")

        header_text_layout.addWidget(title_label)
        header_text_layout.addWidget(self.subtitle_label)
        header_layout.addWidget(header_icon)
        header_layout.addLayout(header_text_layout)
        header_layout.addStretch(1)
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("SettingsButton")
        self.settings_btn.setText("⚙")
        self.settings_btn.setFixedSize(38, 38)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.setAccessibleName("设置")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.clicked.connect(self.open_settings)
        header_layout.addWidget(self.settings_btn)
        layout.addWidget(header_card)

        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(260)
        editor_font = QFont(app.font())
        editor_font.setPointSize(14)
        self.editor.setFont(editor_font)
        self.editor.setAccessibleName("翻译结果正文")
        editor_shadow = QGraphicsDropShadowEffect(self.editor)
        editor_shadow.setBlurRadius(24)
        editor_shadow.setOffset(0, 5)
        editor_shadow.setColor(QColor(40, 65, 105, 24))
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
        self.manual_btn = QPushButton("手动输入")
        self.read_btn = QToolButton()
        self.read_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaVolume))
        self.read_btn.setFixedSize(38, 38)
        self.read_btn.setToolTip("朗读译文")
        self.read_btn.setAccessibleName("朗读译文")
        close_btn = QPushButton("关闭")
        self.copy_btn.setObjectName("PrimaryButton")
        close_btn.setObjectName("CloseButton")
        close_btn.setDefault(True)
        close_btn.setAutoDefault(True)
        self.open_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.read_btn.setEnabled(False)

        for button in (
            self.open_btn,
            self.copy_btn,
            self.manual_btn,
            self.read_btn,
            close_btn,
        ):
            button.setCursor(Qt.PointingHandCursor)

        self.open_btn.clicked.connect(self.open_saved_file)
        self.copy_btn.clicked.connect(self.copy_translation)
        self.manual_btn.clicked.connect(self.manual_translate)
        self.read_btn.clicked.connect(self.read_translation)
        close_btn.clicked.connect(self.close)

        button_row.addWidget(self.open_btn)
        button_row.addWidget(self.manual_btn)
        button_row.addStretch(1)
        button_row.addWidget(self.read_btn)
        button_row.addWidget(close_btn)
        button_row.addWidget(self.copy_btn)
        layout.addLayout(button_row)
        self.resize(840, 500)

    def set_status(self, text, status="idle"):
        self.status_label.setText(text)
        self.status_label.setProperty("status", status)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def apply_config_to_ui(self):
        global_hotkey = QKeySequence(str(self.config.get("hotkey", "Alt+T"))).toString(
            QKeySequence.NativeText
        )
        manual_hotkey = QKeySequence(
            str(self.config.get("manual_input_hotkey", "Ctrl+I"))
        )
        if manual_hotkey.isEmpty():
            manual_hotkey = QKeySequence("Ctrl+I")
        self.manual_input_shortcut.setKey(manual_hotkey)
        manual_hotkey_text = manual_hotkey.toString(QKeySequence.NativeText)
        self.manual_btn.setText(f"手动输入  {manual_hotkey_text}")
        self.subtitle_label.setText(
            f"{global_hotkey} 翻译 · {manual_hotkey_text} 手动输入 · Enter / Esc 隐藏"
        )

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
            QMessageBox.warning(self, "TranEasy", f"保存文件不存在：\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def copy_translation(self):
        if self.current_translation:
            QGuiApplication.clipboard().setText(self.current_translation.strip())
            self.set_status("✓ 已复制", "success")

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.config = save_read_aloud_settings(
                dialog.auto_read_checkbox.isChecked(),
                dialog.max_chars_spin.value(),
                dialog.manual_input_hotkey,
            )
        except Exception as exc:
            QMessageBox.critical(self, "设置保存失败", str(exc))
            return
        self.apply_config_to_ui()
        self.set_status("✓ 设置已保存", "success")

    def read_translation(self):
        if not self.current_tts_text:
            QMessageBox.information(
                self,
                "无法朗读",
                "当前没有可朗读的译文。",
            )
            return

        if (
            self.current_audio_text == self.current_tts_text
            and self.current_audio_file
            and Path(self.current_audio_file).exists()
        ):
            self.play_audio_file()
            return

        if self.active_audio_worker is not None:
            self.set_status("● 正在准备语音", "loading")
            return

        word_count = count_tts_words(self.current_tts_text)
        if word_count > TTS_MAX_WORDS:
            QMessageBox.information(
                self,
                "超过语音合成上限",
                f"当前译文共 {word_count} 个词，语音合成最多支持 "
                f"{TTS_MAX_WORDS} 个词。\n中文按单字、英文按单词计数。",
            )
            return

        worker = AudioDownloadWorker(self.current_tts_text, self.config)
        worker.signals.finished.connect(self.audio_download_finished)
        worker.signals.failed.connect(self.audio_download_failed)
        self.active_audio_worker = worker
        self.read_btn.setEnabled(False)
        self.set_status("● 正在准备语音", "loading")
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def audio_download_finished(self, result):
        self.active_audio_worker = None
        audio_path = Path(result["audio_path"])
        if result["tts_text"] != self.current_tts_text:
            audio_path.unlink(missing_ok=True)
            return
        self.cleanup_audio_file()
        self.current_audio_file = audio_path
        self.current_audio_text = result["tts_text"]
        self.read_btn.setEnabled(True)
        self.play_audio_file()

    @Slot(object)
    def audio_download_failed(self, result):
        self.active_audio_worker = None
        self.read_btn.setEnabled(bool(self.current_tts_text))
        append_resident_log(
            f"audio download failed\n{result.get('traceback', result.get('error', ''))}"
        )
        if result["tts_text"] != self.current_tts_text:
            return
        self.set_status("朗读失败", "idle")
        QMessageBox.warning(self, "朗读失败", result["error"])

    def play_audio_file(self):
        if not self.current_audio_file or not Path(self.current_audio_file).exists():
            self.set_status("朗读文件不可用", "idle")
            return
        self.media_player.stop()
        self.media_player.setSource(QUrl.fromLocalFile(str(self.current_audio_file)))
        self.media_player.play()
        self.set_status("▶ 正在朗读", "loading")

    @Slot(QMediaPlayer.Error, str)
    def audio_error(self, error, error_string):
        if error == QMediaPlayer.NoError:
            return
        detail = error_string.strip() or "请检查网络连接和有道语音合成服务配置"
        append_resident_log(f"audio playback failed | {detail}")
        self.set_status("朗读失败", "idle")
        QMessageBox.warning(self, "朗读失败", detail)

    @Slot(QMediaPlayer.MediaStatus)
    def audio_status_changed(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.set_status("✓ 朗读完成", "success")

    def cleanup_audio_file(self):
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        if self.current_audio_file:
            try:
                Path(self.current_audio_file).unlink(missing_ok=True)
            except OSError:
                append_resident_log(
                    f"audio temp cleanup failed: {self.current_audio_file}"
                )
        self.current_audio_file = None
        self.current_audio_text = None

    def manual_translate(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "手动输入翻译", "请输入要翻译的英文/文本：", ""
        )
        if not ok:
            return
        source = text.strip()
        if not source:
            QMessageBox.information(self, "TranEasy", "输入内容为空。")
            return
        self.manual_translation_requested.emit(source)

    def show_loading(self):
        self.current_tts_text = None
        self.cleanup_audio_file()
        self.editor.setPlainText("正在翻译，请稍候……")
        self.format_editor_text()
        self.path_label.setText("")
        self.open_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.read_btn.setEnabled(False)
        self.set_status("● 正在翻译", "loading")
        self.bring_to_front()

    def update_result(self, source_text, translation, saved_file):
        self.current_source = source_text
        self.current_translation = translation
        self.current_saved_file = Path(saved_file)
        tts_text = translation.strip()
        if tts_text != self.current_tts_text:
            self.cleanup_audio_file()
        self.current_tts_text = tts_text
        self.editor.setPlainText(tts_text)
        self.format_editor_text()
        self.path_label.setText(f"保存至  {self.current_saved_file.name}")
        self.path_label.setToolTip(str(self.current_saved_file))
        self.open_btn.setEnabled(True)
        self.copy_btn.setEnabled(bool(tts_text))
        self.read_btn.setEnabled(bool(tts_text))
        word_count = count_tts_words(tts_text)
        if word_count > TTS_MAX_WORDS:
            self.read_btn.setToolTip(
                f"译文共 {word_count} 个词，超过语音合成上限 {TTS_MAX_WORDS} 个词"
            )
        else:
            self.read_btn.setToolTip(f"朗读译文（{word_count}/{TTS_MAX_WORDS} 词）")
        self.set_status("✓ 已保存", "success")
        self.bring_to_front()
        auto_read_limit = min(
            int(self.config.get("auto_read_max_chars", TTS_MAX_WORDS)),
            TTS_MAX_WORDS,
        )
        if (
            tts_text
            and bool(self.config.get("auto_read_aloud", False))
            and word_count <= auto_read_limit
        ):
            QTimer.singleShot(0, self.read_translation)

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


def _mix_color(start, end, progress):
    return QColor(
        *(round(a + (b - a) * progress) for a, b in zip(
            (start.red(), start.green(), start.blue(), start.alpha()),
            (end.red(), end.green(), end.blue(), end.alpha()),
        ))
    )


def _mix_rect(start, end, progress):
    return QRect(*(
        round(a + (b - a) * progress)
        for a, b in zip(
            (start.x(), start.y(), start.width(), start.height()),
            (end.x(), end.y(), end.width(), end.height()),
        )
    ))


class RoundedSurface(QWidget):
    def __init__(self, parent, color, radius=18):
        super().__init__(parent)
        self.surface_color = QColor(color)
        self.radius = radius
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_surface_color(self, color):
        self.surface_color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
        painter.setBrush(self.surface_color)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), self.radius, self.radius)


class RailActionButton(QPushButton):
    def __init__(self, label, symbol, parent=None):
        super().__init__(label, parent)
        self.symbol = symbol
        self.setObjectName("RailAction")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#ffffff"), 1.6))
        painter.setBrush(QColor(255, 255, 255, 43))
        painter.drawEllipse(8, 7, 28, 28)
        painter.setBrush(Qt.NoBrush)
        if self.symbol == "copy":
            painter.drawRect(18, 14, 10, 13)
            painter.drawLine(15, 17, 15, 29)
            painter.drawLine(15, 29, 25, 29)
        elif self.symbol == "read":
            painter.drawLine(15, 18, 19, 18)
            painter.drawLine(19, 18, 23, 14)
            painter.drawLine(23, 14, 23, 28)
            painter.drawLine(23, 28, 19, 24)
            painter.drawLine(19, 24, 15, 24)
            painter.drawArc(20, 14, 13, 14, -70 * 16, 140 * 16)
        elif self.symbol == "manual":
            painter.drawLine(16, 27, 27, 16)
            painter.drawLine(18, 29, 29, 18)
            painter.drawLine(16, 27, 18, 29)
        elif self.symbol == "history":
            for y in (16, 21, 26):
                painter.drawLine(16, y, 28, y)
        elif self.symbol == "settings":
            painter.drawEllipse(18, 17, 8, 8)
            for x1, y1, x2, y2 in ((22, 13, 22, 16), (22, 26, 22, 29), (14, 21, 17, 21), (27, 21, 30, 21)):
                painter.drawLine(x1, y1, x2, y2)


class MenuToggleButton(QToolButton):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        for y in (15, 21, 27):
            painter.drawLine(12, y, 24, y)


class HistoryRecordButton(QPushButton):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#e9e9e7"))
        painter.drawRoundedRect(8, 8, 35, 42, 7, 7)
        painter.setPen(QPen(QColor("#a0a0a0"), 2))
        painter.drawLine(15, 19, 35, 19)
        painter.drawLine(15, 25, 35, 25)
        painter.setPen(QPen(QColor("#e65f3e"), 2))
        painter.drawLine(15, 31, 29, 31)


class ModernResultWindow(ResultWindow):
    """Compact result card with one reusable animated rail and no idle animation."""

    RAIL_RECT = QRect(10, 10, 184, 360)
    CARD_RECT = QRect(206, 10, 378, 360)
    RAIL_DARK = QColor("#494949")
    RAIL_LIGHT = QColor("#f4f4f2")
    ORANGE = QColor("#e65f3e")

    @staticmethod
    def _card_size_limits():
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 760, 640
        bounds = screen.availableGeometry()
        return min(760, max(320, bounds.width() - 226)), min(640, max(320, bounds.height() - 40))

    def setup_ui(self):
        max_width, max_height = self._card_size_limits()
        card_width = min(int(self.config.get("result_card_width", 378)), max_width)
        card_height = min(int(self.config.get("result_card_height", 360)), max_height)
        self.CARD_RECT = QRect(206, 10, card_width, card_height)
        self.RAIL_RECT = QRect(10, 10, 184, card_height)
        self.setWindowTitle("翻译结果")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(card_width + 216, card_height + 20)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry()
            self.move(
                bounds.center().x() - self.CARD_RECT.center().x(),
                bounds.center().y() - self.CARD_RECT.center().y(),
            )
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.setStyleSheet("""
            QFrame#TranslationCard { background:#ffffff; border:1px solid #e8e8e6; border-radius:20px; }
            QToolButton#RailToggle { background:#e65f3e; color:white; border:0; border-radius:12px; font-size:17px; }
            QToolButton#RailToggle:pressed, QPushButton#RailAction:pressed { background:#cf5233; }
            QPushButton#RailAction, QToolButton#RailAction, QPushButton#SettingsButton {
                background:#e65f3e; color:white; border:0; border-radius:12px;
                padding:7px 9px 7px 42px; text-align:left; font-size:11px; font-weight:600;
            }
            QPushButton#RailAction:hover, QToolButton#RailAction:hover,
            QPushButton#SettingsButton:hover { background:#d95736; }
            QPushButton#RailAction:disabled, QToolButton#RailAction:disabled { background:#a8a8a5; }
            QLabel#ResultStatus { color:#555; font-size:10px; background:transparent; }
            QLabel#ResultHint { color:#888; font-size:9px; background:transparent; }
            QTextEdit#TranslationEditor { background:transparent; border:0; color:#202020; }
            QToolButton#CardClose { background:transparent; border:0; color:#777; font-size:17px; }
            QToolButton#CardClose:hover { color:#e65f3e; }
            QWidget#RailPage, QStackedWidget#RailStack { background:transparent; }
            QLabel#RailTitle { color:#292929; font-size:12px; font-weight:700; background:transparent; }
            QToolButton#RailBack { background:#e6e6e3; border:0; border-radius:8px; color:#555; font-size:16px; }
            QTextEdit#RailInput, QLineEdit#RailInput, QKeySequenceEdit#RailInput, QSpinBox#RailInput {
                background:white; border:1px solid #dededa; border-radius:9px; color:#292929;
                padding:5px; font-size:11px; selection-background-color:#efad99;
            }
            QTextEdit#RailInput:focus, QLineEdit#RailInput:focus,
            QKeySequenceEdit#RailInput:focus { border:1px solid #e65f3e; }
            QPushButton#RailPrimary { background:#e65f3e; color:white; border:0; border-radius:10px; padding:8px; font-weight:700; }
            QPushButton#HistoryCard { background:white; color:#292929; border:1px solid #e4e4e1;
                border-radius:11px; padding:8px 6px 8px 50px; text-align:left; font-size:10px; }
            QPushButton#HistoryCard:hover { border-color:#e65f3e; }
            QScrollArea#RailScroll { background:transparent; border:0; }
            QScrollArea#RailScroll QWidget { background:transparent; }
            QLabel#SettingLabel { color:#444; font-size:10px; background:transparent; }
            QCheckBox#RailCheck { color:#333; font-size:10px; background:transparent; }
            QCheckBox#RailCheck::indicator:checked { background:#e65f3e; border:1px solid #e65f3e; }
        """)

        self.card = QFrame(self)
        self.card.setObjectName("TranslationCard")
        self.card.setGeometry(self.CARD_RECT)
        self.card.installEventFilter(self)
        self._drag_offset = None
        self.toggle_btn = MenuToggleButton(self.card)
        self.toggle_btn.setObjectName("RailToggle")
        self.toggle_btn.setGeometry(12, 12, 36, 42)
        self.toggle_btn.setAccessibleName("展开或收起工具栏")
        self.toggle_btn.clicked.connect(self.toggle_rail)
        self.status_label = QLabel("等待翻译", self.card)
        self.status_label.setObjectName("ResultStatus")
        self.status_label.setProperty("status", "idle")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setGeometry(card_width - 158, 17, 105, 30)
        self.close_btn = QToolButton(self.card)
        self.close_btn.setObjectName("CardClose")
        self.close_btn.setText("×")
        self.close_btn.setGeometry(card_width - 41, 16, 28, 28)
        self.close_btn.setAccessibleName("隐藏翻译结果")
        self.close_btn.clicked.connect(self.hide)
        self.editor = QTextEdit(self.card)
        self.editor.setObjectName("TranslationEditor")
        self.editor.setReadOnly(True)
        self.editor.setGeometry(28, 84, card_width - 56, card_height - 132)
        editor_font = QFont(QApplication.instance().font())
        editor_font.setPointSize(int(self.config.get("result_font_size", 14)))
        self.editor.setFont(editor_font)
        self.editor.setAccessibleName("翻译结果正文")
        self.subtitle_label = QLabel(self.card)
        self.subtitle_label.setObjectName("ResultHint")
        self.subtitle_label.setGeometry(28, card_height - 44, card_width - 48, 25)
        self.path_label = QLabel(self)
        self.path_label.hide()

        self.rail = RoundedSurface(self, self.RAIL_DARK)
        self.rail.setGeometry(self.RAIL_RECT)
        self.rail.hide()
        rail_layout = QVBoxLayout(self.rail)
        rail_layout.setContentsMargins(11, 12, 11, 12)
        self.rail_stack = QStackedWidget(self.rail)
        self.rail_stack.setObjectName("RailStack")
        rail_layout.addWidget(self.rail_stack)
        self._pages = {}
        self._build_home_page()
        self._build_manual_page()
        self._build_history_page()
        self._build_source_page()
        self._build_settings_page()
        self.rail_stack.setCurrentWidget(self._pages["home"])
        self.ghost = RoundedSurface(self, self.ORANGE, 12)
        self.ghost.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.ghost.hide()
        self._rail_open = False
        self._stage = "home"
        self._stage_stack = []
        self._animation = None
        self._animation_generation = 0
        self._motion_enabled = True
        if sys.platform == "win32":
            try:
                enabled = ctypes.c_int()
                if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
                    self._motion_enabled = bool(enabled.value)
            except (AttributeError, OSError):
                pass
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.escape_shortcut.activated.connect(self._escape)
        self._update_mask()

    def apply_config_to_ui(self):
        super().apply_config_to_ui()
        font_size = int(self.config.get("result_font_size", 14))
        if self.editor.font().pointSize() != font_size:
            font = QFont(self.editor.font())
            font.setPointSize(font_size)
            self.editor.setFont(font)
            self.format_editor_text()
        max_width, max_height = self._card_size_limits()
        width = min(int(self.config.get("result_card_width", 378)), max_width)
        height = min(int(self.config.get("result_card_height", 360)), max_height)
        self._set_card_size(width, height)

    def _set_card_size(self, width, height):
        if self.CARD_RECT.width() == width and self.CARD_RECT.height() == height:
            return
        # Keep the visible translation card anchored while its size changes.
        card_center = self.mapToGlobal(self.CARD_RECT.center())
        self._animation_generation += 1
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
            self._animation = None
        self.ghost.hide()
        self.CARD_RECT = QRect(206, 10, width, height)
        self.RAIL_RECT = QRect(10, 10, 184, height)
        self.setFixedSize(width + 216, height + 20)
        self.move(card_center - self.CARD_RECT.center())
        self.card.setGeometry(self.CARD_RECT)
        self.rail.setGeometry(self.RAIL_RECT)
        self.status_label.setGeometry(width - 158, 17, 105, 30)
        self.close_btn.setGeometry(width - 41, 16, 28, 28)
        self.editor.setGeometry(28, 84, width - 56, height - 132)
        self.subtitle_label.setGeometry(28, height - 44, width - 48, 25)
        self._update_mask()

    def eventFilter(self, watched, event):
        if watched is self.card:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            elif event.type() == QEvent.MouseMove and self._drag_offset is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            elif event.type() == QEvent.MouseButtonRelease:
                self._drag_offset = None
        return super().eventFilter(watched, event)

    def _escape(self):
        if self._rail_open and self._stage != "home":
            self.back_stage()
        else:
            self.hide()

    def _page(self, name, title=None):
        page = QWidget()
        page.setObjectName("RailPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        if title:
            head = QHBoxLayout()
            back = QToolButton()
            back.setObjectName("RailBack")
            back.setText("‹")
            back.setFixedSize(27, 27)
            back.clicked.connect(self.back_stage)
            label = QLabel(title)
            label.setObjectName("RailTitle")
            head.addWidget(back)
            head.addWidget(label)
            head.addStretch()
            layout.addLayout(head)
        self.rail_stack.addWidget(page)
        self._pages[name] = page
        return page, layout

    def _action(self, text, symbol, callback):
        button = RailActionButton(text, symbol)
        button.setFixedHeight(42)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _build_home_page(self):
        _, layout = self._page("home")
        self.copy_btn = self._action("复制译文", "copy", self.copy_translation)
        self.read_btn = self._action("朗读译文", "read", self.read_translation)
        self.manual_btn = self._action("手动输入", "manual", self.manual_translate)
        self.open_btn = self._action("打开记录", "history", self.open_saved_file)
        self.settings_btn = self._action("设置", "settings", self.open_settings)
        self.settings_btn.setObjectName("SettingsButton")
        for button in (self.copy_btn, self.read_btn, self.manual_btn, self.open_btn, self.settings_btn):
            layout.addWidget(button)
        layout.addStretch()
        self.copy_btn.setEnabled(False)
        self.read_btn.setEnabled(False)

    def _build_manual_page(self):
        _, layout = self._page("manual", "手动输入")
        self.manual_input = QTextEdit()
        self.manual_input.setObjectName("RailInput")
        self.manual_input.setPlaceholderText("输入要翻译的文字…")
        layout.addWidget(self.manual_input, 1)
        submit = QPushButton("翻译这段文字")
        submit.setObjectName("RailPrimary")
        submit.clicked.connect(self._submit_manual)
        layout.addWidget(submit)

    def _build_history_page(self):
        _, layout = self._page("history", "翻译记录")
        self.history_scroll = QScrollArea()
        self.history_scroll.setObjectName("RailScroll")
        self.history_scroll.setWidgetResizable(True)
        self.history_content = QWidget()
        self.history_layout = QVBoxLayout(self.history_content)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(7)
        self.history_scroll.setWidget(self.history_content)
        layout.addWidget(self.history_scroll, 1)

    def _build_source_page(self):
        _, layout = self._page("source", "翻译原文")
        self.source_view = QTextEdit()
        self.source_view.setObjectName("RailInput")
        self.source_view.setReadOnly(True)
        layout.addWidget(self.source_view, 1)

    def _build_settings_page(self):
        _, layout = self._page("settings", "设置")
        scroll = QScrollArea()
        scroll.setObjectName("RailScroll")
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 3, 0)
        form.setSpacing(7)
        self.auto_read_checkbox = QCheckBox("自动朗读")
        self.auto_read_checkbox.setObjectName("RailCheck")
        self.auto_read_checkbox.setChecked(bool(self.config.get("auto_read_aloud", False)))
        form.addWidget(self.auto_read_checkbox)
        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setObjectName("RailInput")
        self.max_chars_spin.setRange(1, TTS_MAX_WORDS)
        self.max_chars_spin.setValue(int(self.config.get("auto_read_max_chars", TTS_MAX_WORDS)))
        self.max_chars_spin.setSuffix(" 个词")
        self._setting_field(form, "朗读上限", self.max_chars_spin)
        self.manual_hotkey_edit = QKeySequenceEdit(QKeySequence(str(self.config.get("manual_input_hotkey", "Ctrl+I"))))
        self.manual_hotkey_edit.setObjectName("RailInput")
        self.manual_hotkey_edit.setMaximumSequenceLength(1)
        self._setting_field(form, "手动输入快捷键", self.manual_hotkey_edit)
        self.screenshot_checkbox = QCheckBox("启用截图翻译")
        self.screenshot_checkbox.setObjectName("RailCheck")
        self.screenshot_checkbox.setChecked(bool(self.config.get("screenshot_enabled", True)))
        form.addWidget(self.screenshot_checkbox)
        self.screenshot_hotkey_edit = QKeySequenceEdit(QKeySequence(str(self.config.get("screenshot_hotkey", "Ctrl+Alt+T"))))
        self.screenshot_hotkey_edit.setObjectName("RailInput")
        self.screenshot_hotkey_edit.setMaximumSequenceLength(1)
        self.screenshot_hotkey_edit.setEnabled(self.screenshot_checkbox.isChecked())
        self.screenshot_checkbox.toggled.connect(self.screenshot_hotkey_edit.setEnabled)
        self._setting_field(form, "截图快捷键", self.screenshot_hotkey_edit)
        max_width, max_height = self._card_size_limits()
        self.result_font_edit = QLineEdit(str(self.config.get("result_font_size", 14)))
        self.result_font_edit.setObjectName("RailInput")
        self.result_font_edit.setValidator(QIntValidator(10, 24, self.result_font_edit))
        self._setting_field(form, "译文字号（磅）", self.result_font_edit)
        self.card_width_edit = QLineEdit(str(self.CARD_RECT.width()))
        self.card_width_edit.setObjectName("RailInput")
        self.card_width_edit.setValidator(QIntValidator(320, max_width, self.card_width_edit))
        self._setting_field(form, "白框宽度（px）", self.card_width_edit)
        self.card_height_edit = QLineEdit(str(self.CARD_RECT.height()))
        self.card_height_edit.setObjectName("RailInput")
        self.card_height_edit.setValidator(QIntValidator(320, max_height, self.card_height_edit))
        self._setting_field(form, "白框高度（工具栏同步，px）", self.card_height_edit)
        load_environment()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setObjectName("RailInput")
        self.api_key_edit.setPlaceholderText("已配置，留空不变" if self.config.get("youdao_app_key") else "输入应用 ID")
        self._setting_field(form, "文本翻译 App ID", self.api_key_edit)
        self.api_secret_edit = QLineEdit()
        self.api_secret_edit.setObjectName("RailInput")
        self.api_secret_edit.setEchoMode(QLineEdit.Password)
        self.api_secret_edit.setPlaceholderText("已配置，留空不变" if has_secret() else "输入应用密钥")
        self._setting_field(form, "文本翻译 App Secret", self.api_secret_edit)
        self.clear_secret_checkbox = QCheckBox("清除文本翻译 Secret")
        self.clear_secret_checkbox.setObjectName("RailCheck")
        form.addWidget(self.clear_secret_checkbox)
        self.tts_api_key_edit = QLineEdit()
        self.tts_api_key_edit.setObjectName("RailInput")
        self.tts_api_key_edit.setPlaceholderText("已配置，留空不变" if self.config.get("youdao_tts_app_key") else "输入语音合成 App ID")
        self._setting_field(form, "语音合成 App ID", self.tts_api_key_edit)
        self.tts_api_secret_edit = QLineEdit()
        self.tts_api_secret_edit.setObjectName("RailInput")
        self.tts_api_secret_edit.setEchoMode(QLineEdit.Password)
        self.tts_api_secret_edit.setPlaceholderText("已配置，留空不变" if has_secret("tts") else "输入语音合成 App Secret")
        self._setting_field(form, "语音合成 App Secret", self.tts_api_secret_edit)
        self.clear_tts_secret_checkbox = QCheckBox("清除语音合成 Secret")
        self.clear_tts_secret_checkbox.setObjectName("RailCheck")
        form.addWidget(self.clear_tts_secret_checkbox)
        form.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("RailPrimary")
        save_btn.clicked.connect(self.save_settings)
        layout.addWidget(save_btn)

    @staticmethod
    def _setting_field(layout, label_text, field):
        label = QLabel(label_text)
        label.setObjectName("SettingLabel")
        layout.addWidget(label)
        layout.addWidget(field)

    def set_status(self, text, status="idle"):
        super().set_status(text.lstrip("✓● "), status)

    def _update_mask(self):
        region = QRegion(self.CARD_RECT)
        if self.rail.isVisible():
            region |= QRegion(self.rail.geometry())
        self.setMask(region)

    def _button_rect(self, button):
        return QRect(button.mapTo(self, button.rect().topLeft()), button.size())

    def _start_animation(self, duration, on_frame, on_finish):
        self._animation_generation += 1
        generation = self._animation_generation
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
        if not self._motion_enabled:
            self._animation = None
            on_frame(1.0)
            on_finish()
            return
        animation = QVariantAnimation(self)
        animation.setDuration(duration)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.valueChanged.connect(lambda value: on_frame(float(value)) if generation == self._animation_generation else None)
        animation.finished.connect(lambda: on_finish() if generation == self._animation_generation else None)
        self._animation = animation
        animation.start()

    def toggle_rail(self):
        opening = not self._rail_open
        self._rail_open = opening
        if opening and not self.rail.isVisible():
            self.rail.setGeometry(self._button_rect(self.toggle_btn))
            self.rail_stack.hide()
            self.rail.show()
        if self._stage != "home":
            self.rail_stack.setCurrentWidget(self._pages["home"])
            self._stage = "home"
            self._stage_stack.clear()
            self.ghost.hide()
        start = self.rail.geometry()
        end = self.RAIL_RECT if opening else self._button_rect(self.toggle_btn)
        start_color = QColor(self.rail.surface_color)
        def frame(progress):
            self.rail.setGeometry(_mix_rect(start, end, progress))
            self.rail.set_surface_color(_mix_color(start_color, self.RAIL_DARK, progress))
            self.rail_stack.setVisible(self.rail.width() > 105)
            self._update_mask()
        def finish():
            self.rail.setGeometry(end)
            if not opening:
                self.rail.hide()
            else:
                self.rail_stack.show()
            self._update_mask()
        self._start_animation(260, frame, finish)

    def _show_stage(self, name, origin, back="home"):
        if not self._rail_open:
            self.toggle_rail()
            QTimer.singleShot(270, lambda: self._show_stage(name, origin, back) if self._rail_open else None)
            return
        self._stage_stack.append((back, origin))
        self._stage = name
        start = self.ghost.geometry() if self.ghost.isVisible() else self._button_rect(origin)
        end = self.RAIL_RECT
        start_color = QColor(self.rail.surface_color)
        button_color = QColor("#ffffff") if back != "home" else self.ORANGE
        page = self._pages[name]
        switched = False
        self.ghost.setGeometry(start)
        self.ghost.set_surface_color(button_color)
        self.ghost.show()
        self.ghost.raise_()
        self.toggle_btn.raise_()
        def frame(progress):
            nonlocal switched
            self.rail.set_surface_color(_mix_color(start_color, self.RAIL_LIGHT, progress))
            self.ghost.setGeometry(_mix_rect(start, end, progress))
            color = _mix_color(button_color, self.RAIL_LIGHT, progress)
            color.setAlpha(round(255 * (1 - max(0, (progress - .68) / .32))))
            self.ghost.set_surface_color(color)
            if progress > .2 and not switched:
                self.rail_stack.setCurrentWidget(page)
                switched = True
        def finish():
            self.rail_stack.setCurrentWidget(page)
            self.rail.set_surface_color(self.RAIL_LIGHT)
            self.ghost.hide()
        self._start_animation(470, frame, finish)

    def back_stage(self):
        if self._stage == "home":
            return
        target_name, origin = self._stage_stack.pop() if self._stage_stack else ("home", self.manual_btn)
        self._stage = target_name
        start = self.ghost.geometry() if self.ghost.isVisible() else self.RAIL_RECT
        end = self._button_rect(origin)
        start_color = QColor(self.rail.surface_color)
        end_color = self.RAIL_DARK if target_name == "home" else self.RAIL_LIGHT
        target_page = self._pages[target_name]
        self.ghost.setGeometry(start)
        self.ghost.set_surface_color(start_color)
        self.ghost.show()
        self.ghost.raise_()
        switched = False
        def frame(progress):
            nonlocal switched
            self.rail.set_surface_color(_mix_color(start_color, end_color, progress))
            self.ghost.setGeometry(_mix_rect(start, end, progress))
            color = _mix_color(start_color, self.ORANGE if target_name == "home" else QColor("#ffffff"), progress)
            color.setAlpha(round(255 * (1 - max(0, (progress - .68) / .32))))
            self.ghost.set_surface_color(color)
            if progress > .2 and not switched:
                self.rail_stack.setCurrentWidget(target_page)
                switched = True
        def finish():
            self.rail_stack.setCurrentWidget(target_page)
            self.rail.set_surface_color(end_color)
            self.ghost.hide()
        self._start_animation(420, frame, finish)

    def manual_translate(self):
        self._show_stage("manual", self.manual_btn)
        QTimer.singleShot(460, self.manual_input.setFocus)

    def _submit_manual(self):
        source = self.manual_input.toPlainText().strip()
        if not source:
            self.manual_input.setFocus()
            return
        self.source_view.setPlainText(source)
        self._show_stage("source", self.manual_input, "manual")
        self.manual_translation_requested.emit(source)

    def open_saved_file(self):
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        records = load_recent_records(self.config)
        if records:
            for record in records:
                source = " ".join(record["source"].split())
                button = HistoryRecordButton(f"{source[:24]}\n{record['time']}")
                button.setObjectName("HistoryCard")
                button.setFixedHeight(58)
                button.clicked.connect(lambda checked=False, item=record, card=button: self._select_history(item, card))
                self.history_layout.addWidget(button)
        else:
            empty = QLabel("还没有翻译记录")
            empty.setObjectName("SettingLabel")
            self.history_layout.addWidget(empty)
        self.history_layout.addStretch()
        self._show_stage("history", self.open_btn)

    def _select_history(self, record, card):
        self.current_source = record["source"]
        self.current_translation = record["translation"]
        self.current_saved_file = Path(record["file"])
        self.current_tts_text = record["translation"]
        self.source_view.setPlainText(record["source"])
        self.editor.setPlainText(record["translation"])
        self.format_editor_text()
        self.copy_btn.setEnabled(True)
        self.read_btn.setEnabled(True)
        self.set_status("● 已翻译", "success")
        self._show_stage("source", card, "history")

    def open_settings(self):
        self._show_stage("settings", self.settings_btn)

    def save_settings(self):
        manual = self.manual_hotkey_edit.keySequence().toString(QKeySequence.PortableText)
        screenshot = self.screenshot_hotkey_edit.keySequence().toString(QKeySequence.PortableText)
        global_key = QKeySequence(str(self.config.get("hotkey", "Alt+T"))).toString(QKeySequence.PortableText)
        if not manual or not screenshot:
            QMessageBox.information(self, "快捷键不能为空", "请设置手动输入和截图快捷键。")
            return
        if manual in {"Enter", "Return", "Esc", "Escape"} or len({manual, screenshot, global_key}) != 3:
            QMessageBox.information(self, "快捷键冲突", "快捷键不能与翻译热键或彼此重复，也不能占用 Enter / Esc。")
            return
        if self.clear_secret_checkbox.isChecked() and self.api_secret_edit.text().strip():
            QMessageBox.information(self, "密钥设置冲突", "请清空密钥输入框，或取消勾选清除密钥。")
            return
        size_edits = (self.result_font_edit, self.card_width_edit, self.card_height_edit)
        if any(not edit.hasAcceptableInput() for edit in size_edits):
            QMessageBox.information(self, "尺寸设置无效", "请填写范围内的译文字号、白框宽度和高度。")
            return
        try:
            self.config = save_result_settings(
                self.auto_read_checkbox.isChecked(), self.max_chars_spin.value(), manual,
                self.screenshot_checkbox.isChecked(), screenshot,
                self.api_key_edit.text(), self.api_secret_edit.text(),
                self.clear_secret_checkbox.isChecked(),
                int(self.result_font_edit.text()), int(self.card_width_edit.text()), int(self.card_height_edit.text()),
                tts_app_key=self.tts_api_key_edit.text(),
                tts_app_secret=self.tts_api_secret_edit.text(),
                clear_tts_secret=self.clear_tts_secret_checkbox.isChecked(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "设置保存失败", str(exc))
            return
        self.api_key_edit.clear()
        self.api_secret_edit.clear()
        self.tts_api_key_edit.clear()
        self.tts_api_secret_edit.clear()
        self.clear_tts_secret_checkbox.setChecked(False)
        self.tts_api_key_edit.setPlaceholderText("已配置，留空不变" if self.config.get("youdao_tts_app_key") else "输入语音合成 App ID")
        self.tts_api_secret_edit.setPlaceholderText("已配置，留空不变" if has_secret("tts") else "输入语音合成 App Secret")
        self.clear_secret_checkbox.setChecked(False)
        self.api_key_edit.setPlaceholderText("已配置，留空不变" if self.config.get("youdao_app_key") else "输入应用 ID")
        self.api_secret_edit.setPlaceholderText("已配置，留空不变" if has_secret() else "输入应用密钥")
        self.apply_config_to_ui()
        self.set_status("✓ 设置已保存", "success")
        self.back_stage()


class ResidentApp(QObject):
    def __init__(self, instance_lock, app):
        super().__init__()
        try:
            migrate_legacy_credentials()
        except Exception as exc:
            # Keep the app usable even when an old secret cannot be migrated.
            append_resident_log(f"credential migration unavailable | type={type(exc).__name__}")
        self.instance_lock = instance_lock
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.active_worker = None
        self.active_request = None
        self.capture_request = None
        self.capture_overlays = []
        self.capture_previous_result_visible = False
        self.app = app
        self.app.setQuitOnLastWindowClosed(False)
        for directory in (REQUEST_DIR, PROCESSING_DIR, PROCESSED_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        self.recover_interrupted_requests()

        window_started_at = time.perf_counter()
        self.window = ModernResultWindow()
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
        # Run OCR initialization outside Qt's worker pool so it cannot queue a translation.
        self.ocr_prewarm_started = False
        QTimer.singleShot(1500, self.prewarm_ocr_when_idle)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.process_pending_requests)
        self.poll_timer.start(POLL_INTERVAL_MS)

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.write_state)
        self.heartbeat_timer.start(HEARTBEAT_INTERVAL_MS)

        self.app.aboutToQuit.connect(self.cleanup)
        self.write_state()
        append_resident_log(f"resident app started | pid={os.getpid()} | version={RESIDENT_VERSION}")

    def prewarm_ocr_when_idle(self):
        if self.ocr_prewarm_started:
            return
        if self.active_worker is not None or self.capture_request is not None:
            QTimer.singleShot(1000, self.prewarm_ocr_when_idle)
            return
        self.ocr_prewarm_started = True
        threading.Thread(target=self.prewarm_ocr, name="ocr-prewarm", daemon=True).start()

    @staticmethod
    def prewarm_ocr():
        try:
            from screen_ocr import initialize_engine

            timings = {}
            initialize_engine(timings)
            append_perf_log(
                "ocr_prewarm",
                " | ".join(f"{name}={elapsed:.0f}ms" for name, elapsed in timings.items()),
            )
        except Exception as exc:
            # Missing optional OCR dependencies must not affect Alt+T startup.
            append_resident_log(f"ocr prewarm skipped | {exc}")

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
        if SHUTDOWN_PATH.exists():
            try:
                requested_pid = SHUTDOWN_PATH.read_text(encoding="utf-8-sig").strip()
                SHUTDOWN_PATH.unlink(missing_ok=True)
                if requested_pid == str(os.getpid()):
                    self.app.quit()
                    return
            except OSError:
                pass
        if self.active_worker is not None or self.capture_request is not None:
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
                action = str(payload.get("action", "translate")).strip()
                if action not in {"translate", "capture"}:
                    raise RuntimeError("不支持的请求类型")
                if action == "translate" and not text:
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

            if action == "capture":
                self.begin_capture(request_id, claimed_path)
            else:
                self.start_translation(text, source, request_id, claimed_path)
            return

    def begin_capture(self, request_id, request_path):
        self.capture_request = (request_id, request_path)
        self.capture_previous_result_visible = self.window.isVisible()
        self.window.hide()
        # Give Windows a moment to remove the old result window before grabbing pixels.
        QTimer.singleShot(120, self.show_capture_overlays)

    def show_capture_overlays(self):
        if self.capture_request is None:
            return
        try:
            grabbing_at = time.perf_counter()
            screenshots = [
                (screen, screen.grabWindow(0)) for screen in QGuiApplication.screens()
            ]
            grab_ms = (time.perf_counter() - grabbing_at) * 1000
            if not screenshots or any(pixmap.isNull() for _, pixmap in screenshots):
                raise RuntimeError("无法获取屏幕图像")
            append_perf_log("screen_grab", f"id={self.capture_request[0]} | grab={grab_ms:.0f}ms")
            for screen, snapshot in screenshots:
                overlay = ScreenCaptureOverlay(screen, snapshot)
                overlay.captured.connect(self.capture_completed)
                overlay.cancelled.connect(self.capture_cancelled)
                self.capture_overlays.append(overlay)
                overlay.show()
            self.capture_overlays[0].raise_()
            self.capture_overlays[0].activateWindow()
        except Exception as exc:
            append_resident_log(f"screen capture failed\n{traceback.format_exc()}")
            self.capture_failed(str(exc))

    @Slot(bytes, float)
    def capture_completed(self, image_bytes, png_encode_ms):
        if self.capture_request is None:
            return
        request_id, request_path = self.capture_request
        append_perf_log(
            "screen_encode",
            f"id={request_id} | png_encode={png_encode_ms:.0f}ms | bytes={len(image_bytes)}",
        )
        self.close_capture_overlays()
        self.capture_request = None
        self.start_translation("", "screenshot", request_id, request_path, image_bytes)

    @Slot()
    def capture_cancelled(self):
        if self.capture_request is None:
            return
        _, request_path = self.capture_request
        self.close_capture_overlays()
        self.capture_request = None
        self.finalize_successful_request(request_path, False)
        if self.capture_previous_result_visible:
            self.window.bring_to_front()
        QTimer.singleShot(0, self.process_pending_requests)

    def capture_failed(self, message):
        if self.capture_request is None:
            return
        _, request_path = self.capture_request
        self.close_capture_overlays()
        self.capture_request = None
        self.archive_failed_request(request_path)
        QMessageBox.critical(self.window, "截图翻译失败", message)
        QTimer.singleShot(0, self.process_pending_requests)

    def close_capture_overlays(self):
        for overlay in self.capture_overlays:
            overlay.close()
            overlay.deleteLater()
        self.capture_overlays.clear()

    @Slot(str)
    def process_manual_request(self, text):
        if self.active_worker is not None or self.capture_request is not None:
            QMessageBox.information(self.window, "TranEasy", "已有翻译任务正在进行，请稍候。")
            return
        request_id = f"manual_{int(time.time() * 1000)}"
        self.start_translation(text, "manual", request_id)

    def start_translation(self, text, source, request_id, request_path=None, image_bytes=None):
        self.active_request = request_path
        worker = TranslationWorker(text, source, request_id, request_path, image_bytes)
        worker.signals.finished.connect(self.translation_finished)
        worker.signals.failed.connect(self.translation_failed)
        if image_bytes is not None:
            worker.signals.ocr_ready.connect(self.screenshot_ocr_ready)
            worker.signals.empty.connect(self.screenshot_empty)
        self.active_worker = worker
        # Screenshots stay unobtrusive until OCR finds text; empty areas need no popup.
        self.thread_pool.start(worker)
        if image_bytes is None:
            self.window.show_loading()

    @Slot(str)
    def screenshot_ocr_ready(self, request_id):
        if self.active_worker is not None and self.active_worker.request_id == request_id:
            self.window.show_loading()

    @Slot(object)
    def screenshot_empty(self, result):
        if self.active_worker is None or self.active_worker.request_id != result["request_id"]:
            return
        append_perf_log(
            "resident_request_empty",
            f"id={result['request_id']} | source=screenshot | "
            f"total={result['elapsed_ms']:.0f}ms | "
            + " | ".join(
                f"{name}={elapsed:.0f}ms" for name, elapsed in result["timings"].items()
            ),
        )
        if result["request_path"]:
            self.finalize_successful_request(Path(result["request_path"]), False)
        self.finish_active_job()

    @Slot(object)
    def translation_finished(self, result):
        rendering_at = time.perf_counter()
        self.window.config = result["config"]
        self.window.apply_config_to_ui()
        self.window.update_result(
            result["source_text"],
            result["translation"],
            result["saved_file"],
        )
        render_ms = (time.perf_counter() - rendering_at) * 1000
        request_path = result["request_path"]
        if request_path:
            self.finalize_successful_request(
                Path(request_path), bool(result["config"].get("keep_processed_requests", False))
            )
        stage_details = " | ".join(
            f"{name}={elapsed:.0f}ms" for name, elapsed in result["timings"].items()
        )
        append_perf_log(
            "resident_request",
            f"id={result['request_id']} | source={result['source_label']} | "
            f"total={result['elapsed_ms']:.0f}ms | {stage_details} | render={render_ms:.0f}ms",
        )
        self.finish_active_job()

    @Slot(object)
    def translation_failed(self, result):
        stage_details = " | ".join(
            f"{name}={elapsed:.0f}ms" for name, elapsed in result["timings"].items()
        )
        append_perf_log(
            "resident_request_failed",
            f"id={result['request_id']} | source={result['source_label']} | "
            f"total={result['elapsed_ms']:.0f}ms | {stage_details}",
        )
        append_resident_log(
            f"process request failed: {result['request_id']} | reason={result['diagnostic']}"
        )
        if result["request_path"]:
            self.archive_failed_request(Path(result["request_path"]))
        self.window.current_source = result.get("source_text", "")
        self.window.current_translation = ""
        self.window.current_tts_text = None
        self.window.source_view.setPlainText(result.get("source_text", ""))
        self.window.editor.setPlainText(result["error"])
        self.window.copy_btn.setEnabled(False)
        self.window.read_btn.setEnabled(False)
        self.window.set_status("翻译失败", "error")
        self.window.bring_to_front()
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
        self.close_capture_overlays()
        self.window.cleanup_audio_file()
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
    if "--release-smoke" in sys.argv:
        test_appdata = Path(os.environ.get("LOCALAPPDATA", "")).resolve()
        if os.environ.get("SCREENTRANS_RELEASE_TEST") != "1" or not test_appdata.is_relative_to(APP_DIR) or load_config().get("provider") != "mock":
            raise RuntimeError("Smoke tests must use isolated mock data; no real credentials are allowed")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TranEasy.Desktop.0.1")
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    app.setWindowIcon(QIcon(str(resource_root / "assets" / "transeasy-icon.ico")))
    instance_lock = QLockFile(str(LOCK_PATH))
    instance_lock.setStaleLockTime(10000)
    if not instance_lock.tryLock(0):
        append_resident_log("resident app start skipped: another instance owns the lock")
        return 0

    try:
        resident = ResidentApp(instance_lock, app)
        if "--release-smoke" in sys.argv:
            from release_smoke import run_smoke
            phase = next((value.split("=", 1)[1] for value in sys.argv if value.startswith("--smoke-phase=")), "write")
            return run_smoke(resident, app, ScreenCaptureOverlay, phase)
        return resident.run()
    except Exception:
        append_resident_log(f"resident app startup failed\n{traceback.format_exc()}")
        instance_lock.unlock()
        raise


if __name__ == "__main__":
    sys.exit(main())
