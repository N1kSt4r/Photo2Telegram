"""Create an isolated environment and launch Photo2Telegram on any supported OS."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(usage='run.sh / run.bat ["/path/to/photos"] [--data DIR] [--port PORT]')
    parser.add_argument('root', nargs='?', type=Path, default=Path.cwd())
    parser.add_argument('--data', type=Path)
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    if not args.root.is_dir():
        print(f'Папка не найдена: {args.root}', file=sys.stderr)
        return 1
    if sys.version_info < (3, 10):
        print('Нужен Python 3.10 или новее.', file=sys.stderr)
        return 1
    env = ROOT / '.venv'
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Создаём виртуальное окружение…', flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    requirements = ROOT / 'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = env / 'photo2telegram-requirements.sha256'
    ready = marker.exists() and marker.read_text(encoding='utf-8') == digest
    if ready:
        ready = subprocess.run([str(python), '-c', 'import PIL, pillow_heif, filelock'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not ready:
        print('Устанавливаем зависимости приложения…', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
        marker.write_text(digest, encoding='utf-8')
    os.environ.setdefault('PYTHONUTF8', '1')
    command = [str(python), str(ROOT / 'src/server.py'), '--root', str(args.root), '--open']
    if args.data is not None:
        command.extend(['--data', str(args.data)])
    if args.port is not None:
        command.extend(['--port', str(args.port)])
    if os.name != 'nt':
        os.execv(str(python), command)
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'Не удалось запустить приложение: {error}', file=sys.stderr)
        sys.exit(1)
