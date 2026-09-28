"""Bounded disk cache for generated previews, independent of project files."""
import json
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path


class PreviewCache:
    DEFAULT_LIMIT = 2 * 1024**3
    OWNED = re.compile(r'^[0-9a-f]{20}-\d+-\d+-(?:(?:480|1800)(?:-v\d+)?\.jpg|duration\.json)$')

    def __init__(self, directory, settings):
        self.directory, self.settings = Path(directory), Path(settings)
        self.condition = threading.Condition(threading.RLock())
        self.pins = {}
        self.local = threading.local()
        self.allowed = None
        self.active = 0
        self.clearing = False
        self.entries = OrderedDict()
        self.total = 0
        self.limit = self.DEFAULT_LIMIT
        if self.settings.exists():
            value = json.loads(self.settings.read_text(encoding='utf-8')).get('limit_bytes')
            if isinstance(value, int) and value > 0:
                self.limit = value
        self.directory.mkdir(parents=True, exist_ok=True)
        entries = []
        for path in self.directory.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if self.OWNED.fullmatch(path.name):
                stat = path.stat()
                entries.append((stat.st_atime, path, stat.st_size))
            elif self.OWNED.fullmatch(path.name.replace('.tmp.jpg', '.jpg').replace('-duration.tmp', '-duration.json')):
                path.unlink(missing_ok=True)
        for _, path, size in sorted(entries):
            self.entries[path] = size
            self.total += size

    @contextmanager
    def use(self, path):
        """Pin files until they have been read, and drain users before clearing."""
        with self.condition:
            while self.clearing and not getattr(self.local, 'depth', 0):
                self.condition.wait()
            self.local.depth = getattr(self.local, 'depth', 0) + 1
            self.active += 1
            self.pins[path] = self.pins.get(path, 0) + 1
        try:
            yield
        finally:
            with self.condition:
                self.local.depth -= 1
                self.active -= 1
                self.pins[path] -= 1
                if not self.pins[path]:
                    del self.pins[path]
                    if self.allowed is not None and path.name not in self.allowed:
                        self._remove(path)
                self._trim()
                self.condition.notify_all()

    def record(self, path):
        with self.condition:
            stat = path.stat()
            self.total -= self.entries.pop(path, 0)
            self.entries[path] = stat.st_size
            self.total += stat.st_size
            # Persist recency across restarts without changing the JPEG's mtime.
            try:
                os.utime(path, ns=(time.time_ns(), stat.st_mtime_ns))
            except OSError:
                pass
            self._trim()

    def _remove(self, path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        self.total -= self.entries.pop(path, 0)
        return True

    def _trim(self):
        if self.total <= self.limit:
            return
        for path in list(self.entries):
            if path not in self.pins:
                self._remove(path)
            if self.total <= self.limit:
                break

    def prune_obsolete(self, allowed):
        with self.condition:
            self.allowed = allowed
            for path in list(self.entries):
                if path.name not in allowed and path not in self.pins:
                    self._remove(path)
            self._trim()

    def stats(self):
        with self.condition:
            groups = {name: {'files': 0, 'bytes': 0} for name in ('thumbnails', 'large', 'metadata')}
            for path, size in self.entries.items():
                group = 'metadata' if path.name.endswith('-duration.json') else 'thumbnails' if re.search(r'-480(?:-v\d+)?\.jpg$', path.name) else 'large'
                groups[group]['files'] += 1
                groups[group]['bytes'] += size
            return dict(bytes=self.total, files=len(self.entries), limit_bytes=self.limit, groups=groups)

    def set_limit(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 64 * 1024**2 <= value <= 100 * 1024**3:
            raise ValueError('Лимит должен быть от 64 МБ до 100 ГБ')
        with self.condition:
            temp = self.settings.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as f:
                json.dump({'limit_bytes': value}, f)
                f.flush()
                os.fsync(f.fileno())
            temp.replace(self.settings)
            self.limit = value
            self._trim()
            return self.stats()

    def clear(self):
        with self.condition:
            while self.clearing:
                self.condition.wait()
            self.clearing = True
            self.condition.notify_all()
            try:
                while self.active:
                    self.condition.wait()
                for path in list(self.entries):
                    self._remove(path)
                if self.entries:
                    raise OSError('Не удалось удалить часть файлов кэша. Проверьте права доступа к папке превью.')
                return self.stats()
            finally:
                self.clearing = False
                self.condition.notify_all()
