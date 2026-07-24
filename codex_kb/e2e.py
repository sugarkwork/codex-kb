"""Client-side cryptography for the shared codex-kb service.

The server stores only opaque ciphertext, a password verifier, public keys,
and access-token hashes.  The per-user vault key and X25519 private key never
leave a trusted client in plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


VAULT_AAD = b"codex-kb vault key v1"
PRIVATE_KEY_AAD = b"codex-kb x25519 private key v1"


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def derive_passphrase_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 12:
        raise ValueError("encryption passphrase must be at least 12 characters")
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def encrypt_bytes(key: bytes, plaintext: bytes, *, aad: bytes) -> dict[str, str]:
    nonce = os.urandom(12)
    return {"nonce": b64encode(nonce), "ciphertext": b64encode(AESGCM(key).encrypt(nonce, plaintext, aad))}


def decrypt_bytes(key: bytes, envelope: dict[str, str], *, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(b64decode(envelope["nonce"]), b64decode(envelope["ciphertext"]), aad)
    except (KeyError, ValueError) as error:
        raise ValueError("invalid encrypted payload") from error


def encrypt_json(key: bytes, value: Any, *, aad: bytes) -> str:
    return json.dumps(encrypt_bytes(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), aad=aad), separators=(",", ":"))


def decrypt_json(key: bytes, value: str, *, aad: bytes) -> Any:
    try:
        envelope = json.loads(value)
        return json.loads(decrypt_bytes(key, envelope, aad=aad).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("encrypted JSON cannot be decrypted") from error


def create_vault_material(passphrase: str) -> tuple[bytes, bytes, dict[str, str], str, str]:
    """Return local keys plus opaque server-safe vault material."""
    vault_key = os.urandom(32)
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public_bytes = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    salt = os.urandom(16)
    wrapping_key = derive_passphrase_key(passphrase, salt)
    vault_envelope = encrypt_bytes(wrapping_key, vault_key, aad=VAULT_AAD)
    vault_envelope["salt"] = b64encode(salt)
    vault_envelope["kdf"] = "scrypt-n32768-r8-p1"
    encrypted_private_key = encrypt_json(vault_key, {"private_key": b64encode(private_bytes)}, aad=PRIVATE_KEY_AAD)
    return vault_key, private_bytes, vault_envelope, encrypted_private_key, b64encode(public_bytes)


def restore_vault_material(passphrase: str, vault_envelope: dict[str, str], encrypted_private_key: str) -> tuple[bytes, bytes]:
    if vault_envelope.get("kdf") != "scrypt-n32768-r8-p1":
        raise ValueError("unsupported vault key derivation")
    wrapping_key = derive_passphrase_key(passphrase, b64decode(vault_envelope["salt"]))
    vault_key = decrypt_bytes(wrapping_key, vault_envelope, aad=VAULT_AAD)
    private_key = b64decode(decrypt_json(vault_key, encrypted_private_key, aad=PRIVATE_KEY_AAD)["private_key"])
    if len(vault_key) != 32 or len(private_key) != 32:
        raise ValueError("invalid vault material")
    return vault_key, private_key


def public_key_fingerprint(public_key: str) -> str:
    return hashlib.sha256(b64decode(public_key)).hexdigest()[:32]


def seal_for_recipient(file_key: bytes, recipient_public_key: str, *, file_id: str) -> str:
    peer = X25519PublicKey.from_public_bytes(b64decode(recipient_public_key))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(peer)
    wrapping_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=hashlib.sha256(file_id.encode("utf-8")).digest(), info=b"codex-kb file share v1").derive(shared)
    envelope = encrypt_bytes(wrapping_key, file_key, aad=f"codex-kb shared file {file_id}".encode("utf-8"))
    envelope["ephemeral_public_key"] = b64encode(ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return json.dumps(envelope, separators=(",", ":"))


def open_shared_file_key(envelope_json: str, private_key: bytes, *, file_id: str) -> bytes:
    try:
        envelope = json.loads(envelope_json)
        peer = X25519PublicKey.from_public_bytes(b64decode(envelope.pop("ephemeral_public_key")))
        own = X25519PrivateKey.from_private_bytes(private_key)
        shared = own.exchange(peer)
        wrapping_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=hashlib.sha256(file_id.encode("utf-8")).digest(), info=b"codex-kb file share v1").derive(shared)
        return decrypt_bytes(wrapping_key, envelope, aad=f"codex-kb shared file {file_id}".encode("utf-8"))
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        raise ValueError("shared file key cannot be decrypted") from error


def encrypt_file(source: Path, target: Path, key: bytes) -> dict[str, Any]:
    """Write nonce + AES-GCM ciphertext + tag and return plaintext integrity metadata."""
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_file, target.open("wb") as output_file:
        output_file.write(nonce)
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)
    return {"name": source.name, "size_bytes": size, "sha256": digest.hexdigest()}


def decrypt_file(source: Path, target: Path, key: bytes, expected_sha256: str) -> None:
    size = source.stat().st_size
    if size < 28:
        raise ValueError("encrypted file is incomplete")
    with source.open("rb") as input_file:
        nonce = input_file.read(12)
        input_file.seek(size - 16)
        tag = input_file.read(16)
        input_file.seek(12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        digest = hashlib.sha256()
        remaining = size - 28
        with target.open("wb") as output_file:
            while remaining:
                chunk = input_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("encrypted file is truncated")
                remaining -= len(chunk)
                plaintext = decryptor.update(chunk)
                digest.update(plaintext)
                output_file.write(plaintext)
            plaintext = decryptor.finalize()
            digest.update(plaintext)
            output_file.write(plaintext)
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        target.unlink(missing_ok=True)
        raise ValueError("decrypted file checksum does not match")
