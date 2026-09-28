import shutil
import json
from unittest.mock import patch
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.preview_cache import PreviewCache
from src.server import Library


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = PreviewCache(self.root / 'previews', self.root / 'settings.json')

    def entry(self, index, size=10):
        path = self.cache.directory / f'{index:020x}-1-10-480-v2.jpg'
        path.write_bytes(b'x' * size)
        self.cache.record(path)
        return path

    def test_lru_and_pinned_files(self):
        a, b = self.entry(1), self.entry(2)
        self.cache.record(a)
        self.cache.limit = 20
        with self.cache.use(a):
            c = self.entry(3)
            self.assertTrue(a.exists())
            self.assertFalse(b.exists())
            self.assertTrue(c.exists())
        self.assertEqual(self.cache.stats()['bytes'], 20)

    def test_oversized_file_is_read_before_eviction(self):
        self.cache.limit = 5
        path = self.cache.directory / f'{1:020x}-1-10-480-v2.jpg'
        with self.cache.use(path):
            path.write_bytes(b'1234567890')
            self.cache.record(path)
            self.assertEqual(path.read_bytes(), b'1234567890')
        self.assertFalse(path.exists())
        self.assertEqual(self.cache.stats()['bytes'], 0)

    def test_limit_persists_and_is_validated(self):
        value = 512 * 1024**2
        self.cache.set_limit(value)
        reloaded = PreviewCache(self.cache.directory, self.cache.settings)
        self.assertEqual(reloaded.limit, value)
        for invalid in [True, None, -1, 1.5, '2 GB']:
            with self.assertRaises(ValueError):
                self.cache.set_limit(invalid)

    def test_prune_obsolete_defers_pinned_files(self):
        a, b = self.entry(1), self.entry(2)
        with self.cache.use(a):
            self.cache.prune_obsolete({b.name})
            self.assertTrue(a.exists())
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())

    def test_clear_waits_for_reader_and_allows_nested_video_metadata(self):
        a = self.entry(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.cache.use(a):
                future = pool.submit(self.cache.clear)
                # Condition.wait releases the mutex, so nested cache users must
                # be allowed to finish instead of deadlocking with the cleaner.
                with self.cache.condition:
                    started = self.cache.condition.wait_for(lambda: self.cache.clearing, timeout=2)
                self.assertTrue(started)
                self.assertFalse(future.done())
                with self.cache.use(a):
                    self.assertEqual(a.read_bytes(), b'x' * 10)
            self.assertEqual(future.result(timeout=2)['bytes'], 0)
        self.assertFalse(a.exists())

    def test_clear_preserves_unrelated_files_and_settings(self):
        self.entry(1)
        unrelated = self.cache.directory / 'notes.txt'
        unrelated.write_text('keep', encoding='utf-8')
        self.cache.set_limit(1024**3)
        self.assertEqual(self.cache.clear()['files'], 0)
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertTrue(self.cache.settings.exists())

    def test_obsolete_versions_and_project_survive_real_library_clear(self):
        media = self.root / 'media'
        media.mkdir()
        source = media / '2026-09-20 12-00-00.png'
        shutil.copy2(Path(__file__).parent / 'fixtures' / '01-pattern.png', source)
        data = self.root / 'data'
        library = Library(media, data)
        ident = library.photos[0]['id']
        state = dict(revision=0, posts=[dict(id='post', title='Пост', caption='Текст', photos=[ident])], hidden=[], active='post')
        library.save(state)
        saved = (data / 'project.json').read_bytes()
        original = source.read_bytes()
        old = library.preview(ident, 480)
        with source.open('ab') as f:
            f.write(b'changed')
        library.refresh()
        self.assertFalse(old.exists())
        preview = library.preview(ident, 480)
        legacy = preview.with_name(preview.name.replace('-v2.jpg', '.jpg'))
        legacy.write_bytes(preview.read_bytes())
        reloaded = Library(media, data)
        self.assertFalse(legacy.exists())
        self.assertTrue(preview.exists())
        self.assertEqual(reloaded.preview_cache.clear()['bytes'], 0)
        self.assertEqual((data / 'project.json').read_bytes(), saved)
        self.assertEqual(source.read_bytes(), original + b'changed')
        self.assertEqual(reloaded.preview(ident, 480, read_bytes=True)[:2], b'\xff\xd8')

    def test_lowering_limit_trims_existing_files_immediately(self):
        for index in range(3):
            path = self.entry(index, 0)
            with path.open('wb') as f:
                f.truncate(32 * 1024**2)
            self.cache.record(path)
        result = self.cache.set_limit(64 * 1024**2)
        self.assertEqual(result['bytes'], 64 * 1024**2)
        self.assertEqual(result['files'], 2)

    def test_stats_count_small_large_and_metadata_separately(self):
        for suffix, size in [('480-v2.jpg', 10), ('1800-v2.jpg', 20), ('duration.json', 5)]:
            path = self.cache.directory / f'{1:020x}-1-10-{suffix}'
            path.write_bytes(b'x' * size)
            self.cache.record(path)
        stats = self.cache.stats()
        self.assertEqual(stats['files'], 3)
        self.assertEqual(stats['bytes'], 35)
        self.assertEqual(stats['groups'], {
            'thumbnails': {'files': 1, 'bytes': 10},
            'large': {'files': 1, 'bytes': 20},
            'metadata': {'files': 1, 'bytes': 5},
        })
        self.cache.clear()
        self.assertTrue(all(group['files'] == 0 for group in self.cache.stats()['groups'].values()))

    def test_known_video_duration_returns_after_clear_without_probe(self):
        media = self.root / 'media'
        media.mkdir()
        shutil.copy2(Path(__file__).parent / 'fixtures' / '03-motion.mov', media / '2026-09-20 12-00-00.mov')
        data = self.root / 'data'
        library = Library(media, data)
        video = library.photos[0]
        video['duration'] = 2.0
        library.catalog_path.write_text(json.dumps(library.catalog), encoding='utf-8')
        library.preview_cache.clear()
        self.assertEqual(library.preview_cache.stats()['groups']['metadata']['files'], 0)
        with patch('src.server.subprocess.run', side_effect=AssertionError('Must reuse known duration')):
            reloaded = Library(media, data)
        self.assertEqual(reloaded.photos[0]['duration'], 2.0)
        self.assertEqual(reloaded.preview_cache.stats()['groups']['metadata']['files'], 1)
        reloaded.preview_cache.clear()
        with patch('src.server.subprocess.run', side_effect=AssertionError('Must reuse known duration')):
            reloaded.refresh()
        self.assertEqual(reloaded.preview_cache.stats()['groups']['metadata']['files'], 1)
