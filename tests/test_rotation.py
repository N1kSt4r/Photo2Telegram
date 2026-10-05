import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat
from src.server import Library, save_jpeg


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photo = self.root / 'pattern.png'
        image = Image.new('RGB', (160, 96), 'red')
        image.paste('blue', (80, 0, 160, 48))
        image.paste('lime', (0, 48, 80, 96))
        image.paste('yellow', (80, 48, 160, 96))
        image.save(self.photo)
        self.library = Library(self.root, self.root / 'data')
        self.publisher = self.library.publisher
        self.publisher.outbox.mkdir(exist_ok=True)
        self.ident = self.library.photos[0]['id']
        self.library.state.update(active='post', posts=[dict(id='post', title='Test', caption='', photos=[self.ident])])

    def rotate(self, angle, ident=None):
        state = copy.deepcopy(self.library.state)
        state.setdefault('rotations', {})[ident or self.ident] = angle
        self.library.save(state)

    def assert_image(self, image, expected):
        self.assertEqual(image.size, expected.size)
        self.assertLess(max(ImageStat.Stat(ImageChops.difference(image.convert('RGB'), expected.convert('RGB'))).mean), 12)

    def test_validation_persistence_cache_and_original_export(self):
        initial = self.publisher._media_cache_key(self.photo, dict(mode='local'))
        original = self.photo.read_bytes()
        for invalid in ([], {self.ident: True}, {self.ident: 90.0}, {self.ident: '90'}, {self.ident: -90},
                        {self.ident: 360}, {'missing': 90}):
            with self.assertRaises(ValueError):
                self.library.save(dict(self.library.state, rotations=invalid))
        self.rotate(90)
        restored = Library(self.root, self.root / 'data')
        self.assertEqual(restored.state['rotations'], {self.ident: 90})
        self.publisher.outbox.mkdir(exist_ok=True)
        key = self.publisher._media_cache_key(self.photo, dict(mode='local'))
        self.assertNotEqual(initial, key)
        result = self.publisher._prepare_media(self.photo, dict(mode='local'))
        self.assertEqual(result['rotation'], 90)
        with patch.object(self.publisher, '_prepare_media_file', side_effect=AssertionError('Must reuse cache')):
            cached = self.publisher._prepare_media(self.photo, dict(mode='local'))
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['rotation'], 90)
        job = self.library.jobs[self.library.export()]
        deadline = time.monotonic() + 10
        while job['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(job['status'], 'done')
        self.assertEqual(next(Path(job['path']).glob('*/*.png')).read_bytes(), original)
        self.assertEqual(self.photo.read_bytes(), original)
        self.rotate(0)
        self.assertEqual(self.publisher._media_cache_key(self.photo, dict(mode='local')), initial)

    def test_photo_turns_and_exif_orientation(self):
        for angle, transpose in ((90, Image.Transpose.ROTATE_270), (180, Image.Transpose.ROTATE_180), (270, Image.Transpose.ROTATE_90)):
            self.rotate(angle)
            result = self.publisher._prepare_media(self.photo, dict(mode='local'))
            with Image.open(result['path']) as actual, Image.open(self.photo) as original:
                self.assert_image(actual, original.transpose(transpose))
        source = self.root / 'oriented.jpg'
        with Image.open(self.photo) as original:
            exif = Image.Exif(); exif[274] = 6
            original.save(source, quality=100, subsampling=0, exif=exif)
            expected = original.transpose(Image.Transpose.ROTATE_180)
        target = self.root / 'turned.jpg'
        save_jpeg(source, target, 2560, quality=95, subsampling=0, rotation=90)
        with Image.open(target) as actual:
            self.assert_image(actual, expected)
            self.assertIn(actual.getexif().get(274), (None, 1))

    def test_change_during_preparation_and_stale_prepared_post(self):
        self.rotate(90)
        key = self.publisher._media_cache_key(self.photo, dict(mode='local'))
        original_prepare = self.publisher._prepare_media_file
        def changing(source, settings, target):
            self.rotate(180)
            return original_prepare(source, settings, target)
        with patch.object(self.publisher, '_prepare_media_file', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'поворота'):
                self.publisher._prepare_media(self.photo, dict(mode='local'))
        self.assertFalse((self.publisher.media_cache / (key+'.json')).exists())
        self.publisher.save_settings(dict(token='12345:test', channel='@channel', mode='cloud'))
        with patch.object(self.publisher, '_check_destination', return_value=dict(chat_id=-10012345, bot_id=12345, channel='Channel', bot='bot')):
            self.publisher.prepare()
            deadline = time.monotonic() + 10
            while self.publisher.busy() and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.publisher.busy())
        prepared = self.publisher.status()['job']['items'][0]
        self.assertEqual(prepared['status'], 'ready')
        self.assertEqual(prepared['files'][0]['rotation'], 180)
        self.rotate(270)
        self.assertNotEqual(self.publisher.status()['job']['items'][0]['status'], 'ready')
        with self.assertRaises(ValueError):
            self.publisher.send(self.publisher.job['id'])

    def make_video(self, metadata_rotation=0):
        if not self.library.ffmpeg or not self.library.ffprobe:
            self.skipTest('FFmpeg/FFprobe are optional and not installed')
        source = self.root / 'video.mp4'
        subprocess.run([self.library.ffmpeg, '-nostdin', '-v', 'error', '-y', '-loop', '1', '-i', str(self.photo),
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-r', '4', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
        if metadata_rotation:
            tagged = self.root / 'tagged.mp4'
            subprocess.run([self.library.ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-c', 'copy',
                            '-metadata:s:v:0', f'rotate={metadata_rotation}', str(tagged)], check=True)
            probe = json.loads(subprocess.check_output([self.library.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(tagged)]))
            if not probe['streams'][0].get('side_data_list'):
                # New FFmpeg uses an explicit input display matrix rather than the rotate tag.
                subprocess.run([self.library.ffmpeg, '-nostdin', '-v', 'error', '-y', '-display_rotation',
                                str(metadata_rotation), '-i', str(source), '-c', 'copy', str(tagged)], check=True)
            tagged.replace(source)
        self.library.refresh()
        return source, hashlib.sha256(source.name.encode()).hexdigest()[:20]

    def video_frame(self, source):
        data = subprocess.check_output([self.library.ffmpeg, '-nostdin', '-v', 'error', '-i', str(source),
                                        '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
        return Image.open(io.BytesIO(data))

    def test_video_turns_audio_dimensions_thumbnail_and_original(self):
        source, ident = self.make_video()
        before = source.read_bytes()
        for angle, transpose in ((90, Image.Transpose.ROTATE_270), (180, Image.Transpose.ROTATE_180), (270, Image.Transpose.ROTATE_90)):
            self.rotate(angle, ident)
            result = self.publisher._prepare_media(source, dict(mode='local'))
            with self.video_frame(result['path']) as actual, Image.open(self.photo) as original:
                self.assert_image(actual, original.transpose(transpose))
                self.assertEqual((result['video']['width'], result['video']['height']), actual.size)
            with Image.open(result['thumbnail']) as thumbnail:
                self.assertEqual(thumbnail.width > thumbnail.height, angle == 180)
            streams = json.loads(subprocess.check_output([self.library.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(result['path'])]))['streams']
            self.assertIn('audio', [s['codec_type'] for s in streams])
        self.library.state['mutedVideos'] = {ident: True}
        muted = self.publisher._prepare_media(source, dict(mode='local'))
        streams = json.loads(subprocess.check_output([self.library.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(muted['path'])]))['streams']
        self.assertNotIn('audio', [s['codec_type'] for s in streams])
        self.assertEqual(source.read_bytes(), before)
        self.library.ffmpeg = None
        with self.assertRaisesRegex(ValueError, 'поворота видео нужен FFmpeg'):
            self.publisher._prepare_media(source, dict(mode='local'))

    def test_video_metadata_orientation_is_applied_once_before_user_rotation(self):
        source, ident = self.make_video(metadata_rotation=90)
        self.rotate(90, ident)
        result = self.publisher._prepare_media(source, dict(mode='local'))
        with self.video_frame(result['path']) as actual, Image.open(self.photo) as original:
            self.assert_image(actual, original)
            self.assertEqual((result['video']['width'], result['video']['height']), actual.size)
