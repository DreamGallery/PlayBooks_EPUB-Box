"""Choose system fonts from book metadata, independently of the UI locale."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

DC = '{http://purl.org/dc/elements/1.1/}'
XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'
MAX_METADATA = 2 * 1024 * 1024


def source_language(path):
    if not path:
        return ''
    path = Path(path)
    try:
        if path.suffix.lower() == '.epub':
            with zipfile.ZipFile(path) as book:
                def read(name):
                    if book.getinfo(name).file_size > MAX_METADATA:
                        raise ValueError('Oversized metadata')
                    return ET.fromstring(book.read(name))
                container = read('META-INF/container.xml')
                opf = next(e.attrib['full-path'] for e in container.iter()
                           if e.tag.rsplit('}', 1)[-1] == 'rootfile')
                root = read(opf)
        elif path.suffix.lower() == '.acsm' and path.stat().st_size <= MAX_METADATA:
            root = ET.fromstring(path.read_bytes())
        else:
            return ''
        title = next(root.iter(DC + 'title'), None)
        if title is not None and title.get(XML_LANG):
            return title.get(XML_LANG).strip()
        return next((e.text.strip() for e in root.iter(DC + 'language') if e.text and e.text.strip()), '')
    except (OSError, ValueError, KeyError, StopIteration, ET.ParseError, zipfile.BadZipFile, RuntimeError):
        return ''


def title_language(title, language=''):
    tag = language.replace('_', '-').lower() if isinstance(language, str) else ''
    primary = tag.split('-')[0]
    primary = {'jpn': 'ja', 'zho': 'zh', 'chi': 'zh', 'kor': 'ko', 'eng': 'en'}.get(primary, primary)
    if primary == 'zh':
        if any(part in tag.split('-') for part in ('hant', 'tw', 'hk', 'mo')):
            return 'zh-Hant'
        if any(part in tag.split('-') for part in ('hans', 'cn', 'sg')):
            return 'zh-Hans'
    elif primary and primary not in ('und', 'mul'):
        return primary
    # Script hints are a fallback, never a substitute for explicit metadata.
    if not primary or primary in ('und', 'mul'):
        if re.search(r'[\u3041-\u3096\u30a1-\u30fa\uff66-\uff9d]', title):
            return 'ja'
        if re.search(r'[\uac00-\ud7af\u1100-\u11ff]', title):
            return 'ko'
    if any(c in title for c in '體圖書龍國學愛門風夢劍說傳記戰術異變戀繪讀'):
        return 'zh-Hant'
    if primary == 'zh' or re.search(r'[\u3400-\u9fff]', title):
        return 'zh-Hans'
    return 'und'


def title_style(title, language='', platform=None):
    import flet as ft

    language = title_language(title, language)
    families = {
        'ja': ('Hiragino Sans', 'Yu Gothic UI', 'Meiryo', 'Noto Sans CJK JP'),
        'zh-Hans': ('PingFang SC', 'Microsoft YaHei UI', 'Microsoft YaHei', 'Noto Sans CJK SC'),
        'zh-Hant': ('PingFang TC', 'Microsoft JhengHei UI', 'Microsoft JhengHei', 'Noto Sans CJK TC'),
        'ko': ('Apple SD Gothic Neo', 'Malgun Gothic', 'Malgun Gothic', 'Noto Sans CJK KR'),
    }.get(language, ('Helvetica Neue', 'Segoe UI', 'Arial', 'Noto Sans'))
    mac, windows, alternate, linux = families
    if platform == ft.PagePlatform.MACOS:
        ordered = [mac, linux, windows, alternate]
    elif platform == ft.PagePlatform.WINDOWS:
        ordered = [windows, alternate, linux, mac]
    else:
        ordered = [linux, windows, alternate, mac]
    return ft.TextStyle(font_family=ordered[0], font_family_fallback=ordered[1:],
                        letter_spacing=0, height=1.3)
