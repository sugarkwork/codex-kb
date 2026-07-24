"""Secure-ish local storage for remote bearer tokens and E2E keys."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .db import default_home


PROFILE_CREDENTIALS = "remote-credentials.json"
CWD_CREDENTIALS = ".codex-kb-credentials.json"


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
    required = ("server_url", "token", "vault_key", "private_key", "public_key", "username")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError(f"remote credentials at {path} are incomplete")
    return value


def save_credentials(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    _restrict_permissions(path)


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
