import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from src.server import Library

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = FIXTURES / '01-pattern.png'
        for n in range(12):
            shutil.copy2(source, self.root / f'2026-09-20 07-51-{n:02d}.png')
        self.data = self.root / 'data'
        self.library = Library(self.root, self.data)

    def tearDown(self):
        self.temp.cleanup()

    def project(self):
        ids = [p['id'] for p in self.library.photos]
        return dict(revision=0, hidden=[ids[4]], active='post-1', posts=[dict(id='post-1', title='Первый / пост', caption='Тестовая съёмка\nДень первый', photos=[ids[2], ids[0], ids[1]])])

    def test_persistence_conflict_and_validation(self):
        state = self.project()
        self.assertEqual(self.library.save(state), 1)
        loaded = Library(self.root, self.data)
        self.assertEqual(loaded.state, state)
        self.assertIsNone(self.library.save(self.project()))
        too_many = self.project()
        too_many['posts'][0]['photos'] = [p['id'] for p in self.library.photos][:11]
        with self.assertRaises(ValueError):
            self.library.save(too_many)
        state['posts'][0]['caption'] = 'Новая подпись'
        self.library.save(state)
        self.assertEqual(json.loads((self.data / 'project.backup.json').read_text(encoding='utf-8'))['revision'], 1)

    def test_export_original_bytes_order_caption(self):
        state = self.project()
        self.library.save(state)
        ident = self.library.export()
        job = self.library.jobs[ident]
        deadline = time.monotonic() + 20
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(job['status'], 'done', job)
        path = Path(job['path'])
        manifest = json.loads((path / 'Порядок постов.json').read_text(encoding='utf-8'))
        folder = path / manifest[0]['folder']
        for i, name in enumerate(manifest[0]['files']):
            src = self.library.files[state['posts'][0]['photos'][i]]
            self.assertEqual(hashlib.sha256(src.read_bytes()).digest(), hashlib.sha256((folder / name).read_bytes()).digest())
            self.assertEqual((folder / name).suffix, '.png')
            self.assertTrue(name.startswith(f'{i+1:02d} — '))
        self.assertEqual((folder / 'Подпись.txt').read_text(encoding='utf-8'), state['posts'][0]['caption'])
        self.assertEqual(len(list(self.root.glob('*.png'))), 12)

    def test_video_in_timeline_and_mixed_export(self):
        source = FIXTURES / '03-motion.mov'
        target = self.root / '2026-09-20 07-51-05_video.MOV'
        shutil.copy2(source, target)
        library = Library(self.root, self.data)
        video = next(p for p in library.photos if p['kind'] == 'video')
        self.assertEqual(video['date'], '2026-09-20T07:51:05')
        self.assertEqual(library.photos[6]['id'], video['id'])
        state = self.project()
        state['posts'][0]['photos'].insert(1, video['id'])
        library.save(state)
        ident = library.export()
        job = library.jobs[ident]
        deadline = time.monotonic() + 20
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(job['status'], 'done', job)
        output = Path(job['path'])
        exported = next(output.glob('*/*.MOV'))
        self.assertTrue(exported.name.startswith('02 — '))
        self.assertEqual(hashlib.sha256(exported.read_bytes()).digest(), hashlib.sha256(source.read_bytes()).digest())
        self.assertEqual(Library(self.root, self.data).state, state)

    def test_export_includes_text_posts_in_order_and_skips_empty_drafts(self):
        for text_only in (False, True):
            with self.subTest(text_only=text_only):
                text = dict(id='text', title='Текст', caption='Я' * 4096, photos=[])
                posts = [text] if text_only else [self.project()['posts'][0], text]
                self.library.state['posts'] = posts + [dict(id='empty', title='Empty', caption='  ', photos=[])]
                job = self.library.jobs[self.library.export()]
                deadline = time.monotonic() + 10
                while job['status'] == 'working' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(job['status'], 'done', job)
                self.assertEqual(job['done'], len(posts))
                self.assertEqual(job['total'], len(posts))
                output = Path(job['path'])
                manifest = json.loads((output / 'Порядок постов.json').read_text(encoding='utf-8'))
                self.assertEqual([p['caption'] for p in manifest], [p['caption'] for p in posts])
                text_folder = output / manifest[-1]['folder']
                self.assertEqual([p.name for p in text_folder.iterdir()], ['Подпись.txt'])
                self.assertEqual((text_folder / 'Подпись.txt').read_text(encoding='utf-8'), text['caption'])

    def test_video_preview_and_duration(self):
        target = self.root / '2026-09-20 07-51-05_video.MOV'
        shutil.copy2(FIXTURES / '03-motion.mov', target)
        library = Library(self.root, self.data)
        if not library.ffmpeg or not library.ffprobe:
            self.skipTest('FFmpeg/FFprobe are optional and not installed')
        video = next(p for p in library.photos if p['kind'] == 'video')
        self.assertAlmostEqual(video['duration'], 2, places=1)
        preview = library.preview(video['id'], 480)
        self.assertEqual(preview.read_bytes()[:2], b'\xff\xd8')
        modified = preview.stat().st_mtime_ns
        self.assertEqual(library.preview(video['id'], 480).stat().st_mtime_ns, modified)

    def test_export_without_video_tools_and_empty_captions(self):
        target = self.root / '2026-09-20 07-51-05_video.MOV'
        shutil.copy2(FIXTURES / '03-motion.mov', target)
        library = Library(self.root, self.data)
        library.ffmpeg = library.ffprobe = None
        video = next(p for p in library.photos if p['kind'] == 'video')
        for cache in library.cache.glob('*-duration.json'):
            cache.unlink()
        self.assertIsNone(library.video_duration(target, video['id']))
        state = self.project()
        state['posts'] = [dict(id=str(i), title=str(i), caption=caption, photos=[video['id']])
                          for i, caption in enumerate(['', ' \n\t', 'Текст'])]
        state['active'] = '0'
        library.save(state)
        job = library.jobs[library.export()]
        deadline = time.monotonic() + 20
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(job['status'], 'done', job)
        output = Path(job['path'])
        self.assertEqual(len(list(output.glob('*/*.MOV'))), 3)
        captions = list(output.glob('*/Подпись.txt'))
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0].read_text(encoding='utf-8'), 'Текст')

    def test_supported_image_fixtures(self):
        for n, name in enumerate(['02-blue.jpg', '04-pattern.heic']):
            shutil.copy2(FIXTURES / name, self.root / f'2026-09-21 12-00-{n:02d}{Path(name).suffix}')
        library = Library(self.root, self.data)
        for p in library.photos:
            if p['date'].startswith('2026-09-21'):
                self.assertEqual(library.preview(p['id'], 480).read_bytes()[:2], b'\xff\xd8')

    def test_preview_is_jpeg_and_original_intact(self):
        ident = self.library.photos[0]['id']
        original = self.library.files[ident].read_bytes()
        preview = self.library.preview(ident, 480)
        self.assertEqual(preview.read_bytes()[:2], b'\xff\xd8')
        self.assertEqual(self.library.files[ident].read_bytes(), original)
        self.assertEqual(self.library.preview(ident, 480), preview)


if __name__ == '__main__':
    unittest.main()
