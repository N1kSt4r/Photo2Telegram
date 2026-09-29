"""Optional Telegram publishing. No network activity until explicitly requested."""
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image


if __package__:
    from .telegram_credentials import TelegramCredentials
else:
    from telegram_credentials import TelegramCredentials


class TelegramError(Exception):
    def __init__(self, message, uncertain=False, retry_after=0):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


def write_json(path, value, private=False):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        if private:
            os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def validated_settings(value, previous):
    mode = value.get('mode', 'cloud')
    if mode not in ('cloud', 'local'):
        raise ValueError('Выберите обычный или локальный Bot API')
    token = value.get('token', '').strip() or previous.get('token', '')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise ValueError('Укажите токен, полученный у BotFather')
    channel = str(value.get('channel', '')).strip()
    if not re.fullmatch(r'@[A-Za-z0-9_]+|-\d+', channel):
        raise ValueError('Укажите @имя_канала или его числовой ID, например -1001234567890')
    endpoint = 'https://api.telegram.org' if mode == 'cloud' else str(value.get('endpoint', '')).strip().rstrip('/')
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Адрес Bot API должен иметь вид http://127.0.0.1:8081, без токена и пути')
    try:
        parsed.port
    except ValueError:
        raise ValueError('Некорректный порт Bot API') from None
    return dict(mode=mode, token=token, channel=channel, endpoint=endpoint,
                silent=bool(value.get('silent', False)))


