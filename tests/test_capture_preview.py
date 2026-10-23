"""Preview remains readable while OCR receives untouched source pixels."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.deps'))
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QDialog
from rust_breeder import capture_dialog as capture
from rust_breeder.i18n import set_language,tr,localize_tree


class CapturePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh_CN')
        self.dialog=capture.ScreenCaptureDialog((),'Hemp')
        self.image=QImage(540,356,QImage.Format.Format_RGB32)
        self.image.fill(QColor('#434947'))
        self.image.setPixelColor(200,145,QColor('#a4dc54'))

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.worker.wait(5000)
        self.app.processEvents()
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        set_language('zh_CN')

    def test_default_preview_is_larger_and_small_window_keeps_review_table(self):
        self.dialog.set_preview(self.image)
        self.dialog.show()
        self.app.processEvents()
        self.assertGreaterEqual(self.dialog.preview.height(),240)
        pixmap=self.dialog.preview.pixmap()
        self.assertGreater(pixmap.height()/pixmap.devicePixelRatio(),200)
        self.dialog.resize(740,650)
        self.app.processEvents()
        self.assertLessEqual(self.dialog.height(),650)
        self.assertGreaterEqual(self.dialog.table.height(),130)
        self.assertGreater(self.dialog.preview.height(),100)
        self.assertTrue(self.dialog.preview_button.isEnabled())

    def test_resize_preserves_original_pixels_and_clear_never_restores_stale_preview(self):
        self.image.setDevicePixelRatio(2.0)
        self.dialog.set_preview(self.image)
        original=self.dialog.preview.source_image()
        self.assertEqual(original.size(),self.image.size())
        self.assertEqual(self.image.devicePixelRatio(),2.0)
        for width,height in ((500,170),(900,350),(400,150),(900,350)):
            self.dialog.preview.resize(width,height)
            self.dialog.preview._render_image()
            self.assertEqual(self.dialog.preview.source_image(),original)
        self.dialog.preview.clear()
        self.dialog.preview.setText('限定范围预览')
        self.dialog.preview.resize(600,200)
        self.dialog.preview._render_image()
        self.assertTrue(self.dialog.preview.source_image().isNull())
        self.assertFalse(self.dialog.preview_button.isEnabled())
        self.assertEqual(self.dialog.preview.text(),'限定范围预览')

    def test_ocr_receives_full_resolution_while_display_is_resized(self):
        seen=[]
        def recognize(png,**kwargs):
            image=QImage.fromData(png)
            seen.append(image)
            return ('WHYXGW',)
        with patch.object(capture,'recognize_gene_image',side_effect=recognize):
            self.dialog.recognize_image(self.image)
            worker=self.dialog.worker
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0].size(),self.image.size())
        self.assertEqual(seen[0].pixelColor(200,145),self.image.pixelColor(200,145))
        self.assertEqual(self.dialog.table.item(0,1).text(),'WHYXGW')

    def test_enlarged_view_zoom_and_language_switch_preserve_source_and_fit(self):
        viewer=capture.CapturePreviewDialog(self.image,self.dialog)
        try:
            viewer.resize(700,500)
            viewer.show()
            self.app.processEvents()
            for factor in (1,2,4,1):
                viewer.zoom.setCurrentIndex(viewer.zoom.findData(factor))
                self.app.processEvents()
                self.assertFalse(viewer.area.widgetResizable())
                self.assertAlmostEqual(viewer.preview.pixmap().width(),self.image.width()*factor,delta=2)
                self.assertAlmostEqual(viewer.preview.pixmap().height(),self.image.height()*factor,delta=2)
                self.assertEqual(viewer.preview.source_image(),self.image)
            viewer.zoom.setCurrentIndex(viewer.zoom.findData(0))
            self.app.processEvents()
            self.assertTrue(viewer.area.widgetResizable())
            self.assertLessEqual(viewer.preview.width(),viewer.area.viewport().width())
            for code in ('en','ru','zh_CN'):
                set_language(code)
                self.assertEqual(viewer.zoom.itemText(0),tr('适应窗口'))
                self.assertEqual(viewer.preview.source_image(),self.image)
        finally:
            viewer.close()
            viewer.deleteLater()

    def test_enlarge_button_does_not_keep_completed_viewer_dialogs(self):
        self.dialog.set_preview(self.image)
        with patch.object(capture.CapturePreviewDialog,'exec',return_value=QDialog.DialogCode.Accepted):
            self.dialog.show_large_preview()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.assertEqual(self.dialog.findChildren(capture.CapturePreviewDialog),[])


    def test_changing_capture_screen_discards_old_preview(self):
        self.dialog.set_preview(self.image)
        self.dialog.screen_choice_changed()
        self.assertTrue(self.dialog.preview.source_image().isNull())
        self.assertFalse(self.dialog.preview_button.isEnabled())

    def test_enlarged_title_uses_original_source_after_foreign_language_start(self):
        set_language('en')
        viewer=capture.CapturePreviewDialog(self.image,self.dialog)
        try:
            self.assertEqual(viewer.windowTitle(),tr('识别画面 · 放大核对'))
            for code in ('ru','zh_CN','en'):
                set_language(code)
                localize_tree(viewer)
                self.assertEqual(viewer.windowTitle(),tr('识别画面 · 放大核对'))
        finally:
            viewer.close()
            viewer.deleteLater()


if __name__=='__main__':
    unittest.main()
