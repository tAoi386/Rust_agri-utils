"""Recovery must preserve user stock and never substitute a different project."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rust_breeder.project_library import ProjectLibrary
from rust_breeder.preferences import preferred_language
from rust_breeder.storage import ProjectError, load_project, save_project, validate_project

ROOT = Path(__file__).resolve().parents[1]

class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='storage-recovery-', dir=ROOT / 'artifacts')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = {'version': 1, 'inventory': [{'id': 'one', 'genes': 'GYYGGG', 'name': 'original', 'quantity': 41}],
                     'target': 'GGGGGG', 'target_mode': 'counts', 'slots': [None] * 9, 'crop': 'yellow'}

    def test_corrupt_index_recovers_verified_projects_and_matching_active_autosave(self):
        library = ProjectLibrary(self.root)
        first = library.create('First project', self.data)
        second = library.create('Other project', dict(self.data, inventory=[]))
        save_project(self.root / 'autosave.json', dict(self.data, local_project_id=first))
        damaged = b'{broken library'
        library.index_path.write_bytes(damaged)
        restored = ProjectLibrary(self.root)
        self.assertEqual({row['id'] for row in restored.entries}, {first, second})
        self.assertEqual(restored.current_id, first)
        self.assertEqual(restored.name(first), 'First project')
        self.assertEqual(restored.load(first)['inventory'], self.data['inventory'])
        self.assertTrue(restored.recovery_messages)
        self.assertIn(damaged, [path.read_bytes() for path in (restored.root / 'recovery').iterdir()])

    def test_corrupt_current_project_uses_only_its_matching_autosave_and_preserves_bad_file(self):
        library = ProjectLibrary(self.root)
        project_id = library.create('Current', self.data)
        save_project(self.root / 'autosave.json', dict(self.data, local_project_id=project_id))
        damaged = b'not project json'
        library.path(project_id).write_bytes(damaged)
        restored = library.load(project_id)
        self.assertEqual(restored['inventory'], self.data['inventory'])
        self.assertEqual(library.path(project_id).read_bytes(), damaged)
        self.assertIn(damaged, [path.read_bytes() for path in (library.root / 'recovery').iterdir()])
        library.save(project_id, restored)
        self.assertEqual(library.load(project_id)['inventory'], self.data['inventory'])

    def test_unrelated_autosave_cannot_replace_a_corrupt_project(self):
        library = ProjectLibrary(self.root)
        first = library.create('First', self.data)
        second = library.create('Second', dict(self.data, inventory=[]))
        save_project(self.root / 'autosave.json', dict(self.data, local_project_id=first))
        library.path(second).write_bytes(b'broken second')
        with self.assertRaises(ProjectError):
            library.load(second)
        self.assertEqual(library.path(second).read_bytes(), b'broken second')

    def test_deep_state_failure_can_recover_without_losing_existing_stock(self):
        library = ProjectLibrary(self.root)
        project_id = library.create('Current', self.data)
        save_project(self.root / 'autosave.json', dict(self.data, local_project_id=project_id))
        damaged = dict(self.data, local_project_id=project_id, parallel={'version': 999})
        library.path(project_id).write_text(json.dumps(damaged), encoding='utf-8')
        restored = library.load(project_id)
        self.assertEqual(restored['inventory'], self.data['inventory'])
        self.assertNotIn('parallel', restored)

    def test_invalid_nested_states_are_rejected_before_overwriting_a_good_project(self):
        path = self.root / 'good.json'
        save_project(path, self.data)
        before = path.read_bytes()
        for key in ('journey', 'parallel'):
            with self.subTest(key=key):
                damaged = dict(self.data, **{key: {'version': 999}})
                with self.assertRaises(ProjectError):
                    save_project(path, damaged)
                self.assertEqual(path.read_bytes(), before)

    def test_external_validator_failure_also_uses_only_matching_recovery(self):
        library = ProjectLibrary(self.root)
        project_id = library.create('Current', self.data)
        save_project(self.root / 'autosave.json', dict(self.data, local_project_id=project_id))
        altered = dict(self.data, local_project_id=project_id, marker='reject')
        save_project(library.path(project_id), altered)
        def validate(data):
            if data.get('marker') == 'reject':
                raise ValueError('invalid extra state')
        self.assertNotIn('marker', library.load(project_id, validator=validate))

    def test_failed_index_save_keeps_current_selection_and_metadata_in_memory(self):
        library = ProjectLibrary(self.root)
        first = library.create('First', self.data)
        second = library.create('Second', self.data)
        before = deepcopy(library.entries)
        with patch.object(library, '_write_index', side_effect=ProjectError('disk unavailable')):
            with self.assertRaises(ProjectError):
                library.save(first, self.data)
        self.assertEqual(library.current_id, second)
        self.assertEqual(library.entries, before)

    def test_excessively_nested_preferences_do_not_crash_startup(self):
        path = self.root / 'settings.json'
        path.write_text('{"nested":' + '[' * 10000 + '0' + ']' * 10000 + '}', encoding='utf-8')
        self.assertEqual(preferred_language(path), 'zh_CN')

    def test_explicit_empty_recovery_keeps_unreadable_index_and_project_bytes(self):
        folder = self.root / 'projects'
        folder.mkdir()
        (folder / 'library.json').write_bytes(b'broken index')
        broken_project = folder / ('a' * 32 + '.json')
        broken_project.write_bytes(b'broken project')
        with self.assertRaises(ProjectError):
            ProjectLibrary(self.root)
        library = ProjectLibrary(self.root, recover_empty=True)
        self.assertEqual(library.entries, [])
        self.assertEqual(library.current_id, '')
        self.assertEqual(broken_project.read_bytes(), b'broken project')
        self.assertIn(b'broken index', [p.read_bytes() for p in (folder / 'recovery').iterdir()])

    def test_nonfinite_preferences_are_ignored_and_language_save_remains_valid(self):
        from rust_breeder.preferences import save_language
        path = self.root / 'settings.json'
        path.write_text('{"language":"en","invalid":NaN}', encoding='utf-8')
        self.assertEqual(preferred_language(path), 'zh_CN')
        save_language(path, 'ru')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'language': 'ru'})

    def test_pretty_preferences_cannot_exceed_read_limit_or_destroy_prior_settings(self):
        from rust_breeder.preferences import save_language
        path = self.root / 'settings.json'
        values = {'many': list(range(10000)), 'language': 'en'}
        raw = json.dumps(values, separators=(',', ':'))
        self.assertLess(len(raw), 64 * 1024)
        path.write_text(raw, encoding='utf-8')
        before = path.read_bytes()
        with self.assertRaises(OSError):
            save_language(path, 'ru')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(preferred_language(path), 'en')

if __name__ == '__main__':
    unittest.main()
