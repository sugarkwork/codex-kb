"""Local storage for remote bearer tokens and E2E keys.

On Windows these credentials are protected with DPAPI for the current Windows
user.  They are still a local trust boundary: a process running as that user
can use them, which is necessary for a local Codex session to use the remote
service.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .db import default_home


PROFILE_CREDENTIALS = "remote-credentials.json"
CWD_CREDENTIALS = ".codex-kb-credentials.json"
WINDOWS_DPAPI_FORMAT = "windows-dpapi-current-user-v1"
WINDOWS_DPAPI_DESCRIPTION = "codex-kb remote credentials"


def default_credentials_path() -> Path:
    return default_home() / PROFILE_CREDENTIALS


def resolve_credentials_path(explicit: Path | None, cwd: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    current = (cwd or Path.cwd()) / CWD_CREDENTIALS
    return current if current.is_file() else default_credentials_path()


def load_credentials(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"remote credentials were not found at {path}; run 'codex-kb remote register' or 'remote login'") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"remote credentials at {path} are not valid JSON") from error
    if isinstance(value, dict) and value.get("protection") == WINDOWS_DPAPI_FORMAT:
        value = _unprotect_windows_credentials(value, path)
    return _validate_credentials(value, path)


def save_credentials(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(_validate_credentials(value, path), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stored: dict[str, Any]
    if os.name == "nt":
        stored = {
            "format": "codex-kb credentials",
            "version": 1,
            "protection": WINDOWS_DPAPI_FORMAT,
            "ciphertext": base64.b64encode(_protect_for_current_windows_user(serialized)).decode("ascii"),
        }
    else:
        stored = json.loads(serialized.decode("utf-8"))
    temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    _restrict_permissions(path)


def _validate_credentials(value: Any, path: Path) -> dict[str, Any]:
    required = ("server_url", "token", "vault_key", "private_key", "public_key", "username")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError(f"remote credentials at {path} are incomplete")
    return value


def _unprotect_windows_credentials(value: dict[str, Any], path: Path) -> dict[str, Any]:
    ciphertext = value.get("ciphertext")
    if not isinstance(ciphertext, str):
        raise ValueError(f"remote credentials at {path} are incomplete")
    if os.name != "nt":
        raise ValueError(f"remote credentials at {path} are protected with Windows DPAPI and must be used by their original Windows user")
    try:
        plaintext = _unprotect_for_current_windows_user(base64.b64decode(ciphertext.encode("ascii"), validate=True))
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError, OSError) as error:
        raise ValueError(f"remote credentials at {path} cannot be decrypted by this Windows user") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"remote credentials at {path} are incomplete")
    return decoded


def _protect_for_current_windows_user(plaintext: bytes) -> bytes:
    return _crypt_protect_data(plaintext, protect=True)


def _unprotect_for_current_windows_user(ciphertext: bytes) -> bytes:
    return _crypt_protect_data(ciphertext, protect=False)


def _crypt_protect_data(value: bytes, *, protect: bool) -> bytes:
    """Call the current-user Windows DPAPI without a third-party dependency."""
    if os.name != "nt":
        raise OSError("Windows DPAPI is only available on Windows")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    if not value:
        raise ValueError("DPAPI input must not be empty")
    input_buffer = ctypes.create_string_buffer(value)
    input_blob = DataBlob(len(value), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        crypt = crypt32.CryptProtectData
        crypt.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob)]
        crypt.restype = ctypes.c_bool
        success = crypt(ctypes.byref(input_blob), WINDOWS_DPAPI_DESCRIPTION, None, None, None, 0, ctypes.byref(output_blob))
    else:
        crypt = crypt32.CryptUnprotectData
        crypt.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob)]
        crypt.restype = ctypes.c_bool
        success = crypt(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob))
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.pbData)


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    username = os.environ.get("USERNAME")
    if not username:
        return
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
