"""Opaque-storage API for the end-to-end encrypted shared codex-kb service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("KB_DATA_DIR", str(Path.home() / ".codex-kb-service"))),
            max_upload_bytes=int(os.environ.get("KB_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))),
        )


class VaultEnvelope(BaseModel):
    salt: str = Field(min_length=1, max_length=256)
    nonce: str = Field(min_length=1, max_length=256)
    ciphertext: str = Field(min_length=1, max_length=512)
    kdf: str = Field(pattern="^scrypt-n32768-r8-p1$")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=1024)
    vault_envelope: VaultEnvelope
    encrypted_private_key: str = Field(min_length=1, max_length=4096)
    public_key: str = Field(min_length=1, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=1024)


class EncryptedKnowledgeRequest(BaseModel):
    ciphertext: str = Field(min_length=1, max_length=1_000_000)


class EncryptedSecretRequest(BaseModel):
    id: str = Field(min_length=36, max_length=36)
    ciphertext: str = Field(min_length=1, max_length=100_000)


class EncryptedSecretUpdateRequest(BaseModel):
    ciphertext: str = Field(min_length=1, max_length=100_000)


class EncryptedFileRequest(BaseModel):
    id: str = Field(min_length=36, max_length=36)
    metadata_ciphertext: str = Field(min_length=1, max_length=32_000)
    owner_key_envelope: str = Field(min_length=1, max_length=32_000)
    ciphertext_size: int = Field(ge=28, le=100 * 1024 * 1024 + 28)


class EncryptedShareRequest(BaseModel):
    recipient_username: str = Field(min_length=3, max_length=32)
    key_envelope: str = Field(min_length=1, max_length=32_000)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    username: str
    display_name: str
    token_id: str


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, remote: str, maximum: int, seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            key = (bucket, remote)
            values = [value for value in self._entries.get(key, []) if value > now - seconds]
            if len(values) >= maximum:
                raise HTTPException(status_code=429, detail="Too many attempts. Please try again later.")
            values.append(now)
            self._entries[key] = values


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.files_dir = self.settings.data_dir / "files"
        self.files_dir.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.settings.data_dir / "kb-service.sqlite3"
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    vault_envelope_json TEXT NOT NULL,
                    encrypted_private_key TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_owner_updated ON knowledge(owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS secrets (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_secrets_owner_updated ON secrets(owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    stored_name TEXT NOT NULL UNIQUE,
                    metadata_ciphertext TEXT NOT NULL,
                    ciphertext_size INTEGER NOT NULL,
                    content_uploaded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_files_owner_created ON files(owner_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS file_key_envelopes (
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    key_envelope TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(file_id, recipient_id)
                );
                CREATE INDEX IF NOT EXISTS idx_file_key_recipient ON file_key_envelopes(recipient_id, file_id);
                """
            )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$%s$%s" % (base64.urlsafe_b64encode(salt).decode("ascii"), base64.urlsafe_b64encode(digest).decode("ascii"))


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = stored.split("$")
        if algorithm != "scrypt":
            return False
        expected_bytes = base64.urlsafe_b64decode(expected.encode("ascii"))
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt.encode("ascii")), n=int(n), r=int(r), p=int(p), dklen=len(expected_bytes), maxmem=64 * 1024 * 1024
        )
        return hmac.compare_digest(digest, expected_bytes)
    except (ValueError, UnicodeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _username(value: str) -> str:
    result = value.strip().lower()
    if not USERNAME_RE.fullmatch(result):
        raise HTTPException(status_code=422, detail="Username must be 3-32 lowercase letters, digits, '.', '_' or '-'.")
    return result


def _validate_public_key(value: str) -> None:
    try:
        if len(base64.urlsafe_b64decode(value.encode("ascii"))) != 32:
            raise ValueError
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="Invalid encryption public key.") from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    store = Store(settings)
    limiter = SlidingWindowLimiter()
    app = FastAPI(title="codex-kb encrypted service", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        return response

    def remote_ip(request: Request) -> str:
        return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")

    def issue_token(connection: sqlite3.Connection, user_id: str) -> tuple[str, str]:
        token = "kbpat_" + secrets.token_urlsafe(32)
        token_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO api_tokens (id, user_id, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (token_id, user_id, _token_hash(token), utc_now()),
        )
        return token_id, token

    def auth(request: Request) -> AuthContext:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is required.")
        token = authorization[7:].strip()
        with store.connection() as connection:
            row = connection.execute(
                """
                SELECT u.id AS user_id, u.username, u.display_name, t.id AS token_id FROM api_tokens t
                JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.revoked_at IS NULL
                """,
                (_token_hash(token),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is invalid or revoked.")
            connection.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (utc_now(), row["token_id"]))
            return AuthContext(row["user_id"], row["username"], row["display_name"], row["token_id"])

    def owned_knowledge(connection: sqlite3.Connection, knowledge_id: str, user_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM knowledge WHERE id=? AND owner_id=?", (knowledge_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Knowledge item was not found.")
        return row

    def owned_secret(connection: sqlite3.Connection, secret_id: str, user_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM secrets WHERE id=? AND owner_id=?", (secret_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Secret was not found.")
        return row

    def accessible_file(connection: sqlite3.Connection, file_id: str, user_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT f.*, owner.username AS owner_username, k.key_envelope,
                   CASE WHEN f.owner_id=? THEN 1 ELSE 0 END AS is_owner
            FROM files f JOIN users owner ON owner.id=f.owner_id
            JOIN file_key_envelopes k ON k.file_id=f.id AND k.recipient_id=?
            WHERE f.id=? AND f.content_uploaded=1
            """,
            (user_id, user_id, file_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File was not found.")
        return row

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return """<!doctype html><meta charset=utf-8><title>codex-kb encrypted</title><main><h1>codex-kb encrypted service</h1><p>Use the installed <code>codex-kb remote register</code> command. Knowledge and files are encrypted on the client before upload; this server cannot display their contents.</p></main>"""

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def health() -> dict[str, str]:
        return {"status": "ok", "storage": "opaque-e2e"}

    @app.post("/api/auth/register", status_code=201)
    def register(payload: RegisterRequest, request: Request) -> dict[str, Any]:
        limiter.check("register", remote_ip(request), 4, 60 * 60)
        username = _username(payload.username)
        _validate_public_key(payload.public_key)
        user_id = str(uuid.uuid4())
        with store.connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (id, username, display_name, password_hash, vault_envelope_json, encrypted_private_key, public_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username, payload.display_name.strip(), _hash_password(payload.password), payload.vault_envelope.model_dump_json(), payload.encrypted_private_key, payload.public_key, utc_now()),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(status_code=409, detail="That username is already registered.") from error
            token_id, token = issue_token(connection, user_id)
        return {"user": {"id": user_id, "username": username, "display_name": payload.display_name.strip()}, "token": token, "token_id": token_id}

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
        limiter.check("login", remote_ip(request), 10, 10 * 60)
        username = _username(payload.username)
        with store.connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row is None or not _verify_password(payload.password, row["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid username or password.")
            token_id, token = issue_token(connection, row["id"])
        return {
            "user": {"id": row["id"], "username": row["username"], "display_name": row["display_name"]},
            "token": token,
            "token_id": token_id,
            "vault_envelope": json_load(row["vault_envelope_json"]),
            "encrypted_private_key": row["encrypted_private_key"],
            "public_key": row["public_key"],
        }

    @app.get("/api/auth/me")
    def me(context: AuthContext = Depends(auth)) -> dict[str, str]:
        return {"id": context.user_id, "username": context.username, "display_name": context.display_name, "token_id": context.token_id}

    @app.delete("/api/auth/token", status_code=204)
    def revoke_current_token(context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            connection.execute("UPDATE api_tokens SET revoked_at=? WHERE id=?", (utc_now(), context.token_id))
        return Response(status_code=204)

    @app.get("/api/users/{username}/encryption-key")
    def encryption_key(username: str, context: AuthContext = Depends(auth)) -> dict[str, str]:
        with store.connection() as connection:
            row = connection.execute("SELECT username, public_key FROM users WHERE username=?", (_username(username),)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Recipient is not registered.")
        return dict(row)

    @app.get("/api/knowledge")
    def list_knowledge(context: AuthContext = Depends(auth)) -> list[dict[str, str]]:
        with store.connection() as connection:
            rows = connection.execute("SELECT id, ciphertext, created_at, updated_at FROM knowledge WHERE owner_id=? ORDER BY updated_at DESC", (context.user_id,)).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/knowledge", status_code=201)
    def create_knowledge(payload: EncryptedKnowledgeRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        knowledge_id = str(uuid.uuid4())
        now = utc_now()
        with store.connection() as connection:
            connection.execute("INSERT INTO knowledge (id, owner_id, ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (knowledge_id, context.user_id, payload.ciphertext, now, now))
        return {"id": knowledge_id, "created_at": now, "updated_at": now}

    @app.get("/api/knowledge/{knowledge_id}")
    def get_knowledge(knowledge_id: str, context: AuthContext = Depends(auth)) -> dict[str, str]:
        with store.connection() as connection:
            row = owned_knowledge(connection, knowledge_id, context.user_id)
        return dict(row)

    @app.put("/api/knowledge/{knowledge_id}")
    def update_knowledge(knowledge_id: str, payload: EncryptedKnowledgeRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        now = utc_now()
        with store.connection() as connection:
            owned_knowledge(connection, knowledge_id, context.user_id)
            connection.execute("UPDATE knowledge SET ciphertext=?, updated_at=? WHERE id=? AND owner_id=?", (payload.ciphertext, now, knowledge_id, context.user_id))
        return {"id": knowledge_id, "updated_at": now}

    @app.delete("/api/knowledge/{knowledge_id}", status_code=204)
    def delete_knowledge(knowledge_id: str, context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            owned_knowledge(connection, knowledge_id, context.user_id)
            connection.execute("DELETE FROM knowledge WHERE id=? AND owner_id=?", (knowledge_id, context.user_id))
        return Response(status_code=204)

    @app.get("/api/secrets")
    def list_secrets(context: AuthContext = Depends(auth)) -> list[dict[str, str]]:
        with store.connection() as connection:
            rows = connection.execute("SELECT id, ciphertext, created_at, updated_at FROM secrets WHERE owner_id=? ORDER BY updated_at DESC", (context.user_id,)).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/secrets", status_code=201)
    def create_secret(payload: EncryptedSecretRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        try:
            uuid.UUID(payload.id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Secret ID must be a UUID.") from None
        now = utc_now()
        with store.connection() as connection:
            try:
                connection.execute("INSERT INTO secrets (id, owner_id, ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (payload.id, context.user_id, payload.ciphertext, now, now))
            except sqlite3.IntegrityError as error:
                raise HTTPException(status_code=409, detail="Secret ID already exists.") from error
        return {"id": payload.id, "created_at": now, "updated_at": now}

    @app.get("/api/secrets/{secret_id}")
    def get_secret(secret_id: str, context: AuthContext = Depends(auth)) -> dict[str, str]:
        with store.connection() as connection:
            row = owned_secret(connection, secret_id, context.user_id)
        return dict(row)

    @app.put("/api/secrets/{secret_id}")
    def update_secret(secret_id: str, payload: EncryptedSecretUpdateRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        now = utc_now()
        with store.connection() as connection:
            owned_secret(connection, secret_id, context.user_id)
            connection.execute("UPDATE secrets SET ciphertext=?, updated_at=? WHERE id=? AND owner_id=?", (payload.ciphertext, now, secret_id, context.user_id))
        return {"id": secret_id, "updated_at": now}

    @app.delete("/api/secrets/{secret_id}", status_code=204)
    def delete_secret(secret_id: str, context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            owned_secret(connection, secret_id, context.user_id)
            connection.execute("DELETE FROM secrets WHERE id=? AND owner_id=?", (secret_id, context.user_id))
        return Response(status_code=204)

    @app.get("/api/files")
    def list_files(context: AuthContext = Depends(auth)) -> list[dict[str, Any]]:
        with store.connection() as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.metadata_ciphertext, f.ciphertext_size, f.created_at, owner.username AS owner_username,
                       k.key_envelope, CASE WHEN f.owner_id=? THEN 1 ELSE 0 END AS is_owner
                FROM files f JOIN users owner ON owner.id=f.owner_id
                JOIN file_key_envelopes k ON k.file_id=f.id AND k.recipient_id=?
                WHERE f.content_uploaded=1 ORDER BY f.created_at DESC
                """,
                (context.user_id, context.user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/files", status_code=201)
    def create_file(payload: EncryptedFileRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        try:
            uuid.UUID(payload.id)
        except ValueError:
            raise HTTPException(status_code=422, detail="File ID must be a UUID.") from None
        if payload.ciphertext_size > settings.max_upload_bytes + 28:
            raise HTTPException(status_code=413, detail="Encrypted file exceeds the configured maximum size.")
        stored_name = payload.id.replace("-", "")
        with store.connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO files (id, owner_id, stored_name, metadata_ciphertext, ciphertext_size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (payload.id, context.user_id, stored_name, payload.metadata_ciphertext, payload.ciphertext_size, utc_now()),
                )
                connection.execute(
                    "INSERT INTO file_key_envelopes (file_id, recipient_id, key_envelope, created_at) VALUES (?, ?, ?, ?)",
                    (payload.id, context.user_id, payload.owner_key_envelope, utc_now()),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(status_code=409, detail="File ID already exists.") from error
        return {"id": payload.id}

    @app.put("/api/files/{file_id}/content", status_code=204)
    async def upload_content(file_id: str, request: Request, context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            row = connection.execute("SELECT * FROM files WHERE id=? AND owner_id=?", (file_id, context.user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File was not found.")
        if row["content_uploaded"]:
            raise HTTPException(status_code=409, detail="Encrypted file content was already uploaded.")
        declared = request.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) != row["ciphertext_size"]):
            raise HTTPException(status_code=422, detail="Encrypted file size does not match its reservation.")
        target = store.files_dir / row["stored_name"]
        size = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=store.files_dir, prefix=".upload-", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > row["ciphertext_size"]:
                        raise HTTPException(status_code=413, detail="Encrypted upload is larger than reserved.")
                    temporary.write(chunk)
            if size != row["ciphertext_size"]:
                raise HTTPException(status_code=422, detail="Encrypted upload is incomplete.")
            os.replace(temporary_path, target)
        except Exception:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        with store.connection() as connection:
            connection.execute("UPDATE files SET content_uploaded=1 WHERE id=? AND owner_id=?", (file_id, context.user_id))
        return Response(status_code=204)

    @app.get("/api/files/{file_id}/content")
    def download_content(file_id: str, context: AuthContext = Depends(auth)) -> FileResponse:
        with store.connection() as connection:
            row = accessible_file(connection, file_id, context.user_id)
        path = store.files_dir / row["stored_name"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Encrypted file content is unavailable.")
        return FileResponse(path, media_type="application/octet-stream", filename=f"{file_id}.bin")

    @app.delete("/api/files/{file_id}", status_code=204)
    def delete_file(file_id: str, context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            row = connection.execute("SELECT stored_name FROM files WHERE id=? AND owner_id=?", (file_id, context.user_id)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="File was not found.")
            connection.execute("DELETE FROM files WHERE id=? AND owner_id=?", (file_id, context.user_id))
        (store.files_dir / row["stored_name"]).unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/api/files/{file_id}/shares", status_code=201)
    def share_file(file_id: str, payload: EncryptedShareRequest, context: AuthContext = Depends(auth)) -> dict[str, str]:
        recipient = _username(payload.recipient_username)
        with store.connection() as connection:
            file_row = connection.execute("SELECT 1 FROM files WHERE id=? AND owner_id=? AND content_uploaded=1", (file_id, context.user_id)).fetchone()
            if file_row is None:
                raise HTTPException(status_code=404, detail="File was not found.")
            recipient_row = connection.execute("SELECT id FROM users WHERE username=?", (recipient,)).fetchone()
            if recipient_row is None:
                raise HTTPException(status_code=404, detail="Recipient is not registered.")
            if recipient_row["id"] == context.user_id:
                raise HTTPException(status_code=422, detail="You already own this file.")
            connection.execute(
                "INSERT INTO file_key_envelopes (file_id, recipient_id, key_envelope, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(file_id, recipient_id) DO UPDATE SET key_envelope=excluded.key_envelope, created_at=excluded.created_at",
                (file_id, recipient_row["id"], payload.key_envelope, utc_now()),
            )
        return {"file_id": file_id, "recipient_username": recipient}

    @app.delete("/api/files/{file_id}/shares/{username}", status_code=204)
    def revoke_share(file_id: str, username: str, context: AuthContext = Depends(auth)) -> Response:
        with store.connection() as connection:
            if connection.execute("SELECT 1 FROM files WHERE id=? AND owner_id=?", (file_id, context.user_id)).fetchone() is None:
                raise HTTPException(status_code=404, detail="File was not found.")
            recipient = connection.execute("SELECT id FROM users WHERE username=?", (_username(username),)).fetchone()
            if recipient is None or not connection.execute("DELETE FROM file_key_envelopes WHERE file_id=? AND recipient_id=?", (file_id, recipient["id"])).rowcount:
                raise HTTPException(status_code=404, detail="Share was not found.")
        return Response(status_code=204)

    return app


def json_load(value: str) -> dict[str, str]:
    try:
        result = json.loads(value)
    except ValueError as error:
        raise HTTPException(status_code=500, detail="Stored encryption metadata is invalid.") from error
    return result


app = create_app()
