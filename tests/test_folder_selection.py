import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from src import launch
from src.server import LibraryManager, LocalHTTPServer, handler_for


class FolderSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / 'Первая папка'
        self.b = self.root / 'Вторая папка'
        for folder, color in [(self.a, 'red'), (self.b, 'blue')]:
            folder.mkdir()
            Image.new('RGB', (20, 20), color).save(folder / 'same.png')
        self.manager = LibraryManager(self.a, self.root / 'data')

    def test_drafts_hidden_and_legacy_location_survive_switch_and_restart(self):
        a = self.manager.current
        photo = a.photos[0]['id']
        state = {'revision': 0, 'hidden': [photo], 'posts': [
            {'id': 'post', 'title': 'Съёмка', 'caption': 'Подпись', 'photos': [photo]}], 'active': 'post'}
        a.save(state)
        self.assertEqual(a.data, self.root / 'data')
        original = a.state_path.read_bytes()
        b = self.manager.switch(self.b)
        self.assertEqual(b.state['posts'], [])
        self.assertEqual(b.state['hidden'], [])
        self.assertNotEqual(a.data, b.data)
        self.assertNotEqual(self.manager.key(a.root), self.manager.key(b.root))
        old_token = a.token
        self.manager.switch(self.a)
        self.assertIs(self.manager.current, a)
        self.assertNotEqual(a.token, old_token)
        self.assertEqual(self.manager.current.state_path.read_bytes(), original)
        restarted = LibraryManager(self.b, self.root / 'data')
        self.assertEqual(restarted.current.data, b.data)
        restarted.switch(self.a)
        self.assertEqual(restarted.current.state['posts'][0]['caption'], 'Подпись')

    def test_path_suggestions_filter_directories_and_support_home(self):
        (self.root / 'Первая фотография.jpg').write_bytes(b'not a directory')
        suggestions = self.manager.suggest_folders(str(self.root / 'пер'))['folders']
        self.assertEqual([item['name'] for item in suggestions], ['Первая папка'])
        self.assertTrue(suggestions[0]['path'].endswith(os.sep))
        with patch('src.server.os.path.expanduser', return_value=str(self.root)):
            names = [item['name'] for item in self.manager.suggest_folders('~')['folders']]
            self.assertIn('Вторая папка', names)
        self.assertEqual(self.manager.suggest_folders(str(self.root / 'missing') + os.sep)['folders'], [])
        names = [item['name'] for item in self.manager.suggest_folders(str(self.root) + os.sep)['folders']]
        self.assertNotIn('Первая фотография.jpg', names)

    def test_opened_path_and_suggestions_show_the_same_children(self):
        (self.a / 'Вложенная').mkdir()
        for path in (str(self.a), str(self.a) + os.sep):
            listing = self.manager.folders(path)
            self.assertEqual(listing['inputPath'], str(self.a) + os.sep)
            suggested = self.manager.suggest_folders(listing['inputPath'])
            self.assertEqual([entry['name'] for entry in suggested['folders']],
                             [entry['name'] for entry in listing['folders']])
        anchor = self.manager.folders(self.root.anchor)
        self.assertEqual(anchor['inputPath'], self.root.anchor)

    def test_up_uses_typed_path_even_when_incomplete(self):
        listing = self.manager.folders(str(self.root / 'Пер'), up=True)
        self.assertEqual(listing['path'], str(self.root))
        listing = self.manager.folders(str(self.a) + os.sep, up=True)
        self.assertEqual(listing['path'], str(self.root))
        anchor = Path(self.root.anchor)
        self.assertEqual(self.manager.folders(str(anchor), up=True)['path'], str(anchor))

    def test_failed_switch_and_running_export_keep_current_project(self):
        old = self.manager.current
        with self.assertRaises(ValueError):
            self.manager.switch(self.root / 'missing')
        self.assertIs(self.manager.current, old)
        old.jobs['export'] = {'status': 'working'}
        with self.assertRaises(ValueError):
            self.manager.switch(self.b)
        self.assertIs(self.manager.current, old)

    def test_folder_listing_and_http_switch_reject_stale_tabs(self):
        listing = self.manager.folders(str(self.root))
        self.assertIn('Первая папка', [entry['name'] for entry in listing['folders']])
        self.assertNotIn('same.png', [entry['name'] for entry in self.manager.folders(str(self.a))['folders']])
        server = LocalHTTPServer(('127.0.0.1', 0), handler_for(self.manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def request(path, data=None, token=None):
            return urlopen(Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                   headers={'X-Local-Token': token} if token else {}), timeout=5)

        with request('/api/library') as response:
            old = json.load(response)
        with self.assertRaises(HTTPError):
            request('/api/folders', {'path': str(self.root)})
        with request('/api/folder', {'path': str(self.b)}, old['token']) as response:
            self.assertTrue(json.load(response)['ok'])
        with request('/api/library') as response:
            new = json.load(response)
        self.assertEqual(new['folderPath'], str(self.b.resolve()))
        self.assertNotEqual(new['token'], old['token'])
        with self.assertRaises(HTTPError) as error:
            request('/api/state', old['state'], old['token'])
        self.assertEqual(error.exception.code, 409)
        ident = old['photos'][0]['id']
        with self.assertRaises(HTTPError) as error:
            request(f'/photo/{ident}?project={old["project"]}')
        self.assertEqual(error.exception.code, 409)
        with request(f'/photo/{ident}?project={new["project"]}') as response:
            self.assertEqual(response.read()[:2], b'\xff\xd8')

    def test_channel_check_does_not_block_saving_status_or_cancel(self):
        library = self.manager.current
        entered, release = threading.Event(), threading.Event()
        def check():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test check was not released')
            return {'ok': True}
        server = LocalHTTPServer(('127.0.0.1', 0), handler_for(self.manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, value):
            req = Request(f'http://127.0.0.1:{server.server_port}'+path,
                          data=json.dumps(value).encode(), headers={'X-Local-Token': library.token})
            with urlopen(req, timeout=3) as response:
                return json.load(response)
        try:
            with patch.object(library.publisher, 'check', side_effect=check), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(request, '/api/telegram/check', {})
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(request('/api/telegram/status', {})['busy'])
                    state = json.loads(json.dumps(library.state))
                    self.assertEqual(request('/api/state', state)['revision'], 1)
                    self.assertTrue(request('/api/telegram/cancel', {})['ok'])
                    with self.assertRaises(HTTPError) as error:
                        request('/api/telegram/container/action', {'action': 'stop'})
                    self.assertEqual(error.exception.code, 400)
                finally:
                    release.set()
                self.assertTrue(future.result()['ok'])
                self.assertEqual(self.manager.channel_checks, 0)
        finally:
            release.set()
            server.shutdown(); server.server_close(); thread.join(5)


class DefaultFolderTest(unittest.TestCase):
    def test_launcher_defaults_to_working_directory_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            (root / 'requirements.txt').write_bytes(b'')
            (root / '.venv/photo2telegram-requirements.sha256').write_text(hashlib.sha256(b'').hexdigest())
            with patch.object(launch, 'ROOT', root), patch.object(sys, 'argv', ['launch.py']), \
                    patch('src.launch.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch('src.launch.subprocess.call', return_value=0) as call, \
                    patch('src.launch.os.execv', side_effect=SystemExit(0)) as execute:
                if os.name == 'nt':
                    self.assertEqual(launch.main(), 0)
                    command = call.call_args.args[0]
                else:
                    with self.assertRaises(SystemExit):
                        launch.main()
                    command = execute.call_args.args[1]
                self.assertEqual(command[command.index('--root') + 1], str(Path.cwd()))
