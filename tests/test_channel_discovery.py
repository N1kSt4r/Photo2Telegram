import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.server import Library
from src.telegram_sender import TelegramError


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.publisher=Library(root, root/'data').publisher
        self.calls=[];self.delete=True;self.webhook='';self.updates=None
        self.destination=dict(bot='bot',bot_id=12345,chat_id=-10012345,channel='Channel')
        outer=self
        class Client:
            def check(self):return outer.destination.copy()
            def call(self, method, fields=None, files=None):
                outer.calls.append((method,copy.deepcopy(fields)))
                if method=='getWebhookInfo':return dict(url=outer.webhook)
                if method=='getMe':return dict(id=12345,username='bot')
                if method=='getUpdates':
                    if outer.updates is not None:return outer.updates()
                    code=outer.publisher.discovery.state['code']
                    return [dict(update_id=1,channel_post=dict(text='wrong',date=time.time(),chat=dict(id=-100999,type='channel'),message_id=8)),
                            dict(update_id=2,channel_post=dict(text=code,date=time.time(),chat=dict(id=-10012345,type='channel'),message_id=9))]
                if method=='deleteMessage':
                    if not outer.delete:raise TelegramError('Not enough rights')
                    return True
                raise AssertionError(method)
        self.publisher.client_factory=lambda _:Client()
        self.config=dict(token='12345:secret',mode='cloud',silent=False)
        self.addCleanup(self.stop)

    def wait(self):
        deadline=time.monotonic()+4
        while self.publisher.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.publisher.busy())
        return self.publisher.discovery.public()

    def stop(self):
        self.publisher.cancel_discovery()
        self.wait()

    def test_match_saves_channel_and_deletes_only_matching_message(self):
        state=copy.deepcopy(self.publisher.library.state)
        self.publisher.discover_channel(self.config)
        result=self.wait()
        self.assertEqual(result['status'],'connected')
        self.assertEqual(result['code'],'')
        self.assertEqual(self.publisher.settings['channel'],'-10012345')
        self.assertEqual(self.publisher.library.state,state)
        self.assertEqual([fields for method,fields in self.calls if method=='deleteMessage'],[dict(chat_id=-10012345,message_id=9)])
        self.assertNotIn('secret',str(result))

    def test_no_delete_permission_keeps_successful_connection(self):
        self.delete=False
        self.publisher.discover_channel(self.config)
        result=self.wait()
        self.assertEqual(result['status'],'connected')
        self.assertFalse(result['deleted'])
        self.assertIn('вручную',result['message'])
        self.assertEqual(self.publisher.settings['channel'],'-10012345')

    def test_webhook_is_not_removed(self):
        self.webhook='https://example.com/bot'
        self.publisher.discover_channel(self.config)
        result=self.wait()
        self.assertEqual(result['status'],'error')
        self.assertIn('webhook',result['message'])
        self.assertEqual([m for m,f in self.calls],['getWebhookInfo'])

    def test_cancel_ignores_delayed_match_and_blocks_second_session(self):
        entered=threading.Event();release=threading.Event()
        def delayed():
            code=self.publisher.discovery.state['code'];entered.set();release.wait(2)
            return [dict(update_id=1,channel_post=dict(text=code,date=time.time(),chat=dict(id=-10012345,type='channel'),message_id=9))]
        self.updates=delayed
        self.publisher.discover_channel(self.config)
        self.assertTrue(entered.wait(2))
        other = Library(self.publisher.library.root, self.publisher.library.root/'other-data').publisher
        with self.assertRaisesRegex(ValueError, 'уже идёт подключение'):
            other.discover_channel(self.config)
        self.assertFalse(other.busy())
        self.assertRegex(self.publisher.discovery.public()['code'],r'^[a-z]{16}$')
        with self.assertRaises(ValueError):self.publisher.discover_channel(self.config)
        with self.assertRaises(ValueError):self.publisher.save_settings(dict(self.config,channel='@other'))
        self.publisher.cancel_discovery();release.set()
        self.assertEqual(self.wait()['status'],'cancelled')
        self.assertNotIn('channel',self.publisher.settings)
        self.assertFalse(any(m=='deleteMessage' for m,f in self.calls))

    def test_rejects_old_or_non_channel_messages_and_times_out(self):
        def updates():
            code=self.publisher.discovery.state['code']
            return [dict(update_id=1,channel_post=dict(text=code,date=0,chat=dict(id=-10012345,type='channel'))),
                    dict(update_id=2,channel_post=dict(text=code,date=time.time(),chat=dict(id=123,type='private')))]
        self.updates=updates
        with patch('src.channel_discovery.time.monotonic',side_effect=[0,1,301]):
            self.publisher.discover_channel(self.config)
            # Do not use time.monotonic here: patch affects the shared time module.
            for _ in range(100):
                if not self.publisher.busy():break
                time.sleep(.01)
        self.assertEqual(self.wait()['status'],'expired')
        self.assertNotIn('channel',self.publisher.settings)

    def test_conflict_explains_competing_application(self):
        def conflict():raise TelegramError('Conflict: terminated by other getUpdates request')
        self.updates=conflict;self.publisher.discover_channel(self.config)
        self.assertIn('Другое приложение',self.wait()['message'])
