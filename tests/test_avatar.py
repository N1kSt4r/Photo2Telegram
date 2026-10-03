import io
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps
from src.server import Library, save_jpeg
from src.telegram_sender import TelegramError


class AvatarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'photo.png'
        image = Image.new('RGB', (400, 200), 'red')
        image.paste(Image.new('RGB', (200, 200), 'blue'), (200, 0))
        image.save(self.source)
        self.library = Library(self.root, self.root / 'data')
        self.publisher = self.library.publisher
        self.publisher.save_settings(dict(token='12345:test', channel='@channel', mode='cloud'))
        self.destination = dict(chat_id=-10012345, bot_id=12345, channel='Channel')
        self.data = dict(photo=self.library.photos[0]['id'], crop=[.75, .5, 1], chat_id=-10012345, bot_id=12345)

    def test_square_from_original_preserves_source_project_and_journal(self):
        source = self.source.read_bytes()
        state = repr(self.library.state)
        journal = repr(self.publisher.journal)
        uploads = []
        class Client:
            def check(inner):
                return self.destination
            def call(inner, method, fields, files):
                self.assertEqual(method, 'setChatPhoto')
                self.assertEqual(fields, dict(chat_id=-10012345, photo='attach://avatar'))
                uploads.append(files['avatar'].read_bytes())
                return True
        with patch.object(self.publisher, 'client_factory', return_value=Client()):
            self.assertTrue(self.publisher.set_avatar(self.data)['ok'])
        with Image.open(io.BytesIO(uploads[0])) as image:
            self.assertEqual(image.size, (200, 200))
            self.assertEqual(image.format, 'JPEG')
            self.assertGreater(image.getpixel((100,100))[2], 240)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(repr(self.library.state), state)
        self.assertEqual(repr(self.publisher.journal), journal)

    def test_rotation_crop_and_lanczos_size(self):
        source = self.root / 'rotated.jpg'
        image = Image.new('RGB', (1600, 800), 'red')
        image.paste(Image.new('RGB', (800, 800), 'blue'), (800,0))
        exif = Image.Exif();exif[274] = 6
        image.save(source, quality=100, exif=exif)
        target = self.root / 'avatar.jpg'
        save_jpeg(source, target, 640, crop=[.5,.75,1], quality=95, subsampling=0)
        with Image.open(target) as result:
            self.assertEqual(result.size, (640,640))
            self.assertGreater(result.getpixel((320,320))[2], 240)
            self.assertNotIn(274, result.getexif())

    def test_invalid_selection_and_changed_channel_never_publish(self):
        with patch.object(self.publisher, 'client_factory') as factory:
            for crop in (None, [], [0,0,0], [.5,.5,6], [math.nan,.5,1], [True,.5,1], ['x',0,1]):
                with self.subTest(crop=crop), self.assertRaises(ValueError):
                    self.publisher.set_avatar(dict(self.data, crop=crop))
            for photo in ('../outside.jpg', [], None):
                with self.subTest(photo=photo), self.assertRaises(ValueError):
                    self.publisher.set_avatar(dict(self.data, photo=photo))
            factory.assert_not_called()
            factory.return_value.check.return_value = dict(self.destination, chat_id=-100999)
            with self.assertRaisesRegex(ValueError, 'изменился'):
                self.publisher.set_avatar(self.data)
            factory.return_value.call.assert_not_called()

    def test_network_failure_is_not_retried(self):
        with patch.object(self.publisher, 'client_factory') as factory:
            factory.return_value.check.return_value = self.destination
            factory.return_value.call.side_effect = TelegramError('Timeout', uncertain=True)
            with self.assertRaisesRegex(ValueError, 'Проверьте аватарку'):
                self.publisher.set_avatar(self.data)
            factory.return_value.call.assert_called_once()
