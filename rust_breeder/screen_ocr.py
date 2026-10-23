"""Offline screen-image OCR using the Windows recognition engine.

Call ``recognize_gene_image`` in a worker: starting the Windows OCR process can
take several seconds on its first use. Qt is loaded lazily for badge cleanup.
Images are temporary local files and are never sent to a remote service.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unicodedata


class OcrError(RuntimeError):
    """The local image could not be recognized by Windows OCR."""


_GENE_TOKEN = re.compile(r"[A-Z0-9_]+")
_GLYPH_SEPARATOR = re.compile(r"[ \t\u00a0|·•,，、;；:\-]+\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_OCR_TIMEOUT_SECONDS = 15
_MAX_LOCATION_PIXELS = 16_800_000


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise OcrError("本地 OCR 识别超时，请缩小区域或稍后重试。")


def extract_gene_sequences(text: str) -> tuple[str, ...]:
    """Return strict six-position genes in reading order, keeping duplicates.

    OCR may insert spaces between glyphs or groups of glyphs. Join those only
    within the same line. A seven-letter token or a seven-glyph separated run
    is rejected in full, so it cannot silently become a different six-gene
    seed. No visually similar letters are guessed or replaced.
    """
    normalized = unicodedata.normalize("NFKC", text).upper()
    found: list[str] = []
    for line in normalized.splitlines():
        pieces: list[str] = []
        previous_end: int | None = None

        def flush() -> None:
            joined = "".join(pieces)
            if len(joined) == 6:
                found.append(joined)
            pieces.clear()

        for match in _GENE_TOKEN.finditer(line):
            token = match.group()
            if previous_end is not None and not _GLYPH_SEPARATOR.fullmatch(
                line[previous_end:match.start()]
            ):
                flush()
            if set(token) <= set("GYHWX"):
                if len(token) == 6:
                    flush()
                    found.append(token)
                elif len(token) < 6:
                    pieces.append(token)
                else:
                    flush()
            else:
                flush()
            previous_end = match.end()
        flush()
    return tuple(found)


# Embedded for source and frozen builds alike; no external script/data file is
# needed. The image path travels in an environment variable, never executable
# PowerShell source. JSON is the sole stdout protocol.
_WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$stream = $null
$bitmap = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] > $null
    [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime] > $null
    [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] > $null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] > $null
    [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime] > $null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] > $null
    [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime] > $null
    [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] > $null

    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
        Select-Object -First 1
    function Await-WinRT($operation, [Type]$resultType) {
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        $task.GetAwaiter().GetResult()
    }

    $english = New-Object Windows.Globalization.Language('en-US')
    $engine = $null
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($english)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($english)
    }
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) {
        throw 'No Windows OCR language is installed. Add English language OCR in Windows Settings > Time & language > Language & region.'
    }

    $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:RUST_SEED_OCR_IMAGE)) ([Windows.Storage.StorageFile])
    $stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $maximum = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($bitmap.PixelWidth -gt $maximum -or $bitmap.PixelHeight -gt $maximum) {
        throw "The selected image is too large for Windows OCR (maximum dimension: $maximum). Select a smaller screen area."
    }
    $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    # Export actual positions: Windows sometimes reports widely spaced badge
    # glyphs as separate lines, so Python reconstructs the screen rows.
    $words = @($result.Lines | ForEach-Object { $_.Words } | ForEach-Object {
        @{text = $_.Text; x = $_.BoundingRect.X; y = $_.BoundingRect.Y; width = $_.BoundingRect.Width; height = $_.BoundingRect.Height}
    })
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    @{text = ($lines -join "`n"); words = $words; language = $engine.RecognizerLanguage.LanguageTag} | ConvertTo-Json -Compress -Depth 4
} catch {
    @{error = $_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
"""
_ENCODED_OCR_SCRIPT = base64.b64encode(_WINDOWS_OCR_SCRIPT.encode("utf-16-le")).decode("ascii")


