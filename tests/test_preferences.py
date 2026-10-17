from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from rust_breeder.preferences import preferred_language, save_language

ROOT = Path(__file__).resolve().parents[1]


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'artifacts', prefix='preferences-')
        self.path = Path(self.temp.name) / 'data/settings.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_all_languages_round_trip_and_preserve_other_preferences(self):
        self.assertEqual(preferred_language(self.path), 'zh_CN')
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'other': {'value': '原有设置'}}), encoding='utf-8')
        for code in ('zh_CN', 'en', 'ru'):
            save_language(self.path, code)
            self.assertEqual(preferred_language(self.path), code)
            self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['other'], {'value': '原有设置'})

    def test_bad_language_or_file_falls_back_to_chinese(self):
        self.path.parent.mkdir()
        for value in ('{', '[]', '{"language":[]}', '{"language":"invalid"}', ' ' * (65537)):
            self.path.write_text(value, encoding='utf-8')
            self.assertEqual(preferred_language(self.path), 'zh_CN')

    def test_failed_replace_preserves_previous_language(self):
        save_language(self.path, 'en')
        before = self.path.read_bytes()
        with patch('rust_breeder.preferences.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                save_language(self.path, 'ru')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
