import json
from pathlib import Path
import tempfile
import unittest

from rust_breeder.project_library import ProjectLibrary
from rust_breeder.storage import ProjectError, save_project


class ProjectLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='project-library-', dir=Path(__file__).resolve().parents[1] / 'artifacts')
        self.root = Path(self.temp.name)
        self.data = {'version': 1, 'inventory': [{'id': 'one', 'genes': 'GYYGGG', 'name': '苗', 'quantity': 2}],
                     'target': 'YYYGGG', 'target_mode': 'counts', 'slots': [None] * 9, 'crop': 'blue'}

    def tearDown(self):
        self.temp.cleanup()

    def test_two_projects_save_reopen_and_keep_independent_stock(self):
        library = ProjectLibrary(self.root)
        first = library.create('蓝莓', self.data)
        second = library.create('新试验', dict(self.data, inventory=[]))
        library.save(first, self.data)
        restored = ProjectLibrary(self.root)
        self.assertEqual(restored.current_id, first)
        self.assertEqual(restored.load(first)['inventory'], self.data['inventory'])
        self.assertEqual(restored.load(second)['inventory'], [])
        self.assertEqual(restored.name(second), '新试验')
        self.assertFalse(list(restored.root.glob('*.tmp')))

    def test_legacy_import_is_idempotent_and_preserves_active_project(self):
        source = self.root / '以前的项目.json'
        save_project(source, self.data)
        library = ProjectLibrary(self.root)
        active = library.create('当前', self.data)
        original = source.read_bytes()
        self.assertEqual(library.import_legacy([source]), 1)
        self.assertEqual(library.import_legacy([source]), 0)
        self.assertEqual(library.current_id, active)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(ProjectLibrary(self.root).entries), 2)

    def test_path_traversal_and_broken_index_are_rejected(self):
        library = ProjectLibrary(self.root)
        for value in ('../outside', 'C:/outside', '', None):
            with self.assertRaises(ProjectError):
                library.path(value)
        library.root.mkdir(parents=True)
        library.index_path.write_text(json.dumps({'projects': [{'id': '../x', 'name': 'x'}]}), encoding='utf-8')
        with self.assertRaises(ProjectError):
            ProjectLibrary(self.root)