def _powershell_path() -> Path:
    if os.name != "nt":
        raise OcrError("屏幕识别需要 Windows 10/11 的本地 OCR 功能。")
    windows_dir = os.environ.get("SystemRoot", r"C:\Windows")
    executable = Path(windows_dir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise OcrError("未找到 Windows PowerShell，无法启动本地屏幕识别。")
    return executable


@dataclass(frozen=True)
class _PreparedGenes:
    image: bytes
    # Each entry describes the six original colors, never a guessed letter.
    colors: tuple[tuple[int, ...], ...]
    row_height: float = 128.0


def _colored_components(colors: bytearray, width: int, height: int, *, deadline: float | None = None) -> list[tuple[int, int, int, int, int, int]]:
    """Run-length components avoid flooding millions of background pixels."""
    parents: list[int] = []
    # left, top, right, bottom, colored pixel count, color
    bounds: list[list[int]] = []
    previous: list[tuple[int, int, int, int]] = []

    def root(label: int) -> int:
        while parents[label] != label:
            parents[label] = parents[parents[label]]
            label = parents[label]
        return label

    for y in range(height):
        if y % 64 == 0:
            _check_deadline(deadline)
        row = y * width
        current: list[tuple[int, int, int, int]] = []
        x = 0
        previous_start = 0
        while x < width:
            color = colors[row + x]
            if not color:
                x += 1
                continue
            left = x
            while x + 1 < width and colors[row + x + 1] == color:
                x += 1
            right = x
            label = len(parents)
            # Bound pathological photo noise; ordinary screenshots are far
            # below this limit and still use only a small number of runs.
            if label >= 200_000:
                return []
            parents.append(label)
            bounds.append([left, y, right, y, right - left + 1, color])
            while previous_start < len(previous) and previous[previous_start][1] < left - 1:
                previous_start += 1
            index = previous_start
            while index < len(previous) and previous[index][0] <= right + 1:
                _, _, previous_color, previous_label = previous[index]
                if color == previous_color:
                    first, second = root(label), root(previous_label)
                    if first != second:
                        if second < first:
                            first, second = second, first
                        parents[second] = first
                        a, b = bounds[first], bounds[second]
                        a[0], a[1], a[2], a[3], a[4] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]
                index += 1
            current.append((left, right, color, label))
            x += 1
        previous = current
    return [(box[0], box[1], box[2] - box[0] + 1, box[3] - box[1] + 1, box[4], box[5]) for index, box in enumerate(bounds) if parents[index] == index]


def _merge_badge_fragments(boxes: list[tuple[int, int, int, int, int, int]], *, deadline: float | None = None) -> list[tuple[int, int, int, int, int, int]]:
    """Reconnect a thin circle arc split off by a glyph touching its edge."""
    merged: list[tuple[int, int, int, int, int, int] | None] = list(boxes)
    for index, fragment in enumerate(merged):
        if index % 32 == 0:
            _check_deadline(deadline)
        if fragment is None:
            continue
        x, y, width, height, count, color = fragment
        if not (6 <= height <= 180 and width / height < 0.35):
            continue
        best: tuple[float, int, tuple] | None = None
        for other_index, box in enumerate(merged):
            if box is None or index == other_index:
                continue
            bx, by, bw, bh, pixels, other_color = box
            if color != other_color or not (0.45 <= bw / bh <= 1.5) or pixels < count * 2 or max(height, bh) > min(height, bh) * 2:
                continue
            overlap = min(y + height, by + bh) - max(y, by)
            gap = max(bx - x - width, x - bx - bw, 0)
            left, top = min(x, bx), min(y, by)
            right, bottom = max(x + width, bx + bw), max(y + height, by + bh)
            if overlap >= min(height, bh) * 0.65 and gap <= max(height, bh) * 0.15 and 0.65 <= (right - left) / (bottom - top) <= 1.5:
                candidate = (gap, other_index, (left, top, right - left, bottom - top, count + pixels, color))
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
        if best is not None:
            merged[index] = None
            merged[best[1]] = best[2]
    return [box for box in merged if box is not None]


