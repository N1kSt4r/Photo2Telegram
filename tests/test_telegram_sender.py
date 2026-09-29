import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

from src.server import Library, LocalHTTPServer
from src.telegram_sender import BotClient, Publisher, TelegramError, validated_settings

TOKEN = '12345:test-token'
DESTINATION = dict(bot='test_bot', bot_id=12345, chat_id=-1001234567890, channel='Тестовый канал', username='test_channel')


class FakeBot:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.denied = False

    def check(self):
        if self.denied:
            raise ValueError('Нет права публикации')
        return DESTINATION.copy()

    def call(self, method, fields=None, files=None, progress=None):
        self.calls.append((method, copy.deepcopy(fields), {k: v.read_bytes() for k, v in (files or {}).items()}))
        if self.fail:
            raise self.fail
        count = len(fields['media']) if method == 'sendMediaGroup' else 1
        return [{'message_id': 100 + i} for i in range(count)] if count > 1 else {'message_id': 100}


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['01-pattern.png', '02-blue.jpg']:
            shutil.copyfile(Path(__file__).parent / 'fixtures' / name, self.root / name)
        self.library = Library(self.root, self.root / 'data')
        self.publisher = self.library.publisher
        self.fake = FakeBot()
        self.publisher.client_factory = lambda _: self.fake
        self.publisher.save_settings(dict(token=TOKEN, channel='@test_channel', mode='cloud'))
        self.library.state.update(active='one', posts=[dict(id='one', title='Первый', caption='Подпись', photos=[p['id'] for p in self.library.photos])])

    def test_photo_resizes_keeps_orientation_and_quality(self):
        from PIL import Image, JpegImagePlugin
        source = self.root / 'large.png'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (3200, 2000), (80, 160, 220)).save(source, exif=exif)
        before = source.read_bytes()
        target = self.root / 'prepared.jpg'
        self.publisher._prepare_media_file(source, dict(mode='local'), target)
        with Image.open(target) as image:
            self.assertEqual(image.size, (1600, 2560))
            self.assertEqual(JpegImagePlugin.get_sampling(image), 2)
            self.assertLessEqual(image.quantization[0][0], 2)
        self.assertEqual(source.read_bytes(), before)

    def test_small_photo_is_not_upscaled(self):
        from PIL import Image
        source = self.root / 'small.png'
        Image.new('RGB', (640, 480)).save(source)
        target = self.root / 'prepared.jpg'
        self.publisher._prepare_media_file(source, dict(mode='local'), target)
        with Image.open(target) as image:
            self.assertEqual(image.size, (640, 480))

    def wait(self):
        deadline = time.monotonic() + 10
        while self.publisher._working and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.publisher._working)
        return self.publisher.status()['job']

    def prepare(self):
        self.publisher.prepare()
        return self.wait()

    def test_prepare_does_not_publish_album_preserves_order_and_originals(self):
        originals = {p: p.read_bytes() for p in self.library.files.values()}
        job = self.prepare()
        self.assertEqual(job['status'], 'ready')
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn(TOKEN, json.dumps(self.publisher.status()))
        self.assertTrue(job['items'][0]['files'][0]['converted'])
        # Draft edits before sending apply without reconverting media.
        self.library.state['posts'][0]['caption'] = 'Изменённый черновик'
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        method, fields, files = self.fake.calls[0]
        self.assertEqual(method, 'sendMediaGroup')
        self.assertEqual(fields['chat_id'], DESTINATION['chat_id'])
        self.assertEqual(fields['media'][0]['caption'], 'Изменённый черновик')
        self.assertNotIn('caption', fields['media'][1])
        self.assertEqual([m['media'] for m in fields['media']], ['attach://file0', 'attach://file1'])
        self.assertTrue(all(content.startswith(b'\xff\xd8') for content in files.values()))
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(self.publisher.outbox.exists())
        self.assertEqual(self.prepare()['skipped'], 1)
        self.assertEqual(len(self.fake.calls), 1)
        restored = Publisher(self.library, self.publisher.save_jpeg)
        self.assertEqual(next(iter(restored.journal.values()))['status'], 'sent')

    def test_photo_preparation_parallel_across_posts_preserves_order(self):
        photo = self.library.photos[0]['id']
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption='', photos=[photo]) for i in range(3)]
        barrier = threading.Barrier(3)
        original = self.publisher._prepare_media
        active = 0
        peak = 0
        lock = threading.Lock()
        def concurrent(source, settings):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return original(source, settings)
            finally:
                with lock:
                    active -= 1
        self.publisher._prepare_media = concurrent
        job = self.prepare()
        self.assertEqual(job['status'], 'ready')
        self.assertEqual(peak, 3)
        self.assertEqual([p['id'] for p in job['items']], ['0', '1', '2'])
        self.assertEqual([p['post']['id'] for p in self.publisher.job['_prepared']], ['0', '1', '2'])
        self.assertEqual(self.fake.calls, [])
        self.publisher.cancel()

    def test_recent_check_reused_and_settings_change_invalidates(self):
        from unittest.mock import Mock
        self.fake.check = Mock(return_value=DESTINATION.copy())
        self.publisher.check()
        self.publisher.save_settings(dict(token='', channel='@test_channel', mode='cloud'))
        self.prepare()
        self.assertEqual(self.fake.check.call_count, 1)
        self.publisher.cancel()
        self.publisher.save_settings(dict(token='', channel='@another_channel', mode='cloud'))
        self.prepare()
        self.assertEqual(self.fake.check.call_count, 2)
        self.publisher.cancel()
        self.publisher.check()
        self.assertEqual(self.fake.check.call_count, 3)

    def test_shared_pool_runs_twelve_mixed_files(self):
        for i in range(12):
            path = self.root / f'{i}.mp4' if i % 2 else self.root / f'{i}.jpg'
            path.write_bytes(b'test')
            self.library.files[str(i)] = path
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption='', photos=[str(i)]) for i in range(12)]
        barrier = threading.Barrier(12)
        def prepare(source, settings):
            barrier.wait(timeout=5)
            target = self.publisher.outbox / source.name
            target.write_bytes(b'prepared')
            return dict(path=target, name=source.name, kind='video' if source.suffix=='.mp4' else 'photo', converted=True)
        self.publisher._prepare_media = prepare
        job = self.prepare()
        self.assertEqual([p['status'] for p in job['items']], ['ready'] * 12)
        self.publisher.cancel()

    def test_cancel_running_preparation_releases_busy_and_allows_retry(self):
        entered, release = threading.Event(), threading.Event()
        original = self.publisher._prepare_media
        def slow(source, settings):
            entered.set()
            release.wait(5)
            return original(source, settings)
        self.publisher._prepare_media = slow
        self.publisher.prepare()
        self.assertTrue(entered.wait(5))
        self.publisher.cancel()
        self.assertEqual(self.publisher.status()['job']['status'], 'cancelling')
        self.assertTrue(self.publisher.status()['busy'])
        release.set()
        self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertFalse(self.publisher.status()['busy'])
        self.assertFalse(self.publisher.outbox.exists())
        self.publisher._prepare_media = original
        self.assertEqual(self.prepare()['status'], 'ready')
        self.publisher.cancel()

    def test_cancel_terminates_running_media_process(self):
        import sys
        errors = []
        def run():
            try:
                self.publisher._run_media_tool([sys.executable, '-c', 'import time; time.sleep(60)'], 90)
            except ValueError as error:
                errors.append(str(error))
        thread = threading.Thread(target=run)
        thread.start()
        self.publisher.cancelled.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['Подготовка отменена'])

    def test_disk_cache_survives_cancel_restart_and_is_removed_after_send(self):
        job = self.prepare()
        self.assertFalse(job['items'][0]['files'][0]['cached'])
        self.publisher.cancel()
        self.assertTrue(list(self.publisher.media_cache.glob('*.json')))
        self.publisher = Publisher(self.library, self.publisher.save_jpeg)
        self.publisher.client_factory = lambda _: self.fake
        with patch.object(self.publisher, '_prepare_media_file', side_effect=AssertionError('Cache was not reused')):
            job = self.prepare()
        self.assertTrue(all(f['cached'] for f in job['items'][0]['files']))
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(list(self.publisher.media_cache.iterdir()), [])

    def test_changed_source_invalidates_cache_and_failed_send_retains_it(self):
        self.prepare();self.publisher.cancel()
        source = next(iter(self.library.files.values()))
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        job = self.prepare()
        self.assertEqual(sum(not f['cached'] for f in job['items'][0]['files']), 1)
        self.fake.fail = TelegramError('Rejected')
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'error')
        self.assertTrue(list(self.publisher.media_cache.glob('*.json')))
        job = self.prepare()
        self.assertTrue(all(f['cached'] for f in job['items'][0]['files']))
        self.publisher.cancel()

    def test_same_cached_media_in_two_posts_survives_first_publication(self):
        photo = self.library.photos[0]['id']
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption='', photos=[photo]) for i in range(2)]
        job = self.prepare()
        self.assertEqual(len(list(self.publisher.media_cache.glob('*.json'))), 1)
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(len(self.fake.calls), 2)

    def test_stopped_preparation_keeps_complete_posts_sendable(self):
        first, second = list(self.library.files)[:2]
        self.library.state['posts'] = [
            dict(id='first', title='First', caption='Snapshot', photos=[first]),
            dict(id='partial', title='Partial', caption='', photos=[first, second]),
            dict(id='last', title='Last', caption='last', photos=[])]
        entered, release = threading.Event(), threading.Event()
        original = self.publisher._prepare_unless_cancelled
        def prepare(source, settings):
            if source == self.library.files[second]:
                entered.set()
                release.wait(5)
            return original(source, settings)
        self.publisher._prepare_unless_cancelled = prepare
        self.publisher.prepare()
        self.assertTrue(entered.wait(5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = self.publisher.status()['job']['items']
            if items and items[0]['status'] == 'ready':
                break
            time.sleep(.01)
        self.assertEqual(items[0]['status'], 'ready')
        self.publisher.cancel()
        release.set()
        job = self.wait()
        self.assertEqual(job['status'], 'ready')
        self.assertTrue(job['stopped'])
        self.assertEqual([p['status'] for p in job['items']], ['ready', 'unprepared', 'ready'])
        self.assertTrue(self.publisher.job['_prepared'][0]['media'][0]['path'].exists())
        with self.assertRaises(ValueError):
            self.publisher.send(job['id'], ['partial'])
        self.library.state['posts'][0]['caption'] = 'Changed after preparation'
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.calls[0][1]['caption'], 'Changed after preparation')

    def test_failed_send_resumes_same_snapshot_and_updates_posts_live(self):
        photo = self.library.photos[0]['id']
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption=str(i), photos=[photo]) for i in range(3)]
        job = self.prepare()
        original = self.fake.call
        def fail_second(method, fields, files, progress):
            if fields['caption'] == '1':
                live = self.publisher.status()['job']
                self.assertEqual(live['sent'], 1)
                self.assertEqual([p['status'] for p in live['items']], ['sent', 'sending', 'ready'])
                raise TelegramError('Rejected')
            return original(method, fields, files, progress)
        self.fake.call = fail_second
        self.publisher.send(job['id'])
        failed = self.wait()
        self.assertEqual(failed['status'], 'error')
        self.assertTrue(failed['can_resume'])
        self.assertTrue(self.publisher.outbox.exists())
        self.fake.call = original
        self.library.state['posts'][1]['caption'] = 'Edited draft'
        with patch.object(self.publisher, '_prepare_media', side_effect=AssertionError('Must not prepare again')):
            self.publisher.send(job['id'])
            self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual([call[1]['caption'] for call in self.fake.calls], ['0', 'Edited draft', '2'])
        self.assertEqual(self.publisher.status()['job']['sent'], 3)
        restored = self.prepare()
        self.assertEqual([len(p['files']) for p in restored['items']], [1, 1, 1])

    def test_unknown_send_requires_resolution_then_retries_without_preparation(self):
        job = self.prepare()
        self.fake.fail = TelegramError('Connection lost', uncertain=True)
        self.publisher.send(job['id']);self.wait()
        self.assertEqual(self.publisher.status()['job']['items'][0]['status'], 'unknown')
        with self.assertRaises(ValueError):
            self.publisher.send(job['id'], ['one'])
        self.publisher.resolve(next(iter(self.publisher.journal)), 'retry')
        self.fake.fail = None
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')

    def test_initial_overview_restores_cache_sent_and_unknown_without_network(self):
        initial = self.publisher.status()['job']
        self.assertEqual(initial['total'], 1)
        self.assertEqual(initial['items'][0]['status'], 'unprepared')
        self.prepare();self.publisher.cancel()
        restored = Publisher(self.library, self.publisher.save_jpeg)
        with patch.object(restored, 'client_factory', side_effect=AssertionError('Must stay offline')):
            overview = restored.status()['job']
        self.assertEqual(overview['items'][0]['status'], 'cached')
        self.assertTrue(all(f['bytes'] for f in overview['items'][0]['files']))
        self.publisher = restored
        self.publisher.client_factory = lambda _: self.fake
        job = self.prepare()
        self.publisher.send(job['id']);self.wait()
        restored = Publisher(self.library, self.publisher.save_jpeg)
        self.assertEqual(restored.status()['job']['items'][0]['status'], 'sent')
        self.assertTrue(restored.status()['job']['items'][0]['link'])
        key = next(iter(restored.journal))
        restored._record(key, dict(restored.journal[key], status='unknown'))
        item = restored.status()['job']['items'][0]
        self.assertEqual(item['status'], 'unknown')
        self.assertEqual(item['resolve_key'], key)
        restored.save_settings(dict(token='', channel='@different', mode='cloud'))
        self.assertNotEqual(restored.status()['job']['items'][0]['status'], 'sent')

    def test_previews_present_while_checking_channel_and_after_failure(self):
        entered, release = threading.Event(), threading.Event()
        def check():
            entered.set()
            release.wait(5)
            raise ValueError('Нет доступа к каналу')
        self.fake.check = check
        self.publisher.prepare()
        self.assertTrue(entered.wait(5))
        job = self.publisher.status()['job']
        self.assertEqual(len(job['items']), 1)
        self.assertEqual([f['id'] for f in job['items'][0]['files']], self.library.state['posts'][0]['photos'])
        release.set()
        job = self.wait()
        self.assertEqual(job['channel_status'], 'error')
        self.assertEqual(len(job['items'][0]['files']), 2)
        self.assertEqual(job['items'][0]['status'], 'ready')

    def test_failed_media_preparation_keeps_all_preview_references(self):
        with patch.object(self.publisher, '_prepare_media', side_effect=ValueError('Conversion failed')):
            job = self.prepare()
        self.assertEqual(job['items'][0]['status'], 'error')
        self.assertEqual([f['id'] for f in job['items'][0]['files']], self.library.state['posts'][0]['photos'])

    def test_prepared_file_access_is_scoped_and_survives_in_cache(self):
        job = self.prepare()
        reference = job['items'][0]['files'][0]['prepared_ref']
        path = self.publisher.prepared_file(reference)
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes()[:2], b'\xff\xd8')
        for invalid in ('../telegram-settings.json', 'telegram-settings.json', str(path), '../'+reference):
            self.assertIsNone(self.publisher.prepared_file(invalid))
        self.publisher.cancel()
        self.assertIsNone(self.publisher.prepared_file(reference))
        restored = Publisher(self.library, self.publisher.save_jpeg)
        cached_ref = restored.status()['job']['items'][0]['files'][0]['prepared_ref']
        self.assertIsNotNone(restored.prepared_file(cached_ref))

    def test_channel_check_and_files_are_independent_and_retry_reuses_files(self):
        entered, release = threading.Event(), threading.Event()
        def checking():
            entered.set()
            release.wait(5)
            raise ValueError('Network unavailable')
        self.fake.check = checking
        self.publisher.prepare()
        self.assertTrue(entered.wait(5))
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            job = self.publisher.status()['job']
            if job['items'][0]['status']=='ready':
                break
            time.sleep(.01)
        self.assertEqual(job['items'][0]['status'], 'ready')
        self.assertEqual(job['channel_status'], 'checking')
        release.set()
        job = self.wait()
        self.assertEqual(job['channel_status'], 'error')
        self.fake.check = lambda: DESTINATION.copy()
        with patch.object(self.publisher, '_prepare_media', side_effect=AssertionError('Already prepared')):
            self.publisher.check()
            self.publisher.send(job['id'])
            self.assertEqual(self.wait()['status'], 'done')

    def test_draft_reorder_and_removed_file_apply_without_preparing(self):
        job = self.prepare()
        ids = self.library.state['posts'][0]['photos']
        self.library.state['posts'][0]['photos'] = list(reversed(ids))
        self.library.state['posts'][0]['caption'] = 'New caption'
        with patch.object(self.publisher, '_prepare_media', side_effect=AssertionError('Already prepared')):
            status = self.publisher.status()['job']
            self.assertEqual([f['id'] for f in status['items'][0]['files']], list(reversed(ids)))
            self.library.state['posts'][0]['photos'] = [ids[1]]
            self.publisher.send(job['id']);self.wait()
        self.assertEqual(self.fake.calls[0][0], 'sendPhoto')
        self.assertEqual(self.fake.calls[0][1]['caption'], 'New caption')

    def test_partial_post_identifies_failed_file_and_keeps_ready_sibling(self):
        original = self.publisher._prepare_media
        failed_source = self.library.files[self.library.state['posts'][0]['photos'][1]]
        def convert(source, settings):
            if source == failed_source:
                raise ValueError('Test codec failure')
            return original(source, settings)
        self.publisher._prepare_media = convert
        job = self.prepare()
        files = job['items'][0]['files']
        self.assertEqual(job['items'][0]['status'], 'error')
        self.assertEqual([f['status'] for f in files], ['ready', 'error'])
        self.assertEqual(files[1]['error'], 'Test codec failure')
        self.assertTrue(self.publisher.prepared_file(files[0]['prepared_ref']).exists())
        self.assertFalse(files[0].get('error'))

    def test_send_ready_after_restart_checks_channel_without_reconverting(self):
        self.prepare();self.publisher.cancel()
        restored = Publisher(self.library, self.publisher.save_jpeg)
        restored.client_factory = lambda _: self.fake
        self.publisher = restored
        self.assertEqual(restored.status()['job']['items'][0]['status'], 'cached')
        with patch.object(restored, '_prepare_media_file', side_effect=AssertionError('Must use ready cache')):
            restored.send_ready(['one'])
            self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(len(self.fake.calls), 1)

    def test_ready_send_refuses_modified_file_and_invalid_caption(self):
        job = self.prepare()
        path = self.publisher.job['_prepared'][0]['media'][0]['path']
        path.write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'изменился'):
            self.publisher.send(job['id'])
        self.assertEqual(self.fake.calls, [])
        self.publisher.cancel()
        self.library.state['posts'][0]['caption'] = 'x'*1025
        # The stale cache cannot be advertised as ready after its size changed.
        with self.assertRaises(ValueError):
            self.publisher.send_ready(['one'])

    def test_send_ready_keeps_files_when_channel_check_fails(self):
        self.prepare();self.publisher.cancel()
        self.fake.denied = True
        self.publisher.send_ready(['one'])
        job = self.wait()
        self.assertEqual(job['channel_status'], 'error')
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.publisher.outbox.exists())

    def test_selected_send_keeps_all_posts_visible_during_and_after_send(self):
        photo = self.library.photos[0]['id']
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption=str(i), photos=[photo]) for i in range(3)]
        self.prepare();self.publisher.cancel()
        self.publisher = Publisher(self.library, self.publisher.save_jpeg)
        self.publisher.client_factory = lambda _: self.fake
        entered, release = threading.Event(), threading.Event()
        original = self.fake.call
        def blocked(*args, **kwargs):
            entered.set();release.wait(5)
            return original(*args, **kwargs)
        self.fake.call = blocked
        self.publisher.send_ready(['1'])
        self.assertTrue(entered.wait(5))
        try:
            job = self.publisher.status()['job']
            self.assertEqual([p['id'] for p in job['items']], ['0','1','2'])
            self.assertEqual([p['status'] for p in job['items']], ['cached','sending','cached'])
        finally:
            release.set()
        job = self.wait()
        self.assertEqual([p['id'] for p in job['items']], ['0','1','2'])
        self.assertEqual([p['status'] for p in job['items']], ['cached','sent','cached'])
        self.assertEqual(job['sent'], 1)
        self.assertEqual(len(self.fake.calls), 1)
        self.publisher.send_ready(['0','2'])
        job = self.wait()
        self.assertEqual([p['status'] for p in job['items']], ['sent']*3)
        self.assertEqual(len(self.fake.calls), 3)

    def mixed_posts(self):
        self.library.state['posts'] = [
            dict(id='first', title='Первый', caption='first', photos=[]),
            dict(id='bad', title='Ошибка', caption='x' * 4097, photos=[]),
            dict(id='last', title='Последний', caption='last', photos=[])]

    def test_prepare_continues_after_error_and_default_sends_prefix(self):
        self.mixed_posts()
        job = self.prepare()
        self.assertEqual([p['status'] for p in job['items']], ['ready', 'error', 'ready'])
        self.assertEqual(self.fake.calls, [])
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual([c[1]['text'] for c in self.fake.calls], ['first'])
        job = self.prepare()
        self.assertEqual([p['status'] for p in job['items']], ['sent', 'error', 'ready'])
        with self.assertRaises(ValueError):
            self.publisher.send(job['id'])
        self.publisher.send(job['id'], ['last'])
        self.wait()
        self.assertEqual([c[1]['text'] for c in self.fake.calls], ['first', 'last'])

    def test_selection_validated_and_runtime_failure_stops_later_posts(self):
        self.mixed_posts()
        job = self.prepare()
        for ids in ([], ['bad'], ['missing'], 'first'):
            with self.assertRaises(ValueError):
                self.publisher.send(job['id'], ids)
        self.fake.fail = TelegramError('Rejected')
        self.publisher.send(job['id'], ['last', 'first'])
        self.assertEqual(self.wait()['status'], 'error')
        self.assertEqual([c[1]['text'] for c in self.fake.calls], ['first'])

    def test_failed_media_copies_removed_without_removing_ready_copies(self):
        photo = self.library.photos[0]['id']
        self.library.state['posts'] = [
            dict(id='bad', title='Bad', caption='', photos=[photo, 'missing']),
            dict(id='good', title='Good', caption='', photos=[photo])]
        job = self.prepare()
        self.assertEqual([p['status'] for p in job['items']], ['error', 'ready'])
        self.assertEqual(len(list(self.publisher.outbox.iterdir())), 1)
        self.publisher.cancel()

    def test_text_and_single_photo_methods(self):
        self.library.state['posts'][0]['photos'] = []
        job = self.prepare()
        self.publisher.send(job['id']);self.wait()
        self.assertEqual(self.fake.calls[-1][0], 'sendMessage')
        self.library.state['posts'] = [dict(id='two', title='Фото', caption='', photos=[self.library.photos[0]['id']])]
        job = self.prepare()
        self.publisher.send(job['id']);self.wait()
        self.assertEqual(self.fake.calls[-1][0], 'sendPhoto')

    def test_uncertain_outcome_blocks_retries_until_manual_resolution(self):
        self.fake.fail = TelegramError('Обрыв связи', uncertain=True)
        job = self.prepare()
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'error')
        key = next(iter(self.publisher.journal))
        self.assertEqual(self.publisher.journal[key]['status'], 'unknown')
        self.assertEqual(self.prepare()['status'], 'error')
        self.assertEqual(len(self.fake.calls), 1)
        self.publisher.resolve(key, 'retry')
        self.fake.fail = None
        job = self.prepare()
        self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(len(self.fake.calls), 2)

    def test_interrupted_send_is_unknown_after_restart(self):
        self.publisher._record('key', dict(status='sending', post_id='one'))
        restored = Publisher(self.library, self.publisher.save_jpeg)
        self.assertEqual(restored.journal['key']['status'], 'unknown')

    def test_known_rejection_cancel_and_validation(self):
        job = self.prepare()
        self.publisher.save_settings(dict(token=TOKEN, channel='@test_channel', mode='cloud'))
        self.publisher.cancel()
        self.assertEqual(self.publisher.status()['job']['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.publisher.send(job['id'])
        self.library.state['posts'][0]['caption'] = 'x' * 1025
        self.assertEqual(self.prepare()['status'], 'error')
        self.assertFalse(self.fake.calls)
        self.library.state['posts'][0]['caption'] = ''
        self.fake.fail = TelegramError('Bad Request')
        job = self.prepare();self.publisher.send(job['id']);self.wait()
        self.assertEqual(next(iter(self.publisher.journal.values()))['status'], 'error')

    def test_local_video_limit_and_streaming_source_copy(self):
        video = self.root / 'large.mp4'
        with video.open('wb') as stream:
            stream.truncate(50_000_001)
        self.library.ffprobe = None
        self.publisher.outbox.mkdir()
        with self.assertRaisesRegex(ValueError, '50 МБ'):
            self.publisher._prepare_media(video, dict(mode='cloud'))
        media = self.publisher._prepare_media(video, dict(mode='local'))
        self.assertEqual(media['path'].stat().st_size, 50_000_001)
        self.assertFalse(media['converted'])

    def test_credentials_are_private_and_blank_token_preserves_value(self):
        self.publisher.save_settings(dict(token='', channel='@test_channel', mode='local', endpoint='http://127.0.0.1:8081'))
        self.assertEqual(self.publisher.settings['token'], TOKEN)
        self.assertNotIn('token', self.publisher.public_settings())
        if os.name != 'nt':
            self.assertEqual(self.publisher.settings_path.stat().st_mode & 0o777, 0o600)
        for url in ['ftp://localhost', 'http://user:pass@localhost', 'http://localhost/botSECRET', 'http://localhost?token=secret']:
            with self.assertRaises(ValueError):
                validated_settings(dict(token=TOKEN, channel='@channel', mode='local', endpoint=url), {})

    def test_stop_after_current_and_resume_skips_completed(self):
        self.library.state['posts'] = [dict(id=str(i), title=str(i), caption='Текст', photos=[]) for i in range(2)]
        original = self.fake.call
        def stopping(*args, **kwargs):
            result = original(*args, **kwargs)
            self.publisher.cancel()
            return result
        self.fake.call = stopping
        job = self.prepare();self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertEqual(len(self.fake.calls), 1)
        self.fake.call = original
        job = self.prepare()
        self.assertEqual(job['skipped'], 1)
        self.publisher.send(job['id']);self.wait()
        self.assertEqual(len(self.fake.calls), 2)

    def test_no_rights_or_missing_file_never_publishes(self):
        self.fake.denied = True
        failed = self.prepare()
        self.assertEqual(failed['channel_status'], 'error')
        self.assertEqual(failed['items'][0]['status'], 'ready')
        with self.assertRaises(ValueError):
            self.publisher.send(failed['id'])
        self.fake.denied = False
        next(iter(self.library.files.values())).unlink()
        self.assertEqual(self.prepare()['status'], 'error')
        self.assertFalse(self.fake.calls)

    def test_rate_limit_retries_only_confirmed_rejection(self):
        original = self.fake.call
        count = 0
        def limited(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise TelegramError('Too many requests', retry_after=1)
            return original(*args, **kwargs)
        self.fake.call = limited
        job = self.prepare();self.publisher.send(job['id'])
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(count, 2)
        self.assertEqual(len(self.fake.calls), 1)

    def test_optional_video_conversion(self):
        if not self.library.ffmpeg:
            self.skipTest('FFmpeg is optional')
        source = Path(__file__).parent / 'fixtures' / '03-motion.mov'
        self.publisher.outbox.mkdir()
        before = source.read_bytes()
        media = self.publisher._prepare_media(source, dict(mode='cloud'))
        self.assertEqual(media['kind'], 'video')
        self.assertGreater(media['path'].stat().st_size, 0)
        self.assertEqual(source.read_bytes(), before)

    def test_journal_failure_before_request_never_sends(self):
        job = self.prepare()
        with patch('src.telegram_sender.write_json', side_effect=OSError('Disk full')):
            self.publisher.send(job['id'])
            self.assertEqual(self.wait()['status'], 'error')
        self.assertFalse(self.fake.calls)
        self.assertFalse(self.publisher.journal)

    def test_journal_failure_after_upload_remains_unknown(self):
        from src.telegram_sender import write_json
        job = self.prepare()
        writes = 0
        def fail_after_first(path, value, **kwargs):
            nonlocal writes
            writes += 1
            if writes > 1:
                raise OSError('Disk full')
            return write_json(path, value, **kwargs)
        with patch('src.telegram_sender.write_json', side_effect=fail_after_first):
            self.publisher.send(job['id'])
            self.assertEqual(self.wait()['status'], 'error')
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(next(iter(self.publisher.journal.values()))['status'], 'unknown')
        restored = Publisher(self.library, self.publisher.save_jpeg)
        self.assertEqual(next(iter(restored.journal.values()))['status'], 'unknown')


class TransportTest(unittest.TestCase):
    def test_real_multipart_transport_and_read_only_permission_check(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                method = self.path.rsplit('/', 1)[-1]
                calls.append((method, body))
                if method in ('getMe', 'getChat', 'getChatMember'):
                    if self.headers['Content-Type'] != 'application/json':
                        self.send_response(400);self.end_headers();self.wfile.write(b'<html>Bad multipart request</html>')
                        return
                    json.loads(body)
                results = {'getMe': {'id': 12345, 'username': 'test_bot'},
                           'getChat': {'id': -1001234567890, 'title': 'Канал', 'type': 'channel'},
                           'getChatMember': {'status': 'administrator', 'can_post_messages': True},
                           'sendPhoto': {'message_id': 1}}
                data = json.dumps({'ok': True, 'result': results[method]}).encode()
                self.send_response(200);self.send_header('Content-Length', str(len(data)));self.end_headers();self.wfile.write(data)
        server = LocalHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(thread.join, 5);self.addCleanup(server.shutdown)
        client = BotClient(dict(endpoint=f'http://127.0.0.1:{server.server_port}', token=TOKEN, channel='@channel'))
        self.assertEqual(client.check()['chat_id'], -1001234567890)
        self.assertCountEqual([c[0] for c in calls[:2]], ['getMe', 'getChat'])
        self.assertEqual(calls[2][0], 'getChatMember')
        path = Path(__file__).parent / 'fixtures' / '02-blue.jpg'
        progress = []
        result = client.call('sendPhoto', {'chat_id': -1001234567890, 'photo': 'attach://file0'}, {'file0': path},
                             lambda sent, total: progress.append((sent, total)))
        self.assertEqual(progress[0][0], 0)
        self.assertEqual(progress[-1][0], progress[-1][1])
        self.assertGreater(progress[-1][0], path.stat().st_size)
        self.assertEqual(result['message_id'], 1)
        self.assertIn(path.read_bytes(), calls[-1][1])
        self.assertIn(b'name="file0"; filename="file0.jpg"', calls[-1][1])


    def test_non_json_error_distinguishes_check_from_publishing(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 400
        response.getheader.return_value = 'text/html'
        response.read.return_value = b'<html>Bad Request</html>'
        client = BotClient(dict(endpoint='http://127.0.0.1:8081', token=TOKEN))
        with patch('src.telegram_sender.http.client.HTTPConnection', return_value=connection):
            for method, uncertain in [('getMe', False), ('sendMessage', True)]:
                with self.subTest(method=method), self.assertRaises(TelegramError) as caught:
                    client.call(method)
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assertIn(method, str(caught.exception))
                self.assertIn('HTTP 400', str(caught.exception))
                self.assertIn('text/html', str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
