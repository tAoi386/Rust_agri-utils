"""Exercise the shipped executable, including both isolated planning jobs."""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
import traceback

from . import __version__
from .i18n import localize_tree, set_language
from .models import InventoryItem, Target
from .parallel import validate_parallel_report
from .planner_process import plan_parallel_process, plan_route_process


def check_package(window, app, base: Path):
    output = base / 'artifacts'
    output.mkdir(exist_ok=True)
    result = {'ok': False, 'version': __version__}
    window.resize(1280, 800)
    try:
        rows = (InventoryItem('center', 'GYYGGG', 1, 'Seed A'),
                InventoryItem('a', 'YHYGGG', 1, 'Seed B'),
                InventoryItem('b', 'YGGGGG', 1, 'Seed C'))
        report = plan_route_process(rows, Target(), max_rounds=2, max_seconds=2,
                                    max_combinations=120000, objective='plants')
        assert report.found and report.best is not None, 'Sequential child process found no fixture route.'
        window.inventory = list(rows)
        window.refresh_inventory()
        page = window.guided
        page.accept_route(report)
        assert page.state.pending is not None and page.stage == 2, 'Result entry is not immediate.'
        predicted = page.state.pending.predicted_pattern
        before = tuple(window.inventory)
        page.completed_check.setCurrentIndex(page.completed_check.findData(True))
        page.actual_edit.setText('XXXXXX')
        page.complete_current_round()
        assert tuple(window.inventory) == before and not page.state.rounds, 'Mismatch changed stock.'
        page.actual_edit.setText(predicted)
        page.actual_quantity.setValue(3)
        assert page.commit_button.isEnabled(), 'Matching actual result cannot be confirmed.'
        for code in ('zh_CN', 'en', 'ru'):
            set_language(code)
            localize_tree(window)
            app.processEvents()
            assert page.actual_edit.text() == predicted, 'Language switch changed the actual gene draft.'
            assert window.inventory == list(rows), 'Language switch changed inventory.'
            window.grab().save(str(output / f'packaged-sequential-{code}.png'))
        set_language('zh_CN')
        page.complete_current_round()
        assert len(page.state.rounds) == 1 and window.inventory[-1].quantity == 3, 'Sequential accounting failed.'
        assert Target().matches(window.inventory[-1].genes), 'Sequential actual output is not 3Y3G.'
        app.processEvents()
        window.grab().save(str(output / 'packaged-history.png'))
        page.undo_completed_round()
        assert tuple(window.inventory) == rows and page.state.pending is not None, 'Sequential undo failed.'
        page.reset_pending()
        result['sequential_process_and_confirmation'] = True

        split = tuple(InventoryItem(f'raw-{index}', genes, 1, f'Seed {index + 1}')
                      for index, genes in enumerate(('HGYGHG', 'HGGGYY', 'HYYGYG',
                                                     'YHYYHH', 'GYHYYY', 'YYHYYY', 'YHYYHG')))
        blank = {'version': 1, 'inventory': [asdict(row) for row in split],
                 'target': 'YYYGGG', 'target_mode': 'counts', 'crop': 'hemp',
                 'slots': [None] * 9, 'workspace': 'parallel'}
        window.apply_project(blank)
        parallel = window.parallel
        parallel_report = plan_parallel_process(split, Target(), box_count=3, main_box=1,
                                                 max_waves=2, max_donors=2, max_seconds=4,
                                                 max_combinations=120000)
        assert parallel_report.found and len(parallel_report.waves) == 2, 'Parallel fixture has no two-batch route.'
        assert len(parallel_report.waves[0].steps) == 2, 'Parallel fixture did not branch in the first batch.'
        validate_parallel_report(parallel_report, split, Target())
        parallel.accept_route(parallel_report)
        assert parallel.state.pending is not None, 'Parallel report was not applied.'
        first_steps = parallel._current_steps()
        first_page = parallel.box_pages[first_steps[0].box_id]
        first_page.actual_edit.setText('XXXXXX')
        parallel.complete_current_wave()
        assert tuple(window.inventory) == split and not parallel.state.waves, 'Parallel mismatch changed stock.'
        for step in first_steps:
            parallel.box_pages[step.box_id].actual_edit.setText(step.expected_genes)
        assert parallel.commit_button.isEnabled(), 'Matching parallel actuals cannot be confirmed.'
        for code in ('zh_CN', 'en', 'ru'):
            set_language(code)
            localize_tree(window)
            app.processEvents()
            window.grab().save(str(output / f'packaged-parallel-{code}.png'))
            assert all(parallel.box_pages[s.box_id].actual_edit.text() == s.expected_genes for s in first_steps)
        set_language('zh_CN')
        # Test accounting before starting the normal automatic next search.
        normal_start = parallel.start_planning
        parallel.start_planning = lambda: None
        try:
            parallel.complete_current_wave()
        finally:
            parallel.start_planning = normal_start
        assert len(parallel.state.waves) == 1, 'Parallel first batch did not commit.'
        next_report = plan_parallel_process(tuple(window.inventory), Target(), box_count=3, main_box=1,
                                            max_waves=1, max_donors=2, max_seconds=2,
                                            max_combinations=120000)
        assert next_report.found, 'Actual first-batch clones cannot finish the route.'
        parallel.accept_route(next_report)
        for step in parallel._current_steps():
            parallel.box_pages[step.box_id].actual_edit.setText(step.expected_genes)
        parallel.complete_current_wave()
        assert len(parallel.state.waves) == 2, 'Parallel final batch did not commit.'
        assert any(row.quantity > 0 and Target().matches(row.genes) for row in window.inventory), 'Parallel final stock has no 3Y3G clone.'
        parallel.undo_completed_wave()
        assert len(parallel.state.waves) == 1 and parallel.state.pending is not None, 'Parallel batch undo failed.'
        result.update(parallel_process_and_confirmation=True,
                      parallel_first_batch_outputs=[s.expected_genes for s in first_steps],
                      parallel_final_output=next_report.waves[-1].steps[-1].expected_genes,
                      languages=['zh_CN', 'en', 'ru'], window_title=window.windowTitle())
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter
        from .capture_dialog import ScreenCaptureDialog, CapturePreviewDialog, image_png
        image = QImage(720, 320, QImage.Format.Format_RGB32)
        image.fill(QColor('#3b4241'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont('Arial', 28, QFont.Weight.Bold))
        for index, gene in enumerate('WHYXGW'):
            rect = (62 + index * 100, 120, 74, 74)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#77a649' if gene in 'GYH' else '#bb6254'))
            painter.drawEllipse(*rect)
            painter.setPen(QColor('white'))
            painter.drawText(*rect, Qt.AlignmentFlag.AlignCenter, gene)
        painter.end()
        capture = ScreenCaptureDialog((), 'Hemp', window)
        large = None
        try:
            capture.set_preview(image)
            capture.show()
            app.processEvents()
            assert capture.preview.height() >= 200, 'Capture preview is still a thumbnail.'
            for code in ('zh_CN', 'en', 'ru'):
                set_language(code)
                localize_tree(capture)
                app.processEvents()
                assert capture.preview.source_image() == image, 'Preview changed source pixels.'
                capture.grab().save(str(output / f'packaged-capture-{code}.png'))
            large = CapturePreviewDialog(image, capture)
            large.show()
            large.zoom.setCurrentIndex(large.zoom.findData(2))
            app.processEvents()
            assert abs(large.preview.pixmap().width() - image.width() * 2) <= 2, 'Pixel zoom is incorrect.'
            import os
            if os.name == 'nt' and os.environ.get('RUST_SEED_TEST_WINDOWS_OCR') == '1':
                from .screen_ocr import recognize_gene_image
                assert recognize_gene_image(image_png(image), colored_only=True) == ('WHYXGW',), 'Packaged Windows OCR failed.'
                result['capture_real_ocr'] = True
                fixture_folder = os.environ.get('RUST_SEED_TEST_OCR_FIXTURES')
                if fixture_folder:
                    samples = {
                        'rust-detail-genes.png': 'WHYXGW',
                        'rust-wheat-detail-genes.png': 'GXGXGH',
                        'rust-hemp-detail-genes.png': 'XYHYXH',
                    }
                    for name, expected in samples.items():
                        png = (Path(fixture_folder) / name).read_bytes()
                        assert recognize_gene_image(png, colored_only=True) == (expected,), f'Packaged real-game OCR failed: {name}'
                    result['capture_real_game_samples'] = list(samples.values())
            result['capture_preview_and_zoom'] = True
        finally:
            if large is not None:
                large.close()
                large.deleteLater()
            capture.close()
            capture.deleteLater()
            set_language('zh_CN')
        result['ok'] = True
    except Exception as exc:
        result.update(error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
        logging.getLogger(__name__).exception('Packaged application check failed')
    finally:
        (output / 'packaged-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        window.autosave_timer.stop()
        window.dirty = False
        window.close()
        app.exit(0 if result['ok'] else 1)
