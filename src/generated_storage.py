"""Account for generated files; never follow links or delete unlisted exports."""
import json
from pathlib import Path
import re


def file_stats(paths):
    seen, count, size = set(), 0, 0
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino) if stat.st_ino else str(path)
            count += 1
            if identity not in seen:
                seen.add(identity)
                size += stat.st_size
        except OSError:
            continue
    return dict(files=count, bytes=size)


def prepared_paths(directory, pattern):
    if directory.is_symlink() or directory.resolve() != directory.absolute() or not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if re.fullmatch(pattern, p.name) and not p.is_symlink() and p.is_file()]


def simple_name(value):
    return isinstance(value, str) and value not in ('', '.', '..') and '/' not in value and '\\' not in value


def export_files(root):
    """Recognize both new and legacy exports from their manifests."""
    parent = root / 'Посты для Telegram'
    if parent.is_symlink() or parent.resolve().parent != root.resolve() or not parent.is_dir():
        return []
    exports = []
    for directory in sorted(parent.iterdir()):
        if directory.is_symlink() or directory.resolve().parent != parent.resolve() or not directory.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}-[a-f0-9]{4}', directory.name):
            continue
        manifest = directory / 'Порядок постов.json'
        try:
            if manifest.is_symlink():
                continue
            posts = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(posts, list) or not posts:
                continue
            paths, folders = [], []
            for post in posts:
                if not isinstance(post, dict) or not simple_name(post.get('folder')) or not isinstance(post.get('files'), list) or not all(simple_name(name) for name in post['files']):
                    raise ValueError('Invalid export manifest')
                folder = directory / post['folder']
                if folder.is_symlink() or folder.resolve().parent != directory.resolve():
                    raise ValueError('Linked export folder')
                folders.append(folder)
                paths.extend(folder / name for name in post['files'])
                if post.get('caption', '').strip():
                    paths.append(folder / 'Подпись.txt')
            paths.extend([directory / 'Как отправить.txt', manifest])
            exports.append((directory, paths, folders))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return exports


def clear_exports(root):
    for directory, paths, folders in export_files(root):
        # Keep the manifest until listed content has been removed successfully.
        for path in paths:
            if not path.is_symlink():
                path.unlink(missing_ok=True)
        for folder in set(folders):
            try:
                folder.rmdir()
            except (FileNotFoundError, OSError):
                pass  # Files added by the user are retained.
        try:
            directory.rmdir()
        except OSError:
            pass
