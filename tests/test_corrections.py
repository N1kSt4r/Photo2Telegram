import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from src.server import Library, save_jpeg


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'photo.png'
        Image.new('RGB', (64, 48), (100, 150, 200)).save(self.source)
        self.library = Library(self.root, self.root / 'data')
        self.publisher = self.library.publisher
        self.publisher.outbox.mkdir(exist_ok=True)
        self.ident = self.library.photos[0]['id']
        self.library.state.update(active='post', posts=[dict(id='post', title='Test', caption='', photos=[self.ident])])

    def correct(self, values):
        self.library.save(dict(self.library.state, corrections={self.ident: values}))

    def test_validation_persistence_originals_and_cache(self):
        for value in ([], {'missing': {}}, {self.ident: []}, {self.ident: {'gamma': 1}},
                      {self.ident: {'brightness': True}}, {self.ident: {'contrast': 101}},
                      {self.ident: {'saturation': -101}}, {self.ident: {'brightness': 2.5}}):
            with self.assertRaisesRegex(ValueError, 'коррекция'):
                self.library.validate(dict(self.library.state, corrections=value))
        self.library.catalog['video'] = dict(kind='video')
        with self.assertRaises(ValueError):
            self.library.validate(dict(self.library.state, corrections={'video': {'brightness': 1}}))
        baseline = self.publisher._media_cache_key(self.source, dict(mode='local'))
        original = self.source.read_bytes()
        self.correct(dict(brightness=20, saturation=-40))
        restored = Library(self.root, self.root / 'data')
        self.assertEqual(restored.state['corrections'][self.ident], dict(brightness=20, saturation=-40))
        self.publisher.outbox.mkdir(exist_ok=True)
        self.assertNotEqual(baseline, self.publisher._media_cache_key(self.source, dict(mode='local')))
        result = self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertEqual(result['correction'], dict(brightness=20, saturation=-40))
        with patch.object(self.publisher, '_prepare_media_file', side_effect=AssertionError('Cache must be used')):
            self.assertTrue(self.publisher._prepare_media(self.source, dict(mode='local'))['cached'])
        job = self.library.jobs[self.library.export()]
        deadline = time.monotonic() + 10
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(job['status'], 'done')
        self.assertEqual(next(Path(job['path']).glob('*/*.png')).read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        self.correct(dict(brightness=0, saturation=0))
        self.assertEqual(baseline, self.publisher._media_cache_key(self.source, dict(mode='local')))

    def test_last_correction_validation_persistence_and_independence(self):
        for value in ([], None, {'unknown': 1}, {'brightness': True}, {'contrast': 101},
                      {'saturation': -101}, {'brightness': 1.5}):
            with self.assertRaisesRegex(ValueError, 'последняя коррекция'):
                self.library.validate(dict(self.library.state, lastCorrection=value))
        baseline = self.publisher._media_cache_key(self.source, dict(mode='local'))
        self.library.save(dict(self.library.state, lastCorrection=dict(brightness=15, contrast=-20, saturation=30)))
        restored = Library(self.root, self.root / 'data')
        self.assertEqual(restored.state['lastCorrection'], dict(brightness=15, contrast=-20, saturation=30))
        self.assertEqual(baseline, self.publisher._media_cache_key(self.source, dict(mode='local')))
        self.assertFalse(self.library.state.get('corrections'))

    def test_actual_pixels_and_rotation(self):
        # Known sRGB swatch: luminance .213*100 + .715*150 + .072*200 = 142.95.
        for correction, expected in [({'brightness': -100}, (0, 0, 0)),
                                      ({'contrast': -100}, (128, 128, 128)),
                                      ({'saturation': -100}, (143, 143, 143)),
                                      ({'brightness': 20}, (120, 180, 240)),
                                      ({'contrast': 100}, (73, 173, 255)),
                                      ({'brightness': 20, 'contrast': 30, 'saturation': -40}, (145, 191, 238))]:
            target = self.root / 'output.jpg'
            save_jpeg(self.source, target, 2560, quality=100, subsampling=0, rotation=90, correction=correction)
            with Image.open(target) as image:
                self.assertEqual(image.size, (48, 64))
                pixel = image.getpixel((20, 20))
                self.assertLessEqual(max(abs(a-b) for a,b in zip(pixel, expected)), 2, (correction, pixel))

    def test_changed_correction_cannot_commit_or_send_old_preparation(self):
        self.correct({'brightness': 10})
        key = self.publisher._media_cache_key(self.source, dict(mode='local'))
        prepare = self.publisher._prepare_media_file
        def change_during_preparation(source, settings, target):
            self.correct({'brightness': 20})
            return prepare(source, settings, target)
        with patch.object(self.publisher, '_prepare_media_file', side_effect=change_during_preparation):
            with self.assertRaisesRegex(ValueError, 'коррекции'):
                self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertFalse((self.publisher.media_cache / (key+'.json')).exists())
        self.publisher.save_settings(dict(token='12345:test', channel='@channel', mode='cloud'))
        with patch.object(self.publisher, '_check_destination', return_value=dict(chat_id=-10012345, bot_id=12345, channel='Channel', bot='bot')):
            self.publisher.prepare()
            deadline = time.monotonic() + 10
            while self.publisher.busy() and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.publisher.busy())
        item = self.publisher.status()['job']['items'][0]
        self.assertEqual(item['status'], 'ready')
        self.assertEqual(item['files'][0]['correction'], {'brightness': 20})
        sent = copy.deepcopy(item)
        self.correct({'brightness': 30})
        self.assertNotEqual(self.publisher.status()['job']['items'][0]['status'], 'ready')
        with self.assertRaises(ValueError):
            self.publisher.send(self.publisher.job['id'])
        self.assertEqual(self.publisher._sent_files(self.library.state['posts'][0], sent)[0]['correction'], {'brightness': 20})
        self.assertEqual(self.publisher._sent_files(self.library.state['posts'][0], {'status': 'sent'})[0]['correction'], {})
