#!/usr/bin/env python3
"""Local photo editor with optional, explicit Telegram publishing."""
import argparse
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageCms, ImageOps
from pillow_heif import register_heif_opener
from filelock import FileLock, Timeout

if __package__:
    from .preview_cache import PreviewCache
    from .telegram_sender import Publisher
else:
    from preview_cache import PreviewCache
    from telegram_sender import Publisher

register_heif_opener()

APP = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {'.mov', '.mp4', '.m4v', '.webm'}
EXTENSIONS = VIDEO_EXTENSIONS | {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.tif', '.tiff', '.webp'}


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def open_local(path):
    path = str(Path(path).resolve())
    if sys.platform == 'win32':
        os.startfile(path)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path])


def image_date(path):
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            details = exif.get_ifd(34665) if 34665 in exif else {}
            for value in (details.get(36867), exif.get(36867), details.get(36868), exif.get(306)):
                if isinstance(value, bytes):
                    value = value.decode('ascii', errors='ignore')
                if isinstance(value, str):
                    try:
                        return datetime.strptime(value.strip(' \x00'), '%Y:%m:%d %H:%M:%S').isoformat()
                    except ValueError:
                        continue
    except (OSError, ValueError, SyntaxError):
        pass
    return None


def image_preview(source, target, size):
    with Image.open(source) as original:
        profile = original.info.get('icc_profile')
        original.thumbnail((size, size), Image.Resampling.LANCZOS)
        image = ImageOps.exif_transpose(original)
        if 'A' in image.getbands() or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGBA', rgba.size, 'white')
            image = Image.alpha_composite(background, rgba).convert('RGB')
        if profile:
            try:
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                  ImageCms.createProfile('sRGB'), outputMode='RGB')
            except (OSError, ValueError, ImageCms.PyCMSError):
                image = image.convert('RGB')
        else:
            image = image.convert('RGB')
        image.save(target, 'JPEG', quality=85, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())


