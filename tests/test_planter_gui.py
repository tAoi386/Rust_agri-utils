"""A planting diagram must keep each supplied genotype in its saved slot."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.deps'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

try:
    from PySide6.QtWidgets import QApplication
    from rust_breeder import app as application
    from rust_breeder.engine import predict
    from rust_breeder.journey import JourneyState, prepare_round
    from rust_breeder.models import InventoryItem, Recipe
    from rust_breeder.planter import positions_for
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, 'Qt is needed for planting diagram checks.')
class PlanterDiagramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='placement-', dir=ROOT / 'artifacts')
        self.autosave = patch.object(application, 'AUTOSAVE', Path(self.temp.name) / 'autosave.json')
        self.autosave.start()
        self.window = application.MainWindow(restore=False)

    def tearDown(self):
        self.window.autosave_timer.stop()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        self.autosave.stop()
        self.temp.cleanup()

    def arrange(self, count):
        center = InventoryItem('center', 'XXXXXX')
        choices = ('GYHHHH', 'YGGHHH', 'GGYHHH', 'YGYHHH', 'GGGYHH', 'YGGYHH', 'GYGYHH', 'YYGGHH')
        donors = tuple(InventoryItem(f'd{index}', genes) for index, genes in enumerate(choices[:count]))
        self.window.inventory = [center, *donors]
        self.window.refresh_inventory()
        guide = self.window.guided
        recipe = Recipe(center, donors, predict(center.genes, [row.genes for row in donors]), self.window.target())
        guide.state = prepare_round(JourneyState(target=recipe.target, center_id=center.id),
                                    self.window.inventory, recipe)
        guide.stage = 2
        guide.render_round()
        return guide, recipe

    def test_new_diagram_places_exact_genotypes_in_recommended_order(self):
        for count in range(1, 9):
            with self.subTest(count=count):
                guide, recipe = self.arrange(count)
                expected = {4: recipe.center.genes}
                expected.update((position, row.genes) for position, row in zip(positions_for(count), recipe.donors))
                for index, (_, genes, _) in enumerate(guide.cells):
                    self.assertEqual(genes.text(), expected.get(index, '留空'))
                self.assertEqual(sum(genes.text() != '留空' for _, genes, _ in guide.cells), count + 1)
                self.assertEqual(guide.current_recipe().prediction, recipe.prediction)
                self.assertEqual(tuple(self.window.inventory), (recipe.center, *recipe.donors))

    def test_new_four_corner_layout_survives_project_reload(self):
        guide, recipe = self.arrange(4)
        snapshot = self.window.snapshot()
        self.window.apply_project(snapshot)
        self.assertEqual(guide.state.pending.layout, 'auto')
        self.assertEqual(guide.stage, 2)
        self.assertEqual({i for i, (_, gene, _) in enumerate(guide.cells) if gene.text() != '留空'},
                         {0, 2, 4, 6, 8})
        self.assertIn('四角', guide.layout_label.text())
        self.assertIn('1、9、3、7', guide.instruction_body.text())
        self.assertEqual(guide.current_recipe().prediction, recipe.prediction)

    def test_already_planted_legacy_project_is_never_rearranged_on_load(self):
        guide, recipe = self.arrange(2)
        snapshot = self.window.snapshot()
        del snapshot['journey']['pending']['layout']
        snapshot['guided']['stage'] = 2
        self.window.apply_project(snapshot)
        self.assertEqual(guide.state.pending.layout, 'cross')
        self.assertEqual(guide.stage, 2)
        self.assertEqual(guide.cells[1][1].text(), recipe.donors[0].genes)
        self.assertEqual(guide.cells[3][1].text(), recipe.donors[1].genes)
        self.assertEqual(guide.cells[0][1].text(), '留空')
        self.assertIn('已保存', guide.layout_label.text())
        self.assertEqual(guide.current_recipe().prediction, recipe.prediction)
        self.assertFalse(guide.center_combo.isEnabled())