def _prepare_badge_image(png_bytes: bytes, *, deadline: float | None = None) -> _PreparedGenes | None:
    """Isolate both colored glyphs and letters inside red/green badges.

    Components retain their screen positions and original color. Complete,
    regularly spaced groups are enlarged independently; unrelated icons or a
    damaged group cannot discard another seed. Only pixels are transformed,
    never the recognized letters.
    """
    from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter

    source = QImage.fromData(png_bytes, "PNG").convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = source.width(), source.height()
    # Locate at original resolution, including 4K and two adjacent 4K screens;
    # scaling the whole screen first would erase small gene letters.
    if source.isNull() or width * height > _MAX_LOCATION_PIXELS:
        return None
    _check_deadline(deadline)
    # The common empty/neutral desktop case needs no per-pixel Python scan.
    if source.allGray():
        return None
    pixels = source.constBits()
    stride = source.bytesPerLine()
    colors = bytearray(width * height)
    for y in range(height):
        if y % 64 == 0:
            _check_deadline(deadline)
        offset = y * stride
        for x in range(width):
            index = offset + x * 4
            red, green, blue = pixels[index], pixels[index + 1], pixels[index + 2]
            if green >= 80 and green > red + 20 and green > blue + 20:
                colors[y * width + x] = 1
            elif red >= 80 and red > green + 20 and red > blue + 20:
                colors[y * width + x] = 2

    # x, y, width, height, color, glyph image, whether this is a filled badge.
    components: list[tuple[int, int, int, int, int, QImage | None, bool]] = []
    boxes = _merge_badge_fragments(_colored_components(colors, width, height, deadline=deadline), deadline=deadline)
    for position, (left, top, box_width, box_height, count, color) in enumerate(boxes):
        if position % 32 == 0:
            _check_deadline(deadline)
        if not (3 <= box_width <= 180 and 6 <= box_height <= 180 and 0.18 <= box_width / box_height <= 1.8 and count >= 8):
            continue

        glyph = QImage(box_width, box_height, QImage.Format.Format_RGB32)
        glyph.fill(0xFFFFFFFF)
        # A badge has a mostly continuous colored ring around its glyph. A
        # colored H/W can be quite dense too, so density alone is insufficient.
        ring_count = ring_colored = 0
        for y in range(box_height):
            for x in range(box_width):
                radius = ((x + 0.5 - box_width / 2) / (box_width / 2)) ** 2 + ((y + 0.5 - box_height / 2) / (box_height / 2)) ** 2
                if 0.52 <= radius <= 0.85:
                    ring_count += 1
                    ring_colored += colors[(top + y) * width + left + x] == color
        badge = box_width >= 10 and box_height >= 10 and 0.65 <= box_width / box_height <= 1.5 and ring_colored >= ring_count * 0.62
        if badge:
            # Filled circles/tiles cover the middle of all four outer sides.
            # H and W may enclose a few background pixels through anti-aliasing,
            # but do not cover those four sides like a filled badge does.
            radius = max(1, min(box_width, box_height) // 12)
            for sx, sy in ((box_width // 2, 1), (box_width // 2, box_height - 2), (1, box_height // 2), (box_width - 2, box_height // 2)):
                sample = [colors[(top + y) * width + left + x] == color for y in range(max(0, sy - radius), min(box_height, sy + radius + 1)) for x in range(max(0, sx - radius), min(box_width, sx + radius + 1))]
                if sum(sample) < len(sample) * 0.45:
                    badge = False
                    break
        bright_threshold: int | None = None
        bright_levels: tuple[float, float] | None = None
        if box_width >= 10 and box_height >= 10 and 0.65 <= box_width / box_height <= 1.5:
            # Rust's small detail badges have white glyphs that touch the
            # colored outer ring. H/G can therefore fail the closed-ring test.
            # Look for neutral light ink brighter than the surrounding panel,
            # rather than interpreting the remaining colored fill as a letter.
            # A bare colored glyph on white has the same white background
            # inside/outside its bounds and cannot satisfy this contrast test.
            background: list[int] = []
            for y in range(max(0, top - 2), min(height, top + box_height + 2)):
                for x in range(max(0, left - 2), min(width, left + box_width + 2)):
                    if left <= x < left + box_width and top <= y < top + box_height:
                        continue
                    offset = y * stride + x * 4
                    channels = (pixels[offset], pixels[offset + 1], pixels[offset + 2])
                    if max(channels) - min(channels) <= 65:
                        background.append(sum(channels) // 3)
            if background:
                background.sort()
                threshold = max(140, background[len(background) // 2] + 35)
                bright_count = 0
                for y in range(box_height):
                    for x in range(box_width):
                        if ((x + 0.5 - box_width / 2) / (box_width / 2)) ** 2 + ((y + 0.5 - box_height / 2) / (box_height / 2)) ** 2 > 0.84:
                            continue
                        offset = (top + y) * stride + (left + x) * 4
                        channels = (pixels[offset], pixels[offset + 1], pixels[offset + 2])
                        bright_count += max(channels) - min(channels) <= 65 and min(channels) >= threshold
                if 8 <= bright_count <= box_width * box_height * 0.45:
                    badge = True
                    bright_threshold = threshold
                    fill_values: list[int] = []
                    light_values: list[int] = []
                    for y in range(box_height):
                        for x in range(box_width):
                            offset = (top + y) * stride + (left + x) * 4
                            channels = (pixels[offset], pixels[offset + 1], pixels[offset + 2])
                            if colors[(top + y) * width + left + x] == color:
                                fill_values.append(min(channels))
                            if min(channels) >= threshold and max(channels) - min(channels) <= 65:
                                light_values.append(min(channels))
                    fill_values.sort()
                    low = max(background[len(background) // 2] - 10, fill_values[len(fill_values) // 2])
                    high = max(light_values)
                    bright_levels = (low, max(low + 35, high))
        outside: set[int] = set()
        if badge and bright_threshold is None:
            # A colored G resembles a filled ring, but its background is open
            # to the edge. Only enclosed neutral pixels belong to a badge's
            # letter; this prevents turning a bare G into its inverse shape.
            pending = [y * box_width + x for y in range(box_height) for x in range(box_width) if x in (0, box_width - 1) or y in (0, box_height - 1)]
            while pending:
                point = pending.pop()
                if point in outside:
                    continue
                x, y = point % box_width, point // box_width
                if colors[(top + y) * width + left + x] == color:
                    continue
                outside.add(point)
                if x:
                    pending.append(point - 1)
                if x + 1 < box_width:
                    pending.append(point + 1)
                if y:
                    pending.append(point - box_width)
                if y + 1 < box_height:
                    pending.append(point + box_width)
        ink = 0
        for y in range(box_height):
            for x in range(box_width):
                if badge:
                    if y * box_width + x in outside:
                        continue
                    # Stay inside the filled circle, excluding its background.
                    if bright_threshold is None and ((x + 0.5 - box_width / 2) / (box_width / 2)) ** 2 + ((y + 0.5 - box_height / 2) / (box_height / 2)) ** 2 > 0.84:
                        continue
                    index = (top + y) * stride + (left + x) * 4
                    channels = (pixels[index], pixels[index + 1], pixels[index + 2])
                    if bright_levels is not None:
                        # Keep the small font's original anti-aliasing. A hard
                        # threshold turns Rust's tiny X into a block resembling
                        # I; linear contrast removes the colored fill without
                        # discarding the faint diagonal stroke pixels.
                        low, high = bright_levels
                        strength = max(0.0, min(1.0, (min(channels) - low) / (high - low)))
                        shade = round(255 * (1 - strength))
                        glyph.setPixel(x, y, 0xFF000000 | shade * 0x010101)
                        ink += strength >= 0.3
                        continue
                    is_ink = max(channels) - min(channels) <= 65 and (min(channels) >= bright_threshold if bright_threshold is not None else (min(channels) >= 175 or max(channels) <= 100))
                else:
                    is_ink = colors[(top + y) * width + left + x] == color
                if is_ink:
                    glyph.setPixel(x, y, 0xFF000000)
                    ink += 1
        if badge and ink < 8 and count < box_width * box_height * 0.70:
            # A sparse colored stroke with no enclosed glyph is plain text.
            badge = False
            glyph.fill(0xFFFFFFFF)
            ink = 0
            for y in range(box_height):
                for x in range(box_width):
                    if colors[(top + y) * width + left + x] == color:
                        glyph.setPixel(x, y, 0xFF000000)
                        ink += 1
        if ink < 8 or (badge and ink > box_width * box_height * 0.45):
            # Retain an unreadable badge's position. In two adjacent seeds,
            # five good glyphs must not shift the next seed's group boundary.
            if badge:
                components.append((left, top, box_width, box_height, color, None, True))
            continue
        components.append((left, top, box_width, box_height, color, glyph, badge))

    rows: list[list[tuple]] = []
    for box in sorted(components, key=lambda item: (item[1] + item[3] / 2, item[0])):
        center = box[1] + box[3] / 2
        if rows and abs(center - (rows[-1][0][1] + rows[-1][0][3] / 2)) <= min(box[3], rows[-1][0][3]) * 0.4:
            rows[-1].append(box)
        else:
            rows.append([box])

    complete_rows: list[list[tuple]] = []
    for row in rows:
        typical_height = sorted(box[3] for box in row)[len(row) // 2]
        # UI scaling can make the separators between genes six pixels high.
        # Their height relative to the six glyphs still identifies them.
        row = [box for box in row if box[3] >= typical_height * 0.55]
        runs: list[list[tuple]] = []
        for box in sorted(row):
            if runs:
                previous = runs[-1][-1]
                gap = box[0] - previous[0] - previous[2]
                same_size = max(box[3], previous[3]) <= min(box[3], previous[3]) * 1.55
                if gap <= max(box[3], previous[3]) * 3.5 and same_size and box[6] == previous[6]:
                    runs[-1].append(box)
                    continue
            runs.append([box])
        for run in runs:
            # Do not take six arbitrary letters out of a seven-letter run.
            if len(run) % 6:
                continue
            for start in range(0, len(run), 6):
                group = run[start:start + 6]
                pitches = [b[0] + b[2] / 2 - a[0] - a[2] / 2 for a, b in zip(group, group[1:])]
                if min(pitches) > 0 and max(pitches) <= min(pitches) * 1.9 and all(box[5] is not None for box in group):
                    complete_rows.append(group)
    if not complete_rows:
        return None
    # Bound the output batch, then shrink only unusually tall results to stay
    # within Windows OCR's dimension limit.
    complete_rows = complete_rows[:24]
    result = QImage(680, len(complete_rows) * 128 + 24, QImage.Format.Format_RGB32)
    result.fill(0xFFFFFFFF)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    used_width = 0.0
    for row_index, row in enumerate(complete_rows):
        text_x = 22.0
        # Enlarging a 14-pixel Rust badge to 96 pixels can make Windows read
        # an X's diagonals as two parentheses. A moderate glyph size preserves
        # its connected shape without binarizing away the faint center.
        badge_height = min(64, int(596 / sum(box[2] / box[3] for box in row)))
        for column, (_, _, _, _, _, glyph, badge) in enumerate(row):
            spacing = 8
            if badge:
                # Keep the badge's natural letter margins; tight cropping
                # thickens a low-resolution font. Compact the complete boxes.
                ink = [(x, y) for y in range(glyph.height()) for x in range(glyph.width()) if glyph.pixelColor(x, y).red() < 160]
                if not ink:
                    # Leave this prepared row incomplete. Its strict six-gene
                    # check will reject it while other rows remain readable.
                    break
                left, top = min(x for x, _ in ink), min(y for _, y in ink)
                right, bottom = max(x for x, _ in ink), max(y for _, y in ink)
                if (bottom - top + 1) / glyph.height() < 0.66:
                    # Large circles around small text need less empty margin,
                    # or repeated glyphs are mistaken for decorative shapes.
                    glyph = glyph.copy(left, top, right - left + 1, bottom - top + 1).scaledToHeight(48, Qt.TransformationMode.SmoothTransformation)
                    spacing = 6
                else:
                    glyph = glyph.scaledToHeight(badge_height, Qt.TransformationMode.SmoothTransformation)
            else:
                glyph = glyph.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = text_x
            y = 16 + row_index * 128 + (96 - glyph.height()) / 2
            painter.drawImage(QRectF(x, y, glyph.width(), glyph.height()), glyph)
            text_x += glyph.width() + spacing
        used_width = max(used_width, text_x)
    painter.end()
    # Keep text and margins proportional. Windows may ignore a short single
    # row surrounded by the otherwise unused 680-pixel-wide canvas.
    result = result.copy(0, 0, min(result.width(), math.ceil(used_width + 16)), result.height())
    row_height = 128.0
    if result.height() > 2400:
        row_height *= 2400 / result.height()
        result = result.scaledToHeight(2400, Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not result.save(buffer, "PNG"):
        return None
    return _PreparedGenes(bytes(buffer.data()), tuple(tuple(box[4] for box in row) for row in complete_rows), row_height)


def _recognize_image_text(png_bytes: bytes, executable: Path, timeout: float) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="rust-seed-ocr-") as directory:
            image_path = Path(directory) / "capture.png"
            image_path.write_bytes(png_bytes)
            environment = os.environ.copy()
            environment["RUST_SEED_OCR_IMAGE"] = str(image_path)
            process = subprocess.run(
                [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ENCODED_OCR_SCRIPT],
                capture_output=True,
                text=True,
                encoding="utf-8-sig",
                errors="replace",
                env=environment,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise OcrError("本地 OCR 识别超时，请缩小区域或稍后重试。") from error
    except OSError as error:
        raise OcrError(f"无法运行本地 OCR：{error}") from error
    try:
        response = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise OcrError("Windows OCR 未返回有效结果，请确认系统的 OCR 语言功能可用。") from error
    if not isinstance(response, dict):
        raise OcrError("Windows OCR 返回了无法读取的结果，请重试。")
    if process.returncode != 0 or response.get("error"):
        detail = str(response.get("error") or "Windows OCR process failed.").strip()
        raise OcrError(f"本地 OCR 无法识别图片：{detail}\n可在 Windows 设置 → 时间和语言 → 语言和区域中安装英语的光学字符识别功能。")
    recognized = response.get("text")
    if not isinstance(recognized, str):
        raise OcrError("Windows OCR 未返回可识别的文字，请重试。")
    rows = _screen_word_rows(response.get("words"))
    if rows:
        return _PositionedText("\n".join(text for _, text in rows), rows)
    return recognized


class _PositionedText(str):
    """OCR text plus row centers, retained for color validation after reflow."""

    def __new__(cls, value: str, rows: tuple[tuple[float, str], ...]):
        instance = super().__new__(cls, value)
        instance.rows = rows
        return instance


def _words_in_screen_order(words: object) -> str:
    """Reconstruct rows and retain large horizontal gaps between seed cards."""
    return "\n".join(text for _, text in _screen_word_rows(words))


def _screen_word_rows(words: object) -> tuple[tuple[float, str], ...]:
    if not isinstance(words, list):
        return ()
    valid: list[tuple[float, float, float, float, str]] = []
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            return ()
        coordinates = [word.get(key) for key in ("x", "y", "height")]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in coordinates):
            return ()
        x, y, height = coordinates
        if height <= 0:
            return ()
        # Older captured/mock payloads have no width. Estimate their extent,
        # rather than losing all recognition when restoring an older result.
        width = word.get("width", max(height * 0.5, len(word["text"]) * height * 0.6))
        if type(width) not in (int, float) or not math.isfinite(width) or width <= 0:
            return ()
        valid.append((y + height / 2, x, height, width, word["text"]))
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for word in sorted(valid):
        if rows and abs(word[0] - rows[-1][0][0]) <= max(3, min(word[2], rows[-1][0][2]) * 0.6):
            rows[-1].append(word)
        else:
            rows.append([word])
    result: list[tuple[float, str]] = []
    for row in rows:
        groups: list[list[tuple]] = []
        for word in sorted(row, key=lambda word: word[1]):
            if groups:
                previous = groups[-1][-1]
                gap = word[1] - previous[1] - previous[3]
                if gap <= max(word[2], previous[2]) * 2.5:
                    groups[-1].append(word)
                    continue
            groups.append([word])
        for group in groups:
            result.append((sum(word[0] for word in group) / len(group), " ".join(word[4] for word in group)))
    return tuple(result)


def _validated_colored_genes(text: str, prepared: _PreparedGenes) -> tuple[str, ...]:
    """Reject a red G/Y/H or green W/X; never substitute another letter.

    Screen row coordinates, rather than result order, keep a missing OCR row
    from shifting every following seed onto the wrong color signature.
    """
    rows = getattr(text, "rows", None)
    if rows is None:
        # Windows returns word geometry. This fallback is also useful for
        # alternate local engines/tests that retain one line per prepared row.
        lines = text.splitlines()
        if len(lines) != len(prepared.colors):
            return ()
        rows = tuple(((0.5 + index) * prepared.row_height, line) for index, line in enumerate(lines))
    # Ordinary screen text uses large gaps to separate different seed cards.
    # The prepared image deliberately has exactly one seed per row, however,
    # and its small badge letters can themselves exceed that gap threshold.
    # Rejoin only fragments mapped to that same generated row.
    fragments: dict[int, list[str]] = {}
    for center, line in rows:
        index = int(center // prepared.row_height)
        if 0 <= index < len(prepared.colors):
            fragments.setdefault(index, []).append(line)
    found: list[str] = []
    for index in sorted(fragments):
        candidates = extract_gene_sequences(" ".join(fragments[index]))
        if len(candidates) != 1:
            continue
        genes = candidates[0]
        if all((1 if gene in "GYH" else 2) == color for gene, color in zip(genes, prepared.colors[index])):
            found.append(genes)
    return tuple(found)


def recognize_gene_image(png_bytes: bytes, *, colored_only: bool = False) -> tuple[str, ...]:
    """Recognize a local PNG through Windows OCR; never upload the image.

    Returned sequences preserve repeated occurrences for the caller's review
    and deduplication. Recognition success with no valid genes returns ``()``.
    Operational failures raise ``OcrError`` with a user-readable explanation.
    ``colored_only`` locates six colored glyphs before starting OCR. This mode
    is safe for automatic whole-screen scans: unrelated plain-text genes are
    never sent to OCR, and an empty screen does not start a subprocess.
    """
    if not isinstance(png_bytes, bytes) or not png_bytes.startswith(_PNG_SIGNATURE):
        raise OcrError("屏幕截图不是有效的 PNG 图片，请重新选择识别区域。")
    if len(png_bytes) > _MAX_IMAGE_BYTES:
        raise OcrError("截图超过 32 MB，请缩小识别区域后重试。")
    deadline = time.monotonic() + _OCR_TIMEOUT_SECONDS
    if colored_only:
        prepared = _prepare_badge_image(png_bytes, deadline=deadline)
        _check_deadline(deadline)
        if prepared is None:
            return ()
        return _recognize_prepared(prepared, _powershell_path(), deadline)
    executable = _powershell_path()
    genes = extract_gene_sequences(_recognize_image_text(png_bytes, executable, _OCR_TIMEOUT_SECONDS))
    # A valid alphabet alone cannot establish accuracy: OCR can misread a red
    # W as a green-compatible H. Always inspect recognizable colored groups.
    _check_deadline(deadline)
    prepared = _prepare_badge_image(png_bytes, deadline=deadline)
    if prepared is None:
        if time.monotonic() > deadline:
            raise OcrError("本地 OCR 识别超时，请缩小区域或稍后重试。")
        return genes
    return _recognize_prepared(prepared, executable, deadline)


def _recognize_prepared(prepared: _PreparedGenes, executable: Path, deadline: float) -> tuple[str, ...]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OcrError("本地 OCR 识别超时，请缩小区域或稍后重试。")
    enhanced = _recognize_image_text(prepared.image, executable, remaining)
    _check_deadline(deadline)
    genes = _validated_colored_genes(enhanced, prepared)
    if genes or extract_gene_sequences(enhanced):
        # A complete but color-conflicting reading must stay rejected. A retry
        # is only for letters the engine could not form into a six-gene row.
        return genes
    retry = _rescale_prepared_for_retry(prepared)
    if retry is None:
        return ()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OcrError("本地 OCR 识别超时，请缩小区域或稍后重试。")
    enhanced = _recognize_image_text(retry.image, executable, remaining)
    _check_deadline(deadline)
    return _validated_colored_genes(enhanced, retry)


def _rescale_prepared_for_retry(prepared: _PreparedGenes) -> _PreparedGenes | None:
    """Retry unreadable small text at another size, without replacing glyphs.

    Windows can miss a clear six-glyph row at a particular raster size. A
    modest resampling recovers the real 125%-UI-scale detail strip while
    retaining its original letter shapes, color signatures and row positions.
    """
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage

    source = QImage.fromData(prepared.image, "PNG")
    if source.isNull():
        return None
    # Prepared batches already stay below 2400 px. Do not make a tall batch
    # exceed the Windows engine limit just to retry a missed small text row.
    scale = 1.25 if max(source.width(), source.height()) * 1.25 <= 2400 else 0.8
    image = source.scaled(
        round(source.width() * scale), round(source.height() * scale),
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
    )
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if image.isNull() or not image.save(buffer, "PNG"):
        return None
    return _PreparedGenes(
        bytes(buffer.data()), prepared.colors,
        prepared.row_height * image.height() / source.height(),
    )
