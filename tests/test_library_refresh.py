import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.server import Library

FIXTURES = Path(__file__).parent / 'fixtures'


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'media'
        self.root.mkdir()
        self.data = base / 'data'
        self.first = self.root / '2026-09-20 12-00-00.png'
        self.second = self.root / '2026-09-20 13-00-00.jpg'
        shutil.copy2(FIXTURES / '01-pattern.png', self.first)
        shutil.copy2(FIXTURES / '02-blue.jpg', self.second)
        self.lib = Library(self.root, self.data)
        self.ids = [p['id'] for p in self.lib.photos]
        self.state = dict(revision=0, hidden=[self.ids[1]], active='post',
                          posts=[dict(id='post', title='История', caption='Подпись', photos=self.ids[:])])
        self.lib.save(self.state)
        self.saved = (self.data / 'project.json').read_bytes()

    def test_add_remove_restore_without_changing_drafts(self):
        self.first.unlink()
        new = self.root / '2026-09-20 11-00-00.MOV'
        shutil.copy2(FIXTURES / '03-motion.mov', new)
        self.lib.refresh()
        self.assertEqual(len(self.lib.photos), 3)
        self.assertTrue(self.lib.catalog[self.ids[0]]['missing'])
        self.assertEqual(self.lib.catalog[self.ids[0]]['name'], self.first.name)
        self.assertEqual(self.lib.photos[0]['name'], new.name)
        self.assertEqual((self.data / 'project.json').read_bytes(), self.saved)
        with self.assertRaisesRegex(ValueError, self.first.name):
            self.lib.export()
        self.assertFalse((self.root / 'Посты для Telegram').exists())
        shutil.copy2(FIXTURES / '01-pattern.png', self.first)
        self.lib.refresh()
        self.assertFalse(self.lib.catalog[self.ids[0]]['missing'])
        self.assertEqual(self.lib.state, self.state)

    def test_missing_files_survive_restart_and_allow_editing(self):
        self.first.unlink()
        self.second.unlink()
        reloaded = Library(self.root, self.data)
        self.assertTrue(all(p['missing'] for p in reloaded.photos))
        self.assertEqual(reloaded.state, self.state)
        value = copy.deepcopy(reloaded.state)
        value['posts'][0]['caption'] = 'Изменённая подпись'
        value['posts'][0]['photos'].reverse()
        reloaded.save(value)
        again = Library(self.root, self.data)
        self.assertEqual(again.state, value)
        self.assertEqual(again.state['hidden'], [self.ids[1]])

    def test_rename_is_new_file_not_relink(self):
        renamed = self.root / 'renamed.png'
        self.first.rename(renamed)
        self.lib.refresh()
        new_id = hashlib.sha256(renamed.name.encode()).hexdigest()[:20]
        self.assertTrue(self.lib.catalog[self.ids[0]]['missing'])
        self.assertFalse(self.lib.catalog[new_id]['missing'])
        self.assertNotIn(new_id, self.lib.state['posts'][0]['photos'])
        self.assertEqual(self.lib.state, self.state)

    def test_legacy_project_with_missing_references(self):
        (self.data / 'library.json').unlink()
        self.first.unlink()
        self.second.unlink()
        reloaded = Library(self.root, self.data)
        self.assertEqual(reloaded.state, self.state)
        self.assertTrue(all(p['missing'] for p in reloaded.photos))
        self.assertEqual({p['id'] for p in reloaded.photos}, set(self.ids))
        reloaded.save(copy.deepcopy(reloaded.state))
        shutil.copy2(FIXTURES / '01-pattern.png', self.first)
        reloaded.refresh()
        self.assertEqual(reloaded.catalog[self.ids[0]]['name'], self.first.name)
        self.assertFalse(reloaded.catalog[self.ids[0]]['missing'])

    def test_unknown_new_reference_is_rejected(self):
        value = copy.deepcopy(self.state)
        value['posts'][0]['photos'].append('unknown-id')
        with self.assertRaisesRegex(ValueError, 'Неизвестный файл'):
            self.lib.save(value)

    def test_changed_file_invalidates_preview_version(self):
        old_version = self.lib.catalog[self.ids[0]]['version']
        with self.first.open('ab') as f:
            f.write(b'changed')
        self.lib.refresh()
        self.assertNotEqual(self.lib.catalog[self.ids[0]]['version'], old_version)
        self.assertEqual(self.lib.state, self.state)

    def test_export_detects_removal_even_before_refresh(self):
        self.first.unlink()
        with self.assertRaisesRegex(ValueError, 'недоступные файлы'):
            self.lib.export()
        self.assertFalse((self.root / 'Посты для Telegram').exists())

    def test_unavailable_root_does_not_replace_catalog(self):
        original = copy.deepcopy(self.lib.photos)
        moved = self.root.with_name('moved')
        self.root.rename(moved)
        with self.assertRaises(FileNotFoundError):
            self.lib.refresh()
        self.assertEqual(self.lib.photos, original)
        self.assertEqual((self.data / 'project.json').read_bytes(), self.saved)

    def test_nested_export_and_cache_not_scanned(self):
        folder = self.root / 'Посты для Telegram'
        folder.mkdir()
        shutil.copy2(self.first, folder / 'export.png')
        self.lib.refresh()
        self.assertEqual(len(self.lib.photos), 2)
