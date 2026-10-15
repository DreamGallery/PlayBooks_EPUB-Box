"""Conservative, recoverable cleanup of recognized image and working caches."""
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    size: int
    stamp: tuple


def inspect_entry(path):
    if path.is_symlink() or path.resolve() != path or not path.is_dir() or not re.fullmatch(r'[\w-]{12}', path.name):
        return None
    allowed = {'images', 'segments', 'images.json', 'manifest.json', '.DS_Store'}
    if any(p.name not in allowed for p in path.iterdir()):
        return None
    stamp = []
    size = 0
    for p in sorted(path.rglob('*')):
        if p.is_symlink() or p.resolve() != p:
            return None
        if not p.is_dir() and not p.is_file():
            return None
        if p.is_file():
            rel = p.relative_to(path)
            if len(rel.parts) > 1:
                extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg'} if rel.parts[0] == 'images' else {'.json', '.css', '.xhtml', '.html'}
                if p.name != '.DS_Store' and p.suffix.lower() not in extensions:
                    return None
            st = p.stat()
            size += st.st_size
            stamp.append((str(rel), st.st_size, st.st_mtime_ns, st.st_ino))
    try:
        metadata = json.loads((path / 'images.json').read_text(encoding='utf-8'))
        if metadata.get('volume_id') != path.name or not isinstance(metadata.get('images'), list):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return CacheEntry(path, size, tuple(stamp))


def scan_cache(directory, project, protected=()):
    if not str(directory).strip():
        raise ValueError('Specify an image cache folder first.')
    raw = Path(directory).expanduser()
    root = raw.resolve()
    project = Path(project).resolve()
    if raw.is_symlink() or root in (Path(root.anchor), Path.home().resolve(), project) or root in project.parents:
        raise ValueError('This directory is too broad to be a cleanup target.')
    protected = [Path(p).expanduser().resolve() for p in protected if p]
    if any(root == p or p in root.parents for p in protected):
        raise ValueError('The cache is inside a protected directory and cannot be cleared.')
    if not root.exists():
        return root, [], 0
    if not root.is_dir():
        raise ValueError('The image cache path is not a directory.')
    entries, skipped = [], 0
    for path in sorted(root.iterdir()):
        if path.name == '.DS_Store':
            continue
        if any(path == p or path in p.parents for p in protected):
            skipped += 1
            continue
        entry = inspect_entry(path)
        if entry:
            entries.append(entry)
        else:
            skipped += 1
    return root, entries, skipped


def trash_cache(entries, state):
    images, _, failed = trash_caches(entries, [], state)
    return images, failed


def _trash_entries(entries, inspect):
    from send2trash import send2trash
    moved, failed = 0, 0
    for entry in entries:
        try:
            if inspect(entry.path) != entry:
                failed += 1
                continue
            send2trash(str(entry.path))
            moved += 1
        except (OSError, ValueError):
            failed += 1
    return moved, failed


def inspect_work_entry(path):
    if path.is_symlink() or path.resolve() != path or not path.is_dir():
        return None
    if path.parent.name == 'books' and re.fullmatch(r'[a-f0-9]{64}', path.name):
        allowed = {'download.epub', 'readable.epub', 'download.part', 'readable.part', '.DS_Store'}
    elif path.parent.name == 'exports' and path.parent.parent.name == 'library' and re.fullmatch(r'[\w-]{12}', path.name):
        allowed = {'source.acsm', 'source.epub', '.DS_Store'}
    else:
        return None
    stamp, size = [], 0
    for file in sorted(path.iterdir()):
        if file.name not in allowed or file.is_symlink() or not file.is_file():
            return None
        info = file.stat()
        size += info.st_size
        stamp.append((file.name, info.st_size, info.st_mtime_ns, info.st_ino))
    return CacheEntry(path, size, tuple(stamp)) if stamp else None


def scan_work_cache(directory, project, protected=()):
    raw = Path(directory).expanduser()
    root, project = raw.resolve(), Path(project).resolve()
    if not str(directory).strip() or raw.is_symlink() or root in (Path(root.anchor), Path.home().resolve(), project) or root in project.parents:
        raise ValueError('Unsafe working cache directory.')
    protected = [Path(p).expanduser().resolve() for p in protected if p]
    entries, skipped = [], 0
    for folder in (root / 'books', root / 'library' / 'exports'):
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError('Linked working cache directories cannot be cleared safely.')
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if any(path == p or path in p.parents or p in path.parents for p in protected):
                skipped += 1
                continue
            entry = inspect_work_entry(path)
            if entry:
                entries.append(entry)
            else:
                skipped += 1
    return root, entries, skipped


def trash_work_cache(entries, state):
    _, work, failed = trash_caches([], entries, state)
    return work, failed


def trash_caches(images, work, state):
    from .adobe_backend import state_lock
    # Keep both groups under one lock so processing cannot start between them.
    with state_lock(Path(state).expanduser().resolve() / 'pipeline'):
        image_moved, image_failed = _trash_entries(images, inspect_entry)
        work_moved, work_failed = _trash_entries(work, inspect_work_entry)
    return image_moved, work_moved, image_failed + work_failed
