import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from src.server import Library


class VideoAudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'clip.mov'
        shutil.copyfile(Path(__file__).parent / 'fixtures/03-motion.mov', self.source)
        self.library = Library(self.root, self.root / 'data')
        self.publisher = self.library.publisher
        self.ident = self.library.photos[0]['id']
        self.library.state.update(active='post', posts=[dict(id='post', title='Video', caption='', photos=[self.ident])])
        self.publisher.outbox.mkdir(exist_ok=True)

    def mute(self, enabled=True):
        state = copy.deepcopy(self.library.state)
        state['mutedVideos'] = {self.ident: True} if enabled else {}
        self.library.save(state)

    def audio_source(self, codec='libx264'):
        if not self.library.ffmpeg or not self.library.ffprobe:
            self.skipTest('FFmpeg/FFprobe are optional and not installed')
        subprocess.run([self.library.ffmpeg, '-nostdin', '-v', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc=size=160x96:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-t', '1', '-c:v', codec, '-pix_fmt', 'yuv420p', '-threads', '1', '-c:a', 'aac', str(self.source)], check=True)
        self.library.refresh()

    def streams(self, source):
        return json.loads(subprocess.check_output([self.library.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(source)]))['streams']

    def packets(self, source):
        return json.loads(subprocess.check_output([self.library.ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_packets', '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash', '-of', 'json', str(source)]))['packets']

    def test_persistence_validation_and_optional_ffmpeg(self):
        self.mute()
        restored = Library(self.root, self.root / 'data')
        self.assertEqual(restored.state['mutedVideos'], {self.ident: True})
        for invalid in ([], {'unknown': True}, {self.ident: 'yes'}):
            with self.assertRaises(ValueError):
                self.library.save(dict(self.library.state, mutedVideos=invalid))
        self.library.ffmpeg = None
        with self.assertRaisesRegex(ValueError, 'удаления звука нужен FFmpeg'):
            self.publisher._prepare_media(self.source, dict(mode='local'))

    def test_muting_remuxes_without_changing_video_and_uses_separate_cache(self):
        self.audio_source()
        original = self.source.read_bytes()
        normal_key = self.publisher._media_cache_key(self.source, dict(mode='local'))
        self.assertIn('audio', [s['codec_type'] for s in self.streams(self.source)])
        self.mute()
        media = self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertNotEqual(normal_key, media['cache_key'])
        self.assertEqual([s['codec_type'] for s in self.streams(media['path'])], ['video'])
        self.assertEqual(self.packets(self.source), self.packets(media['path']))
        self.assertEqual(self.source.read_bytes(), original)
        with patch.object(self.publisher, '_prepare_media_file', side_effect=AssertionError('Must reuse muted cache')):
            reused = self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertTrue(reused['cached'])
        self.mute(False)
        self.assertEqual(self.publisher._media_cache_key(self.source, dict(mode='local')), normal_key)
        unmuted = self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertIn('audio', [s['codec_type'] for s in self.streams(unmuted['path'])])
        self.mute()
        job = self.library.jobs[self.library.export()]
        deadline = time.monotonic()+10
        while job['status']=='working' and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(job['status'], 'done')
        exported = next(Path(job['path']).glob('*/*.mov'))
        self.assertEqual(exported.read_bytes(), original)

    def test_change_during_preparation_does_not_commit_wrong_cache(self):
        self.audio_source()
        self.mute()
        key = self.publisher._media_cache_key(self.source, dict(mode='local'))
        original_prepare = self.publisher._prepare_media_file
        def changing(source, settings, target):
            self.mute(False)
            return original_prepare(source, settings, target)
        with patch.object(self.publisher, '_prepare_media_file', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'звука'):
                self.publisher._prepare_media(self.source, dict(mode='local'))
        self.assertFalse((self.publisher.media_cache / (key+'.json')).exists())

    def test_prepared_post_becomes_unready_when_audio_choice_changes(self):
        self.audio_source()
        self.publisher.save_settings(dict(token='12345:test', channel='@channel', mode='cloud'))
        with patch.object(self.publisher, '_check_destination', return_value=dict(chat_id=-10012345, bot_id=12345, channel='Channel', bot='bot')):
            self.publisher.prepare()
            deadline=time.monotonic()+10
            while self.publisher.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.publisher.busy())
        self.assertEqual(self.publisher.status()['job']['items'][0]['status'], 'ready')
        self.mute()
        self.assertNotEqual(self.publisher.status()['job']['items'][0]['status'], 'ready')
        with self.assertRaises(ValueError):
            self.publisher.send(self.publisher.job['id'])

    def test_incompatible_video_is_converted_without_audio(self):
        self.audio_source('mpeg4')
        original = self.source.read_bytes()
        self.mute()
        media = self.publisher._prepare_media(self.source, dict(mode='local'))
        streams = self.streams(media['path'])
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]['codec_name'], 'h264')
        self.assertEqual((streams[0]['width'], streams[0]['height']), (160, 96))
        self.assertEqual(self.source.read_bytes(), original)
