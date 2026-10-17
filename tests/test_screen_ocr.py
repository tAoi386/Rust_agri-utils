"""Strict gene parsing and local Windows OCR process/error behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rust_breeder import screen_ocr


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\nmock-image-data"
RUST_DETAIL_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "rust-detail-genes.png"
RUST_WHEAT_DETAIL_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "rust-wheat-detail-genes.png"


class GeneOcrParserTests(unittest.TestCase):
    def test_preserves_gene_positions_duplicates_and_reading_order(self):
        self.assertEqual(
            screen_ocr.extract_gene_sequences("GYYGHX WWYGGY\nGYYGHX"),
            ("GYYGHX", "WWYGGY", "GYYGHX"),
        )

    def test_joins_separated_glyphs_and_groups_within_one_row(self):
        self.assertEqual(
            screen_ocr.extract_gene_sequences("基因: G Y H W X G\nGY HW XG\nG|Y|H|W|X|G"),
            ("GYHWXG", "GYHWXG", "GYHWXG"),
        )

    def test_normalizes_case_and_full_width_without_guessing_letters(self):
        self.assertEqual(screen_ocr.extract_gene_sequences("ｇｙｈｗｘｇ\ngyhwxg"), ("GYHWXG", "GYHWXG"))
        self.assertEqual(screen_ocr.extract_gene_sequences("GVHWXG GYHMXG GYHWKQ"), ())

    def test_rejects_invalid_longer_tokens_and_seven_separated_glyphs(self):
        for value in ("GGYHWXG", "GYHWXGG", "AGYHWXG", "GYHWXG1", "1GYHWXG", "_GYHWXG", "G Y H W X G Y", "GG GG GG GG"):
            with self.subTest(value=value):
                self.assertEqual(screen_ocr.extract_gene_sequences(value), ())

    def test_does_not_join_across_rows_or_invalid_characters(self):
        self.assertEqual(screen_ocr.extract_gene_sequences("GYH\nWXG\nGY!HWXG\nG Y Z H W X G"), ())

    def test_reconstructs_columnwise_windows_words_as_horizontal_gene_rows(self):
        words = []
        # Deliberately feed in the column order that Windows reports for badges.
        for column, pair in enumerate(zip("GYYGHX", "WWYGGY")):
            for row, letter in enumerate(pair):
                words.append({"text": letter, "x": column * 60, "y": row * 70, "height": 32})
        text = screen_ocr._words_in_screen_order(words)
        self.assertEqual(screen_ocr.extract_gene_sequences(text), ("GYYGHX", "WWYGGY"))

    def test_invalid_word_geometry_falls_back_to_engine_text(self):
        for words in (None, {}, [{"text": "GYYGHX", "x": float("nan"), "y": 1, "height": 20}], [{"text": "GYYGHX", "x": 1, "y": 1, "height": 0}], [{"text": "GYYGHX", "x": 1, "y": 1, "height": 20, "width": -1}]):
            with self.subTest(words=words):
                self.assertEqual(screen_ocr._words_in_screen_order(words), "")

    def test_separate_seed_cards_on_one_baseline_keep_their_six_positions(self):
        for include_width in (False, True):
            words = []
            for base, genes in ((0, "GYYGHX"), (500, "WWYGGY")):
                for column, gene in enumerate(genes):
                    word = {"text": gene, "x": base + column * 36, "y": 10, "height": 22}
                    if include_width:
                        word["width"] = 16
                    words.append(word)
            with self.subTest(include_width=include_width):
                self.assertEqual(screen_ocr.extract_gene_sequences(screen_ocr._words_in_screen_order(words)), ("GYYGHX", "WWYGGY"))

    def test_distant_fragments_cannot_be_joined_into_an_invented_seed(self):
        words = [{"text": "GYH", "x": 0, "y": 5, "height": 20, "width": 36}, {"text": "WXG", "x": 600, "y": 5, "height": 20, "width": 36}]
        self.assertEqual(screen_ocr.extract_gene_sequences(screen_ocr._words_in_screen_order(words)), ())


class GeneImagePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf"
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    @staticmethod
    def image(genes="GYYGHX", *, badges=True, pixels=20, noise=False, blank_position=None, width=900, height=110, badge_size=42, background="#20262c", glyph_colors=None):
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor(background))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont("Arial")
        font.setBold(True)
        font.setPixelSize(pixels)
        painter.setFont(font)
        pitch, size = (badge_size + 10, badge_size) if badges else (32, 30)
        for column, gene in enumerate(genes):
            x, y = 20 + column * pitch, 20
            color = QColor("#66ad55" if gene in "GYH" else "#cb6262")
            if badges:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(x, y, size, size)
                painter.setPen(QColor((glyph_colors or {}).get(column, "white")))
            else:
                painter.setPen(color)
            if column != blank_position:
                painter.drawText(x, y, size, size, Qt.AlignmentFlag.AlignCenter, gene)
        if noise:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#66ad55"))
            painter.drawEllipse(width - 80, 20, 42, 42)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    def test_two_adjacent_badge_groups_are_not_discarded_as_twelve_letters(self):
        prepared = screen_ocr._prepare_badge_image(self.image("GYYGHXWWYGGY"))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((1, 1, 1, 1, 1, 2), (2, 2, 1, 1, 1, 1)))
        self.assertTrue(prepared.image.startswith(b"\x89PNG"))

    def test_unrelated_colored_icon_does_not_discard_the_gene_group(self):
        prepared = screen_ocr._prepare_badge_image(self.image(noise=True))
        self.assertIsNotNone(prepared)
        self.assertEqual(len(prepared.colors), 1)

    def test_blank_badge_keeps_boundaries_and_does_not_discard_neighbor_seed(self):
        prepared = screen_ocr._prepare_badge_image(self.image("GYYGHXWWYGGY", blank_position=2))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((2, 2, 1, 1, 1, 1),))

    def test_seven_contiguous_badges_are_not_truncated_into_six(self):
        self.assertIsNone(screen_ocr._prepare_badge_image(self.image("GYYGHXY")))

    def test_small_colored_letters_are_enlarged_without_badge_fill_assumptions(self):
        for pixels in (12, 16, 20):
            for genes in ("GYYGHX", "WWYGGY", "HHWXYG"):
                with self.subTest(pixels=pixels, genes=genes):
                    prepared = screen_ocr._prepare_badge_image(self.image(genes, badges=False, pixels=pixels))
                    self.assertIsNotNone(prepared)
                    self.assertEqual(prepared.colors, (tuple(1 if gene in "GYH" else 2 for gene in genes),))

    def test_full_hd_capture_still_receives_color_enhancement(self):
        prepared = screen_ocr._prepare_badge_image(self.image(width=1920, height=1080))
        self.assertIsNotNone(prepared)
        self.assertEqual(len(prepared.colors), 1)

    def test_real_rust_detail_crop_locates_all_small_badges(self):
        # This 145x30 crop contains only the six genes, with no player name,
        # inventory, chat, or other screen content from the source screenshot.
        prepared = screen_ocr._prepare_badge_image(RUST_DETAIL_FIXTURE.read_bytes())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((2, 1, 1, 2, 1, 2),))

    def test_real_wheat_detail_keeps_all_six_colored_positions(self):
        # Only the anonymous 155x30 gene strip is stored, not the player or
        # inventory. The two tiny X glyphs previously became parentheses.
        prepared = screen_ocr._prepare_badge_image(RUST_WHEAT_DETAIL_FIXTURE.read_bytes())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((1, 2, 1, 2, 1, 1),))

    def test_small_white_badges_keep_one_interpretation_across_all_six_positions(self):
        for genes in ("WHYXGW", "HHHHHH", "GGGGGG"):
            with self.subTest(genes=genes):
                prepared = screen_ocr._prepare_badge_image(self.image(genes, pixels=12, badge_size=14, background="#64605b"))
                self.assertIsNotNone(prepared)
                self.assertEqual(prepared.colors, (tuple(1 if gene in "GYH" else 2 for gene in genes),))

    def test_bare_colored_letters_on_white_background_are_not_inverted(self):
        prepared = screen_ocr._prepare_badge_image(self.image("HHWXYG", badges=False, pixels=20, background="white"))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((1, 1, 2, 2, 1, 1),))

    def test_unreadable_low_contrast_badges_do_not_discard_the_neighbor_seed(self):
        prepared = screen_ocr._prepare_badge_image(self.image("GYYGHXWWYGGY", glyph_colors={index: "#6e6e6e" for index in range(6)}))
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.colors, ((2, 2, 1, 1, 1, 1),))

    def test_split_colored_badge_arc_merges_without_joining_whole_letters(self):
        pieces = [(10, 10, 3, 11, 20, 1), (14, 7, 14, 18, 100, 1), (60, 7, 14, 18, 100, 1)]
        self.assertEqual(screen_ocr._merge_badge_fragments(pieces), [(10, 7, 18, 18, 120, 1), pieces[2]])
        whole_letters = [(10, 10, 8, 14, 50, 1), (19, 10, 8, 14, 50, 1)]
        self.assertEqual(screen_ocr._merge_badge_fragments(whole_letters), whole_letters)

    def test_real_detail_ui_scaling_keeps_the_six_badge_positions(self):
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
        source = QImage(str(RUST_DETAIL_FIXTURE))
        for scale in (1.25, 2):
            image = source.scaled(round(source.width() * scale), round(source.height() * scale), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            prepared = screen_ocr._prepare_badge_image(bytes(buffer.data()))
            with self.subTest(scale=scale):
                self.assertIsNotNone(prepared)
                self.assertEqual(prepared.colors, ((2, 1, 1, 2, 1, 2),))

    @classmethod
    def four_k_image(cls, *, colored=True, plain=True):
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter
        image = QImage(3840, 2160, QImage.Format.Format_RGB32)
        image.fill(QColor("#242424"))
        painter = QPainter(image)
        if colored:
            # A small real-size tooltip far from the top-left corner. The
            # full screen must not be resized before locating these glyphs.
            painter.drawImage(3000, 1950, QImage.fromData(cls.image(pixels=16)))
        if plain:
            font = QFont("Arial")
            font.setPixelSize(32)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(150, 200, 600, 80, Qt.AlignmentFlag.AlignLeft, "WWYGGY")
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    def test_automatic_blank_4k_screen_returns_without_starting_ocr(self):
        with patch.object(screen_ocr, "_powershell_path", side_effect=AssertionError("An empty scan must not start OCR.")), patch.object(screen_ocr, "_recognize_image_text") as recognize:
            self.assertEqual(screen_ocr.recognize_gene_image(self.four_k_image(colored=False, plain=False), colored_only=True), ())
        recognize.assert_not_called()

    def test_automatic_screen_ignores_unrelated_plain_text_gene_strings(self):
        with patch.object(screen_ocr, "_powershell_path", side_effect=AssertionError("Plain screen text must not start OCR.")), patch.object(screen_ocr, "_recognize_image_text") as recognize:
            self.assertEqual(screen_ocr.recognize_gene_image(self.four_k_image(colored=False), colored_only=True), ())
        recognize.assert_not_called()

    def test_automatic_4k_screen_sends_only_located_colored_group_to_ocr(self):
        from PySide6.QtGui import QImage
        screenshot = self.four_k_image()

        def recognize(image, executable, timeout):
            self.assertNotEqual(image, screenshot)
            enhanced = QImage.fromData(image)
            self.assertLessEqual(enhanced.width(), 680)
            self.assertGreater(enhanced.width(), 100)
            self.assertEqual(enhanced.height(), 152)
            self.assertGreater(timeout, 0)
            self.assertLess(timeout, 15)
            return "GYYGHX"

        with patch.object(screen_ocr, "_powershell_path", return_value=Path("powershell.exe")), patch.object(screen_ocr, "_recognize_image_text", side_effect=recognize) as engine:
            self.assertEqual(screen_ocr.recognize_gene_image(screenshot, colored_only=True), ("GYYGHX",))
        self.assertEqual(engine.call_count, 1)

    def test_automatic_locator_enforces_deadline_before_engine_start(self):
        screenshot = self.image()
        with patch.object(screen_ocr.time, "monotonic", side_effect=[100.0, 115.1]), patch.object(screen_ocr, "_powershell_path") as executable:
            with self.assertRaisesRegex(screen_ocr.OcrError, "超时"):
                screen_ocr.recognize_gene_image(screenshot, colored_only=True)
        executable.assert_not_called()


class WindowsOcrProcessTests(unittest.TestCase):
    def setUp(self):
        real_temporary_directory = tempfile.TemporaryDirectory
        self.folder = real_temporary_directory(prefix=".ocr-test-", dir=PROJECT_ROOT)
        self.addCleanup(self.folder.cleanup)
        temporary = patch.object(screen_ocr.tempfile, "TemporaryDirectory", side_effect=lambda **kwargs: real_temporary_directory(dir=self.folder.name, **kwargs))
        executable = patch.object(screen_ocr, "_powershell_path", return_value=Path("powershell.exe"))
        fallback = patch.object(screen_ocr, "_prepare_badge_image", return_value=None)
        process = patch.object(screen_ocr.subprocess, "run")
        for replacement in (temporary, executable, fallback, process):
            self.addCleanup(replacement.stop)
        temporary.start()
        executable.start()
        self.fallback = fallback.start()
        self.process = process.start()
        self.process.return_value = self.result({"text": "GYYGHX WWYGGY"})

    @staticmethod
    def result(payload, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(payload), "")

    def test_recognizes_offline_and_removes_temporary_png_after_success(self):
        written_paths = []

        def recognize(*args, **kwargs):
            path = Path(kwargs["env"]["RUST_SEED_OCR_IMAGE"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), PNG)
            self.assertNotIn(str(path), " ".join(args[0]))
            self.assertLessEqual(kwargs["timeout"], 15)
            written_paths.append(path)
            return self.result({"text": "GYYGHX WWYGGY"})

        self.process.side_effect = recognize
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ("GYYGHX", "WWYGGY"))
        self.assertTrue(written_paths)
        self.assertFalse(written_paths[0].exists())
        self.fallback.assert_called_once_with(PNG)

    def test_no_genes_is_a_successful_empty_result(self):
        self.process.return_value = self.result({"text": "unrelated page content"})
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ())
        self.fallback.assert_called_once_with(PNG)

    def test_badge_cleanup_is_recognized_in_a_second_local_pass(self):
        cleaned = PNG + b"cleaned"
        self.fallback.return_value = screen_ocr._PreparedGenes(cleaned, ((1, 1, 1, 1, 1, 2), (2, 2, 1, 1, 1, 1)))
        self.process.side_effect = [self.result({"text": ""}), self.result({"text": "G Y Y G H X\nW W Y G G Y"})]
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ("GYYGHX", "WWYGGY"))
        self.assertEqual(self.process.call_count, 2)

    def test_color_pass_checks_even_an_apparently_valid_first_ocr_result(self):
        self.fallback.return_value = screen_ocr._PreparedGenes(PNG, ((1, 1, 1, 1, 1, 2),))
        self.process.side_effect = [self.result({"text": "GYYGXX"}), self.result({"text": "GYYGHX"})]
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ("GYYGHX",))
        self.assertEqual(self.process.call_count, 2)

    def test_conflicting_color_rejects_a_legal_but_wrong_letter_without_guessing(self):
        self.fallback.return_value = screen_ocr._PreparedGenes(PNG, ((1, 1, 1, 1, 1, 2),))
        self.process.side_effect = [self.result({"text": "GYYGXX"}), self.result({"text": "GYYGXX"})]
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ())

    def test_missing_first_ocr_row_does_not_shift_second_row_colors(self):
        self.fallback.return_value = screen_ocr._PreparedGenes(PNG, ((1, 1, 1, 1, 1, 1), (2, 2, 1, 1, 1, 1)))
        self.process.side_effect = [self.result({"text": ""}), self.result({"text": "WWYGGY", "words": [{"text": "WWYGGY", "x": 20, "y": 160, "width": 500, "height": 48}]})]
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ("WWYGGY",))

    def test_small_badge_word_fragments_rejoin_only_within_their_prepared_row(self):
        self.fallback.return_value = screen_ocr._PreparedGenes(PNG, ((1, 1, 1, 1, 1, 2), (2, 2, 1, 1, 1, 1)))
        words = [{"text": gene, "x": 40 + column * 106, "y": 50 + row * 128, "width": 15, "height": 18} for row, genes in enumerate(("GYYGHX", "WWYGGY")) for column, gene in enumerate(genes)]
        # Large screen gaps remain separate for ordinary OCR; generated image
        # row positions provide the additional evidence needed to rejoin them.
        self.assertEqual(screen_ocr.extract_gene_sequences(screen_ocr._words_in_screen_order(words)), ())
        self.process.side_effect = [self.result({"text": ""}), self.result({"text": "", "words": words})]
        self.assertEqual(screen_ocr.recognize_gene_image(PNG), ("GYYGHX", "WWYGGY"))

    def test_total_deadline_is_checked_after_the_second_engine_call(self):
        self.fallback.return_value = screen_ocr._PreparedGenes(PNG, ((1, 1, 1, 1, 1, 2),))
        self.process.side_effect = [self.result({"text": ""}), self.result({"text": "GYYGHX"})]
        with patch.object(screen_ocr.time, "monotonic", side_effect=[100.0, 100.1, 115.1]):
            with self.assertRaisesRegex(screen_ocr.OcrError, "超时"):
                screen_ocr.recognize_gene_image(PNG)

    def test_badge_pass_honors_total_fifteen_second_deadline(self):
        self.process.return_value = self.result({"text": ""})
        self.fallback.return_value = PNG
        with patch.object(screen_ocr.time, "monotonic", side_effect=[100.0, 115.1]):
            with self.assertRaisesRegex(screen_ocr.OcrError, "超时"):
                screen_ocr.recognize_gene_image(PNG)
        self.assertEqual(self.process.call_count, 1)

    def test_timeout_removes_image_and_reports_helpful_error(self):
        self.process.side_effect = subprocess.TimeoutExpired("powershell", 15)
        with self.assertRaisesRegex(screen_ocr.OcrError, "超时"):
            screen_ocr.recognize_gene_image(PNG)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_process_launch_failure_is_a_user_readable_ocr_error(self):
        self.process.side_effect = OSError("PowerShell launch failed")
        with self.assertRaisesRegex(screen_ocr.OcrError, "无法运行"):
            screen_ocr.recognize_gene_image(PNG)

    def test_missing_language_and_engine_errors_are_reported(self):
        self.process.return_value = self.result({"error": "No Windows OCR language is installed."}, 1)
        with self.assertRaisesRegex(screen_ocr.OcrError, "(?s)No Windows OCR language.*安装英语"):
            screen_ocr.recognize_gene_image(PNG)

    def test_malformed_process_output_is_rejected(self):
        results = [
            subprocess.CompletedProcess([], 1, "not JSON", "blocked"),
            self.result(["GYYGHX"]),
            self.result({"text": None}),
            self.result({"error": "Engine failed"}),
        ]
        for result in results:
            with self.subTest(output=result.stdout), self.assertRaises(screen_ocr.OcrError):
                self.process.return_value = result
                screen_ocr.recognize_gene_image(PNG)

    def test_invalid_or_oversized_png_is_rejected_before_launch(self):
        for image in (b"", b"JPEG", None, PNG + bytes(screen_ocr._MAX_IMAGE_BYTES)):
            with self.subTest(kind=type(image)), self.assertRaises(screen_ocr.OcrError):
                screen_ocr.recognize_gene_image(image)
        self.process.assert_not_called()


@unittest.skipUnless(os.name == "nt" and os.environ.get("RUST_SEED_TEST_WINDOWS_OCR") == "1", "Opt-in offline Windows OCR smoke check.")
class WindowsOcrRealBadgeTests(unittest.TestCase):
    def test_real_rust_detail_crop_recognizes_exact_gene_order(self):
        GeneImagePreparationTests.setUpClass()
        image = RUST_DETAIL_FIXTURE.read_bytes()
        self.assertEqual(screen_ocr.recognize_gene_image(image, colored_only=True), ("WHYXGW",))
        self.assertEqual(screen_ocr.recognize_gene_image(image), ("WHYXGW",))

    def test_real_wheat_detail_recognizes_x_without_letter_substitution(self):
        GeneImagePreparationTests.setUpClass()
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
        source = QImage(str(RUST_WHEAT_DETAIL_FIXTURE))
        for scale in (1, 1.25, 2):
            image = source.scaled(round(source.width() * scale), round(source.height() * scale), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            for colored_only in (True, False):
                with self.subTest(scale=scale, colored_only=colored_only):
                    self.assertEqual(screen_ocr.recognize_gene_image(bytes(buffer.data()), colored_only=colored_only), ("GXGXGH",))

    def test_automatic_4k_scan_recognizes_only_colored_genes(self):
        GeneImagePreparationTests.setUpClass()
        screenshot = GeneImagePreparationTests.four_k_image()
        self.assertEqual(screen_ocr.recognize_gene_image(screenshot, colored_only=True), ("GYYGHX",))

    def test_windows_recognizes_white_and_dark_glyphs_on_colored_badges(self):
        # This is an actual Windows engine call, not a mocked OCR response.
        # Regular tests do not require a particular installed OCR language.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
        from PySide6.QtWidgets import QApplication

        qt = QApplication.instance() or QApplication([])
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf"
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
        with tempfile.TemporaryDirectory(prefix=".ocr-real-", dir=PROJECT_ROOT) as folder:
            with patch.object(tempfile, "tempdir", folder):
                for color in ("white", "#16211a"):
                    for font_pixels in (16, 24):
                        with self.subTest(color=color, font_pixels=font_pixels):
                            image = QImage(430, 150, QImage.Format.Format_RGB32)
                            image.fill(QColor("#20262c"))
                            painter = QPainter(image)
                            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                            font = QFont("Arial")
                            font.setPixelSize(font_pixels)
                            font.setBold(True)
                            painter.setFont(font)
                            for row, genes in enumerate(("GYYGHX", "WWYGGY")):
                                for column, gene in enumerate(genes):
                                    x, y = 20 + column * 52, 20 + row * 58
                                    painter.setPen(Qt.PenStyle.NoPen)
                                    painter.setBrush(QColor("#66ad55" if gene in "GYH" else "#cb6262"))
                                    painter.drawEllipse(x, y, 42, 42)
                                    painter.setPen(QColor(color))
                                    painter.drawText(x, y, 42, 42, Qt.AlignmentFlag.AlignCenter, gene)
                            painter.end()
                            buffer = QBuffer()
                            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                            image.save(buffer, "PNG")
                            self.assertEqual(screen_ocr.recognize_gene_image(bytes(buffer.data())), ("GYYGHX", "WWYGGY"))
        self.assertIsNotNone(qt)


if __name__ == "__main__":
    unittest.main()
