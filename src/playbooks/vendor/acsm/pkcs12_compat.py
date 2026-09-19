"""Small oscrypto-compatible surface backed by cryptography (local integration)."""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12


class keys:
    parse_pkcs12 = staticmethod(pkcs12.load_key_and_certificates)


def dump_certificate(cert, encoding='der'):
    return cert.public_bytes(serialization.Encoding.DER)


def dump_private_key(key, password=None, encoding='der'):
    if password is not None:
        raise ValueError('Encrypted key export is not supported by this adapter')
    return key.private_bytes(serialization.Encoding.DER,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
