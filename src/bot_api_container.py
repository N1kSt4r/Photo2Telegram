"""Manage only this repository's Bot API Compose service, without shell commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time


if __package__:
    from .telegram_credentials import TelegramCredentials
else:
    from telegram_credentials import TelegramCredentials


class BotAPIContainer:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.credentials = TelegramCredentials(self.root / '.env')
        self.restart_required = self.credentials.read().get('TELEGRAM_API_RESTART_REQUIRED') == '1'
        self.lock = threading.RLock()
        self.active = False
        self.refreshing = False
        self.checked = 0
        self.state = dict(status='checking', message='Проверяем Docker…')

    def busy(self):
        with self.lock:
            return self.active

    def _set(self, status, message):
        with self.lock:
            self.state = dict(status=status, message=message)

    def _run(self, args, timeout=20):
        executable = shutil.which('docker')
        if not executable:
            candidate = Path('/Applications/Docker.app/Contents/Resources/bin/docker')
            if candidate.is_file():
                executable = str(candidate)
        if not executable:
            raise ValueError('Docker не найден. Нужен работающий Docker с Docker Compose.')
        env = os.environ.copy()
        env['PATH'] = str(Path(executable).parent) + os.pathsep + env.get('PATH', '')
        return subprocess.run([executable, *args], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=timeout,
                              encoding='utf-8', errors='replace')

    def _compose(self, *args, timeout=20):
        return self._run(['compose', '--project-directory', str(self.root),
                          '--env-file', str(self.root / '.env'), '-f', str(self.root / 'compose.yaml'),
                          '-p', 'photo2telegram', *args], timeout)

    def _check(self):
        if self._run(['version', '--format', '{{.Server.Version}}']).returncode:
            raise ValueError('Docker недоступен. Запустите Docker и дождитесь запуска движка.')
        if not (self.root / '.env').is_file() or self._compose('config', '--quiet').returncode:
            raise ValueError('Проверьте Docker Compose и заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.')

    def _inspect(self):
        result = self._compose('ps', '--all', '--format', 'json', 'telegram-bot-api')
        if result.returncode:
            raise ValueError('Не удалось получить состояние контейнера Bot API.')
        output = result.stdout.strip()
        rows = json.loads(output) if output.startswith('[') else [json.loads(line) for line in output.splitlines() if line.strip()]
        if rows and rows[0].get('State') == 'running':
            self._set('running', 'Контейнер Bot API работает. Доступ к каналу проверяется отдельно.')
        elif rows and rows[0].get('State') in ('restarting', 'dead'):
            raise ValueError('Контейнер Bot API не может запуститься. Проверьте ключи в .env и журнал Docker.')
        else:
            self._set('stopped', 'Контейнер Bot API остановлен.')

    def status(self):
        with self.lock:
            if not self.active and not self.refreshing and time.monotonic() - self.checked > 5:
                self.refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return dict(self.state, busy=self.active, credentials=self.credentials.public_api(), restart_required=self.restart_required)

    def _refresh(self):
        try:
            self._check()
            self._inspect()
        except Exception as error:
            self._error(error)
        finally:
            with self.lock:
                self.refreshing = False
                self.checked = time.monotonic()

    def _error(self, error):
        # Never expose Docker output: it may include credentials from .env.
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else (
            'Docker долго не отвечает. Проверьте состояние контейнера в Docker.' if isinstance(error, subprocess.TimeoutExpired)
            else 'Ошибка управления Docker. Проверьте Docker и журнал контейнера.')
        self._set('error', message)

    def action(self, action):
        if action not in ('start', 'stop'):
            raise ValueError('Неизвестная команда контейнера')
        with self.lock:
            if self.active or self.refreshing:
                raise ValueError('Дождитесь завершения текущей проверки или операции Docker')
            self.active = True
            self._set('starting' if action == 'start' else 'stopping',
                      'Запускаем Bot API…' if action == 'start' else 'Останавливаем Bot API…')
            threading.Thread(target=self._operate, args=(action,), daemon=True).start()
            return dict(self.state, busy=True)

    def _operate(self, action):
        try:
            self._check()
            if action == 'start':
                self._set('starting', 'Проверяем и при необходимости скачиваем образ Bot API…')
                result = self._compose('up', '-d', 'telegram-bot-api', timeout=600)
            else:
                result = self._compose('stop', 'telegram-bot-api', timeout=60)
            if result.returncode:
                raise ValueError('Не удалось запустить Bot API. Проверьте интернет, порт 8081 и журнал Docker.' if action == 'start'
                                 else 'Не удалось остановить Bot API. Проверьте состояние контейнера в Docker.')
            self._inspect()
            if action == 'start':
                self.credentials.update({'TELEGRAM_API_RESTART_REQUIRED': '0'})
                self.restart_required = False
        except Exception as error:
            self._error(error)
        finally:
            with self.lock:
                self.active = False
                self.checked = time.monotonic()

    def save_credentials(self, value):
        with self.lock:
            if self.active:
                raise ValueError('Дождитесь завершения операции Docker')
            result = self.credentials.save_api(value)
            if result['changed']:
                self.credentials.update({'TELEGRAM_API_RESTART_REQUIRED': '1'})
                self.restart_required = True
                self.checked = 0
            return result
