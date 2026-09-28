#!/usr/bin/env python3
"""Local photo editor. Python standard library + macOS sips; no uploads."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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


class Library:
    def __init__(self, root, data):
        self.root, self.data = root.resolve(), data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.cache = self.data / 'previews'
        self.cache.mkdir(exist_ok=True)
        self.state_path = self.data / 'project.json'
        self.lock = threading.RLock()
        self.conversions = threading.Semaphore(3)
        self.image_locks = {}
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.ffmpeg = shutil.which('ffmpeg') or ('/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else None)
        self.ffprobe = shutil.which('ffprobe') or ('/opt/homebrew/bin/ffprobe' if Path('/opt/homebrew/bin/ffprobe').exists() else None)
        self.photos = []
        self.files = {}
        for p in sorted(self.root.iterdir()):
            if not p.is_file() or p.suffix.lower() not in EXTENSIONS:
                continue
            ident = hashlib.sha256(p.name.encode()).hexdigest()[:20]
            match = re.match(r'(\d{4}-\d{2}-\d{2})[ _](\d{2})[-:](\d{2})[-:](\d{2})', p.stem)
            if match:
                date = f'{match[1]}T{match[2]}:{match[3]}:{match[4]}'
                source = 'filename'
            elif p.suffix.lower() in VIDEO_EXTENSIONS:
                date = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds')
                source = 'file'
            else:
                result = subprocess.run(['sips', '-g', 'creation', str(p)], capture_output=True, text=True)
                found = re.search(r'creation: (\d{4}):(\d{2}):(\d{2}) (\d{2}:\d{2}:\d{2})', result.stdout)
                date = f'{found[1]}-{found[2]}-{found[3]}T{found[4]}' if found else datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds')
                source = 'metadata' if found else 'file'
            self.photos.append(dict(id=ident, name=p.name, date=date, dateSource=source, kind='video' if p.suffix.lower() in VIDEO_EXTENSIONS else 'photo'))
            self.files[ident] = p
            if p.suffix.lower() in VIDEO_EXTENSIONS:
                self.photos[-1]['duration'] = self.video_duration(p, ident)
        self.photos.sort(key=lambda p: (p['date'], p['name']))
        self.state = dict(revision=0, hidden=[], posts=[], active=None)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            self.validate(self.state)

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
            if any(i not in self.files for i in photos):
                raise ValueError('Фото из проекта отсутствует в исходной папке')
            if not isinstance(post.get('caption'), str) or len(post['caption']) > 100000:
                raise ValueError('Некорректная подпись')
            if not isinstance(post.get('title'), str) or len(post['title']) > 200:
                raise ValueError('Некорректное название')
        if any(not isinstance(i, str) or i not in self.files for i in hidden):
            raise ValueError('Неизвестные скрытые фото')
        if value.get('active') is not None and value['active'] not in ids:
            raise ValueError('Неизвестный текущий пост')

    def save(self, value):
        self.validate(value)
        with self.lock:
            if value['revision'] != self.state['revision']:
                return None
            value['revision'] += 1
            if self.state_path.exists():
                shutil.copy2(self.state_path, self.data / 'project.backup.json')
            atomic_json(self.state_path, value)
            self.state = value
            return value['revision']

    def video_duration(self, path, ident):
        cache = self.cache / f'{ident}-{path.stat().st_mtime_ns}-{path.stat().st_size}-duration.json'
        try:
            if cache.exists():
                return json.loads(cache.read_text())['duration']
            if not self.ffprobe:
                return None
            result = subprocess.run([self.ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, text=True, timeout=15, check=True)
            duration = float(json.loads(result.stdout)['format']['duration'])
            if not math.isfinite(duration) or duration < 0:
                return None
            atomic_json(cache, {'duration': duration})
            return duration
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return None

    def preview(self, ident, size):
        p = self.files[ident]
        fingerprint = f'{p.stat().st_mtime_ns}-{p.stat().st_size}'
        target = self.cache / f'{ident}-{fingerprint}-{size}.jpg'
        with self.lock:
            lock = self.image_locks.setdefault((ident, size), threading.Lock())
        with lock:
            if not target.exists():
                with self.conversions:
                    temp = target.with_suffix('.tmp.jpg')
                    if p.suffix.lower() in VIDEO_EXTENSIONS:
                        if not self.ffmpeg:
                            raise RuntimeError('FFmpeg недоступен')
                        duration = self.video_duration(p, ident)
                        seek = min(1, duration / 3) if duration else 0
                        command = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1', '-ss', str(seek), '-i', str(p), '-map', '0:v:0', '-frames:v', '1', '-vf', f'scale={size}:{size}:force_original_aspect_ratio=decrease', '-q:v', '3', '-threads', '1', str(temp)]
                        result = subprocess.run(command, capture_output=True, timeout=90)
                    else:
                        result = subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '85', '-Z', str(size), str(p), '--out', str(temp)], capture_output=True, timeout=90)
                    if result.returncode or not temp.exists() or temp.stat().st_size == 0:
                        temp.unlink(missing_ok=True)
                        raise RuntimeError('Не удалось создать превью')
                    temp.replace(target)
        return target

    def export(self):
        with self.lock:
            posts = json.loads(json.dumps([p for p in self.state['posts'] if p['photos']]))
            if not posts:
                raise ValueError('Сначала добавьте фотографии в пост')
            if any(len(p['caption']) > 1024 for p in posts):
                raise ValueError('Для экспорта сократите подписи до 1024 символов')
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
                        src = self.files[photo]
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


def handler_for(library):
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
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
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
            url = urlparse(self.path)
            try:
                if url.path == '/api/library':
                    with library.lock:
                        return self.respond(200, dict(photos=library.photos, state=library.state, token=library.token, folder=library.root.name))
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
                    try:
                        path = library.preview(ident, size)
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        if library.files[ident].suffix.lower() in VIDEO_EXTENSIONS:
                            return self.respond(200, (APP / 'web' / 'video.svg').read_bytes(), 'image/svg+xml')
                        raise
                    return self.respond(200, path.read_bytes(), 'image/jpeg', True)
                files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/video.svg': 'video.svg'}
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
            if not self.valid_host() or self.headers.get('X-Local-Token') != library.token:
                return self.respond(403, {'error': 'Нет доступа'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 4_000_000:
                    return self.respond(413, {'error': 'Слишком большой запрос'})
                data = json.loads(self.rfile.read(size) or b'{}')
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
                    subprocess.Popen(['open', str(path)])
                    return self.respond(200, {'ok': True})
                if self.path == '/api/reveal':
                    job = library.jobs.get(data.get('id'))
                    if not job or job['status'] != 'done':
                        raise ValueError('Экспорт ещё не готов')
                    subprocess.Popen(['open', job['path']])
                    return self.respond(200, {'ok': True})
                self.respond(404, {'error': 'Not found'})
            except (ValueError, TypeError) as e:
                self.respond(400, {'error': str(e)})
            except Exception as e:
                self.respond(500, {'error': str(e)})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=APP.parent)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data', type=Path, default=APP.parent / 'data')
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    instance_lock = (args.data / 'instance.lock').open('a+')
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        runtime = args.data / 'runtime.json'
        if runtime.exists():
            url = json.loads(runtime.read_text())['url']
            print(f'Приложение уже открыто: {url}')
            if args.open:
                webbrowser.open(url)
        return
    library = Library(args.root, args.data)
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(library))
            break
        except PermissionError:
            raise SystemExit('Нет разрешения открыть локальный сервер')
        except OSError:
            continue
    if server is None:
        raise SystemExit('Не удалось найти свободный порт')
    url = f'http://127.0.0.1:{server.server_port}'
    atomic_json(args.data / 'runtime.json', {'url': url})
    print(f'Фотопосты: {url}\nНайдено фото и видео: {len(library.photos)}\nДля остановки нажмите Ctrl+C.', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