class Library:
    def __init__(self, root, data):
        self.root, self.data = root.resolve(), data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.cache = self.data / 'previews'
        self.cache.mkdir(exist_ok=True)
        self.preview_cache = PreviewCache(self.cache, self.data / 'cache-settings.json')
        self.state_path = self.data / 'project.json'
        self.lock = threading.RLock()
        self.conversions = threading.Semaphore(6)
        self.nearby_conversions = threading.Semaphore(6)
        self.background_conversions = threading.Semaphore(3)
        self.large_conversions = threading.Semaphore(1)
        self.large_background_conversions = threading.Semaphore(3)
        self.image_locks = {}
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.ffmpeg = shutil.which('ffmpeg') or ('/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else None)
        self.ffprobe = shutil.which('ffprobe') or ('/opt/homebrew/bin/ffprobe' if Path('/opt/homebrew/bin/ffprobe').exists() else None)
        self.photos = []
        self.files = {}
        self.catalog_path = self.data / 'library.json'
        self.catalog = {}
        if self.catalog_path.exists():
            self.catalog = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        self.state = dict(revision=0, hidden=[], posts=[], active=None)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
        # Legacy projects stored only IDs. Keep unresolved references editable.
        references = list(self.state.get('hidden', []))
        for post in self.state.get('posts', []):
            references.extend(post.get('photos', []))
        for ident in references:
            if isinstance(ident, str) and ident not in self.catalog:
                self.catalog[ident] = dict(id=ident, name=f'Недоступный файл ({ident})',
                                          date='', dateSource='unknown', kind='photo')
        self.refresh()
        self.publisher = Publisher(self, image_preview)
        self.validate(self.state)

    def refresh(self):
        """Rescan direct children; preserve identities by filename, never by content."""
        with self.lock:
            # Fail before changing the catalog if the root itself is unavailable.
            paths = sorted(self.root.iterdir())
            catalog = {ident: dict(photo, missing=True) for ident, photo in self.catalog.items()}
            files = {}
            for path in paths:
                if path.suffix.lower() not in EXTENSIONS:
                    continue
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    ident = hashlib.sha256(path.name.encode()).hexdigest()[:20]
                    version = f'{stat.st_mtime_ns}-{stat.st_size}'
                    previous = self.catalog.get(ident)
                    if previous and previous.get('version') == version:
                        photo = dict(previous, missing=False)
                    else:
                        match = re.match(r'(\d{4}-\d{2}-\d{2})[ _](\d{2})[-:](\d{2})[-:](\d{2})', path.stem)
                        if match:
                            date = f'{match[1]}T{match[2]}:{match[3]}:{match[4]}'
                            source = 'filename'
                        else:
                            metadata_date = image_date(path) if path.suffix.lower() not in VIDEO_EXTENSIONS else None
                            date = metadata_date or datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')
                            source = 'metadata' if metadata_date else 'file'
                        photo = dict(id=ident, name=path.name, date=date, dateSource=source,
                                     kind='video' if path.suffix.lower() in VIDEO_EXTENSIONS else 'photo',
                                     version=version, missing=False)
                        if photo['kind'] == 'video':
                            photo['duration'] = self.video_duration(path, ident)
                    catalog[ident] = photo
                    files[ident] = path
                except OSError:
                    # A file can disappear while the directory is being scanned.
                    continue
            atomic_json(self.catalog_path, catalog)
            self.catalog = catalog
            self.files = files
            self.photos = sorted(catalog.values(), key=lambda photo: (photo['date'], photo['name']))
            allowed = set()
            for ident, photo in catalog.items():
                if photo.get('missing') or not photo.get('version'):
                    continue
                prefix = f"{ident}-{photo['version']}"
                allowed.update({f'{prefix}-480-v2.jpg', f'{prefix}-1800-v2.jpg', f'{prefix}-duration.json'})
            self.preview_cache.prune_obsolete(allowed)
            # Restore cleared metadata from the catalog without probing videos
            # again. Only known durations of the same file version are reused.
            for photo in self.photos:
                if photo['kind'] == 'video' and not photo.get('missing'):
                    try:
                        photo['duration'] = self.video_duration(files[photo['id']], photo['id'], known=photo.get('duration'))
                    except OSError:
                        pass
            return self.photos

    def validate(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('revision'), int):
            raise ValueError('Некорректный проект')
        posts, hidden = value.get('posts'), value.get('hidden')
        if not isinstance(posts, list) or not isinstance(hidden, list) or len(posts) > 10000:
            raise ValueError('Некорректный список постов')
        ids = set()
        for post in posts:
            if not isinstance(post, dict) or not isinstance(post.get('id'), str) or post['id'] in ids:
                raise ValueError('Некорректный идентификатор поста')
            ids.add(post['id'])
            photos = post.get('photos')
            if not isinstance(photos, list) or len(photos) > 10 or any(not isinstance(i, str) for i in photos) or len(set(photos)) != len(photos):
                raise ValueError('В посте может быть не более 10 разных файлов')
            if any(i not in self.catalog for i in photos):
                raise ValueError('Неизвестный файл в проекте')
            if not isinstance(post.get('caption'), str) or len(post['caption']) > 100000:
                raise ValueError('Некорректная подпись')
            if not isinstance(post.get('title'), str) or len(post['title']) > 200:
                raise ValueError('Некорректное название')
        if any(not isinstance(i, str) or i not in self.catalog for i in hidden):
            raise ValueError('Неизвестные скрытые файлы')
        if value.get('active') is not None and value['active'] not in ids:
            raise ValueError('Неизвестный текущий пост')

    def save(self, value):
        with self.lock:
            self.validate(value)
            if value['revision'] != self.state['revision']:
                return None
            value['revision'] += 1
            if self.state_path.exists():
                shutil.copy2(self.state_path, self.data / 'project.backup.json')
            atomic_json(self.state_path, value)
            self.state = value
            return value['revision']

    def video_duration(self, path, ident, known=None):
        cache = self.cache / f'{ident}-{path.stat().st_mtime_ns}-{path.stat().st_size}-duration.json'
        with self.preview_cache.use(cache):
            try:
                if cache.exists():
                    duration = json.loads(cache.read_text(encoding='utf-8'))['duration']
                    self.preview_cache.record(cache)
                    return duration
                if isinstance(known, (int, float)) and math.isfinite(known) and known >= 0:
                    duration = known
                else:
                    if not self.ffprobe:
                        return None
                    result = subprocess.run([self.ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, text=True, timeout=15, check=True)
                    duration = float(json.loads(result.stdout)['format']['duration'])
                if not math.isfinite(duration) or duration < 0:
                    return None
                with self.preview_cache.condition:
                    atomic_json(cache, {'duration': duration})
                    self.preview_cache.record(cache)
                return duration
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return None

    def preview(self, ident, size, priority='visible', read_bytes=False):
        p = self.files[ident]
        fingerprint = f'{p.stat().st_mtime_ns}-{p.stat().st_size}'
        target = self.cache / f'{ident}-{fingerprint}-{size}-v2.jpg'
        with self.lock:
            lock = self.image_locks.setdefault((ident, size), threading.Lock())
        with self.preview_cache.use(target):
            with lock:
                if not target.exists():
                    pool = (self.large_background_conversions if priority == 'background' else
                            self.large_conversions) if size > 480 else (
                        self.nearby_conversions if priority == 'nearby' else
                        self.background_conversions if priority == 'background' else self.conversions)
                    with pool:
                        temp = target.with_suffix('.tmp.jpg')
                        if p.suffix.lower() in VIDEO_EXTENSIONS:
                            if not self.ffmpeg:
                                raise RuntimeError('FFmpeg недоступен')
                            duration = self.video_duration(p, ident)
                            seek = min(1, duration / 3) if duration else 0
                            command = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1', '-ss', str(seek), '-i', str(p), '-map', '0:v:0', '-frames:v', '1', '-vf', f'scale={size}:{size}:force_original_aspect_ratio=decrease', '-q:v', '3', '-threads', '1', str(temp)]
                            result = subprocess.run(command, capture_output=True, timeout=90)
                            if result.returncode:
                                temp.unlink(missing_ok=True)
                                raise RuntimeError('Не удалось создать превью видео')
                        else:
                            image_preview(p, temp, size)
                        if not temp.exists() or temp.stat().st_size == 0:
                            temp.unlink(missing_ok=True)
                            raise RuntimeError('Не удалось создать превью')
                        temp.replace(target)
            self.preview_cache.record(target)
            return target.read_bytes() if read_bytes else target

    def export(self):
        with self.lock:
            posts = json.loads(json.dumps([p for p in self.state['posts'] if p['photos']]))
            if not posts:
                raise ValueError('Сначала добавьте фото или видео в пост')
            if any(len(p['caption']) > 1024 for p in posts):
                raise ValueError('Для экспорта сократите подписи до 1024 символов')
            missing = sorted({self.catalog[ident]['name'] for post in posts for ident in post['photos']
                              if ident not in self.files or not self.files[ident].is_file()})
            if missing:
                names = ', '.join(missing[:5])
                suffix = f' и ещё {len(missing) - 5}' if len(missing) > 5 else ''
                raise ValueError(f'В постах есть недоступные файлы: {names}{suffix}. Верните их и обновите библиотеку или уберите из постов.')
            export_files = dict(self.files)
            ident = uuid.uuid4().hex
            job = dict(status='working', done=0, total=sum(len(p['photos']) for p in posts), path='')
            self.jobs[ident] = job
        def run():
            output = self.root / 'Посты для Telegram' / (datetime.now().strftime('%Y-%m-%d %H-%M-%S') + '-' + ident[:4])
            try:
                output.mkdir(parents=True)
                manifest = []
                for n, post in enumerate(posts, 1):
                    title = re.sub(r'[^\w\s—-]', '', post['title'], flags=re.UNICODE).strip()[:65] or 'Пост'
                    folder = output / f'{n:03d} — {title}'
                    folder.mkdir()
                    exported = []
                    for i, photo in enumerate(post['photos'], 1):
                        src = export_files[photo]
                        name = f'{i:02d} — {src.name}'
                        shutil.copy2(src, folder / name)
                        exported.append(name)
                        job['done'] += 1
                    if post['caption'].strip():
                        (folder / 'Подпись.txt').write_text(post['caption'], encoding='utf-8')
                    manifest.append(dict(folder=folder.name, files=exported, caption=post['caption']))
                atomic_json(output / 'Порядок постов.json', manifest)
                (output / 'Как отправить.txt').write_text('Папки пронумерованы в порядке публикации.\nВ каждой папке выберите фотографии и видео, затем вставьте текст из файла «Подпись.txt», если он есть.\nФайлы — точные копии оригиналов. Номера в именах обозначают желаемый порядок; проверьте его в Telegram перед отправкой.\nДля HEIC способ отправки и отображение зависят от клиента Telegram.\n', encoding='utf-8')
                job.update(status='done', path=str(output))
            except Exception as e:
                job.update(status='error', error=str(e), path=str(output))
        threading.Thread(target=run, daemon=True).start()
        return ident


class LibraryManager:
    """Keep each source folder's drafts separate, including legacy data layouts."""
    def __init__(self, root, data):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.registry_path = self.data / 'folders.json'
        self.registry = (json.loads(self.registry_path.read_text(encoding='utf-8'))
                         if self.registry_path.exists() else {})
        self.current = None
        self.libraries = {}
        self.switch(root)

    @staticmethod
    def key(root):
        return hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()

    def switch(self, path):
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError('Укажите путь к папке')
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('Папка не найдена или недоступна')
        with self.lock:
            if self.current and self.current.root == root:
                return self.current
            if self.current and self.current.publisher.busy():
                raise ValueError('Завершите или отмените подготовку/отправку в Telegram перед сменой папки')
            if self.current and any(job['status'] == 'working' for job in self.current.jobs.values()):
                raise ValueError('Дождитесь завершения экспорта перед сменой папки')
            key = self.key(root)
            # The first folder keeps existing project.json/library.json in place.
            relative = self.registry.get(key, {}).get('data', '.' if not self.registry else f'projects/{key}')
            # Reuse locks/cache bookkeeping if requests from the previous visit
            # are still finishing when this folder is opened again.
            library = self.libraries.get(key)
            if library is None:
                library = Library(root, self.data / relative)
            else:
                library.refresh()
            registry = {**self.registry, key: {'root': str(root), 'data': relative}}
            atomic_json(self.registry_path, registry)
            self.registry = registry
            library.token = secrets.token_urlsafe(32)
            self.libraries[key] = library
            self.current = library
            return library

    def suggest_folders(self, value):
        if not isinstance(value, str):
            raise ValueError('Укажите путь к папке')
        expanded = os.path.expanduser(value)
        separators = tuple(s for s in (os.sep, os.altsep) if s)
        if not expanded:
            parent, prefix = self.current.root, ''
        elif expanded.endswith(separators) or value == '~':
            parent, prefix = Path(expanded), ''
        else:
            path = Path(expanded)
            parent, prefix = path.parent, path.name.casefold()
        matches = []
        try:
            for child in parent.iterdir():
                try:
                    if child.name.casefold().startswith(prefix) and child.is_dir():
                        matches.append({'name': child.name, 'path': str(child.absolute()) + os.sep})
                except OSError:
                    continue
        except (OSError, ValueError):
            return {'folders': []}
        matches.sort(key=lambda entry: entry['name'].casefold())
        return {'folders': matches[:100], 'more': len(matches) > 100}

    def folders(self, path, up=False):
        root = Path(path).expanduser().resolve() if path else self.current.root
        if up:
            root = root.parent
        if not root.is_dir():
            raise ValueError('Папка не найдена или недоступна')
        directories = []
        for child in root.iterdir():
            try:
                if child.is_dir():
                    directories.append({'name': child.name, 'path': str(child)})
            except OSError:
                continue
        directories.sort(key=lambda entry: entry['name'].casefold())
        shortcuts = [{'name': 'Домашняя папка', 'path': str(Path.home())},
                     {'name': 'Текущая библиотека', 'path': str(self.current.root)}]
        if os.name == 'nt':
            shortcuts.extend({'name': f'{letter}:', 'path': f'{letter}:/'}
                             for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:/').is_dir())
        input_path = str(root)
        if not input_path.endswith(os.sep):
            input_path += os.sep
        return {'path': str(root), 'inputPath': input_path, 'parent': str(root.parent),
                'folders': directories, 'shortcuts': shortcuts}


def handler_for(manager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, data, content_type='application/json; charset=utf-8', cache=False):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable' if cache else 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def stream_video(self, path):
            size = path.stat().st_size
            start, end = 0, size - 1
            byte_range = self.headers.get('Range')
            if byte_range:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', byte_range)
                if not match or not any(match.groups()):
                    return self.respond(400, {'error': 'Некорректный диапазон'})
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(206 if byte_range else 200)
            self.send_header('Content-Type', {'.mov': 'video/quicktime', '.webm': 'video/webm'}.get(path.suffix.lower(), 'video/mp4'))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Cache-Control', 'no-store')
            if byte_range:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            try:
                with path.open('rb') as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': 'Forbidden host'})
            library = manager.current
            url = urlparse(self.path)
            if url.path.startswith(('/photo/', '/video/', '/telegram-file/')):
                project = parse_qs(url.query).get('project')
                if project and project != [manager.key(library.root)]:
                    return self.respond(409, {'error': 'Библиотека изменена. Обновите страницу.'})
            try:
                if url.path.startswith('/telegram-file/'):
                    path = library.publisher.prepared_file(url.path.removeprefix('/telegram-file/'))
                    if path is None:
                        return self.respond(404, {'error': 'Подготовленный файл больше не доступен. Повторите подготовку.'})
                    if path.suffix == '.mp4':
                        return self.stream_video(path)
                    return self.respond(200, path.read_bytes(), 'image/jpeg')
                if url.path == '/api/cache':
                    return self.respond(200, library.preview_cache.stats())
                if url.path == '/api/library':
                    with library.lock:
                        return self.respond(200, dict(photos=library.photos, state=library.state, token=library.token, folder=library.root.name, folderPath=str(library.root), project=manager.key(library.root)))
                if url.path.startswith('/api/job/'):
                    return self.respond(200, library.jobs[url.path.rsplit('/', 1)[1]])
                if url.path.startswith('/video/'):
                    path = library.files[url.path.rsplit('/', 1)[1]]
                    if path.suffix.lower() not in VIDEO_EXTENSIONS:
                        raise KeyError('Не видео')
                    return self.stream_video(path)
                if url.path.startswith('/photo/'):
                    ident = url.path.rsplit('/', 1)[1]
                    size = 1800 if parse_qs(url.query).get('size') == ['large'] else 480
                    if ident in library.catalog and (ident not in library.files or not library.files[ident].is_file()):
                        return self.respond(200, (APP / 'web' / 'missing.svg').read_bytes(), 'image/svg+xml')
                    try:
                        preview = library.preview(ident, size, priority=self.headers.get('X-Preview-Priority', 'visible'), read_bytes=True)
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        if library.files[ident].suffix.lower() in VIDEO_EXTENSIONS:
                            return self.respond(200, (APP / 'web' / 'video.svg').read_bytes(), 'image/svg+xml')
                        raise
                    return self.respond(200, preview, 'image/jpeg', True)
                files = {'/': 'index.html', '/app.js': 'app.js', '/previews.js': 'previews.js', '/telegram.js': 'telegram.js', '/style.css': 'style.css', '/video.svg': 'video.svg', '/missing.svg': 'missing.svg'}
                if url.path not in files:
                    return self.respond(404, {'error': 'Not found'})
                name = files[url.path]
                mime = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[name.rsplit('.', 1)[1]]
                self.respond(200, (APP / 'web' / name).read_bytes(), mime + '; charset=utf-8')
            except KeyError:
                self.respond(404, {'error': 'Не найдено'})
            except Exception as e:
                self.respond(500, {'error': str(e)})

        def do_POST(self):
            with manager.lock:
                self.handle_post()

        def handle_post(self):
            library = manager.current
            if not self.valid_host():
                return self.respond(403, {'error': 'Нет доступа'})
            if not self.headers.get('X-Local-Token'):
                return self.respond(403, {'error': 'Нет доступа'})
            if self.headers.get('X-Local-Token') != library.token:
                return self.respond(409, {'error': 'Библиотека изменена в другой вкладке. Обновите страницу.'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 4_000_000:
                    return self.respond(413, {'error': 'Слишком большой запрос'})
                data = json.loads(self.rfile.read(size) or b'{}')
                if self.path == '/api/telegram/status':
                    return self.respond(200, library.publisher.status())
                if self.path == '/api/telegram/settings':
                    return self.respond(200, library.publisher.save_settings(data))
                if self.path == '/api/telegram/check':
                    return self.respond(200, library.publisher.check())
                if self.path == '/api/telegram/prepare':
                    return self.respond(200, library.publisher.prepare(data.get('ids')))
                if self.path == '/api/telegram/send-ready':
                    return self.respond(200, library.publisher.send_ready(data.get('ids')))
                if self.path == '/api/telegram/send':
                    return self.respond(200, library.publisher.send(data.get('id'), data.get('ids')))
                if self.path == '/api/telegram/cancel':
                    return self.respond(200, library.publisher.cancel())
                if self.path == '/api/telegram/resolve':
                    return self.respond(200, library.publisher.resolve(data.get('key'), data.get('resolution')))
                if self.path == '/api/folder-suggestions':
                    return self.respond(200, manager.suggest_folders(data.get('path', '')))
                if self.path == '/api/folders':
                    return self.respond(200, manager.folders(data.get('path'), up=data.get('up') is True))
                if self.path == '/api/folder':
                    manager.switch(data.get('path'))
                    return self.respond(200, {'ok': True})
                if self.path == '/api/cache/clear':
                    return self.respond(200, library.preview_cache.clear())
                if self.path == '/api/cache/limit':
                    return self.respond(200, library.preview_cache.set_limit(data.get('limit_bytes')))
                if self.path == '/api/refresh':
                    return self.respond(200, {'photos': library.refresh()})
                if self.path == '/api/state':
                    revision = library.save(data)
                    if revision is None:
                        return self.respond(409, {'error': 'Проект изменён в другой вкладке. Обновите страницу перед продолжением.'})
                    return self.respond(200, {'revision': revision})
                if self.path == '/api/export':
                    return self.respond(200, {'id': library.export()})
                if self.path == '/api/open-video':
                    path = library.files.get(data.get('id'))
                    if not path or path.suffix.lower() not in VIDEO_EXTENSIONS:
                        raise ValueError('Видео не найдено')
                    open_local(path)
                    return self.respond(200, {'ok': True})
                if self.path == '/api/reveal':
                    job = library.jobs.get(data.get('id'))
                    if not job or job['status'] != 'done':
                        raise ValueError('Экспорт ещё не готов')
                    open_local(job['path'])
                    return self.respond(200, {'ok': True})
                self.respond(404, {'error': 'Not found'})
            except (ValueError, TypeError) as e:
                self.respond(400, {'error': str(e)})
            except Exception as e:
                self.respond(500, {'error': str(e)})
    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can block on
        # macOS DNS configuration. A loopback-only app needs no DNS name.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data', type=Path, default=APP.parent / 'data')
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    if not args.root.is_dir():
        parser.error('Папка с фото и видео не найдена')
    instance_lock = FileLock(args.data / 'instance.lock')
    try:
        instance_lock.acquire(timeout=0)
    except Timeout:
        runtime = args.data / 'runtime.json'
        if runtime.exists():
            url = json.loads(runtime.read_text(encoding='utf-8'))['url']
            print(f'Приложение уже открыто: {url}')
            if args.open:
                webbrowser.open(url)
        return
    try:
        manager = LibraryManager(args.root, args.data)
        library = manager.current
        server = None
        for port in range(args.port, args.port + 20):
            try:
                server = LocalHTTPServer(('127.0.0.1', port), handler_for(manager))
                break
            except PermissionError:
                raise SystemExit('Нет разрешения открыть локальный сервер')
            except OSError:
                continue
        if server is None:
            raise SystemExit('Не удалось найти свободный порт')
        url = f'http://127.0.0.1:{server.server_port}'
        atomic_json(args.data / 'runtime.json', {'url': url})
        print(f'Photo2Telegram: {url}\nНайдено фото и видео: {len(library.photos)}\nДля остановки нажмите Ctrl+C.', flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        instance_lock.release()


if __name__ == '__main__':
    main()
