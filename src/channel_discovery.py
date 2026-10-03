"""Explicit, short-lived channel pairing through a one-time message."""
import secrets
import string
import threading
import time

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


class ChannelDiscovery:
    def __init__(self, publisher):
        self.publisher = publisher
        self.active = False
        self.state = None
        self.stop = threading.Event()

    def public(self):
        return dict(self.state) if self.state else None

    def start(self, settings):
        bot_key = settings['token'].split(':')[0]
        with _ACTIVE_LOCK:
            if bot_key in _ACTIVE:
                raise ValueError('Для этого бота уже идёт подключение канала. Остановите его или дождитесь завершения.')
            _ACTIVE.add(bot_key)
        self.stop = threading.Event()
        self.active = True
        self.state = dict(id=secrets.token_hex(8), status='starting', code='', mode=settings['mode'], message='Проверяем бота…')
        threading.Thread(target=self._run, args=(dict(settings), bot_key), daemon=True).start()
        return self.public()

    def cancel(self):
        if self.active and self.state['status'] in ('starting', 'waiting', 'checking', 'cancelling'):
            self.stop.set()
            self.state.update(status='cancelling', code='', message='Останавливаем ожидание…')
        return self.public()

    def _run(self, settings, bot_key):
        publisher = self.publisher
        try:
            client = publisher.client_factory(settings)
            webhook = client.call('getWebhookInfo')
            if webhook.get('url'):
                raise ValueError('У бота настроен webhook другого приложения. Используйте отдельного бота или введите ID канала вручную. Настройки webhook не изменены.')
            bot = client.call('getMe')
            code = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(16))
            started = time.time()
            deadline = time.monotonic() + 300
            with publisher.lock:
                if self.stop.is_set():
                    return
                self.state.update(status='waiting', code=code, expires_at=started+300,
                    bot=bot.get('username', str(bot['id'])), message='Отправьте код отдельным сообщением в канал, где бот — администратор.')
            offset = None
            while not self.stop.is_set() and time.monotonic() < deadline:
                fields = dict(timeout=10, allowed_updates=['channel_post'], limit=100)
                if offset is not None:
                    fields['offset'] = offset
                updates = client.call('getUpdates', fields)
                for update in updates:
                    offset = max(offset or 0, update['update_id']+1)
                    post = update.get('channel_post', {})
                    if (post.get('text') != code or post.get('chat', {}).get('type') != 'channel' or
                            post.get('date', 0) < started-1):
                        continue
                    with publisher.lock:
                        if self.stop.is_set() or time.monotonic() >= deadline:
                            return
                        self.state.update(status='checking', message='Канал найден. Проверяем права бота…')
                    selected = dict(settings, channel=str(post['chat']['id']))
                    destination = publisher.client_factory(selected).check()
                    with publisher.lock:
                        if self.stop.is_set() or time.monotonic() >= deadline:
                            return
                        publisher.save_settings(selected, _pairing=True)
                        publisher._checked = (dict(selected), time.monotonic(), dict(destination))
                        self.state.update(status='connected', code='', destination=destination,
                                          message='Канал найден и подключён. Удаляем проверочное сообщение…')
                    try:
                        deleted = client.call('deleteMessage', dict(chat_id=post['chat']['id'], message_id=post['message_id'])) is True
                    except Exception:
                        deleted = False
                    with publisher.lock:
                        self.state.update(deleted=deleted, message=('Канал найден и подключён. Проверочное сообщение удалено.' if deleted else
                            'Канал найден и подключён. Не удалось удалить проверочное сообщение — удалите его вручную.'))
                    return
                self.stop.wait(.2)
            with publisher.lock:
                if not self.stop.is_set():
                    self.state.update(status='expired', code='', message='Время ожидания истекло. Создайте новый код и отправьте его в канал.')
        except Exception as error:
            message = str(error).replace(settings['token'], '[token]')
            if 'conflict' in message.lower() or 'getupdates' in message.lower() and 'terminated' in message.lower():
                message = 'Другое приложение получает сообщения этого бота. Остановите его или используйте отдельного бота.'
            with publisher.lock:
                self.state.update(status='error', code='', message=message)
        finally:
            with publisher.lock:
                if self.stop.is_set() and self.state['status'] != 'connected':
                    self.state.update(status='cancelled', code='', message='Подключение отменено.')
                elif self.state['status'] in ('checking', 'waiting'):
                    self.state.update(status='expired', code='', message='Время ожидания истекло. Создайте новый код.')
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(bot_key)
                self.active = False
