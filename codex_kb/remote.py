"""Authenticated E2E client for the shared codex-kb service.

The HTTP service is deliberately an opaque store: this module encrypts and
decrypts record bodies and file bytes before they cross the network.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from . import e2e
from .db import KnowledgeInput


DEFAULT_REMOTE_URL = "https://kb.sugar-knight.com"
KNOWLEDGE_AAD = b"codex-kb knowledge v1"
SECRET_AAD_PREFIX = "codex-kb secret v1 "


class RemoteKnowledgeClient:
    """Small HTTPS client with local encryption material supplied by the CLI."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("remote URL must use https://")
        if not self.token:
            raise ValueError("remote API token is required; run 'codex-kb remote register' or 'remote login'")

    def get(self, path: str, query: dict[str, str] | None = None) -> Any:
        suffix = f"?{urlencode(query)}" if query else ""
        return self._request("GET", f"{path}{suffix}")

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", path, payload)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def upload_ciphertext(self, file_id: str, source: Path) -> None:
        """Upload a pre-encrypted temporary file without loading it into memory."""
        parsed = urlsplit(self.base_url)
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=60)
        prefix = parsed.path.rstrip("/")
        endpoint = f"{prefix}/api/files/{file_id}/content" or f"/api/files/{file_id}/content"
        size = source.stat().st_size
        try:
            connection.putrequest("PUT", endpoint)
            connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            with source.open("rb") as input_file:
                while chunk := input_file.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            body = response.read()
            if response.status not in (200, 201, 204):
                raise ValueError(_http_error(response.status, body))
        except OSError as error:
            raise ValueError(f"remote service is unavailable: {error}") from error
        finally:
            connection.close()

    def download_ciphertext(self, file_id: str, target: Path) -> None:
        """Download encrypted bytes into a temporary file and atomically publish it."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".codex-kb-download.tmp")
        request = Request(
            f"{self.base_url}/api/files/{file_id}/content",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            os.replace(temporary, target)
        except HTTPError as error:
            temporary.unlink(missing_ok=True)
            raise ValueError(_http_error(error.code, error.read())) from error
        except (OSError, URLError) as error:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"remote service is unavailable: {error}") from error

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                **({"Content-Type": "application/json; charset=utf-8"} if data is not None else {}),
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
        except HTTPError as error:
            raise ValueError(_http_error(error.code, error.read())) from error
        except URLError as error:
            raise ValueError(f"remote service is unavailable: {error.reason}") from error
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def unauthenticated_request(base_url: str, path: str, payload: dict[str, Any]) -> Any:
    base_url = base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("remote URL must use https://")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(f"{base_url}{path}", data=data, method="POST", headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
    except HTTPError as error:
        raise ValueError(_http_error(error.code, error.read())) from error
    except URLError as error:
        raise ValueError(f"remote service is unavailable: {error.reason}") from error
    return json.loads(body.decode("utf-8"))


def create_remote_account(base_url: str, username: str, display_name: str, password: str, passphrase: str) -> dict[str, str]:
    vault_key, private_key, vault_envelope, encrypted_private_key, public_key = e2e.create_vault_material(passphrase)
    result = unauthenticated_request(
        base_url,
        "/api/auth/register",
        {
            "username": username,
            "display_name": display_name,
            "password": password,
            "vault_envelope": vault_envelope,
            "encrypted_private_key": encrypted_private_key,
            "public_key": public_key,
        },
    )
    return {
        "server_url": base_url.rstrip("/"),
        "token": result["token"],
        "username": result["user"]["username"],
        "vault_key": e2e.b64encode(vault_key),
        "private_key": e2e.b64encode(private_key),
        "public_key": public_key,
    }


def login_remote_account(base_url: str, username: str, password: str, passphrase: str) -> dict[str, str]:
    result = unauthenticated_request(base_url, "/api/auth/login", {"username": username, "password": password})
    vault_key, private_key = e2e.restore_vault_material(passphrase, result["vault_envelope"], result["encrypted_private_key"])
    return {
        "server_url": base_url.rstrip("/"),
        "token": result["token"],
        "username": result["user"]["username"],
        "vault_key": e2e.b64encode(vault_key),
        "private_key": e2e.b64encode(private_key),
        "public_key": result["public_key"],
    }


class EncryptedKnowledgeClient:
    def __init__(self, api: RemoteKnowledgeClient, credentials: dict[str, str]) -> None:
        self.api = api
        try:
            self.vault_key = e2e.b64decode(credentials["vault_key"])
            self.private_key = e2e.b64decode(credentials["private_key"])
            self.public_key = credentials["public_key"]
        except (KeyError, ValueError) as error:
            raise ValueError("local remote credentials do not contain usable E2E keys; log in again") from error
        if len(self.vault_key) != 32 or len(self.private_key) != 32:
            raise ValueError("local remote credentials contain invalid E2E keys; log in again")

    def list_knowledge(self, query: str = "") -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        folded_query = query.casefold().strip()
        for item in self.api.get("/api/knowledge"):
            value = self._decrypt_knowledge(item)
            if not folded_query or folded_query in json.dumps(value, ensure_ascii=False).casefold():
                results.append(value)
        return results

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        return self._decrypt_knowledge(self.api.get(f"/api/knowledge/{knowledge_id}"))

    def create_knowledge(self, value: dict[str, Any]) -> dict[str, Any]:
        result = self.api.post("/api/knowledge", {"ciphertext": self._encrypt_knowledge(value)})
        return {**value, **result}

    def update_knowledge(self, knowledge_id: str, value: dict[str, Any]) -> dict[str, Any]:
        result = self.api.put(f"/api/knowledge/{knowledge_id}", {"ciphertext": self._encrypt_knowledge(value)})
        return {**value, **result}

    def delete_knowledge(self, knowledge_id: str) -> None:
        self.api.delete(f"/api/knowledge/{knowledge_id}")

    def list_secrets(self) -> list[dict[str, str]]:
        """Return secret metadata only. Secret values are never printed by the CLI."""
        results: list[dict[str, str]] = []
        for row in self.api.get("/api/secrets"):
            value = self._decrypt_secret(row)
            results.append({"id": row["id"], "name": value["name"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
        return sorted(results, key=lambda item: item["name"])

    def set_secret(self, name: str, value: str) -> dict[str, str]:
        _validate_secret_value(name, value)
        matching = self._secret_rows_named(name)
        if len(matching) > 1:
            raise ValueError(f"more than one remote secret is named {name}; delete the duplicates before updating it")
        if matching:
            row, _ = matching[0]
            result = self.api.put(f"/api/secrets/{row['id']}", {"ciphertext": self._encrypt_secret(row["id"], {"name": name, "value": value})})
            return {"id": row["id"], "name": name, "updated_at": result["updated_at"]}
        secret_id = str(uuid.uuid4())
        result = self.api.post("/api/secrets", {"id": secret_id, "ciphertext": self._encrypt_secret(secret_id, {"name": name, "value": value})})
        return {"id": result["id"], "name": name, "created_at": result["created_at"], "updated_at": result["updated_at"]}

    def secret_value(self, name: str) -> str:
        matches = self._secret_rows_named(name)
        if not matches:
            raise ValueError(f"remote secret {name} was not found")
        if len(matches) > 1:
            raise ValueError(f"more than one remote secret is named {name}; delete the duplicates before using it")
        return matches[0][1]["value"]

    def delete_secret(self, name: str) -> None:
        matching = self._secret_rows_named(name)
        if not matching:
            raise ValueError(f"remote secret {name} was not found")
        if len(matching) > 1:
            raise ValueError(f"more than one remote secret is named {name}; delete the duplicates before deleting it")
        self.api.delete(f"/api/secrets/{matching[0][0]['id']}")

    def list_files(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in self.api.get("/api/files"):
            metadata = self._file_metadata(item)
            records.append({**metadata, "id": item["id"], "owner_username": item["owner_username"], "is_owner": bool(item["is_owner"]), "created_at": item["created_at"]})
        return records

    def upload_file(self, source: Path) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"file was not found: {source}")
        file_id = str(uuid.uuid4())
        file_key = os.urandom(32)
        descriptor, encrypted_name = tempfile.mkstemp(prefix="codex-kb-upload-", suffix=".bin")
        os.close(descriptor)
        encrypted_path = Path(encrypted_name)
        try:
            metadata = e2e.encrypt_file(source, encrypted_path, file_key)
            metadata_ciphertext = e2e.encrypt_json(file_key, metadata, aad=_file_metadata_aad(file_id))
            owner_key_envelope = e2e.seal_for_recipient(file_key, self.public_key, file_id=file_id)
            self.api.post(
                "/api/files",
                {
                    "id": file_id,
                    "metadata_ciphertext": metadata_ciphertext,
                    "owner_key_envelope": owner_key_envelope,
                    "ciphertext_size": encrypted_path.stat().st_size,
                },
            )
            self.api.upload_ciphertext(file_id, encrypted_path)
            return {"id": file_id, **metadata}
        finally:
            encrypted_path.unlink(missing_ok=True)

    def download_file(self, file_id: str, output: Path | None = None) -> Path:
        row = self._get_file_row(file_id)
        metadata = self._file_metadata(row)
        if output is None:
            output = Path.cwd() / metadata["name"]
        output = output.expanduser().resolve()
        if output.exists():
            raise ValueError(f"output file already exists: {output}")
        descriptor, encrypted_name = tempfile.mkstemp(prefix="codex-kb-download-", suffix=".bin")
        os.close(descriptor)
        encrypted_path = Path(encrypted_name)
        try:
            self.api.download_ciphertext(file_id, encrypted_path)
            e2e.decrypt_file(encrypted_path, output, self._file_key(row), metadata["sha256"])
            return output
        finally:
            encrypted_path.unlink(missing_ok=True)

    def share_file(self, file_id: str, recipient_username: str, expected_fingerprint: str | None = None) -> dict[str, Any]:
        row = self._get_file_row(file_id)
        if not bool(row["is_owner"]):
            raise ValueError("only the file owner can share it")
        recipient = self.api.get(f"/api/users/{recipient_username}/encryption-key")
        fingerprint = e2e.public_key_fingerprint(recipient["public_key"])
        if expected_fingerprint and expected_fingerprint.casefold().replace(" ", "") != fingerprint:
            raise ValueError("recipient encryption-key fingerprint does not match the expected value")
        result = self.api.post(
            f"/api/files/{file_id}/shares",
            {"recipient_username": recipient["username"], "key_envelope": e2e.seal_for_recipient(self._file_key(row), recipient["public_key"], file_id=file_id)},
        )
        return {**result, "recipient_key_fingerprint": fingerprint}

    def revoke_file_share(self, file_id: str, username: str) -> None:
        self.api.delete(f"/api/files/{file_id}/shares/{username}")

    def delete_file(self, file_id: str) -> None:
        self.api.delete(f"/api/files/{file_id}")

    def _encrypt_knowledge(self, value: dict[str, Any]) -> str:
        return e2e.encrypt_json(self.vault_key, value, aad=KNOWLEDGE_AAD)

    def _decrypt_knowledge(self, row: dict[str, Any]) -> dict[str, Any]:
        value = e2e.decrypt_json(self.vault_key, row["ciphertext"], aad=KNOWLEDGE_AAD)
        if not isinstance(value, dict):
            raise ValueError("decrypted knowledge record is not an object")
        return {**value, "id": row["id"], "recorded_at": row.get("created_at", ""), "updated_at": row.get("updated_at", "")}

    def _encrypt_secret(self, secret_id: str, value: dict[str, str]) -> str:
        return e2e.encrypt_json(self.vault_key, value, aad=_secret_aad(secret_id))

    def _decrypt_secret(self, row: dict[str, Any]) -> dict[str, str]:
        value = e2e.decrypt_json(self.vault_key, row["ciphertext"], aad=_secret_aad(row["id"]))
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("value"), str):
            raise ValueError("decrypted secret is invalid")
        _validate_secret_value(value["name"], value["value"])
        return {"name": value["name"], "value": value["value"]}

    def _secret_rows_named(self, name: str) -> list[tuple[dict[str, Any], dict[str, str]]]:
        matches: list[tuple[dict[str, Any], dict[str, str]]] = []
        for row in self.api.get("/api/secrets"):
            value = self._decrypt_secret(row)
            if value["name"] == name:
                matches.append((row, value))
        return matches

    def _get_file_row(self, file_id: str) -> dict[str, Any]:
        for row in self.api.get("/api/files"):
            if row["id"] == file_id:
                return row
        raise ValueError("file was not found or you do not have access")

    def _file_key(self, row: dict[str, Any]) -> bytes:
        key = e2e.open_shared_file_key(row["key_envelope"], self.private_key, file_id=row["id"])
        if len(key) != 32:
            raise ValueError("invalid file encryption key")
        return key

    def _file_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        value = e2e.decrypt_json(self._file_key(row), row["metadata_ciphertext"], aad=_file_metadata_aad(row["id"]))
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("sha256"), str):
            raise ValueError("decrypted file metadata is invalid")
        return value


def _file_metadata_aad(file_id: str) -> bytes:
    return f"codex-kb file metadata {file_id}".encode("utf-8")


def _secret_aad(secret_id: str) -> bytes:
    return f"{SECRET_AAD_PREFIX}{secret_id}".encode("utf-8")


def _validate_secret_value(name: str, value: str) -> None:
    if not name or len(name) > 128 or any(not (character.isupper() or character.isdigit() or character == "_") for character in name) or not (name[0].isupper() or name[0] == "_"):
        raise ValueError("secret names must be uppercase environment-variable names (A-Z, 0-9, _, max 128 characters)")
    if not value or len(value.encode("utf-8")) > 64 * 1024:
        raise ValueError("secret values must be non-empty UTF-8 text of at most 64 KiB")


def _http_error(status_code: int, body: bytes) -> str:
    detail = body.decode("utf-8", errors="replace")
    try:
        detail = json.loads(detail).get("detail", detail)
    except (AttributeError, json.JSONDecodeError):
        pass
    return f"remote service returned HTTP {status_code}: {detail}"


def knowledge_payload(item: KnowledgeInput) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "title": item.title,
        "summary": item.summary,
        "purpose": item.purpose,
        "background": item.background,
        "rationale": item.rationale,
        "outcome": item.outcome,
        "tags": list(item.tags),
        "repo_root": item.repo_root or "",
        "session_id": item.session_id or "",
        "source_url": item.source_url or "",
        "observed_at": item.observed_at or "",
        "effective_from": item.effective_from or "",
        "artifacts": [asdict(artifact) for artifact in item.artifacts],
    }
