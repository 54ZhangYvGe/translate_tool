"""Export the approved TransEasy artwork without redrawing its shapes."""

from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "assets" / "transeasy-icon-source.png"
SVG_PATH = ROOT / "assets" / "transeasy-icon.svg"
PNG_PATH = ROOT / "assets" / "transeasy-icon.png"
PREVIEW_PATH = ROOT / "assets" / "transeasy-icon-preview.png"
ICO_PATH = ROOT / "assets" / "transeasy-icon.ico"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
CANVAS_SIZE = 1024
ART_SIZE = 900


def load_source() -> tuple[np.ndarray, np.ndarray]:
    source = cv2.imread(str(SOURCE_PATH), cv2.IMREAD_GRAYSCALE)
    if source is None:
        raise RuntimeError(f"Unable to read {SOURCE_PATH}")
    alpha = np.clip((245.0 - source.astype(np.float32)) * (255.0 / 225.0), 0, 255).astype(np.uint8)
    points = cv2.findNonZero((alpha > 8).astype(np.uint8))
    if points is None:
        raise RuntimeError("No artwork found in source image")
    x, y, width, height = cv2.boundingRect(points)
    return source[y : y + height, x : x + width], alpha[y : y + height, x : x + width]


def place_on_canvas(alpha: np.ndarray) -> np.ndarray:
    height, width = alpha.shape
    scale = min(ART_SIZE / width, ART_SIZE / height)
    target_width = round(width * scale)
    target_height = round(height * scale)
    resized = cv2.resize(alpha, (target_width, target_height), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((CANVAS_SIZE, CANVAS_SIZE), dtype=np.uint8)
    left = (CANVAS_SIZE - target_width) // 2
    top = (CANVAS_SIZE - target_height) // 2
    canvas[top : top + target_height, left : left + target_width] = resized
    return canvas


def write_png(alpha: np.ndarray) -> None:
    bgra = np.zeros((CANVAS_SIZE, CANVAS_SIZE, 4), dtype=np.uint8)
    bgra[:, :, 3] = alpha
    if not cv2.imwrite(str(PNG_PATH), bgra):
        raise RuntimeError(f"Unable to write {PNG_PATH}")


def write_svg(source_gray: np.ndarray) -> None:
    binary = np.where(source_gray < 190, 255, 0).astype(np.uint8)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    source_height, source_width = source_gray.shape
    scale = min(ART_SIZE / source_width, ART_SIZE / source_height)
    left = (CANVAS_SIZE - source_width * scale) / 2
    top = (CANVAS_SIZE - source_height * scale) / 2
    paths: list[str] = []
    for contour in contours:
        contour = cv2.approxPolyDP(contour, 0.55, True)
        points = contour.reshape(-1, 2)
        if len(points) < 3:
            continue
        first_x, first_y = points[0]
        commands = [f"M{left + first_x * scale:.2f} {top + first_y * scale:.2f}"]
        commands.extend(f"L{left + x * scale:.2f} {top + y * scale:.2f}" for x, y in points[1:])
        commands.append("Z")
        paths.append(" ".join(commands))
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" '
        'viewBox="0 0 1024 1024" role="img" aria-label="TransEasy stone stele icon">\n'
        f'  <path d="{" ".join(paths)}" fill="#000" fill-rule="evenodd"/>\n'
        '</svg>\n'
    )
    SVG_PATH.write_text(svg, encoding="utf-8")


def image_as_png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Unable to encode PNG icon frame")
    return bytes(data)


def write_ico(image: QImage) -> None:
    frames: list[tuple[int, bytes]] = []
    for size in ICON_SIZES:
        frame = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        frames.append((size, image_as_png_bytes(frame)))
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries: list[bytes] = []
    payloads: list[bytes] = []
    for size, payload in frames:
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    ICO_PATH.write_bytes(header + b"".join(entries) + b"".join(payloads))


def write_preview(image: QImage) -> None:
    preview = QImage(image.size(), QImage.Format.Format_ARGB32)
    preview.fill(QColor("white"))
    painter = QPainter(preview)
    painter.drawImage(0, 0, image)
    painter.end()
    preview.save(str(PREVIEW_PATH), "PNG")


def main() -> None:
    source_gray, cropped_alpha = load_source()
    alpha = place_on_canvas(cropped_alpha)
    write_png(alpha)
    write_svg(source_gray)
    app = QGuiApplication.instance() or QGuiApplication([])
    image = QImage(str(PNG_PATH))
    if image.isNull():
        raise RuntimeError(f"Unable to load generated PNG: {PNG_PATH}")
    write_ico(image)
    write_preview(image)
    del app


if __name__ == "__main__":
    main()
