import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from src.server import Library


class GeneratedStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original.png'
        shutil.copyfile(Path(__file__).parent / 'fixtures/01-pattern.png', self.source)
        self.original = self.source.read_bytes()
        self.library = Library(self.root, self.root / 'data')
        self.library.state.update(active='one', posts=[dict(id='one', title='Test', caption='Caption', photos=[self.library.photos[0]['id']])])
        self.library.save(self.library.state)

    def export(self):
        job = self.library.jobs[self.library.export()]
        deadline = time.monotonic() + 5
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotEqual(job['status'], 'working')
        return job

    def test_export_copies_are_counted_and_cleaned_without_touching_sources_or_added_files(self):
        job = self.export()
        self.assertEqual(job['status'], 'done')
        folder = Path(job['path'])
        manifest = json.loads((folder / 'Порядок постов.json').read_text(encoding='utf-8'))
        post = folder / manifest[0]['folder']
        copy = post / manifest[0]['files'][0]
        self.assertEqual(copy.read_bytes(), self.original)
        added = post / 'my-notes.txt'; added.write_text('keep')
        stats = self.library.generated_stats()['exports']
        self.assertEqual(stats['folders'], 1)
        self.assertEqual(stats['files'], 4)
        self.assertGreater(stats['bytes'], len(self.original))
        project = self.library.state_path.read_bytes()
        self.library.clear_generated('exports')
        self.assertFalse(copy.exists())
        self.assertEqual(added.read_text(), 'keep')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.library.state_path.read_bytes(), project)
        self.assertEqual(self.library.generated_stats()['exports']['files'], 0)

    def test_incomplete_export_can_be_cleaned(self):
        with patch('src.server.shutil.copy2', side_effect=OSError('disk full')):
            job = self.export()
        self.assertEqual(job['status'], 'error')
        self.assertEqual(self.library.generated_stats()['exports']['folders'], 1)
        self.library.clear_generated('exports')
        self.assertFalse(Path(job['path']).exists())

    def test_unrecognized_or_traversing_exports_are_never_deleted(self):
        folder = self.root / 'Посты для Telegram' / '2026-09-29 12-00-00-abcd'
        folder.mkdir(parents=True)
        manifest = folder / 'Порядок постов.json'
        manifest.write_text(json.dumps([dict(folder='../../',files=['original.png'],caption='')]))
        self.library.clear_generated('exports')
        self.assertTrue(manifest.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_prepared_cleanup_clears_queue_but_preserves_journal_project_and_unknown_files(self):
        publisher = self.library.publisher
        media = publisher.media_cache / ('a'*64+'.jpg'); media.write_bytes(b'jpeg')
        metadata = media.with_suffix('.json'); metadata.write_text('{}')
        publisher.outbox.mkdir()
        out = publisher.outbox / ('b'*32+'.jpg')
        try:
            os.link(media, out)
        except OSError:
            self.skipTest('Hard links unavailable')
        note = publisher.media_cache / 'notes.txt'; note.write_text('keep')
        publisher.journal_path.write_text('{}')
        publisher.job = {'id': 'test', 'status': 'ready'}
        self.assertEqual(publisher.storage_stats()['bytes'], 6)
        self.assertEqual(publisher.storage_stats()['files'], 3)
        publisher._working = True
        with self.assertRaises(ValueError):
            self.library.clear_generated('prepared')
        self.assertTrue(media.exists())
        publisher._working = False
        self.library.clear_generated('prepared')
        self.assertIsNone(publisher.job)
        self.assertFalse(media.exists()); self.assertFalse(out.exists())
        self.assertEqual(note.read_text(), 'keep')
        self.assertTrue(self.library.state_path.exists())
        self.assertEqual(publisher.journal_path.read_text(), '{}')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_export_cleanup_rejected_while_exporting(self):
        self.library.jobs['working'] = {'status': 'working'}
        with self.assertRaises(ValueError):
            self.library.clear_generated('exports')

    def test_symbolic_links_are_not_followed(self):
        parent = self.root / 'Посты для Telegram'
        target = self.root / 'private'; target.mkdir()
        (target / 'keep.txt').write_text('keep')
        try:
            parent.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable')
        self.library.clear_generated('exports')
        self.assertEqual((target / 'keep.txt').read_text(), 'keep')
