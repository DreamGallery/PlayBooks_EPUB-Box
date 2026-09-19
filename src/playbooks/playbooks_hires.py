"""Download original Google Play illustrations and upgrade EPUB images."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html as htmlmod
import io
import json
import math
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse
from .runtime import cancellable, hidden_process_options, safe_log_text

try:
    from PIL import Image, ImageFile
except ImportError:  # pragma: no cover
    sys.exit("Missing Pillow: pip install pillow")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

PLAY_HOST = "https://play.google.com"
UA_FALLBACK = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
# 不声明 webp/avif，避免服务端把原图转码
IMG_ACCEPT = "image/jpeg,image/png,image/gif,image/*;q=0.9,*/*;q=0.8"
FMT_EXT = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif"}
MEDIA_TO_FMT = {
    "image/jpeg": "JPEG", "image/jpg": "JPEG", "image/pjpeg": "JPEG",
    "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP", "image/bmp": "BMP",
}
FMT_TO_MEDIA = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp"}


def log(msg: str = "") -> None:
    print(safe_log_text(msg), flush=True)


def warn(msg: str) -> None:
    print(f"⚠ {safe_log_text(msg)}", file=sys.stderr, flush=True)


def safe_name(s: str, limit: int = 150) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")[:limit].rstrip('.') or "x"
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.I):
        name = '_' + name
    return name


# URL 工具
def with_params(url: str, **params) -> str:
    """修改 URL 查询参数；值为 None 表示删除该参数。"""
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    for k, v in params.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = [str(v)]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def reader_url(volume_id: str, authuser: Optional[str] = None) -> str:
    u = f"{PLAY_HOST}/books/reader?id={volume_id}&hl=en"
    return with_params(u, authuser=authuser) if authuser else u


def api_url(path_or_url: str, authuser: Optional[str] = None, **params) -> str:
    u = path_or_url if path_or_url.startswith("http") else PLAY_HOST + path_or_url
    u = with_params(u, hl="en", **params)
    if authuser:
        u = with_params(u, authuser=authuser)
    return u


# 解密（与网页阅读器一致：AES-128-CBC）
def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:  # pragma: no cover
        sys.exit("Missing cryptography: pip install cryptography")
    data = data[: len(data) - (len(data) % 16)]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


KEY_RE = re.compile(r"<body[\s\S]*?<[^>]+src\s*=\s*[\"']data:[^\"']*?base64,([^\"']+)[\"']")


def decipher_key(raw: str) -> bytes:
    """还原阅读器页面里被混淆的 AES 密钥（移植自网页阅读器逻辑）。"""
    groups = re.findall(r"\D+\d", raw)
    if not groups:
        raise ValueError("Invalid key data format")
    bits: List[int] = []
    for g in groups:
        idx = int(g[-1])
        bits.append(1 if idx < len(g) and g[idx] == g[-2] else 0)
    shift = 64 % len(bits)
    if shift:
        bits = bits[-shift:] + bits[:-shift]
    key = bytearray()
    for i in range(0, len(bits) - len(bits) % 8, 8):
        chunk = bits[i : i + 8][::-1]
        key.append(int("".join(str(b) for b in chunk), 2))
    if len(key) != 16:
        warn(f"Unexpected key length: {len(key)} bytes (expected 16)")
    return bytes(key)


def extract_aes_key(reader_html: str) -> bytes:
    m = KEY_RE.search(reader_html)
    if not m:
        raise ValueError("Key not found in reader page (not signed in or page structure changed)")
    raw = base64.b64decode(m.group(1)).decode("utf-8", "replace")
    return decipher_key(raw)


def decrypt_segment(b64_text: str, key: bytes) -> str:
    buf = base64.b64decode(b64_text)
    if len(buf) < 20:
        raise ValueError("Chapter data is too short")
    iv, n, data = buf[:16], int.from_bytes(buf[16:20], "little"), buf[20:]
    return aes_cbc_decrypt(key, iv, data)[:n].decode("utf-8", "replace")


def decrypt_blob(buf: bytes, key: bytes) -> bytes:
    """解密 enc_all=1 的二进制资源（IV + 密文，PKCS7 可选）。"""
    out = aes_cbc_decrypt(key, buf[:16], buf[16:])
    if out and 1 <= out[-1] <= 16 and out.endswith(bytes([out[-1]]) * out[-1]):
        out = out[: -out[-1]]
    return out


# 图片工具：探测、感知哈希、格式转换
def sniff_image(data: bytes) -> Optional[Tuple[str, int, int]]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            return ("JPEG" if fmt == "MPO" else fmt), im.size[0], im.size[1]
    except Exception:
        return None


def _flatten_gray(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return im.convert("L")


_COS: Dict[int, List[List[float]]] = {}


def _cos_table(n: int) -> List[List[float]]:
    if n not in _COS:
        _COS[n] = [[math.cos(math.pi * (2 * x + 1) * k / (2 * n)) for x in range(n)] for k in range(n)]
    return _COS[n]


def _gray_pixels(gray: Image.Image, size: Tuple[int, int]) -> List[int]:
    """缩放后的灰度像素（按行展开）。"""
    return list(gray.resize(size, Image.LANCZOS).tobytes())


def phash(gray: Image.Image, hash_size: int = 8, factor: int = 4) -> int:
    """DCT 感知哈希（64 bit），对缩放/重压缩鲁棒。"""
    n = hash_size * factor
    px = _gray_pixels(gray, (n, n))
    rows = [px[i * n : (i + 1) * n] for i in range(n)]
    c = _cos_table(n)
    row_dct = [[sum(r[x] * c[k][x] for x in range(n)) for k in range(hash_size)] for r in rows]
    low = [sum(row_dct[y][j] * c[k][y] for y in range(n)) for k in range(hash_size) for j in range(hash_size)]
    med = sorted(low)[len(low) // 2]
    bits = 0
    for v in low:
        bits = (bits << 1) | (1 if v > med else 0)
    return bits


def dhash(gray: Image.Image, hash_size: int = 16) -> int:
    """差分哈希（256 bit），补充 pHash 的细节区分能力。"""
    w = hash_size + 1
    px = _gray_pixels(gray, (w, hash_size))
    bits = 0
    for r in range(hash_size):
        row = px[r * w : (r + 1) * w]
        for x in range(hash_size):
            bits = (bits << 1) | (1 if row[x + 1] > row[x] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def hash_cost(a: "ImgInfo", b: "ImgInfo") -> float:
    """0 = 完全一致，0.5 ≈ 随机无关图片。"""
    return 0.5 * hamming(a.phash, b.phash) / 64 + 0.5 * hamming(a.dhash, b.dhash) / 256


def convert_image(data: bytes, target_fmt: str) -> bytes:
    """把图片转换为 EPUB 原来的格式（尽量无损/高质量），保留 ICC 配置。"""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        icc = im.info.get("icc_profile")
        out = io.BytesIO()
        if target_fmt == "JPEG":
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(bg, im).convert("RGB")
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            kw = dict(quality=95, subsampling=0, optimize=True)
            if icc:
                kw["icc_profile"] = icc
            im.save(out, "JPEG", **kw)
        elif target_fmt == "PNG":
            if im.mode == "CMYK":
                im = im.convert("RGB")
            kw = {"icc_profile": icc} if icc else {}
            im.save(out, "PNG", **kw)
        elif target_fmt == "GIF":
            im.convert("RGB").convert("P", palette=Image.ADAPTIVE).save(out, "GIF")
        elif target_fmt == "WEBP":
            im.save(out, "WEBP", quality=95, method=6)
        elif target_fmt == "BMP":
            im.convert("RGB").save(out, "BMP")
        else:
            raise ValueError(f"Unsupported target format {target_fmt}")
        return out.getvalue()


# 数据结构
@dataclass
class ImgInfo:
    id: str                 # EPUB: zip 内路径；Play 图书: 资源 ID (pg)
    path: str               # zip 内路径 或 本地文件路径
    width: int
    height: int
    fmt: str                # JPEG / PNG / ...
    order: float = 1.0      # 归一化阅读顺序 0..1（1.0 = 未在正文中引用）
    seq: int = -1           # 原始阅读顺序序号（-1 = 未引用）
    phash: int = 0
    dhash: int = 0
    media_type: str = ""
    label: str = ""         # 附加信息（alt / 章节）
    nbytes: int = 0
    hashable: bool = True

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def dims(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass
class Match:
    epub: ImgInfo
    pb: ImgInfo
    cost: float
    via: str


@dataclass
class ImgRef:
    url: str
    alt: str = ""
    segment: str = ""
    seg_index: int = 0
    kind: str = "resource"  # XHTML/SVG/CSS 原图


def compute_hashes(info: ImgInfo, data: bytes, min_side: int = 24) -> None:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if min(im.size) < min_side:
                info.hashable = False
                return
            g = _flatten_gray(im)
            info.phash = phash(g)
            info.dhash = dhash(g)
    except Exception as e:
        warn(f"Cannot parse image {info.path}: {e}")
        info.hashable = False


# 浏览器（Chrome DevTools 协议）——仅用于登录并取得 cookies / 兜底抓取
class CDPPage:
    def __init__(self, ws_url: str, timeout: float = 180):
        try:
            import websocket  # websocket-client
        except ImportError:  # pragma: no cover
            sys.exit("Missing websocket-client: pip install websocket-client")
        # Chrome 111+ 会拒绝带 Origin 头的 WebSocket 连接，这里不发送 Origin
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=timeout)
        self._next = 0

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        self._next += 1
        mid = self._next
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expression: str):
        r = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        exc = r.get("exceptionDetails")
        if exc:
            desc = (exc.get("exception") or {}).get("description") or exc.get("text") or str(exc)
            raise RuntimeError(f"Page script execution failed: {desc[:300]}")
        return (r.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


FETCH_JS = r"""
(async (url) => {
  const r = await fetch(url, {credentials: 'include', redirect: 'follow'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return JSON.stringify({status: r.status, ct: r.headers.get('content-type') || '', url: r.url, b64: btoa(s)});
})(%s)
"""


def find_browser(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        candidate = shutil.which(explicit) or os.path.expanduser(explicit)
        if not Path(candidate).is_file():
            raise ValueError('Browser executable not found: ' + explicit)
        return candidate
    cands: List[str] = []
    sysname = platform.system()
    if sysname == "Darwin":
        for app in ["Google Chrome", "Google Chrome Canary", "Chromium", "Brave Browser", "Microsoft Edge"]:
            cands.append(f"/Applications/{app}.app/Contents/MacOS/{app}")
            cands.append(os.path.expanduser(f"~/Applications/{app}.app/Contents/MacOS/{app}"))
    elif sysname == "Windows":
        for base in [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")]:
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
                cands.append(os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
        cands.extend(filter(None, (shutil.which(name) for name in ('chrome.exe', 'msedge.exe', 'brave.exe'))))
    else:
        for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"]:
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _http_json(url: str, method: str = "GET", timeout: float = 5):
    import requests
    r = requests.request(method, url, timeout=timeout)
    r.raise_for_status()
    return r.json()


class BrowserAuth:
    """启动/连接 Chrome，等待用户登录，导出 cookies；必要时在页面内 fetch 作为兜底。"""

    def __init__(self, volume_id: str, profile_dir: Path, cdp_url: Optional[str] = None,
                 browser_path: Optional[str] = None, authuser: Optional[str] = None,
                 headless: bool = True):
        self.volume_id = volume_id
        self.profile_dir = profile_dir
        self.cdp_url = cdp_url
        self.browser_path = browser_path
        self.headless = headless
        self.reader_url = reader_url(volume_id, authuser)
        self.proc: Optional[subprocess.Popen] = None
        self.base = ""
        self.page: Optional[CDPPage] = None
        self.ua = UA_FALLBACK

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> None:
        try:
            self._open()
        except BaseException:
            # 即使初始化失败或 Ctrl+C，也不能遗留本次启动的进程。
            self.close(True)
            raise

    def _open(self) -> None:
        self.base = self.cdp_url.rstrip("/") if self.cdp_url else self._reuse_or_launch()
        target = self._pick_target()
        self.page = CDPPage(target["webSocketDebuggerUrl"])
        for m in ("Network.enable", "Page.enable"):
            try:
                self.page.call(m)
            except Exception:
                pass
        try:
            ver = self.page.call("Browser.getVersion")
            self.ua = (ver.get("userAgent") or UA_FALLBACK).replace("HeadlessChrome", "Chrome")
        except Exception:
            pass

    def close(self, quit_browser: bool = True) -> None:
        # proc 只代表本次启动的 Chrome；复用/--cdp 连接没有所有权。
        owned = quit_browser and self.proc is not None and self.proc.poll() is None
        if owned and self.page:
            try:
                self.page.ws.settimeout(3)
                self.page.call("Browser.close")
            except Exception:
                pass  # 正常退出可能先断开 WebSocket，来不及回复。
        if self.page:
            self.page.close()
            self.page = None
        if owned:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            log("Closed Chrome started by this task")

    def _alive(self, base: str) -> bool:
        try:
            _http_json(base + "/json/version", timeout=2)
            return True
        except Exception:
            return False

    def _reuse_or_launch(self) -> str:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        dtap = self.profile_dir / "DevToolsActivePort"
        if dtap.exists():
            try:
                port = int(dtap.read_text().splitlines()[0].strip())
                base = f"http://127.0.0.1:{port}"
                if self._alive(base):
                    log(f"Reusing running Chrome ({base})")
                    return base
            except Exception:
                pass
        exe = find_browser(self.browser_path)
        if not exe:
            sys.exit("Chrome/Chromium not found; specify the executable with --browser")
        # port=0 让 Chrome 写入 DevToolsActivePort，后续进程才能可靠复用。
        # 上方已经确认旧端口不可用，删除过期发现文件，不触碰用户数据。
        if dtap.exists():
            dtap.unlink()
        login = "https://accounts.google.com/ServiceLogin?continue=" + quote(self.reader_url, safe="")
        args = [
            exe,
            f"--user-data-dir={self.profile_dir.resolve()}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
            login,
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        log(f"{'Headless' if self.headless else 'Visible'} Chrome: {exe} (profile: {self.profile_dir})")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     **hidden_process_options())
        for _ in range(80):
            if dtap.exists() and self.proc.poll() is None:
                try:
                    port = int(dtap.read_text().splitlines()[0])
                    base = f"http://127.0.0.1:{port}"
                    if self._alive(base):
                        return base
                except (ValueError, IndexError, OSError):
                    pass
            if self.proc.poll() is not None:
                # 可能已有同配置目录的 Chrome 在运行，本进程把 URL 交给它后退出
                if dtap.exists():
                    try:
                        port2 = int(dtap.read_text().splitlines()[0].strip())
                        base2 = f"http://127.0.0.1:{port2}"
                        if self._alive(base2):
                            self.proc = None  # 连接到已有进程，不拥有其生命周期。
                            return base2
                    except Exception:
                        pass
                raise RuntimeError(
                    "Chrome exited immediately. Another Chrome instance may be using the same profile without a debugging port. "
                    f"Close it first (profile: {self.profile_dir}), or use --profile to select another folder.")
            time.sleep(0.5)
        raise RuntimeError("Cannot connect to the debugging port after starting Chrome")

    def _pick_target(self) -> dict:
        targets = [t for t in _http_json(self.base + "/json/list") if t.get("type") == "page"]

        def score(t: dict) -> int:
            u = t.get("url", "")
            if self.volume_id in u:
                return 0
            if "accounts.google.com" in u:
                return 1
            if "google.com" in u:
                return 2
            return 3

        targets.sort(key=score)
        if targets and score(targets[0]) <= 2 and targets[0].get("webSocketDebuggerUrl"):
            return targets[0]
        return _http_json(self.base + "/json/new?" + self.reader_url, method="PUT")

    # -- 功能 ---------------------------------------------------------------
    def cookies(self) -> List[dict]:
        assert self.page
        r = self.page.call("Network.getCookies", {"urls": [
            "https://play.google.com/", "https://books.google.com/",
            "https://www.google.com/", "https://accounts.google.com/",
        ]})
        return r.get("cookies", [])

    def current_url(self) -> str:
        try:
            return self.page.evaluate("location.href") or ""  # type: ignore[union-attr]
        except Exception:
            return ""

    def ensure_reader_page(self) -> None:
        if "play.google.com/books/reader" not in self.current_url():
            self.page.call("Page.navigate", {"url": self.reader_url})  # type: ignore[union-attr]
            time.sleep(4)

    def fetch(self, url: str) -> Tuple[int, str, bytes]:
        """在阅读器页面上下文内 fetch（与网页阅读器完全相同的身份/头部）。"""
        self.ensure_reader_page()
        v = self.page.evaluate(FETCH_JS % json.dumps(url))  # type: ignore[union-attr]
        d = json.loads(v)
        return int(d["status"]), d.get("ct", ""), base64.b64decode(d["b64"])


def load_netscape_cookies(path: Path) -> List[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cpath, secure, _exp, name, value = parts[:7]
        out.append({"name": name, "value": value, "domain": domain, "path": cpath,
                    "secure": secure.upper() == "TRUE", "httpOnly": http_only})
    return out


# Play 图书客户端
class PlayBooksClient:
    def __init__(self, volume_id: str, authuser: Optional[str] = None, browser: Optional[BrowserAuth] = None,
                 cookies: Optional[List[dict]] = None, pace: float = 0.25):
        try:
            import requests
        except ImportError:  # pragma: no cover
            sys.exit("Missing requests: pip install requests")
        self.requests = requests
        self.volume_id = volume_id
        self.authuser = authuser
        self.browser = browser
        self.page_fallback = False   # 登录确认后才允许在页面内兜底抓取（避免打断用户登录）
        self.pace = pace
        self._last = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": browser.ua if browser else UA_FALLBACK,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": reader_url(volume_id, authuser),
        })
        if cookies:
            self.set_cookies(cookies)

    def set_cookies(self, cookies: List[dict]) -> None:
        self.s.cookies.clear()
        for c in cookies:
            try:
                ck = self.requests.cookies.create_cookie(
                    name=c["name"], value=c["value"], domain=c.get("domain") or "play.google.com",
                    path=c.get("path") or "/", secure=bool(c.get("secure")),
                    rest={"HttpOnly": bool(c.get("httpOnly"))},
                )
                self.s.cookies.set_cookie(ck)
            except Exception:
                pass

    def refresh_cookies(self) -> None:
        if self.browser:
            self.set_cookies(self.browser.cookies())

    def _pace(self) -> None:
        dt = time.time() - self._last
        if dt < self.pace:
            time.sleep(self.pace - dt)
        self._last = time.time()

    def get(self, url: str, accept: str = "*/*", retries: int = 3) -> Tuple[int, str, bytes]:
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            self._pace()
            try:
                r = self.s.get(url, headers={"Accept": accept}, timeout=90, allow_redirects=True)
                status = 401 if "accounts.google.com" in r.url else r.status_code
                if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if status in (401, 403) and self.browser and self.page_fallback:
                    try:
                        return self.browser.fetch(url)
                    except Exception as e:  # 兜底失败则返回原状态
                        warn(f"In-page fetch failed: {e}")
                return status, r.headers.get("content-type", ""), r.content
            except self.requests.RequestException as e:
                last_exc = e
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"Request failed {url}: {last_exc}")

    def get_json(self, url: str) -> dict:
        st, ct, data = self.get(url, "application/json,text/plain,*/*")
        if st != 200:
            raise RuntimeError(f"HTTP {st}")
        text = data.decode("utf-8", "replace").lstrip()
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[1] if "\n" in text else text[4:]
        return json.loads(text)


def wait_for_access(client: PlayBooksClient, volume_id: str, authuser: Optional[str], allow_partial: bool,
                    timeout: float, interactive: bool) -> dict:
    murl = api_url(f"/books/volumes/{volume_id}/manifest", authuser, source="ge-web-app")
    t0 = time.time()
    hinted = False
    last_err = ""
    while True:
        try:
            client.refresh_cookies()
            m = client.get_json(murl)
            preview = (m.get("metadata") or {}).get("preview")
            if preview == "full":
                return m
            if allow_partial and m.get("segment"):
                warn(f"Continuing in preview mode (preview={preview}); only preview chapters and images are available")
                return m
            last_err = f"This account has preview-only access (preview={preview})"
        except Exception as e:
            last_err = str(e)
        if not interactive:
            hint = ("Headless mode cannot sign in interactively. Use --show-browser; if already signed in, verify that the account owns this book."
                    if client.browser else "Verify that the cookies belong to the account owning this book (or use --allow-partial).")
            raise RuntimeError(f"Cannot access full content: {last_err}. {hint}")
        if not hinted:
            log("┃ Sign in to the Google account owning this book in Chrome; the session will be reused.")
            log("┃ Processing resumes automatically after sign-in.")
            hinted = True
        if time.time() - t0 > timeout:
            raise RuntimeError(f"Sign-in timed out: {last_err}")
        time.sleep(3)


# 章节解析：提取插图 URL
TAG_RE = re.compile(r"<(img|image|source|video|object|embed)\b([^>]*)>", re.I)
ATTR_RE = re.compile(r"([A-Za-z_:][-\w:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")
CSS_URL_RE = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.I)
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp|svg)(\?|$)", re.I)


def _normalize_url(u: str) -> Optional[str]:
    u = htmlmod.unescape(u.strip())
    if not u or u.startswith("data:") or u.startswith("#"):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = PLAY_HOST + u
    elif not u.lower().startswith("http"):
        return None
    return u


def _looks_like_image_url(u: str, from_img_tag: bool) -> bool:
    if from_img_tag:
        return True
    parts = urlparse(u)
    q = parse_qs(parts.query)
    return q.get("img", [""])[0] == "1" or "/publisher/content" in parts.path or bool(IMG_EXT_RE.search(parts.path))


def extract_image_refs(html_text: str, css_texts: Iterable[str]) -> List[ImgRef]:
    refs: List[ImgRef] = []
    for m in TAG_RE.finditer(html_text):
        attrs = {k.lower(): (v1 or v2 or v3 or "") for k, v1, v2, v3 in ATTR_RE.findall(m.group(2))}
        url = attrs.get("src") or attrs.get("xlink:href") or attrs.get("href") or attrs.get("data") or attrs.get("poster")
        if not url:
            continue
        u = _normalize_url(url)
        if u and _looks_like_image_url(u, True):
            refs.append(ImgRef(u, htmlmod.unescape(attrs.get("alt", ""))))
    for css in [html_text, *css_texts]:
        for raw in CSS_URL_RE.findall(css or ""):
            u = _normalize_url(raw)
            if u and _looks_like_image_url(u, False):
                refs.append(ImgRef(u, ""))
    return refs


def resource_id(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    pg = q.get("pg", [""])[0]
    return pg or "u_" + hashlib.sha1(url.encode()).hexdigest()[:16]


def hires_variants(url: str) -> Iterable[Tuple[str, bool]]:
    """按优先级生成候选 URL：(url, 是否需要解密)。

    实测：去掉 w/h 即返回出版社上传的原图；若给出比原图更大的 w/h，服务器会把图片
    *放大*（最长边上限 2500）而不是返回更多细节，所以不要用大 w/h 去“榨”分辩率。
    zoom 必须保持原值（改成 1/3 会返回整页缩略图）。
    """
    yield with_params(url, w=None, h=None, enc_all=0), False     # 原始尺寸，未加密
    yield with_params(url, w=None, h=None, enc_all=1), True      # 原始尺寸，加密
    yield with_params(url, enc_all=0), False                     # 阅读器显示尺寸，未加密
    q = parse_qs(urlparse(url).query)
    yield url, q.get("enc_all", ["0"])[0] == "1"                  # 原样


def _fetch_image_variant(client: PlayBooksClient, key: Optional[bytes], u: str, enc: bool) -> Tuple[Optional[Tuple[bytes, str, int, int]], str]:
    st, ct, data = client.get(u, IMG_ACCEPT)
    if st != 200 or not data:
        return None, f"HTTP {st}"
    if enc:
        if not key:
            return None, "No decryption key"
        try:
            data = decrypt_blob(data, key)
        except Exception as e:
            return None, f"Decryption failed: {e}"
    info = sniff_image(data)
    if not info:
        return None, f"Response is not an image ({ct or 'unknown type'})"
    return (data, info[0], info[1], info[2]), ""


def download_image(client: PlayBooksClient, key: Optional[bytes], url: str) -> Tuple[bytes, str, int, int, str]:
    """下载原始尺寸插图；返回 (数据, 格式, 宽, 高, 实际使用的 URL)。"""
    q = parse_qs(urlparse(url).query)
    try:
        declared = int(q.get("w", ["0"])[0]) * int(q.get("h", ["0"])[0])
    except ValueError:
        declared = 0
    last = ""
    for u, enc in hires_variants(url):
        got, err = _fetch_image_variant(client, key, u, enc)
        if not got:
            last = err
            continue
        data, fmt, w, h = got
        # 极少数情况下“去掉 w/h”得到的图比阅读器声明的尺寸还小：再取阅读器尺寸那份，保留更大的
        if declared and w * h < declared * 0.95 and "w" not in parse_qs(urlparse(u).query):
            alt, _ = _fetch_image_variant(client, key, with_params(url, enc_all=0), False)
            if alt and alt[2] * alt[3] > w * h:
                return alt[0], alt[1], alt[2], alt[3], with_params(url, enc_all=0)
        return data, fmt, w, h, u
    raise RuntimeError(last or "Download failed")


def fetch_segment_json(client: PlayBooksClient, link: str, key: Optional[bytes], authuser: Optional[str]) -> dict:
    # 与网页阅读器一致，保留 ops:switch 内的 SVG 原图。
    url = api_url(link, authuser, enc_all=1, av=2, cds=2, ef=4,
                  flvers=1, svg=2, vert=1, multi_toc=1, inline=0)
    st, ct, data = client.get(url)
    if st != 200:
        raise RuntimeError(f"HTTP {st}")
    text = data.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if not key:
        raise RuntimeError("Chapter is encrypted, but no decryption key was obtained")
    return json.loads(decrypt_segment(text, key))


def fetch_css_text(client: PlayBooksClient, url: str, key: Optional[bytes], authuser: Optional[str], cache_dir: Path) -> str:
    cache = cache_dir / ("css_" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".css")
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    text = ""
    try:
        st, ct, data = client.get(api_url(url, authuser, enc_all=1))
        if st == 200:
            raw = data.decode("utf-8", "replace").strip()
            if raw.startswith("{"):
                try:
                    obj = json.loads(raw)
                    text = obj.get("style") or obj.get("content") or ""
                except json.JSONDecodeError:
                    text = raw
            elif key:
                try:
                    dec = decrypt_segment(raw, key)
                    try:
                        obj = json.loads(dec)
                        text = obj.get("style") or obj.get("content") or dec
                    except json.JSONDecodeError:
                        text = dec
                except Exception:
                    text = raw
            else:
                text = raw
    except Exception as e:
        warn(f"CSS download failed {url}: {e}")
    cache.write_text(text, encoding="utf-8")
    return text


def fetch_frontcover(client: PlayBooksClient, volume_id: str) -> Optional[Tuple[bytes, str, int, int, str]]:
    """商店封面（尽力而为，可能不存在或分辩率低于 EPUB 内封面）。"""
    for u in (
        f"https://books.google.com/books/publisher/content/images/frontcover/{volume_id}?fife=w10000-h10000",
        f"https://books.google.com/books/content?id={volume_id}&printsec=frontcover&img=1&zoom=0",
    ):
        try:
            st, ct, data = client.get(u, IMG_ACCEPT)
            info = sniff_image(data) if st == 200 else None
            if info and min(info[1], info[2]) >= 200:
                return data, info[0], info[1], info[2], u
        except Exception:
            continue
    return None


# fetch：抓取高清插图
def do_fetch(volume_id: str, workdir: Path, mode: str, profile_dir: Path, cdp_url: Optional[str],
             browser_path: Optional[str], cookies_file: Optional[Path], authuser: Optional[str],
             allow_partial: bool, login_timeout: float, pace: float, limit: int, quit_browser: bool,
             with_cover: bool = True, headless: bool = True) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{12}', volume_id):
        raise ValueError('Invalid Google volume ID; expected 12 letters, digits, underscores or hyphens')
    if not math.isfinite(pace) or pace < 0 or not math.isfinite(login_timeout) or login_timeout <= 0 or limit < 0:
        raise ValueError('Invalid download limits or timing parameters')
    workdir.mkdir(parents=True, exist_ok=True)
    seg_dir, img_dir = workdir / "segments", workdir / "images"
    seg_dir.mkdir(exist_ok=True)
    img_dir.mkdir(exist_ok=True)

    browser: Optional[BrowserAuth] = None
    cookies: Optional[List[dict]] = None
    if mode == "browser":
        browser = BrowserAuth(volume_id, profile_dir, cdp_url, browser_path, authuser, headless)
        browser.open()
    elif mode == "cookies":
        assert cookies_file
        if not cookies_file.is_file():
            raise RuntimeError(f"Cookies file does not exist: {cookies_file}")
        cookies = load_netscape_cookies(cookies_file)
        if not cookies:
            raise RuntimeError(f"{cookies_file} contains no cookies (Netscape/cookies.txt format required)")
        log(f"Loaded from {cookies_file}: {len(cookies)} cookies")
    else:
        allow_partial = True
        log("Anonymous mode: public previews only")

    try:
        client = PlayBooksClient(volume_id, authuser, browser, cookies, pace)
        manifest = wait_for_access(client, volume_id, authuser, allow_partial, login_timeout,
                                   interactive=browser is not None and (not headless or bool(cdp_url)))
        client.page_fallback = True
        (workdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        meta = manifest.get("metadata") or {}
        segments = sorted(manifest.get("segment") or [], key=lambda s: s.get("order", 0))
        log(f"Title: {meta.get('title', '?')}  |  Author: {meta.get('authors', '?')}  |  Access: {meta.get('preview')}  |  Chapters: {len(segments)}")
        if not segments:
            raise RuntimeError("Manifest contains no reflowable chapters (possibly a fixed-layout or scanned book)")

        # 解密密钥
        key: Optional[bytes] = None
        st, ct, data = client.get(reader_url(volume_id, authuser), "text/html,*/*")
        try:
            key = extract_aes_key(data.decode("utf-8", "replace"))
            log("Obtained chapter decryption key")
        except Exception as e:
            warn(f"Could not extract decryption key: {e} (will try reading unencrypted content)")

        # 章节 → 插图引用
        refs: Dict[str, ImgRef] = {}
        seg_refs: Dict[str, List[str]] = {}
        missing = 0
        for idx, seg in enumerate(segments):
            label = seg.get("label") or f"seg{idx}"
            link = seg.get("link")
            if not link:
                missing += 1
                continue
            # 旧请求遗漏 SVG，使用新版本缓存，自动重新抓取章节。
            cache = seg_dir / "svg-original-v1" / (safe_name(label) + ".json")
            cache.parent.mkdir(exist_ok=True)
            try:
                if cache.exists():
                    obj = json.loads(cache.read_text(encoding="utf-8"))
                else:
                    obj = fetch_segment_json(client, link, key, authuser)
                    cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                warn(f"Chapter {label} download/decryption failed: {e}")
                continue
            css_texts = [obj.get("style") or ""]
            for res in list(seg.get("resource") or []) + list(obj.get("resource") or []):
                if (res.get("mime_type") or "").startswith("text/css") and res.get("url"):
                    css_texts.append(fetch_css_text(client, res["url"], key, authuser, seg_dir))
            found = extract_image_refs(obj.get("content") or "", css_texts)
            # 章节 JSON 自带的资源清单（比正则扫描 HTML 更可靠），作为补充
            extra = list(obj.get("resource_url") or [])
            extra += [r.get("url") for r in (obj.get("resource") or []) if (r.get("mime_type") or "").startswith("image")]
            seen = {r.url for r in found}
            for u in extra:
                nu = _normalize_url(u or "")
                if nu and nu not in seen and _looks_like_image_url(nu, False):
                    found.append(ImgRef(nu, ""))
                    seen.add(nu)
            seg_refs[label] = []
            for r in found:
                rid = resource_id(r.url)
                seg_refs[label].append(rid)
                if rid not in refs:
                    r.segment, r.seg_index = label, idx
                    refs[rid] = r
            log(f"  [{idx + 1}/{len(segments)}] {label:<28} images {len(found):>3}  (unique so far: {len(refs)})")
        if missing:
            warn(f"{missing} chapters have no download URL (expected for previews; otherwise check sign-in)")
        log(f"Found {len(refs)} unique original images (no rendered screenshots)")

        # 下载
        results: List[dict] = []
        items = list(refs.items())
        if limit:
            items = items[:limit]
        for n, (rid, ref) in enumerate(items, 1):
            stem = safe_name(rid)
            existing = next((p for p in img_dir.glob(stem + ".*") if p.is_file()), None)
            if existing:
                info = sniff_image(existing.read_bytes())
                if info:
                    results.append({"id": rid, "file": existing.name, "format": info[0], "width": info[1], "height": info[2],
                                    "bytes": existing.stat().st_size, "segment": ref.segment, "seg_index": ref.seg_index,
                                    "alt": ref.alt, "src_url": ref.url, "fetched_url": "", "cached": True,
                                    "kind": ref.kind})
                    log(f"  [{n}/{len(items)}] {rid:<40} cached {info[1]}x{info[2]}")
                    continue
            try:
                data, fmt, w, h, used = download_image(client, key, ref.url)
            except Exception as e:
                warn(f"  [{n}/{len(items)}] {rid} download failed: {e}")
                continue
            fn = f"{stem}.{FMT_EXT.get(fmt, fmt.lower() or 'bin')}"
            (img_dir / fn).write_bytes(data)
            results.append({"id": rid, "file": fn, "format": fmt, "width": w, "height": h, "bytes": len(data),
                            "segment": ref.segment, "seg_index": ref.seg_index, "alt": ref.alt,
                            "src_url": ref.url, "fetched_url": used, "cached": False,
                            "kind": "resource"})
            log(f"  [{n}/{len(items)}] {rid:<40} {w}x{h} {fmt} {len(data) / 1024:.0f}KB")

        if with_cover:
            cover = fetch_frontcover(client, volume_id)
            if cover:
                data, fmt, w, h, used = cover
                fn = f"__frontcover__.{FMT_EXT.get(fmt, 'bin')}"
                (img_dir / fn).write_bytes(data)
                results.append({"id": "__frontcover__", "file": fn, "format": fmt, "width": w, "height": h, "bytes": len(data),
                                "segment": "", "seg_index": -1, "alt": "cover", "src_url": used, "fetched_url": used, "cached": False})
                log(f"  Store cover {w}x{h} {fmt}")

        out = {
            "volume_id": volume_id,
            "title": meta.get("title"),
            "authors": meta.get("authors"),
            "preview": meta.get("preview"),
            "segments": [{"label": s.get("label"), "order": s.get("order"), "title": s.get("title"),
                          "images": seg_refs.get(s.get("label") or "", [])} for s in segments],
            "images": sorted(results, key=lambda r: (r["seg_index"], r["id"])),
        }
        (workdir / "images.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Done: {len(results)} images saved to {img_dir}")
        return workdir
    finally:
        if browser:
            browser.close(quit_browser)


# EPUB 解析
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base_dir: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0].split("?", 1)[0])
    p = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else posixpath.normpath(href)
    return p.lstrip("./") if p.startswith("./") else p


XHTML_REF_RE = re.compile(r"<(?:img|image|source|video)\b[^>]*?\b(?:src|xlink:href|href|poster)\s*=\s*[\"']([^\"'#]+)", re.I)


@dataclass
class EpubInfo:
    path: Path
    opf_path: str
    opf_xml: str
    images: List[ImgInfo]
    cover: Optional[str] = None
    volume_id: Optional[str] = None
    title: str = ""


def load_epub(epub_path: Path, hashes: bool = True) -> EpubInfo:
    with zipfile.ZipFile(epub_path) as z:
        names = set(z.namelist())
        has_encryption = "META-INF/encryption.xml" in names
        lower_map = {n.lower(): n for n in names}

        def find_entry(p: str) -> Optional[str]:
            if p in names:
                return p
            return lower_map.get(p.lower())

        opf_path = None
        if "META-INF/container.xml" in names:
            try:
                root = ET.fromstring(z.read("META-INF/container.xml"))
                for el in root.iter():
                    if _local(el.tag) == "rootfile" and el.attrib.get("full-path"):
                        opf_path = el.attrib["full-path"]
                        break
            except ET.ParseError:
                pass
        if not opf_path or opf_path not in names:
            opf_path = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
        if not opf_path:
            raise RuntimeError("OPF not found; not a valid EPUB")
        opf_xml = z.read(opf_path).decode("utf-8", "replace")
        root = ET.fromstring(opf_xml)
        opf_dir = posixpath.dirname(opf_path)

        items: Dict[str, dict] = {}
        for el in root.iter():
            if _local(el.tag) == "item":
                items[el.attrib.get("id", "")] = dict(el.attrib)
        spine = [el.attrib.get("idref", "") for el in root.iter() if _local(el.tag) == "itemref"]
        title = next((el.text or "" for el in root.iter() if _local(el.tag) == "title"), "")

        cover_id = None
        for el in root.iter():
            if _local(el.tag) == "meta" and el.attrib.get("name") == "cover":
                cover_id = el.attrib.get("content")
        cover_path = None

        images: Dict[str, ImgInfo] = {}
        for iid, it in items.items():
            mt = (it.get("media-type") or "").lower()
            href = it.get("href") or ""
            if not mt.startswith("image/") or not href:
                continue
            zp = find_entry(_resolve(opf_dir, href))
            if not zp:
                warn(f"Manifest item {href} is missing from ZIP; skipped")
                continue
            is_cover = iid == cover_id or "cover-image" in (it.get("properties") or "")
            if is_cover:
                cover_path = zp
            data = z.read(zp)
            sn = sniff_image(data)
            info = ImgInfo(id=zp, path=zp, width=sn[1] if sn else 0, height=sn[2] if sn else 0,
                           fmt=sn[0] if sn else "", media_type=mt, nbytes=len(data),
                           label="cover" if is_cover else "")
            if mt == "image/svg+xml" or not sn:
                info.hashable = False
            elif hashes:
                compute_hashes(info, data)
            images[zp] = info
        unreadable = [i for i in images.values() if not i.fmt and i.media_type != "image/svg+xml"]
        if unreadable and has_encryption:
            warn(f"{len(unreadable)} images cannot be parsed and EPUB contains META-INF/encryption.xml; the file may still be encrypted")

        # 阅读顺序（按 spine 顺序扫描正文中的图片引用）
        seq = 0
        for idref in spine:
            it = items.get(idref)
            if not it or not (it.get("media-type") or "").startswith(("application/xhtml", "text/html")):
                continue
            zp = find_entry(_resolve(opf_dir, it.get("href") or ""))
            if not zp:
                continue
            text = z.read(zp).decode("utf-8", "replace")
            base = posixpath.dirname(zp)
            refs = XHTML_REF_RE.findall(text) + CSS_URL_RE.findall(text)
            for ref in refs:
                target = find_entry(_resolve(base, htmlmod.unescape(ref)))
                if target in images and images[target].seq < 0:
                    images[target].seq = seq
                    seq += 1
        n = max(seq, 1)
        for im in images.values():
            im.order = im.seq / n if im.seq >= 0 else 1.0

        vol = detect_volume_id(opf_xml)
        return EpubInfo(epub_path, opf_path, opf_xml, list(images.values()), cover_path, vol, title)


def detect_volume_id(opf_xml: str) -> Optional[str]:
    m = re.search(r"google\.[a-z.]+/[^\"'<\s]*?(?:books|reader|details)\?[^\"'<\s]*?\bid=([A-Za-z0-9_-]{12})\b", opf_xml)
    if m:
        return m.group(1)
    m = re.search(r"<dc:identifier[^>]*>\s*(?:urn:)?(?:gbs|google|playbooks)?:?([A-Za-z0-9_-]{12})\s*<", opf_xml, re.I)
    return m.group(1) if m else None


def load_pb_images(images_dir: Path) -> Tuple[List[ImgInfo], dict]:
    """读取 fetch 的输出目录（工作目录或其 images 子目录）。"""
    meta: dict = {}
    if (images_dir / "images.json").exists():
        meta = json.loads((images_dir / "images.json").read_text(encoding="utf-8"))
        base = images_dir / "images"
    elif (images_dir.parent / "images.json").exists() and images_dir.name == "images":
        meta = json.loads((images_dir.parent / "images.json").read_text(encoding="utf-8"))
        base = images_dir
    else:
        base = images_dir / "images" if (images_dir / "images").is_dir() else images_dir
    entries = meta.get("images") or []
    if "images" not in meta:
        files = sorted(p for p in base.iterdir() if p.is_file() and IMG_EXT_RE.search(p.name + "?"))
        entries = [{"id": p.stem, "file": p.name, "seg_index": i} for i, p in enumerate(files)]
    out: List[ImgInfo] = []
    n = max(len(entries), 1)
    for i, e in enumerate(entries):
        # 兼容旧工作目录，但禁止裁切页再次参与替换。
        if (e.get("kind") == "page" or str(e.get("id", "")).startswith("__page__")
                or str(e.get("file", "")).startswith("page__")):
            continue
        p = base / e["file"]
        if not p.exists():
            continue
        data = p.read_bytes()
        sn = sniff_image(data)
        if not sn:
            continue
        info = ImgInfo(id=e.get("id") or p.stem, path=str(p), width=sn[1], height=sn[2], fmt=sn[0],
                       order=i / n, seq=i, label=e.get("alt") or e.get("segment") or "", nbytes=len(data))
        compute_hashes(info, data)
        out.append(info)
    return out, meta


# 匹配
def _tokens(s: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _contiguous(sub: List[str], sup: List[str]) -> bool:
    if not sub or len(sub) > len(sup):
        return False
    return any(sup[i : i + len(sub)] == sub for i in range(len(sup) - len(sub) + 1))


def name_hint(e: ImgInfo, p: ImgInfo) -> bool:
    """EPUB 文件名（去扩展名）与 Play 图书资源 ID 是否同源，如 `9782803632190_090.jpg` ↔ `i_9782803632190_090`。"""
    stem = _tokens(posixpath.splitext(posixpath.basename(e.path))[0])
    pid = _tokens(p.id)
    if pid[:1] == ["i"]:
        pid = pid[1:]
    if not stem or not pid or sum(len(t) for t in stem) < 3:
        return False
    return stem == pid or _contiguous(stem, pid) or _contiguous(pid, stem)


def aspect_ok(a: ImgInfo, b: ImgInfo, tol: float = 0.05) -> bool:
    if min(a.width, a.height, b.width, b.height) <= 0:
        return False
    ra, rb = a.width / a.height, b.width / b.height
    return abs(ra - rb) <= tol * max(ra, rb)


def match_images(epub_imgs: List[ImgInfo], pb_imgs: List[ImgInfo], max_cost: float) -> List[Match]:
    cands: List[Tuple[int, float, float, ImgInfo, ImgInfo, str]] = []
    for e in epub_imgs:
        if not e.hashable:
            continue
        for p in pb_imgs:
            if not p.hashable or not aspect_ok(e, p):
                continue
            hc = hash_cost(e, p)
            if name_hint(e, p) and hc <= max(max_cost, 0.4):
                cands.append((0, hc, abs(e.order - p.order), e, p, "name"))
            elif hc <= max_cost:
                cands.append((1, hc, abs(e.order - p.order), e, p, "hash"))
    cands.sort(key=lambda t: (t[0], round(t[1], 4), t[2]))
    used_e, used_p, matches = set(), set(), []
    for _, hc, _, e, p, via in cands:
        if e.id in used_e or p.id in used_p:
            continue
        used_e.add(e.id)
        used_p.add(p.id)
        matches.append(Match(e, p, hc, via))
    # EPUB 内若有内容相同的重复图片，允许多对一
    for _, hc, _, e, p, via in cands:
        if e.id in used_e:
            continue
        used_e.add(e.id)
        matches.append(Match(e, p, hc, via + "+dup"))
    return matches


# replace：写出新 EPUB
def write_epub(src: Path, dst: Path, replacements: Dict[str, bytes], expected: Dict[str, Tuple[int, int]]) -> None:
    if src.resolve() == dst.resolve():
        raise ValueError("Output path must differ from the source EPUB")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=dst.name + '.', suffix='.tmp', dir=dst.parent, delete=False) as f:
        tmp = Path(f.name)
    try:
        _write_epub(src, tmp, replacements)
        problems = verify_epub(tmp, expected)
        if problems:
            raise RuntimeError('Output EPUB failed validation: ' + '; '.join(problems))
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _write_epub(src: Path, tmp: Path, replacements: Dict[str, bytes]) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w") as zout:
        infos = zin.infolist()
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        zout.writestr(mt, b"application/epub+zip")
        for info in infos:
            if info.filename == "mimetype":
                continue
            data = replacements.get(info.filename)
            if data is None:
                data = zin.read(info)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zout.writestr(zi, data)


def verify_epub(dst: Path, expected: Dict[str, Tuple[int, int]]) -> List[str]:
    problems: List[str] = []
    with zipfile.ZipFile(dst) as z:
        bad = z.testzip()
        if bad:
            problems.append(f"ZIP validation failed: {bad}")
        infos = z.infolist()
        if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype must be the first uncompressed entry")
        elif z.read("mimetype") != b"application/epub+zip":
            problems.append("Invalid mimetype contents")
        for path, (w, h) in expected.items():
            sn = sniff_image(z.read(path))
            if not sn:
                problems.append(f"{path} cannot be parsed as an image")
            elif (sn[1], sn[2]) != (w, h):
                problems.append(f"{path} dimensions {sn[1]}x{sn[2]} do not match expected {w}x{h}")
    try:
        load_epub(dst, hashes=False)
    except Exception as e:
        problems.append(f"EPUB re-parse failed: {e}")
    return problems


def do_replace(epub_path: Path, images_dir: Path, out_path: Optional[Path], min_gain: float, max_cost: float,
               dry_run: bool, report_path: Optional[Path], allow_smaller: bool = False) -> int:
    if not math.isfinite(min_gain) or min_gain <= 0 or not math.isfinite(max_cost) or not 0 <= max_cost <= .5:
        raise ValueError('Invalid matching ranges: min-gain > 0, max-dist between 0 and 0.5')
    log(f"Parsing EPUB: {epub_path}")
    book = load_epub(epub_path)
    log(f"  Title: {book.title or '?'}  |  images {len(book.images)} images")
    pb_imgs, meta = load_pb_images(images_dir)
    log(f"Loading Play Books images: {len(pb_imgs)} images ({images_dir})")
    if not pb_imgs:
        raise RuntimeError("No usable Play Books images; run fetch first")

    matches = match_images(book.images, pb_imgs, max_cost)
    by_epub = {m.epub.id: m for m in matches}

    replacements: Dict[str, bytes] = {}
    expected: Dict[str, Tuple[int, int]] = {}
    rows: List[dict] = []
    for e in sorted(book.images, key=lambda i: (i.order, i.path)):
        row = {"epub": e.path, "old": e.dims(), "old_bytes": e.nbytes, "new": "", "gain": None, "cost": None,
               "source": "", "via": "", "action": "", "note": ""}
        m = by_epub.get(e.id)
        if not e.hashable:
            row["action"], row["note"] = "Skip", "SVG / too small / unreadable"
        elif not m:
            row["action"], row["note"] = "Unmatched", "No matching Play Books image"
        else:
            p = m.pb
            gain = p.pixels / e.pixels if e.pixels else 0
            row.update(new=p.dims(), gain=round(gain, 2), cost=round(m.cost, 3), source=p.id, via=m.via)
            if gain < min_gain and not allow_smaller:
                row["action"] = "Keep"
                row["note"] = "New image has no higher resolution" if gain <= 1.0 else f"Gain below {min_gain:.2f}x"
            else:
                data = Path(p.path).read_bytes()
                # 以文件的真实格式为准（清单里的 media-type 偶有标错）
                target_fmt = e.fmt if e.fmt in FMT_TO_MEDIA else MEDIA_TO_FMT.get(e.media_type, e.fmt)
                if p.fmt != target_fmt and target_fmt in FMT_TO_MEDIA:
                    try:
                        data = convert_image(data, target_fmt)
                        row["note"] = f"{p.fmt}→{target_fmt}"
                    except Exception as ex:
                        row["action"], row["note"] = "Failed", f"Format conversion failed: {ex}"
                        rows.append(row)
                        continue
                replacements[e.path] = data
                expected[e.path] = (p.width, p.height)
                row["action"] = "Replace"
                row["new_bytes"] = len(data)
        rows.append(row)

    matched_pb = {m.pb.id for m in matches}
    unused_pb = [p for p in pb_imgs if p.id not in matched_pb]

    print_report(rows, unused_pb)
    if report_path:
        report_path.write_text(json.dumps({"epub": str(epub_path), "images_dir": str(images_dir), "rows": rows,
                                           "unused_play_images": [{"id": p.id, "dims": p.dims(), "file": p.path} for p in unused_pb]},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Report written to {report_path}")

    n_rep = sum(1 for r in rows if r["action"] == "Replace")
    if dry_run:
        log(f"(Dry run) Would replace {n_rep} images; no file written")
        return n_rep
    if not n_rep:
        log("No images need replacement; no file written")
        return 0
    from .output_naming import output_path
    out = output_path(epub_path, book.title, images_dir, out_path)
    write_epub(epub_path, out, replacements, expected)
    size_old, size_new = epub_path.stat().st_size, out.stat().st_size
    log(f"✓ Replaced {n_rep} images; validation passed → {out}  ({size_old / 1e6:.1f} MB → {size_new / 1e6:.1f} MB)")
    return n_rep


def _pad(s: str, n: int) -> str:
    s = str(s)
    width = sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)
    return s + " " * max(n - width, 0)


def print_report(rows: List[dict], unused_pb: List[ImgInfo]) -> None:
    log()
    hdr = f"{_pad('#', 4)}{_pad('EPUB image', 42)}{_pad('Old size', 12)}{_pad('New size', 12)}{_pad('Gain', 8)}{_pad('Dist.', 7)}{_pad('Source (pg)', 34)}{_pad('Action', 8)}Notes"
    log(hdr)
    log("─" * (len(hdr) + 8))
    for i, r in enumerate(rows, 1):
        gain = f"{r['gain']:.1f}x" if r["gain"] is not None else ""
        cost = f"{r['cost']:.3f}" if r["cost"] is not None else ""
        via = f" [{r['via']}]" if r["via"] and r["via"] != "hash" else ""
        log(f"{_pad(i, 4)}{_pad(r['epub'][-40:], 42)}{_pad(r['old'], 12)}{_pad(r['new'], 12)}{_pad(gain, 8)}{_pad(cost, 7)}{_pad(r['source'][-32:], 34)}{_pad(r['action'], 8)}{r['note']}{via}")
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    log("─" * (len(hdr) + 8))
    log("Summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if unused_pb:
        log(f"Play Books has {len(unused_pb)} images not matched to EPUB: " + ", ".join(f"{p.id}({p.dims()})" for p in unused_pb[:12]) + ("…" if len(unused_pb) > 12 else ""))
    log()


# inspect
def do_inspect(epub_path: Path) -> None:
    book = load_epub(epub_path, hashes=False)
    log(f"{epub_path}\nTitle: {book.title or '?'}   OPF: {book.opf_path}   Volume ID: {book.volume_id or 'Unknown'}   Cover: {book.cover or '-'}")
    log(f"{_pad('#', 4)}{_pad('Path', 50)}{_pad('Size', 12)}{_pad('Format', 6)}{_pad('Bytes', 9)}Reading order")
    for i, im in enumerate(sorted(book.images, key=lambda i: (i.order, i.path)), 1):
        log(f"{_pad(i, 4)}{_pad(im.path[-48:], 50)}{_pad(im.dims(), 12)}{_pad(im.fmt, 6)}{_pad(f'{im.nbytes / 1024:.0f}KB', 9)}{'Unreferenced' if im.seq < 0 else im.seq + 1}{'  (cover)' if im.label == 'cover' else ''}")
    total = sum(i.nbytes for i in book.images)
    log(f"Total: {len(book.images)} images, {total / 1e6:.1f} MB")


# CLI
def add_fetch_args(p: argparse.ArgumentParser, need_id: bool) -> None:
    p.add_argument("--id", required=need_id, help="Play 图书卷 ID（阅读器地址 reader?id=… 中的 id）")
    p.add_argument("--work", default="playbooks_work", help="工作目录（默认 ./playbooks_work/<卷ID>）")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--cdp", help="连接已开启远程调试的 Chrome，如 http://127.0.0.1:9222")
    auth.add_argument("--cookies", type=Path, help="Netscape 格式 cookies.txt（不启动浏览器）")
    auth.add_argument("--anonymous", action="store_true", help="不登录，仅抓取公开预览（测试用）")
    p.add_argument("--profile", type=Path, default=Path(".chrome-profile"), help="独立的 Chrome 配置目录（默认 ./.chrome-profile）")
    p.add_argument("--browser", help="Chrome/Chromium 可执行文件路径")
    p.add_argument("--authuser", help="多账号登录时指定账号序号（阅读器地址中的 authuser）")
    p.add_argument("--allow-partial", action="store_true", help="只有预览权限时也继续")
    p.add_argument("--login-timeout", type=float, default=900, help="等待登录的秒数（默认 900）")
    p.add_argument("--pace", type=float, default=0.25, help="请求间隔秒数（默认 0.25）")
    p.add_argument("--limit", type=int, default=0, help="最多下载多少张插图（0 = 全部，测试用）")
    p.add_argument("--no-cover", action="store_true", help="不额外下载商店封面")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--show-browser", dest="headless", action="store_false", help="显示 Chrome 窗口，用于首次登录或登录失效")
    view.add_argument("--headless", dest="headless", action="store_true", help="无窗口启动 Chrome（默认）")
    lifecycle = p.add_mutually_exclusive_group()
    lifecycle.add_argument("--quit-browser", dest="quit_browser", action="store_true", help="抓取完成自动关闭本次启动的 Chrome（默认；不关闭复用或 --cdp 连接的进程）")
    lifecycle.add_argument("--keep-browser", dest="quit_browser", action="store_false", help="保留本次启动的 Chrome，通常与 --show-browser 合用")
    p.set_defaults(headless=True, quit_browser=True)


def add_replace_args(p: argparse.ArgumentParser, need_images: bool) -> None:
    p.add_argument("--epub", type=Path, required=True, help="输入 EPUB（已去 DRM）")
    if need_images:
        p.add_argument("--images", type=Path, required=True, help="fetch 的工作目录（含 images.json）或其 images 子目录")
    p.add_argument("--out", type=Path, help="输出 EPUB（默认 原书目录/hires/完整书名.epub，自动创建目录）")
    p.add_argument("--min-gain", type=float, default=1.1, help="新图像素数至少为原图的多少倍才替换（默认 1.1）")
    p.add_argument("--max-dist", type=float, default=0.2, help="感知哈希最大距离，0=完全一致 0.5=无关（默认 0.2）")
    p.add_argument("--allow-smaller", action="store_true", help="即使新图不更大也替换（不推荐）")
    p.add_argument("--dry-run", action="store_true", help="只显示匹配/替换计划，不写文件")
    p.add_argument("--report", type=Path, help="把匹配报告写成 JSON")


def fetch_mode(args: argparse.Namespace) -> str:
    if args.anonymous:
        return "anonymous"
    if args.cookies:
        return "cookies"
    return "browser"


@cancellable
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="playbooks_hires.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="从 Play 图书网页阅读器抓取原始尺寸插图")
    add_fetch_args(p_fetch, need_id=True)

    p_rep = sub.add_parser("replace", help="用抓取到的插图替换 EPUB 内的低清插图")
    add_replace_args(p_rep, need_images=True)

    p_run = sub.add_parser("run", help="fetch + replace 一步完成")
    add_replace_args(p_run, need_images=False)
    add_fetch_args(p_run, need_id=False)

    p_ins = sub.add_parser("inspect", help="列出 EPUB 内的插图及分辨率")
    p_ins.add_argument("--epub", type=Path, required=True)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "inspect":
            do_inspect(args.epub)
        elif args.cmd == "fetch":
            do_fetch(args.id, Path(args.work) / args.id, fetch_mode(args), args.profile, args.cdp, args.browser, args.cookies,
                     args.authuser, args.allow_partial, args.login_timeout, args.pace, args.limit, args.quit_browser, not args.no_cover, args.headless)
        elif args.cmd == "replace":
            do_replace(args.epub, args.images, args.out, args.min_gain, args.max_dist, args.dry_run, args.report, args.allow_smaller)
        elif args.cmd == "run":
            vid = args.id
            if not vid:
                vid = load_epub(args.epub, hashes=False).volume_id
                if not vid:
                    sys.exit("Cannot identify the volume ID from EPUB; specify --id (the id in reader?id=...)")
                log(f"Volume ID from EPUB: {vid}")
            work = do_fetch(vid, Path(args.work) / vid, fetch_mode(args), args.profile, args.cdp, args.browser, args.cookies,
                            args.authuser, args.allow_partial, args.login_timeout, args.pace, args.limit, args.quit_browser, not args.no_cover, args.headless)
            do_replace(args.epub, work, args.out, args.min_gain, args.max_dist, args.dry_run, args.report, args.allow_smaller)
    except KeyboardInterrupt:
        warn("Interrupted")
        return 130
    except (RuntimeError, OSError, ValueError, zipfile.BadZipFile) as e:
        warn(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
