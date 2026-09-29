import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.bot_api_container import BotAPIContainer


class ContainerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = BotAPIContainer(Path(self.temp.name))

    def result(self, stdout='', code=0):
        return subprocess.CompletedProcess([], code, stdout, 'secret must not leak')

    def test_compose_fixed_project_and_service_no_shell(self):
        docker_dir = self.controller.root / 'Docker Desktop' / 'bin'
        executable = str(docker_dir / ('docker.exe' if os.name == 'nt' else 'docker'))
        original_path = str(self.controller.root / 'other-bin')
        with patch.dict(os.environ, {'PATH': original_path}), patch('src.bot_api_container.shutil.which', return_value=executable), patch('src.bot_api_container.subprocess.run', return_value=self.result()) as run:
            self.controller._compose('stop', 'telegram-bot-api')
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], ['stop', 'telegram-bot-api'])
            self.assertIn(str(self.controller.root / 'compose.yaml'), args[0])
            self.assertIn('photo2telegram', args[0])
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(args[0][0], executable)
            self.assertEqual(kwargs['env']['PATH'], str(docker_dir) + os.pathsep + original_path)

    def test_running_and_stopped_compose_formats(self):
        for output, status in [('[{"State":"running"}]', 'running'), ('{"State":"exited"}\n', 'stopped'), ('', 'stopped')]:
            with self.subTest(output=output), patch.object(self.controller, '_compose', return_value=self.result(output)):
                self.controller._inspect()
                self.assertEqual(self.controller.state['status'], status)

    def test_start_applies_compose_configuration(self):
        with patch.object(self.controller, '_check'), patch.object(self.controller, '_compose', return_value=self.result()) as compose, patch.object(self.controller, '_inspect'):
            self.controller._operate('start')
            compose.assert_called_once_with('up', '-d', 'telegram-bot-api', timeout=600)
            self.assertFalse(self.controller.busy())

    def test_stop_preserves_volume(self):
        with patch.object(self.controller, '_check'), patch.object(self.controller, '_compose', return_value=self.result()) as compose, patch.object(self.controller, '_inspect'):
            self.controller._operate('stop')
            compose.assert_called_once_with('stop', 'telegram-bot-api', timeout=60)

    def test_error_does_not_leak_output(self):
        with patch.object(self.controller, '_check'), patch.object(self.controller, '_compose', return_value=self.result(code=1)):
            self.controller._operate('start')
        self.assertEqual(self.controller.state['status'], 'error')
        self.assertNotIn('secret', self.controller.state['message'])
        self.assertFalse(self.controller.busy())

    def test_timeout_clears_busy(self):
        self.controller.active = True
        with patch.object(self.controller, '_check', side_effect=subprocess.TimeoutExpired(['docker'], 20, output='secret')):
            self.controller._operate('start')
        self.assertFalse(self.controller.busy())
        self.assertNotIn('secret', self.controller.state['message'])

    def test_reject_unknown_and_concurrent_actions(self):
        with self.assertRaises(ValueError):
            self.controller.action('delete')
        self.controller.active = True
        with self.assertRaises(ValueError):
            self.controller.action('stop')

    def test_missing_env(self):
        with patch.object(self.controller, '_run', return_value=self.result()), self.assertRaisesRegex(ValueError, '.env'):
            self.controller._check()


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        from src.telegram_credentials import TelegramCredentials
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.env'
        self.store = TelegramCredentials(self.path)

    def test_preserves_other_values_and_hides_secrets(self):
        self.path.write_text('# keep\nOTHER=value\nTELEGRAM_BOT_TOKEN_ABC=123:secret\nTELEGRAM_API_ID="123"\n')
        result = self.store.save_api({'api_hash': 'a'*32})
        self.assertTrue(result['has_api_id'])
        self.assertNotIn('a'*32, str(result))
        self.assertEqual(self.store.read()['TELEGRAM_BOT_TOKEN_ABC'], '123:secret')
        self.assertIn('OTHER=value', self.path.read_text())
        self.assertFalse(self.store.save_api({})['changed'])
        import os
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_keys_do_not_change_file(self):
        self.store.save_api({'api_id':'123', 'api_hash':'a'*32})
        before = self.path.read_bytes()
        for value in ({'api_id':'1\nEVIL=x'}, {'api_hash':'bad'}, {'api_id':'0'}):
            with self.assertRaises(ValueError):
                self.store.save_api(value)
            self.assertEqual(self.path.read_bytes(), before)

    def test_migration_and_folder_isolation(self):
        import json
        from src.server import Library
        root=Path(self.temp.name)
        media=root/'photos'; media.mkdir()
        data=root/'data'; data.mkdir()
        (data/'telegram-settings.json').write_text(json.dumps(dict(token='123:secret',channel='-1001',mode='cloud',endpoint='https://api.telegram.org')))
        library=Library(media,data,self.path)
        self.assertEqual(library.publisher.settings['token'],'123:secret')
        self.assertNotIn('123:secret',(data/'telegram-settings.json').read_text())
        self.assertNotIn('123:secret',str(library.publisher.public_settings()))
        other=Library(media,root/'other',self.path)
        other.publisher.save_settings(dict(token='456:other',channel='-1002',mode='cloud'))
        self.assertEqual(library.publisher.settings['token'],'123:secret')
        reopened=Library(media,data,self.path)
        self.assertEqual(reopened.publisher.settings['token'],'123:secret')

    def test_api_changes_require_apply_and_reject_busy(self):
        c=BotAPIContainer(self.path.parent)
        c.save_credentials({'api_id':'123','api_hash':'a'*32})
        self.assertTrue(c.restart_required)
        c.active=True
        with self.assertRaises(ValueError):
            c.save_credentials({'api_id':'456'})