class BotClient:
    """Stream multipart uploads in bounded chunks, without buffering videos in RAM."""
    def __init__(self, settings):
        self.settings = settings

    def call(self, method, fields=None, files=None, progress=None):
        fields, files = fields or {}, files or {}
        boundary = 'photo2telegram-' + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n' + encoded + '\r\n').encode())
        for key, path in files.items():
            # Generated ASCII filenames only; original private names are not sent.
            filename = key + path.suffix
            mime = 'video/mp4' if path.suffix == '.mp4' else 'image/jpeg'
            parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode(), path, b'\r\n'])
        parts.append(f'--{boundary}--\r\n'.encode())
        content_type = f'multipart/form-data; boundary={boundary}'
        if not files:
            # An empty multipart body (getMe) is rejected by some API frontends.
            # JSON is also the simpler format for all calls without attachments.
            parts = [json.dumps(fields, ensure_ascii=False).encode('utf-8')]
            content_type = 'application/json'
        publishing = method.startswith('send')

        def transport_error(message):
            hint = ('Результат отправки неизвестен; проверьте канал перед повтором.' if publishing else
                    'Проверка ничего не публикует. Проверьте адрес и доступность Bot API.')
            return TelegramError(f'{method}: {message} {hint}', uncertain=publishing)

        total = sum(part.stat().st_size if isinstance(part, Path) else len(part) for part in parts)
        parsed = urlsplit(self.settings['endpoint'])
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = cls(parsed.hostname, parsed.port, timeout=600 if method.startswith('send') else 20)
        sent = 0
        try:
            connection.putrequest('POST', f'/bot{self.settings["token"]}/{method}')
            connection.putheader('Content-Type', content_type)
            connection.putheader('Content-Length', str(total))
            connection.endheaders()
            if progress:
                progress(0, total)
            for part in parts:
                if isinstance(part, Path):
                    with part.open('rb') as stream:
                        while chunk := stream.read(256 * 1024):
                            connection.send(chunk)
                            sent += len(chunk)
                            if progress:
                                progress(sent, total)
                else:
                    connection.send(part)
                    sent += len(part)
            if progress:
                progress(total, total)
            response = connection.getresponse()
            payload = response.read(2_000_001)
            response_type = str(response.getheader('Content-Type') or 'не указан').replace(self.settings['token'], '[token]')[:80]
            details = f'HTTP {response.status}, Content-Type: {response_type}.'
            if response.status >= 500 or 300 <= response.status < 400:
                raise transport_error(f'Сервер не подтвердил результат. {details}')
            try:
                result = json.loads(payload)
            except (ValueError, UnicodeError):
                raise transport_error(f'Ответ Bot API не является JSON. {details}') from None
            if not isinstance(result, dict) or 'ok' not in result:
                raise transport_error(f'Неполный ответ Bot API. {details}')
            if not result['ok']:
                message = str(result.get('description', 'Telegram отклонил запрос')).replace(self.settings['token'], '[token]')
                retry = result.get('parameters', {}).get('retry_after', 0)
                raise TelegramError(message, uncertain=int(result.get('error_code', response.status)) >= 500,
                                    retry_after=retry if isinstance(retry, int) and retry > 0 else 0)
            if 'result' not in result:
                raise transport_error(f'Bot API не вернул результат запроса. {details}')
            return result['result']
        except (OSError, http.client.HTTPException):
            # A broken connection may happen AFTER Telegram accepted a post.
            raise transport_error('Соединение прервалось или истекло время ожидания.') from None
        finally:
            connection.close()

    def check(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            bot_future = pool.submit(self.call, 'getMe')
            chat_future = pool.submit(self.call, 'getChat', {'chat_id': self.settings['channel']})
            bot, chat = bot_future.result(), chat_future.result()
        if chat.get('type') != 'channel':
            raise ValueError('Получатель должен быть Telegram-каналом')
        member = self.call('getChatMember', {'chat_id': chat['id'], 'user_id': bot['id']})
        if member.get('status') != 'creator' and not (member.get('status') == 'administrator' and member.get('can_post_messages')):
            raise ValueError('Добавьте бота администратором канала с правом публикации')
        return dict(bot=bot.get('username', str(bot['id'])), bot_id=bot['id'],
                    chat_id=chat['id'], channel=chat.get('title', str(chat['id'])), username=chat.get('username'))


class Publisher:
    def __init__(self, library, save_jpeg):
        self.library, self.save_jpeg = library, save_jpeg
        self.lock = threading.RLock()
        self.settings_path = library.data / 'telegram-settings.json'
        self.journal_path = library.data / 'telegram-sent.json'
        self.credentials = TelegramCredentials(library.credentials_path)
        self.token_key = 'TELEGRAM_BOT_TOKEN_' + hashlib.sha256(str(library.data.resolve()).encode()).hexdigest()[:16].upper()
        self._settings = load_json(self.settings_path, {})
        self.token_key = self._settings.get('_token_key', self.token_key)
        legacy = self._settings.pop('token', '')
        if legacy:
            if not self.credentials.read().get(self.token_key):
                self.credentials.update({self.token_key: legacy})
            self._settings['_token_key'] = self.token_key
            write_json(self.settings_path, self._settings, private=True)
        self.journal = load_json(self.journal_path, {})
        self.job = None
        self._working = False
        self.cancelled = threading.Event()
        self.client_factory = BotClient
        self._checked = None
        self.media_cache = library.data / 'telegram-media-cache'
        self.media_cache.mkdir(exist_ok=True)
        self._cache_locks = {}
        self._overview_cache = None
        self._display_items = {}
        changed = False
        for record in self.journal.values():
            if record['status'] == 'sending':
                record['status'] = 'unknown'
                record['error'] = 'Приложение остановилось во время отправки. Проверьте канал.'
                changed = True
        if changed:
            write_json(self.journal_path, self.journal)
        # This folder contains only this publisher's temporary prepared copies.
        self.outbox = library.data / 'telegram-outbox'
        if self.outbox.exists():
            shutil.rmtree(self.outbox)

    @property
    def settings(self):
        return {**{k: v for k, v in self._settings.items() if k != '_token_key'}, 'token': self.credentials.read().get(self.token_key, '')}

    def busy(self):
        return self._working

    def public_settings(self):
        with self.lock:
            return {**{k: v for k, v in self.settings.items() if k != 'token'}, 'has_token': bool(self.settings.get('token'))}

    def save_settings(self, value):
        with self.lock:
            if self.busy():
                raise ValueError('Сначала завершите или отмените очередь отправки')
            settings = validated_settings(value, self.settings)
            previous = self.settings
            self.credentials.update({self.token_key: settings['token']})
            local_settings = {k: v for k, v in settings.items() if k != 'token'}
            local_settings['_token_key'] = self.token_key
            write_json(self.settings_path, local_settings, private=True)
            if settings != previous:
                self._checked = None
                self.job = None
                self._display_items = {}
                self._cleanup()
            self._settings = local_settings
            return self.public_settings()

    def check(self):
        with self.lock:
            settings = validated_settings(self.settings, {})
        destination = self._check_destination(settings, fresh=True)
        with self.lock:
            if self.job and self.job.get('_settings') == settings:
                self._apply_destination(destination)
                self.job.update(channel_status='ready', channel_error='')
        return destination

    def _check_destination(self, settings, fresh=False):
        with self.lock:
            cached = self._checked
            if not fresh and cached and cached[0] == settings and time.monotonic() - cached[1] < 60:
                return copy.deepcopy(cached[2])
        destination = self.client_factory(settings).check()
        with self.lock:
            self._checked = (dict(settings), time.monotonic(), copy.deepcopy(destination))
        return destination

    def _overview(self):
        settings = self.settings
        with self.library.lock:
            revision = self.library.state['revision']
            posts = copy.deepcopy(self.library.state['posts'])
        signature = (revision, json.dumps(settings, sort_keys=True), len(self.journal))
        if self._overview_cache and self._overview_cache[0] == signature and time.monotonic()-self._overview_cache[1] < 5:
            return copy.deepcopy(self._overview_cache[2])
        channel = settings.get('channel', '')
        bot_id = settings.get('token', '').split(':')[0]
        records = {}
        for key, record in self.journal.items():
            matches = channel in (str(record.get('chat_id')), record.get('channel_input')) or (
                channel.startswith('@') and record.get('link', '').startswith(f'https://t.me/{channel[1:]}/'))
            if matches and str(record.get('bot_id')) == bot_id:
                records[record['post_id']] = dict(record, key=key)
        items, cached_files = [], {}
        for post in posts:
            if not post['photos'] and not post['caption'].strip():
                continue
            previous = records.get(post['id'], {})
            item = dict(id=post['id'], title=post['title'], caption=post['caption'], files=[], status='unprepared')
            if previous.get('status') in ('sent', 'unknown', 'sending'):
                item.update(status='sent' if previous['status']=='sent' else 'unknown',
                            files=self._sent_files(post, previous), link=previous.get('link', ''),
                            resolve_key=previous.get('key'), error=previous.get('error', ''))
            else:
                for ident in post['photos']:
                    if ident not in cached_files:
                        photo = self.library.catalog.get(ident, {})
                        info = dict(id=ident, name=photo.get('name', ident), kind=photo.get('kind', 'photo'), bytes=None, cached=False)
                        source = self.library.files.get(ident)
                        try:
                            if source:
                                key = self._media_cache_key(source, settings)
                                meta = load_json(self.media_cache / (key+'.json'), {})
                                target = self.media_cache / (key+('.mp4' if info['kind']=='video' else '.jpg'))
                                if isinstance(meta, dict) and target.is_file() and meta.get('bytes') == target.stat().st_size:
                                    info.update(bytes=meta['bytes'], cached=True, converted=meta['converted'], prepared_ref=target.name)
                        except (OSError, ValueError, KeyError):
                            pass
                        cached_files[ident] = info
                    item['files'].append(dict(cached_files[ident]))
                if all(f['cached'] for f in item['files']):
                    item['status'] = 'cached'
            items.append(item)
        result = dict(id=None, status='overview', items=items, total=len(items), done=0, skipped=0,
                      message='Состояние постов на диске. Подготовьте очередь для проверки и отправки.')
        self._overview_cache = (signature, time.monotonic(), result)
        return copy.deepcopy(result)

    def _apply_destination(self, destination):
        self.job['destination'] = destination
        for prepared in self.job.get('_prepared', []):
            key = hashlib.sha256(f'{destination["bot_id"]}:{destination["chat_id"]}:{prepared["post"]["id"]}'.encode()).hexdigest()
            prepared['key'] = key
            previous = self.journal.get(key, {})
            for item in self.job['items']:
                if item['id'] == prepared['post']['id'] and previous.get('status') in ('sent', 'unknown', 'sending'):
                    item['status'] = 'sent' if previous['status']=='sent' else 'unknown'

    def _check_for_job(self, job):
        try:
            destination = self._check_destination(job['_settings'], fresh=job.get('_send_after', False))
            with self.lock:
                if self.job is job:
                    self._apply_destination(destination)
                    job.update(channel_status='ready', channel_error='')
        except Exception as error:
            with self.lock:
                if self.job is job:
                    job.update(channel_status='error', channel_error=self._safe_error(error))

    def _sync_drafts(self):
        if not self.job or self._working or '_prepared' not in self.job:
            return
        with self.library.lock:
            ids = {p['id'] for p in self.job.get('_posts', [])}
            posts = [copy.deepcopy(p) for p in self.library.state['posts'] if p['id'] in ids]
        signature = json.dumps(posts, sort_keys=True)
        if signature == self.job.get('_draft_signature'):
            return
        available = self.job.setdefault('_available', {})
        for prepared in self.job['_prepared']:
            for ident, media in zip(prepared['post']['photos'], prepared['media']):
                available[ident] = media
        previous = {p['id']: p for p in self.job['items']}
        prepared, items = [], []
        for post in posts:
            if not post['photos'] and not post['caption'].strip():
                continue
            old = previous.get(post['id'], {})
            if old.get('status') in ('sent', 'unknown'):
                items.append(old)
                prepared.extend(p for p in self.job['_prepared'] if p['post']['id']==post['id'])
                continue
            item = dict(id=post['id'], title=post['title'], caption=post['caption'], status='unprepared', files=self._sent_files(post, {}))
            media = [available.get(ident) for ident in post['photos']]
            valid = True
            for index, (ident, m) in enumerate(zip(post['photos'], media)):
                source = self.library.files.get(ident)
                try:
                    if not m or not source or not m['path'].is_file() or (m.get('cache_key') and m['cache_key'] != self._media_cache_key(source, self.job['_settings'])):
                        valid = False
                    else:
                        item['files'][index].update(status='ready', bytes=m['path'].stat().st_size, converted=m['converted'], prepared_ref=m['path'].name)
                except OSError:
                    valid = False
            limit = 1024 if post['photos'] else 4096
            if len(post['photos'])>10 or len(post['caption'].encode('utf-16-le'))//2>limit:
                item.update(status='error', error='Пост превышает лимит файлов или длины подписи')
            elif valid:
                item['status'] = 'ready'
                item['files'] = [dict(id=ident, name=m['name'], kind=m['kind'], bytes=m['path'].stat().st_size,
                                      converted=m['converted'], prepared_ref=m['path'].name) for ident,m in zip(post['photos'],media)]
                prepared.append(dict(post=post, media=media, key=''))
            items.append(item)
        self.job.update(items=items, _prepared=prepared, _draft_signature=signature)
        if self.job.get('destination'):
            self._apply_destination(self.job['destination'])

    def status(self):
        with self.lock:
            self._sync_drafts()
            job = {key: copy.deepcopy(value) for key, value in (self.job or {}).items() if not key.startswith('_')}
            if not job or (job['status']=='cancelled' and not self.job.get('_prepared')):
                previous_status = job.get('status') if job else None
                job = self._overview()
                if previous_status:
                    job['status'] = previous_status
            if job:
                active = {item['id']: item for item in job['items']}
                overview = self._overview()['items']
                combined = []
                for item in overview:
                    previous = self._display_items.get(item['id'])
                    # Preserve a file-specific failure while the same draft remains unchanged.
                    if previous and previous['status']=='error' and item['status'] not in ('sent','unknown') and (
                            previous['title']==item['title'] and previous['caption']==item['caption'] and
                            [f['id'] for f in previous['files']]==[f['id'] for f in item['files']]):
                        item = copy.deepcopy(previous)
                    combined.append(active.pop(item['id'], item))
                combined.extend(active.values())
                job['items'] = combined
                self._display_items = {item['id']: copy.deepcopy(item) for item in combined}
                for item in job['items']:
                    for file in item['files']:
                        if file.get('prepared_ref') and self.prepared_file(file['prepared_ref']) is None:
                            file.pop('prepared_ref', None)
                            file['missing_output'] = True
                        if not file.get('prepared_ref') and file.get('cache_ref') and self.prepared_file(file['cache_ref']):
                            file['prepared_ref'] = file['cache_ref']
                            file['missing_output'] = False
                        if item['status']=='sent':
                            file['status'] = 'sent'
                        elif file.get('status') != 'error':
                            file['status'] = ('ready' if file.get('prepared_ref') else
                                              'preparing' if file.get('status')=='preparing' and self._working else 'unprepared')
                    destination = job.get('destination')
                    if destination:
                        key = hashlib.sha256(f'{destination["bot_id"]}:{destination["chat_id"]}:{item["id"]}'.encode()).hexdigest()
                        record = self.journal.get(key, {})
                        item.update(link=record.get('link', ''), resolve_key=key if record.get('status')=='unknown' else None)
                job['can_resume'] = not self._working and bool((self.job or {}).get('_prepared')) and any(
                    self.journal.get(p['key'], {}).get('status') != 'sent' for p in self.job['_prepared'])
                job['sent'] = sum(item['status'] == 'sent' for item in job['items'])
            return dict(settings=self.public_settings(), job=job or None, busy=self.busy(),
                        history=[dict(record, key=key) for key, record in self.journal.items()])

    def _update(self, **values):
        with self.lock:
            if self.job.get('status') == 'cancelling' and 'status' not in values:
                values.pop('message', None)
            self.job.update(values)

    def _record(self, key, record):
        with self.lock:
            updated = {**self.journal, key: record}
            write_json(self.journal_path, updated)
            self.journal = updated
            self._overview_cache = None
            self._sync_post_status(record)

    def _sync_post_status(self, record):
        if not self.job or not any(p['key'] == hashlib.sha256(
                f'{record["bot_id"]}:{record["chat_id"]}:{record["post_id"]}'.encode()).hexdigest()
                for p in self.job.get('_prepared', [])):
            return
        for item in self.job['items']:
            if item['id'] == record['post_id']:
                item['status'] = {'error': 'ready', 'retry': 'ready'}.get(record['status'], record['status'])
                item['error'] = record.get('error', '')

    def _sent_files(self, post, previous):
        if previous.get('files') is not None:
            return copy.deepcopy(previous['files'])
        # Older journals did not store prepared sizes. Still show their previews.
        result = []
        for ident in post['photos']:
            photo = self.library.catalog.get(ident, {})
            result.append(dict(id=ident, name=photo.get('name', ident), kind=photo.get('kind', 'photo'),
                               bytes=None, converted=False))
        return result

    def prepare(self, ids=None, cache_only=False, send_after=False):
        with self.lock:
            if self.busy():
                raise ValueError('Очередь уже подготовлена или выполняется')
            settings = validated_settings(self.settings, {})
            with self.library.lock:
                posts = copy.deepcopy(self.library.state['posts'])
                if ids is not None:
                    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                        raise ValueError('Выберите посты для отправки')
                    if set(ids) - {p['id'] for p in posts}:
                        raise ValueError('Пост не найден')
                    posts = [p for p in posts if p['id'] in ids]
                posts = [p for p in posts if p['photos'] or p['caption'].strip()]
                files = dict(self.library.files)
            if not posts:
                raise ValueError('Нет непустых постов для отправки')
            if self.job:
                self.status()
            self._cleanup()
            self.cancelled.clear()
            known = {item['id']: item for item in self._overview()['items']}
            initial_items = []
            for post in posts:
                item = copy.deepcopy(known[post['id']])
                if item['status'] != 'sent':
                    item['status'] = 'preparing'
                initial_items.append(item)
            self.job = dict(id=uuid.uuid4().hex, status='preparing', done=0, total=len(posts),
                            message='Проверяем канал и готовим файлы…', channel_status='checking', channel_error='', items=initial_items, skipped=0,
                            _settings=settings, _posts=posts, _files=files, _cache_only=cache_only, _send_after=send_after, _draft_signature=json.dumps(posts, sort_keys=True))
            self._working = True
            threading.Thread(target=self._prepare, daemon=True).start()
            return {'id': self.job['id']}

    def _prepare(self):
        try:
            settings = self.job['_settings']
            channel_thread = threading.Thread(target=self._check_for_job, args=(self.job,), daemon=True)
            channel_thread.start()
            self._update(mode=settings['mode'], endpoint=settings['endpoint'])
            self.outbox.mkdir(exist_ok=True)
            prepared, summaries, skipped = [], [], 0
            records, tasks, pending = [], {}, []
            # One shared, bounded queue for photos and videos.
            with ThreadPoolExecutor(max_workers=12) as pool:
                for post in self.job['_posts']:
                    key = ''
                    summary = copy.deepcopy(next(item for item in self.job['items'] if item['id'] == post['id']))
                    summary['status'] = 'preparing'
                    record = dict(post=post, key=key, media=[None] * len(post['photos']), summary=summary, pending=0)
                    records.append(record)
                    summaries.append(summary)
                    if self.cancelled.is_set():
                        summary['status'] = 'unprepared'
                        continue
                    try:
                        previous = self.journal.get(summary.get('resolve_key'))
                        if previous and previous['status'] == 'sent':
                            skipped += 1
                            summary['status'] = 'sent'
                            summary['files'] = self._sent_files(post, previous)
                            continue
                        if previous and previous['status'] in ('unknown', 'sending'):
                            raise ValueError(f'Проверьте результат прошлой отправки «{post["title"]}» в истории, прежде чем продолжить')
                        if len(post['photos']) > 10:
                            raise ValueError('В одном посте допускается не больше 10 файлов')
                        limit = 1024 if post['photos'] else 4096
                        if len(post['caption'].encode('utf-16-le')) // 2 > limit:
                            raise ValueError(f'Слишком длинный текст «{post["title"]}»: максимум {limit} символов (эмодзи могут занимать два)')
                        sources = [self.job['_files'].get(ident) for ident in post['photos']]
                        for index, source in enumerate(sources):
                            if source is None or not source.is_file():
                                error = f'{summary["files"][index]["name"]}: исходный файл недоступен. Обновите библиотеку.'
                                summary['files'][index].update(status='error', error=error)
                                summary.update(status='error', error=error)
                                continue
                            if not summary['files'][index].get('cached'):
                                summary['files'][index]['status'] = 'preparing'
                            pending.append((source, record, index))
                            record['pending'] += 1
                        if not sources:
                            summary['status'] = 'ready'
                    except Exception as error:
                        summary.update(status='error', error=self._safe_error(error))
                self._update(items=copy.deepcopy(summaries), skipped=skipped,
                             message='Готовим файлы: до 12 фото и видео одновременно…')
                # Cache hits go first, ahead of CPU-heavy conversions across all posts.
                pending.sort(key=lambda entry: not self._has_media_cache(entry[0], settings))
                for source, record, index in pending:
                    tasks[pool.submit(self._prepare_unless_cancelled, source, settings)] = (record, index)
                completed = 0
                for future in as_completed(tasks):
                    record, index = tasks[future]
                    summary = record['summary']
                    try:
                        record['media'][index] = future.result()
                        media = record['media'][index]
                        if media:
                            summary['files'][index].update(status='ready', error='', bytes=media['path'].stat().st_size,
                                converted=media['converted'], cached=media.get('cached', False), prepared_ref=media['path'].name,
                                cache_ref=(media.get('cache_key','')+media['path'].suffix) if media.get('cache_key') else None)
                        else:
                            summary['files'][index]['status'] = 'unprepared'
                    except Exception as error:
                        summary['files'][index].update(status='unprepared' if self.cancelled.is_set() else 'error', error=self._safe_error(error))
                        summary.update(status='unprepared' if self.cancelled.is_set() else 'error', error=self._safe_error(error))
                    record['pending'] -= 1
                    completed += 1
                    if not record['pending']:
                        if summary['status'] in ('error', 'unprepared') or any(m is None for m in record['media']):
                            if summary['status'] != 'error':
                                summary['status'] = 'unprepared'
                            for media in record['media']:
                                if media:
                                    media['path'].unlink(missing_ok=True)
                        else:
                            summary['status'] = 'ready'
                            summary['files'] = [dict(id=ident, name=m['name'], kind=m['kind'], bytes=m['path'].stat().st_size,
                                                     converted=m['converted'], cached=m.get('cached', False), prepared_ref=m['path'].name, status='ready')
                                                for ident, m in zip(record['post']['photos'], record['media'])]
                    self._update(items=copy.deepcopy(summaries),
                                 done=sum(p['status'] != 'preparing' for p in summaries),
                                 message=f'Подготовлено файлов: {completed} / {len(tasks)}')
            prepared = [dict(post=r['post'], key=r['key'], media=r['media'])
                        for r in records if r['summary']['status'] == 'ready']
            self._update(_prepared=prepared, message='Файлы подготовлены. Ожидаем проверку канала…')
            channel_thread.join()
            with self.lock:
                if self.job.get('destination'):
                    self._apply_destination(self.job['destination'])
                    for item in summaries:
                        updated = next(p for p in self.job['items'] if p['id']==item['id'])
                        item.update(updated)
            if self.cancelled.is_set():
                self._update(status='ready' if prepared else 'cancelled', stopped=True,
                             message=f'Подготовка остановлена. Готово постов: {len(prepared)}. Остальные можно подготовить повторно.',
                             items=summaries, total=len(summaries), done=len(prepared)+skipped,
                             skipped=skipped, _prepared=prepared)
                if not prepared:
                    self._cleanup()
                return
            errors = [item for item in summaries if item['status'] == 'error']
            message = f'Готово: {len(prepared)} · ошибок: {len(errors)}. Выберите посты для публикации'
            if not prepared:
                message = errors[0]['error'] if errors else 'Все выбранные посты уже отправлены'
            self._update(status='ready' if prepared else ('error' if errors else 'done'), message=message,
                         items=summaries, total=len(summaries), done=len(summaries), skipped=skipped, _prepared=prepared)
            if not prepared:
                self._cleanup()
        except Exception as error:
            with self.lock:
                for item in self.job['items']:
                    if item['status'] == 'preparing':
                        item['status'] = 'unprepared' if self.cancelled.is_set() else 'error'
            self._update(status='cancelled' if self.cancelled.is_set() else 'error',
                         message='Подготовка отменена' if self.cancelled.is_set() else self._safe_error(error))
            self._cleanup()
        finally:
            with self.lock:
                self._working = False
                if self.job.get('_send_after') and not self.cancelled.is_set():
                    items = self.job['items']
                    if self.job.get('channel_status')=='ready' and self.job.get('_prepared') and all(p['status'] in ('ready','sent') for p in items):
                        try:
                            self.send(self.job['id'])
                        except Exception as error:
                            self.job.update(status='error', message=self._safe_error(error))
                    elif self.job.get('channel_status')=='error':
                        self.job.update(message='Отправка не началась: '+self.job.get('channel_error','Канал не проверен'))


    def _prepare_unless_cancelled(self, source, settings):
        if self.cancelled.is_set():
            return None
        return self._prepare_media(source, settings)

    def prepared_file(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{64})\.(?:jpg|mp4)', reference):
            return None
        folder = self.outbox if len(reference.split('.')[0]) == 32 else self.media_cache
        path = folder / reference
        if folder == self.media_cache and not path.with_suffix('.json').is_file():
            return None
        return path if path.is_file() and not path.is_symlink() else None

    def _media_cache_key(self, source, settings):
        stat = source.stat()
        value = [str(source.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                 settings['mode'], self.library.ffmpeg, self.library.ffprobe, ('jpeg2560-q85-mp4-v1' if source.suffix.lower() in {'.mov', '.mp4', '.m4v', '.webm'} else 'jpeg2560-q95-420-v4')]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def _has_media_cache(self, source, settings):
        try:
            key = self._media_cache_key(source, settings)
            return (self.media_cache / (key + '.json')).is_file()
        except OSError:
            return False

    @staticmethod
    def _link_or_copy(source, target):
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)

    def _prepare_media(self, source, settings):
        key = self._media_cache_key(source, settings)
        with self.lock:
            lock = self._cache_locks.setdefault(key, threading.Lock())
        with lock:
            video = source.suffix.lower() in {'.mov', '.mp4', '.m4v', '.webm'}
            suffix = '.mp4' if video else '.jpg'
            cached = self.media_cache / (key + suffix)
            metadata = self.media_cache / (key + '.json')
            target = self.outbox / (uuid.uuid4().hex + suffix)
            try:
                try:
                    info = load_json(metadata, {})
                    valid = isinstance(info, dict) and cached.is_file() and info.get('bytes') == cached.stat().st_size and 'converted' in info
                except (OSError, ValueError):
                    valid = False
                if valid:
                    self._link_or_copy(cached, target)
                    os.utime(metadata, None)
                    return dict(path=target, kind='video' if video else 'photo', name=source.name,
                                converted=info['converted'], cached=True, cache_key=key)
                if self.cancelled.is_set():
                    raise ValueError('Подготовка отменена')
                if (self.job or {}).get('_cache_only'):
                    raise ValueError(f'{source.name}: готовый файл отсутствует или устарел. Повторите подготовку.')
                result = self._prepare_media_file(source, settings, target)
                if key != self._media_cache_key(source, settings):
                    raise ValueError(f'{source.name}: исходник изменился во время подготовки')
                # Metadata is the commit marker. Never reuse an unfinished file.
                metadata.unlink(missing_ok=True)
                cached.unlink(missing_ok=True)
                self._link_or_copy(target, cached)
                write_json(metadata, dict(bytes=cached.stat().st_size, converted=result['converted']))
                return dict(result, cached=False, cache_key=key)
            except Exception:
                target.unlink(missing_ok=True)
                raise

    def _remove_sent_cache(self, media):
        needed_names = set()
        destination = (self.job or {}).get('destination')
        if destination:
            with self.library.lock:
                for post in self.library.state['posts']:
                    key = hashlib.sha256(f'{destination["bot_id"]}:{destination["chat_id"]}:{post["id"]}'.encode()).hexdigest()
                    if self.journal.get(key, {}).get('status') != 'sent':
                        needed_names.update(self.library.files[ident].name for ident in post['photos'] if ident in self.library.files)
        for item in media:
            if item.get('name') in needed_names:
                continue
            key = item.get('cache_key')
            if key:
                # The outbox has its own link, so later posts can still use the same file.
                for suffix in ('.json', '.jpg', '.mp4'):
                    try:
                        (self.media_cache / (key + suffix)).unlink(missing_ok=True)
                    except OSError:
                        pass  # Cache cleanup must never change a confirmed publication to an error.

    def _run_media_tool(self, args, timeout):
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            deadline = time.monotonic() + timeout
            while True:
                if self.cancelled.is_set() or time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise ValueError('Подготовка отменена' if self.cancelled.is_set() else 'Истекло время подготовки видео')
                try:
                    stdout, stderr = process.communicate(timeout=.2)
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pass

    def _prepare_media_file(self, source, settings, target):
        before = source.stat()
        video = source.suffix.lower() in {'.mov', '.mp4', '.m4v', '.webm'}
        converted = True
        if not video:
            self.save_jpeg(source, target, 2560, quality=95, subsampling=2)
            with Image.open(target) as image:
                if max(image.size) / min(image.size) > 20:
                    raise ValueError(f'{source.name}: слишком вытянутое фото для Telegram (соотношение больше 20:1)')
            if target.stat().st_size > 10_000_000:
                raise ValueError(f'{source.name}: подготовленное фото превышает 10 МБ')
        else:
            compatible = source.suffix.lower() == '.mp4'
            probe = self.library.ffprobe
            if probe:
                result = self._run_media_tool([probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
                                        timeout=30)
                metadata = json.loads(result.stdout) if result.returncode == 0 else {}
                streams = metadata.get('streams', [])
                videos = [s for s in streams if s.get('codec_type') == 'video']
                audio = [s for s in streams if s.get('codec_type') == 'audio']
                compatible = (compatible and bool(videos) and videos[0].get('codec_name') == 'h264'
                              and all(s.get('codec_name') == 'aac' for s in audio))
            if compatible:
                shutil.copyfile(source, target)
                converted = False
            else:
                if not self.library.ffmpeg:
                    raise ValueError(f'{source.name}: для подготовки этого видео нужен FFmpeg. Используйте MP4 или установите FFmpeg.')
                result = self._run_media_tool([self.library.ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(source),
                                         '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'fast',
                                         '-crf', '20', '-pix_fmt', 'yuv420p', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                                         '-c:a', 'aac', '-movflags', '+faststart', str(target)], timeout=1800)
                if result.returncode:
                    raise ValueError(f'{source.name}: FFmpeg не смог подготовить MP4')
            limit = 2_000_000_000 if settings['mode'] == 'local' else 50_000_000
            if target.stat().st_size > limit:
                raise ValueError(f'{source.name}: видео превышает лимит {limit // 1_000_000} МБ. Выберите локальный Bot API или подготовьте меньший файл.')
        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError(f'{source.name}: исходник изменился во время подготовки; повторите подготовку')
        return dict(path=target, kind='video' if video else 'photo', name=source.name, converted=converted)

    def send_ready(self, ids=None):
        with self.lock:
            if self._working:
                raise ValueError('Дождитесь завершения текущей операции или остановите подготовку')
            self._overview_cache = None
            items = self.status()['job']['items']
            if ids is None:
                ids = []
                for item in items:
                    if item['status'] not in ('ready', 'cached', 'sent'):
                        break
                    if item['status'] != 'sent':
                        ids.append(item['id'])
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                raise ValueError('Выберите готовые посты')
            ready = {item['id'] for item in items if item['status'] in ('ready','cached')}
            if set(ids)-ready:
                raise ValueError('Выбранный пост не готов. Повторите подготовку.')
            return self.prepare(ids, cache_only=True, send_after=True)

    def send(self, ident, ids=None):
        with self.lock:
            self._sync_drafts()
            if self.job and self.job.get('channel_status') != 'ready':
                raise ValueError('Сначала проверьте подключение к каналу. Готовые файлы сохранены.')
            if self._working or not self.job or self.job['id'] != ident or self.job['status'] not in ('ready', 'error', 'cancelled', 'done'):
                raise ValueError('Сначала подготовьте и проверьте посты')
            ready_ids = {p['post']['id'] for p in self.job.get('_prepared', [])
                         if self.journal.get(p['key'], {}).get('status') not in ('sent', 'unknown', 'sending')}
            if ids is None:
                ids = []
                for item in self.job['items']:
                    if item['status'] not in ('ready', 'sent'):
                        break
                    if item['status'] == 'ready':
                        ids.append(item['id'])
            elif not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError('Выберите готовые посты')
            if not ids or set(ids) - ready_ids:
                raise ValueError('Нет готовых постов для выбранного способа отправки')
            # Keep the reviewed order even when selection arrives in another order.
            selected = [p for p in self.job['_prepared'] if p['post']['id'] in ids]
            if any(not m['path'].is_file() for p in selected for m in p['media']):
                raise ValueError('Подготовленный файл исчез. Повторите подготовку.')
            for prepared in selected:
                post = prepared['post']
                limit = 1024 if post['photos'] else 4096
                if len(post['photos'])>10 or len(post['caption'].encode('utf-16-le'))//2>limit:
                    raise ValueError('Пост превышает лимиты. Проверьте подпись и число файлов.')
                for ident, media in zip(post['photos'], prepared['media']):
                    source = self.library.files.get(ident)
                    if not source or not source.is_file() or (media.get('cache_key') and media['cache_key'] != self._media_cache_key(source,self.job['_settings'])):
                        raise ValueError('Исходный файл изменился или недоступен. Повторите подготовку.')
                    reviewed = next(f for item in self.job['items'] if item['id']==post['id'] for f in item['files'] if f['id']==ident)
                    if media['path'].stat().st_size != reviewed['bytes']:
                        raise ValueError('Подготовленный файл изменился. Повторите подготовку.')
            self.cancelled.clear()
            self.job.update(status='sending', message='Отправляем…', done=0,
                            total=len(selected), _sending=selected)
            self._working = True
            threading.Thread(target=self._send, daemon=True).start()
            return {'ok': True}

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.job and self.job['status'] in ('preparing', 'cancelling'):
                self.job.update(status='cancelling', message='Останавливаем подготовку, сохраняем готовые посты…')
            if self.job and not self._working and self.job['status'] in ('ready', 'error', 'cancelled', 'done'):
                self.job.update(status='cancelled', message='Очередь закрыта', _prepared=[])
                self._cleanup()
            return {'ok': True}

    def _send(self):
        active_key = None
        try:
            destination = self.job['destination']
            client = self.client_factory(self.job['_settings'])
            for index, item in enumerate(self.job['_sending']):
                if self.cancelled.is_set():
                    break
                post, media, key = item['post'], item['media'], item['key']
                record = dict(post_id=post['id'], title=post['title'], channel=destination['channel'],
                              chat_id=destination['chat_id'], bot_id=destination['bot_id'], status='sending',
                              time=time.time(), messages=[], link='', channel_input=self.job['_settings']['channel'],
                              files=copy.deepcopy(next(p['files'] for p in self.job['items'] if p['id'] == post['id'])), cache_keys=[m['cache_key'] for m in media if m.get('cache_key')])
                self._update(message=f'Подключаемся к Bot API: {post["title"]}', current=post['id'], uploaded=0, upload_total=0, phase='connecting', transfer_started=time.time())
                params = dict(chat_id=destination['chat_id'], disable_notification=self.job['_settings']['silent'])
                files = {}
                if not media:
                    method = 'sendMessage'
                    params['text'] = post['caption']
                elif len(media) == 1:
                    method = 'sendVideo' if media[0]['kind'] == 'video' else 'sendPhoto'
                    field = media[0]['kind']
                    params.update({field: 'attach://file0', 'caption': post['caption']})
                    files['file0'] = media[0]['path']
                else:
                    method = 'sendMediaGroup'
                    params['media'] = []
                    for n, m in enumerate(media):
                        entry = dict(type=m['kind'], media=f'attach://file{n}')
                        if n == 0:
                            entry['caption'] = post['caption']
                        params['media'].append(entry)
                        files[f'file{n}'] = m['path']
                upload_started = None
                def progress(sent, total):
                    nonlocal upload_started
                    now = time.monotonic()
                    if upload_started is None:
                        upload_started = now
                    waiting = sent >= total
                    self._update(uploaded=sent, upload_total=total,
                                 phase='response' if waiting else 'uploading',
                                 upload_seconds=max(.001, now-upload_started),
                                 upload_speed=sent/max(.001, now-upload_started),
                                 message=(f'Файлы переданы Bot API, ждём подтверждения Telegram: {post["title"]}'
                                          if waiting else f'Загружаем: {post["title"]}'))

                for attempt in range(4):
                    self._record(key, record)
                    active_key = key
                    try:
                        upload_started = None
                        request_started = time.monotonic()
                        self._update(phase='connecting', transfer_started=time.time(), uploaded=0, upload_total=0, upload_speed=0, upload_seconds=0,
                                     message=f'Подключаемся к Bot API: {post["title"]}')
                        result = client.call(method, params, files, progress)
                        break
                    except TelegramError as error:
                        if error.retry_after and not error.uncertain:
                            self._record(key, dict(record, status='error', error=str(error)))
                            active_key = None
                            if attempt < 3:
                                self._update(phase='rate_limit', message=f'Telegram просит подождать {error.retry_after} с…')
                                if self.cancelled.wait(error.retry_after):
                                    self._update(status='cancelled', message='Очередь остановлена')
                                    return
                                continue
                        raise
                messages = result if isinstance(result, list) else [result]
                if len(messages) != max(1, len(media)) or any(not isinstance(m, dict) or not isinstance(m.get('message_id'), int) for m in messages):
                    raise TelegramError('Telegram вернул неполное подтверждение. Проверьте канал.', uncertain=True)
                ids = [message['message_id'] for message in messages]
                username = destination.get('username')
                chat = str(destination['chat_id'])
                link = f'https://t.me/{username}/{ids[0]}' if username else (f'https://t.me/c/{chat[4:]}/{ids[0]}' if chat.startswith('-100') else '')
                self._record(key, dict(record, status='sent', messages=ids, link=link))
                active_key = None
                self._remove_sent_cache(media)
                self._update(done=index + 1, phase='confirmed')
                if index + 1 < self.job['total']:
                    # Count transfer/response time toward the pacing interval.
                    delay = max(0, 1 - (time.monotonic() - request_started))
                    if delay:
                        self._update(phase='pacing', message=f'Пост отправлен. Пауза перед следующим: {delay:.1f} с')
                        self.cancelled.wait(delay)
            self._update(status='cancelled' if self.cancelled.is_set() else 'done', message='Очередь остановлена' if self.cancelled.is_set() else 'Посты отправлены')
        except Exception as error:
            uncertain = not isinstance(error, TelegramError) or error.uncertain
            if active_key:
                record = self.journal[active_key]
                try:
                    self._record(active_key, dict(record, status='unknown' if uncertain else 'error', error=self._safe_error(error)))
                except OSError:
                    # Keep the UI conservative too; the durable pre-send record
                    # will be recovered as unknown after restart.
                    with self.lock:
                        self.journal[active_key] = dict(record, status='unknown', error='Не удалось сохранить результат. Проверьте канал и свободное место на диске.')
                        self._sync_post_status(self.journal[active_key])
            self._update(status='error', message=self._safe_error(error))
        finally:
            # Keep the reviewed copies for retry/resume; delete only after the whole selection is sent.
            if all(self.journal.get(p['key'], {}).get('status') == 'sent' for p in self.job.get('_prepared', [])):
                self._cleanup()
            with self.lock:
                self._working = False

    def resolve(self, key, resolution):
        with self.lock:
            if self.busy():
                raise ValueError('Сначала остановите очередь')
            record = self.journal.get(key)
            if not record or record['status'] != 'unknown' or resolution not in ('sent', 'retry'):
                raise ValueError('Не найден пост с неопределённым результатом')
            self._record(key, dict(record, status='sent' if resolution == 'sent' else 'retry',
                                   error='', manually_checked=True))
            if resolution == 'sent':
                self._remove_sent_cache([{'cache_key': key} for key in record.get('cache_keys', [])])
            return {'ok': True}

    def _safe_error(self, error):
        token = self.settings.get('token', '')
        message = str(error)
        return message.replace(token, '[token]') if token else message

    def _cleanup(self):
        self._overview_cache = None
        shutil.rmtree(self.outbox, ignore_errors=True)
