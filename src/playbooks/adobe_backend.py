"""Local adapters for the pinned ACSM / DeDRM modules. Never log credentials."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
import zipfile
from urllib.parse import urljoin

from .paths import VENDOR_DIR


def download_https(url: str, target: Path):
    """Follow only HTTPS redirects; inspect Location before sending a request."""
    import requests
    for _ in range(6):
        if not url.startswith('https://'):
            raise RuntimeError('Non-HTTPS ebook downloads or redirects are not allowed')
        with requests.get(url, stream=True, timeout=(15, 90), allow_redirects=False) as response:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                location = response.headers.get('Location')
                if not location:
                    raise RuntimeError('Ebook redirect has no destination')
                url = urljoin(url, location)
                continue
            with target.open('wb') as f:
                for chunk in response.iter_content(1024 * 256):
                    f.write(chunk)
            return
    raise RuntimeError('Too many ebook download redirects')


def private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        path.chmod(0o700)


def secure_files(path: Path):
    if os.name == 'nt':
        return  # Windows privacy is controlled by the containing folder's ACL.
    for item in path.rglob('*'):
        if not item.is_symlink():
            item.chmod(0o700 if item.is_dir() else 0o600)


@contextlib.contextmanager
def state_lock(state: Path):
    """OS-owned, nonblocking lock; released automatically on process exit."""
    private_dir(state)
    with (state / '.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise RuntimeError('Another task is using the ADE authorization; try again later') from None
        try:
            yield
        finally:
            release()


@contextlib.contextmanager
def quiet_upstream():
    # Upstream may print license tokens / signed URLs. Do not forward or persist.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def safe_error(stage: str, response=''):
    codes = sorted(set(re.findall(r'\bE_[A-Z0-9_]+\b', str(response))))
    return RuntimeError(stage + (': ' + ', '.join(codes) if codes else ' (check your account, network and authorization)'))


def load_modules(account: Path):
    for folder in ('acsm', 'dedrm'):
        location = str(VENDOR_DIR / folder)
        if location not in sys.path:
            sys.path.insert(0, location)
    import libadobe
    import requests
    from lxml import etree
    etree.set_default_parser(etree.XMLParser(resolve_entities=False, no_network=True))
    from requests.adapters import HTTPAdapter

    class AdobeTLS(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)

    session = requests.Session()
    session.headers.update({'User-Agent': 'book2png'})
    session.mount('https://', AdobeTLS())

    def request(url, data=None, content_type=None):
        # Never transmit Adobe authentication over plaintext HTTP.
        if '://' not in url:
            url = 'https://' + url
        if url.startswith('http://'):
            url = 'https://' + url[7:]
        if not url.startswith('https://'):
            raise RuntimeError('Adobe API requires HTTPS')
        headers = {'Content-Type': content_type} if content_type else {}
        try:
            r = session.request('POST' if data is not None else 'GET', url,
                                data=data, headers=headers, timeout=(15, 90), allow_redirects=False)
            # Avoid forwarding credentials to redirects on a different host.
            r.raise_for_status()
            if 300 <= r.status_code < 400:
                raise RuntimeError('Adobe API redirected; stopped to protect authorization data')
            return r
        except requests.RequestException:
            raise RuntimeError('Adobe HTTPS request failed. Check the network or server certificate; security checks remain enabled') from None

    libadobe.sendHTTPRequest_getSimple = lambda url: request(url).content

    def post(url, document, content_type, returnRC=False):
        r = request(url, document, content_type)
        return (r.status_code, r.content) if returnRC else r.content

    libadobe.sendPOSTHTTPRequest = post
    libadobe.update_account_path(str(account))
    libadobe.devkey_bytes = None
    import libadobeAccount
    import libadobeFulfill
    import ineptepub
    return libadobe, libadobeAccount, libadobeFulfill, ineptepub


def authorized(state: Path) -> bool:
    account = state / 'adobe'
    if not all((account / name).is_file() for name in ('activation.xml', 'device.xml', 'devicesalt')):
        return False
    try:
        from lxml import etree
        root = etree.parse(str(account / 'activation.xml'), etree.XMLParser(resolve_entities=False, no_network=True))
        return bool(root.findall('.//{http://ns.adobe.com/adept}activationToken'))
    except Exception:
        return False


def authorize(state: Path, email: str, password: str):
    if not email.strip() or not password:
        raise RuntimeError('Enter your ByteBooks ID and password; anonymous authorization is not supported')
    with state_lock(state):
        if (state / 'adobe').exists():
            raise RuntimeError('ADE authorization already exists and will not be overwritten. Use another --state directory')
        with tempfile.TemporaryDirectory(prefix='auth-', dir=state) as tmp:
            account = Path(tmp)
            lib, auth, _, _ = load_modules(account)
            try:
                with quiet_upstream():
                    lib.createDeviceKeyFile()
                    # ADE 4.0.3 protocol uses HTTPS; does not launch ADE itself.
                    if not auth.createDeviceFile(True, 3):
                        raise safe_error('Failed to create ADE device')
                    for stage, action in [
                        ('Failed to create ADE account authorization', lambda: auth.createUser(3, None)),
                        ('ADE sign-in failed', lambda: auth.signIn('AdobeID', email.strip(), password)),
                        ('ADE device activation failed', lambda: auth.activateDevice(3, None)),
                    ]:
                        result = action()
                        if not result[0]:
                            raise safe_error(stage, result[1])
                secure_files(account)
                account.rename(state / 'adobe')
            except RuntimeError:
                raise
            except Exception:
                raise safe_error('ADE authorization failed') from None


def import_activation(state: Path, source: Path):
    """Explicit user-selected ACSM authorization folder; never scan other apps."""
    with state_lock(state):
        if (state / 'adobe').exists():
            raise RuntimeError('Authorization already exists; refusing to overwrite')
        names = ('activation.xml', 'device.xml', 'devicesalt')
        if not all((source / n).is_file() for n in names):
            raise RuntimeError('Select an authorization folder containing activation.xml, device.xml and devicesalt')
        with tempfile.TemporaryDirectory(prefix='import-', dir=state) as tmp:
            target = Path(tmp) / 'adobe'
            target.mkdir(mode=0o700)
            for name in names:
                shutil.copyfile(source / name, target / name)
            if not authorized(Path(tmp)):
                raise RuntimeError('Authorization files contain no valid activation')
            secure_files(target)
            target.rename(state / 'adobe')


def fulfill_acsm(source: Path, state: Path, cache: Path) -> Path:
    if not authorized(state):
        raise RuntimeError('ADE is not authorized. Run authorize or sign in through the GUI')
    private_dir(cache)
    target = cache / 'download.epub'
    with state_lock(state):
        if target.exists():
            with zipfile.ZipFile(target) as z:
                if z.testzip() is None:
                    return target
        _, _, fulfill, _ = load_modules(state / 'adobe')
        from lxml import etree
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        # Parse safely before passing to the upstream parser.
        raw = source.read_bytes()
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise RuntimeError('ACSM files containing DTDs or external entities are not supported')
        etree.fromstring(raw, parser)
        with quiet_upstream():
            ok, reply = fulfill.fulfill(str(source), do_notify=True)
        if not ok:
            raise safe_error('ACSM fulfillment failed', reply)
        root = etree.fromstring(reply.encode() if isinstance(reply, str) else reply, parser)
        ns = {'a': 'http://ns.adobe.com/adept'}
        item = root.find('.//a:resourceItemInfo', ns)
        if item is None:
            raise RuntimeError('Adobe response contains no resource information')
        url = item.findtext('a:src', namespaces=ns)
        token = item.find('a:licenseToken', ns)
        if not url or token is None:
            raise RuntimeError('Adobe response contains no download URL or license')
        with quiet_upstream():
            rights = fulfill.buildRights(token)
        if not rights:
            raise RuntimeError('Unable to construct EPUB license information')
        import requests
        if url.startswith('http://'):
            url = 'https://' + url[7:]
        if not url.startswith('https://'):
            raise RuntimeError('Ebook download URL is not HTTPS')
        temp = cache / 'download.part'
        try:
            # Signed URL is deliberately never printed or written to logs.
            download_https(url, temp)
            if not zipfile.is_zipfile(temp):
                raise RuntimeError('Downloaded content is not EPUB (PDF is not supported)')
            with zipfile.ZipFile(temp) as z:
                if z.testzip() is not None:
                    raise RuntimeError('Downloaded EPUB failed validation')
                if 'META-INF/rights.xml' in z.namelist():
                    raise RuntimeError('Downloaded archive already contains license information; stopped to avoid duplicate entries')
            with zipfile.ZipFile(temp, 'a') as z:
                z.writestr('META-INF/rights.xml', rights)
            temp.replace(target)
            secure_files(cache)
            return target
        except requests.RequestException:
            raise RuntimeError('EPUB download failed. Check the network; signed URLs are not logged') from None
        finally:
            temp.unlink(missing_ok=True)
            secure_files(state / 'adobe')


def decrypt_epub(source: Path, target: Path, state: Path, key_file: Path | None = None) -> Path:
    with zipfile.ZipFile(source) as z:
        encrypted = 'META-INF/rights.xml' in z.namelist() and 'META-INF/encryption.xml' in z.namelist()
    if not encrypted:
        return source
    # This pinned upstream module treats every encryption entry as ADEPT AES.
    # Reject mixed/unsupported algorithms rather than corrupting obfuscated fonts.
    from lxml import etree
    with zipfile.ZipFile(source) as z:
        encryption = etree.fromstring(z.read('META-INF/encryption.xml'),
                                     etree.XMLParser(resolve_entities=False, no_network=True))
    for method in encryption.findall('.//{http://www.w3.org/2001/04/xmlenc#}EncryptionMethod'):
        if method.get('Algorithm') != 'http://www.w3.org/2001/04/xmlenc#aes128-cbc':
            raise RuntimeError('Unsupported mixed encryption/font algorithms; stopped without modifying the source')
    with state_lock(state):
        _, account, _, dedrm = load_modules(state / 'adobe')
        key = key_file.read_bytes() if key_file else account.exportAccountEncryptionKeyBytes()
        if not key:
            raise RuntimeError('Missing the account key for this EPUB. Authorize first or provide --key')
        try:
            from Crypto.PublicKey import RSA
            key = RSA.import_key(key).export_key(format='DER', pkcs=1)
        except (ValueError, TypeError, IndexError):
            raise RuntimeError('Adobe key is not a valid RSA private key') from None
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix('.part')
        try:
            try:
                with quiet_upstream():
                    result = dedrm.decryptBook(key, str(source), str(temp))
            except Exception:
                raise RuntimeError('Adobe decryption module cannot process this file. Check the key and DRM version') from None
            if result != 0:
                raise RuntimeError('Cannot decrypt: account/key mismatch or unsupported DRM version')
            # Confirm the content can be parsed, not merely that a ZIP was written.
            from . import playbooks_hires as hires
            hires.load_epub(temp, hashes=False)
            temp.replace(target)
            if os.name != 'nt':
                target.chmod(0o600)
            return target
        finally:
            temp.unlink(missing_ok=True)
