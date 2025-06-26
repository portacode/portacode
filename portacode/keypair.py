from __future__ import annotations

from pathlib import Path
from typing import Tuple
import logging
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .data import get_key_dir

PRIVATE_KEY_FILE = "id_portacode"
PUBLIC_KEY_FILE = "id_portacode.pub"

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

class KeyPair:
    """Container for RSA keypair paths and objects."""

    def __init__(self, private_key_path: Path, public_key_path: Path):
        self.private_key_path = private_key_path
        self.public_key_path = public_key_path

    @property
    def private_key_pem(self) -> bytes:
        return self.private_key_path.read_bytes()

    @property
    def public_key_pem(self) -> bytes:
        return self.public_key_path.read_bytes()

    def sign_challenge(self, challenge: str) -> bytes:
        """Sign a challenge string with the private key."""
        private_key = serialization.load_pem_private_key(
            self.private_key_pem, password=None
        )
        return private_key.sign(
            challenge.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def public_key_der_b64(self) -> str:
        """Return the public key as base64-encoded DER (single line)."""
        pubkey = serialization.load_pem_public_key(self.public_key_pem)
        der = pubkey.public_bytes(encoding=serialization.Encoding.DER, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        return base64.b64encode(der).decode()

    @staticmethod
    def der_b64_to_pem(der_b64: str) -> bytes:
        """Convert base64 DER to PEM format."""
        der = base64.b64decode(der_b64)
        pubkey = serialization.load_der_public_key(der)
        return pubkey.public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)


def _generate_keypair() -> Tuple[bytes, bytes]:
    # Use 1024 bits for smaller demo keys (not secure for production)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    public_pem = (
        private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    return private_pem, public_pem


def get_or_create_keypair() -> KeyPair:
    """Return the existing keypair or generate a new one if missing."""
    key_dir = get_key_dir()
    priv_path = key_dir / PRIVATE_KEY_FILE
    pub_path = key_dir / PUBLIC_KEY_FILE

    if not priv_path.exists() or not pub_path.exists():
        logging.info(f"No keys found, generating new one and saving to {key_dir}")
        private_pem, public_pem = _generate_keypair()
        priv_path.write_bytes(private_pem)
        pub_path.write_bytes(public_pem)
    else:
        logging.info(f"Found existing keys at {key_dir}")

    return KeyPair(priv_path, pub_path)


def fingerprint_public_key(pem: bytes) -> str:
    """Return a short fingerprint for display purposes (SHA-256)."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(pem)
    return digest.finalize().hex()[:16] 