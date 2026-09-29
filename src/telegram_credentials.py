"""Atomic updates of Telegram credentials in the local Compose .env file."""
import os
from pathlib import Path
import re
import tempfile
import threading

_LOCK = threading.RLock()


class TelegramCredentials:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        with _LOCK:
            result = {}
            if self.path.exists():
                for line in self.path.read_text(encoding='utf-8').splitlines():
                    match = re.match(r'^\s*(?:export\s+)?(TELEGRAM_[A-Z0-9_]+)\s*=\s*(.*?)\s*$', line)
                    if match:
                        value = match[2]
                        if value.startswith(('"', "'")):
                            value = value[1:].split(value[0], 1)[0]
                        else:
                            value = value.split(' #', 1)[0].strip()
                        result[match[1]] = value
            return result

    def update(self, values):
        with _LOCK:
            for key, value in values.items():
                if not re.fullmatch(r'TELEGRAM_[A-Z0-9_]+', key) or not re.fullmatch(r'[A-Za-z0-9_:\-]*', value):
                    raise ValueError('Некорректное значение параметра Telegram')
            lines = self.path.read_text(encoding='utf-8').splitlines() if self.path.exists() else []
            lines = [line for line in lines if not any(re.match(r'^\s*(?:export\s+)?'+re.escape(key)+r'\s*=', line) for key in values)]
            lines += [key+'='+value for key, value in values.items()]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.env-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write('\n'.join(lines)+'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def public_api(self):
        values = self.read()
        return dict(has_api_id=bool(values.get('TELEGRAM_API_ID')), has_api_hash=bool(values.get('TELEGRAM_API_HASH')))

    def save_api(self, value):
        with _LOCK:
            previous = self.read()
            ident = str(value.get('api_id', '')).strip() or previous.get('TELEGRAM_API_ID', '')
            secret = str(value.get('api_hash', '')).strip() or previous.get('TELEGRAM_API_HASH', '')
            if not re.fullmatch(r'[1-9][0-9]*', ident):
                raise ValueError('api_id должен быть положительным целым числом')
            if not re.fullmatch(r'[a-fA-F0-9]{32}', secret):
                raise ValueError('api_hash должен содержать 32 шестнадцатеричных символа')
            changed = ident != previous.get('TELEGRAM_API_ID') or secret != previous.get('TELEGRAM_API_HASH')
            self.update({'TELEGRAM_API_ID': ident, 'TELEGRAM_API_HASH': secret})
            return dict(self.public_api(), changed=changed)
