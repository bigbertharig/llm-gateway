"""Bounded browser and versioned editor for an allowlisted Codex workspace."""

from __future__ import annotations

import os
import re
import stat
import hashlib
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_FILE_BYTES = 1_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_VIEW_CHARS = 40_000
MAX_ENTRIES = 150
SKIP_SEARCH_DIRS = {'node_modules', '__pycache__', '.git', 'build', 'dist', 'results', 'target'}


def _parts(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or len(raw) > 400 or '\\' in raw:
        raise ValueError('Invalid workspace path')
    path = Path(raw or '.')
    if path.is_absolute() or any(part == '..' or part.startswith('.') for part in path.parts):
        raise ValueError('Workspace path is not visible')
    return tuple(part for part in path.parts if part != '.')


@contextmanager
def _open(root: Path, raw: str, directory: bool):
    parts = _parts(raw)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if directory or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise ValueError('Workspace path is unavailable') from exc
            os.close(fd)
            fd = next_fd
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Workspace file is unavailable')
        yield fd, Path(*parts).as_posix() if parts else '.'
    finally:
        os.close(fd)


def list_files(root: Path, path: str = '.') -> dict:
    with _open(root, path, True) as (fd, relative):
        with os.scandir(fd) as scan:
            names = sorted((entry.name for entry in scan if not entry.name.startswith('.')),
                           key=lambda name: name.casefold())
        entries = []
        for name in names:
            try:
                mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode):
                continue
            kind = 'directory' if stat.S_ISDIR(mode) else 'file' if stat.S_ISREG(mode) else None
            if kind:
                entries.append({'name': name, 'path': (Path(relative) / name).as_posix(), 'type': kind})
            if len(entries) >= MAX_ENTRIES:
                break
        return {'path': relative, 'entries': entries, 'truncated': len(entries) >= MAX_ENTRIES}


def read_file(root: Path, path: str) -> dict:
    with _open(root, path, False) as (fd, relative):
        size = os.fstat(fd).st_size
        if size > MAX_FILE_BYTES:
            raise ValueError('Workspace file exceeds the viewer limit')
        with os.fdopen(os.dup(fd), 'rb') as source:
            data = source.read()
        try:
            content = data.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('Binary workspace file is unavailable') from exc
        if '\x00' in content:
            raise ValueError('Binary workspace file is unavailable')
        digest = hashlib.sha256(data).hexdigest()
        return {'path': relative, 'content': content[:MAX_VIEW_CHARS],
                'truncated': len(content) > MAX_VIEW_CHARS, 'size_bytes': size,
                'sha256': digest}


def read_image(root: Path, path: str) -> tuple[bytes, str]:
    """Read a bounded raster image after checking its bytes, not just its suffix."""
    with _open(root, path, False) as (fd, _):
        size = os.fstat(fd).st_size
        if size <= 0 or size > MAX_IMAGE_BYTES:
            raise ValueError('Workspace image exceeds the viewer limit')
        with os.fdopen(os.dup(fd), 'rb') as source:
            data = source.read()
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        media_type = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        media_type = 'image/jpeg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        media_type = 'image/gif'
    elif len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        media_type = 'image/webp'
    else:
        raise ValueError('Workspace image format is unavailable')
    return data, media_type


def search_files(root: Path, query: str) -> dict:
    if not isinstance(query, str) or not 2 <= len(query) <= 100 or '\x00' in query:
        raise ValueError('Search text must be 2 to 100 characters')
    matches, scanned, directories = [], 0, 0
    needle = query.casefold()
    with _open(root, '.', True) as (root_fd, _):
        def walk(fd: int, prefix: Path):
            nonlocal scanned, directories
            directories += 1
            with os.scandir(fd) as scan:
                names = sorted((entry.name for entry in scan if not entry.name.startswith('.')))
            for name in names:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                path = prefix / name
                if stat.S_ISDIR(info.st_mode):
                    if name in SKIP_SEARCH_DIRS or directories >= 1500 or len(path.parts) > 20:
                        continue
                    try:
                        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        walk(child_fd, path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES:
                    scanned += 1
                    try:
                        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        with os.fdopen(file_fd, 'r', encoding='utf-8') as source:
                            for number, line in enumerate(source, 1):
                                if number > 5000:
                                    break
                                if needle in line.casefold():
                                    matches.append({'path': path.as_posix(), 'line': number,
                                                    'preview': line.strip()[:220]})
                                    if len(matches) >= 100:
                                        break
                    except (OSError, UnicodeError):
                        pass
                if len(matches) >= 100 or scanned >= 2000:
                    return
        walk(root_fd, Path('.'))
    return {'matches': matches, 'files_scanned': scanned,
            'truncated': len(matches) >= 100 or scanned >= 2000 or directories >= 1500}


def save_file(root: Path, path: str, content: str, expected_sha256: str) -> dict:
    """Replace an existing text file in its pinned directory after version check."""
    parts = _parts(path)
    if (not parts or not isinstance(content, str) or len(content.encode('utf-8')) > MAX_FILE_BYTES
            or '\x00' in content or not isinstance(expected_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
        raise ValueError('Invalid workspace save request')
    parent = '/'.join(parts[:-1]) or '.'
    with _open(root, parent, True) as (parent_fd, _):
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError('Workspace file is unavailable') from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError('Workspace file is unavailable or too large')
            actual = hashlib.sha256(os.pread(fd, before.st_size, 0)).hexdigest()
            if actual != expected_sha256:
                raise FileExistsError('Workspace file changed since it was opened; reload before saving')
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise FileExistsError('Workspace file changed since it was opened; reload before saving')
            data = content.encode('utf-8')
            temp_name = '.dashboard-edit-' + uuid.uuid4().hex
            temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              before.st_mode & 0o777, dir_fd=parent_fd)
            try:
                with os.fdopen(temp_fd, 'wb') as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise FileExistsError('Workspace file changed since it was opened; reload before saving')
                os.replace(temp_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            finally:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            return {'path': Path(*parts).as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
                    'size_bytes': len(data)}
        finally:
            os.close(fd)
