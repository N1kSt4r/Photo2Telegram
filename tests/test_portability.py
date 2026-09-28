import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from PIL import Image, ImageCms
from filelock import FileLock
from src.server import Library, image_preview, image_date, open_local

ROOT = Path(__file__).resolve().parent.parent


class ImageTest(unittest.TestCase):
    def test_orientation_metadata_transparency_and_color(self):
        with tempfile.TemporaryDirectory(prefix='Фото с пробелами ') as temp:
            root = Path(temp)
            image = Image.new('RGB', (120, 60), 'red')
            exif = Image.Exif()
            exif[274] = 6
            exif[34665] = {36867: '2024:02:03 04:05:06'}
            source = root / 'Снимок.jpg'
            image.save(source, exif=exif, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
            self.assertEqual(image_date(source), '2024-02-03T04:05:06')
            image_preview(source, root / 'preview.jpg', 80)
            with Image.open(root / 'preview.jpg') as result:
                self.assertEqual(result.size, (40, 80))
                self.assertNotIn(274, result.getexif())
                self.assertGreater(result.getpixel((20, 40))[0], 240)
                self.assertTrue(result.info.get('icc_profile'))
            transparent = root / 'transparent.png'
            Image.new('RGBA', (10, 10), (255, 0, 0, 0)).save(transparent)
            image_preview(transparent, root / 'white.jpg', 80)
            with Image.open(root / 'white.jpg') as result:
                self.assertEqual(result.getpixel((5, 5)), (255, 255, 255))
            library = Library(root, root / 'data')
            photo = next(p for p in library.photos if p['name'] == 'Снимок.jpg')
            self.assertEqual(photo['dateSource'], 'metadata')

    def test_invalid_metadata_does_not_break_library(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exif = Image.Exif()
            exif[306] = 'not a date'
            Image.new('RGB', (10, 10)).save(root / 'invalid.jpg', exif=exif)
            self.assertIsNone(image_date(root / 'invalid.jpg'))
            self.assertEqual(Library(root, root / 'data').photos[0]['dateSource'], 'file')

    def test_system_open_commands(self):
        path = str((ROOT / 'tests/fixtures/02-blue.jpg').resolve())
        for platform, command in [('darwin', 'open'), ('linux', 'xdg-open')]:
            with patch('src.server.sys.platform', platform), patch('src.server.subprocess.Popen') as launch:
                open_local(path)
                launch.assert_called_once_with([command, path])
        with patch('src.server.sys.platform', 'win32'), patch('src.server.os.startfile', create=True) as launch:
            open_local(path)
            launch.assert_called_once_with(path)


class LauncherTest(unittest.TestCase):
    def test_help_and_missing_directory(self):
        command = (['cmd', '/c', str(ROOT / 'run.bat')] if os.name == 'nt'
                   else ['bash', str(ROOT / 'run.sh')])
        with tempfile.TemporaryDirectory(prefix='Launcher test ') as temp:
            help_result = subprocess.run([*command, '--help'], cwd=temp, capture_output=True, timeout=15)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn(b'Usage:', help_result.stdout)
            empty_result = subprocess.run(command, cwd=temp, capture_output=True, timeout=15)
            self.assertEqual(empty_result.returncode, 1, empty_result.stderr)
            bad_result = subprocess.run([*command, str(Path(temp) / 'missing photos')], cwd=temp,
                                        capture_output=True, timeout=15)
            self.assertEqual(bad_result.returncode, 1, bad_result.stderr)


class ServerProcessTest(unittest.TestCase):
    def test_server_and_second_instance_with_unicode_paths(self):
        with tempfile.TemporaryDirectory(prefix='Проверка сервера ') as temp:
            root = Path(temp)
            media = root / 'Медиа с пробелами'
            media.mkdir()
            Image.new('RGB', (120, 60), 'blue').save(media / 'Кадр.jpg')
            data = root / 'Данные'
            command = [sys.executable, '-X', 'utf8', str(ROOT / 'src/server.py'), '--root', str(media), '--data', str(data), '--port', '0']
            with (root / 'server.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen(command, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while not (data / 'runtime.json').exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue((data / 'runtime.json').exists(), (root / 'server.log').read_text(encoding='utf-8'))
                    url = json.loads((data / 'runtime.json').read_text(encoding='utf-8'))['url']
                    with urllib.request.urlopen(url + '/api/library', timeout=5) as response:
                        library = json.load(response)
                    self.assertEqual(library['photos'][0]['name'], 'Кадр.jpg')
                    with urllib.request.urlopen(url + '/photo/' + library['photos'][0]['id'], timeout=5) as response:
                        self.assertEqual(response.read()[:2], b'\xff\xd8')
                    result = subprocess.run(command, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(url.encode(), result.stdout)
                finally:
                    process.terminate()
                    process.wait(timeout=10)
            # OS releases the lock even after abnormal process termination.
            with FileLock(data / 'instance.lock', timeout=2):
                pass


if __name__ == '__main__':
    unittest.main()
