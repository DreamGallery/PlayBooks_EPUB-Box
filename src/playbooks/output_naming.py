"""Safe title-based EPUB naming shared by GUI and both CLIs."""
import json
from pathlib import Path
import re
import unicodedata


def title_filename(*titles):
    for title in titles:
        if not isinstance(title, str):
            continue
        name = ''.join('_' if unicodedata.category(c).startswith('C') else c for c in title)
        name = re.sub(r'[<>:"/\\|?*]', '_', name).strip().rstrip('. ')
        if not name or not name.strip('._ '):
            continue
        if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name):
            name = '_' + name
        # Leave room for extension and temporary suffix on common filesystems.
        while len(name.encode('utf-8')) > 180:
            name = name[:-1]
        return name.rstrip('. ') + '.epub'
    return '未命名书籍.epub'


def play_title(images):
    if images is None:
        return ''
    images = Path(images)
    paths = [images / 'images.json']
    if images.name == 'images':
        paths.append(images.parent / 'images.json')
    for path in paths:
        try:
            title = json.loads(path.read_text(encoding='utf-8')).get('title')
            if isinstance(title, str) and title.strip():
                return title
        except (OSError, ValueError, AttributeError):
            pass
    return ''


def output_path(source, title, images=None, explicit=None, directory=None):
    source = Path(source)
    return (Path(explicit).expanduser() if explicit else
            (Path(directory).expanduser() if directory else source.parent / 'hires') /
            title_filename(play_title(images), title, source.stem)).resolve()
