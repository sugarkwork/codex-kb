"""SQLite storage and search for codex-kb.

The database intentionally lives outside Codex's own state directory.  Codex
state and transcript formats are implementation details; this module stores a
small, explicit, portable record of knowledge that the user chose to retain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_HOME = Path.home() / ".codex-kb"
VALID_KINDS = {"implementation", "decision", "research", "incident", "note"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_home() -> Path:
    """Return the data directory, allowing isolated tests via CODEX_KB_HOME."""
    import os

    configured = os.environ.get("CODEX_KB_HOME")
    return Path(configured).expanduser() if configured else DEFAULT_HOME


def discover_git(cwd: str | Path | None) -> dict[str, str | None]:
    """Best-effort Git metadata. A non-Git folder is a valid workspace."""
    if not cwd:
        return {"repo_root": None, "branch": None, "git_head": None}
    path = Path(cwd).expanduser()

    def git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *args],
                text=True,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = completed.stdout.strip()
        return value if completed.returncode == 0 and value else None

    return {
        "repo_root": normalize_repo_root(git("rev-parse", "--show-toplevel")),
        "branch": git("branch", "--show-current"),
        "git_head": git("rev-parse", "HEAD"),
    }


def normalize_repo_root(repo_root: str | None) -> str | None:
    """Use one repository identity across PowerShell, Git, and SQLite.

    Git commonly reports `F:/repo` on Windows while users or Hooks may supply
    `F:\\repo`.  Without normalization, a repository-scoped search silently
    misses records that came from the other spelling.
    """
    if not repo_root:
        return None
    try:
        normalized = Path(repo_root).expanduser().resolve(strict=False).as_posix()
    except OSError:
        normalized = str(repo_root).replace("\\", "/")
    return normalized.casefold() if os.name == "nt" else normalized


@dataclass(frozen=True)
class Artifact:
    path: str
    symbol: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    git_commit: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True)
class KnowledgeInput:
    kind: str
    title: str
    summary: str
    purpose: str = ""
    background: str = ""
    rationale: str = ""
    outcome: str = ""
    tags: tuple[str, ...] = ()
    repo_root: str | None = None
    session_id: str | None = None
    source_url: str | None = None
    observed_at: str | None = None
    effective_from: str | None = None
    artifacts: tuple[Artifact, ...] = ()


class KnowledgeBase:
    """A small, dependency-free SQLite knowledge base."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or default_home()).expanduser()
        self.db_path = self.home / "codex-kb.sqlite3"

    def connect(self) -> sqlite3.Connection:
        self.home.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self) -> Iterable[sqlite3.Connection]:
        """Commit or roll back, then release SQLite's Windows file handle."""
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    repo_root TEXT,
                    branch TEXT,
                    git_head TEXT,
                    model TEXT,
                    source TEXT,
                    transcript_path TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('implementation', 'decision', 'research', 'incident', 'note')),
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT '',
                    background TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    repo_root TEXT,
                    session_id TEXT,
                    source_url TEXT,
                    observed_at TEXT,
                    effective_from TEXT,
                    recorded_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY,
                    knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    symbol TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    git_commit TEXT,
                    excerpt TEXT,
                    excerpt_hash TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_repo_root ON knowledge(repo_root);
                CREATE INDEX IF NOT EXISTS idx_knowledge_session_id ON knowledge(session_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_knowledge_id ON artifacts(knowledge_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    title,
                    summary,
                    purpose,
                    background,
                    rationale,
                    outcome,
                    tags,
                    content='knowledge',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
                    INSERT INTO knowledge_fts(rowid, title, summary, purpose, background, rationale, outcome, tags)
                    VALUES (new.id, new.title, new.summary, new.purpose, new.background, new.rationale, new.outcome, new.tags_json);
                END;

                CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, summary, purpose, background, rationale, outcome, tags)
                    VALUES ('delete', old.id, old.title, old.summary, old.purpose, old.background, old.rationale, old.outcome, old.tags_json);
                END;

                CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, summary, purpose, background, rationale, outcome, tags)
                    VALUES ('delete', old.id, old.title, old.summary, old.purpose, old.background, old.rationale, old.outcome, old.tags_json);
                    INSERT INTO knowledge_fts(rowid, title, summary, purpose, background, rationale, outcome, tags)
                    VALUES (new.id, new.title, new.summary, new.purpose, new.background, new.rationale, new.outcome, new.tags_json);
                END;
                """
            )
            self._normalize_existing_repo_roots(connection)

    def start_session(self, event: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        session_id = _required_text(event, "session_id")
        cwd = _required_text(event, "cwd")
        git = discover_git(cwd)
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, cwd, repo_root, branch, git_head, model, source,
                    transcript_path, started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    cwd=excluded.cwd,
                    repo_root=excluded.repo_root,
                    branch=excluded.branch,
                    git_head=excluded.git_head,
                    model=excluded.model,
                    source=excluded.source,
                    transcript_path=excluded.transcript_path,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    cwd,
                    git["repo_root"],
                    git["branch"],
                    git["git_head"],
                    _optional_text(event, "model"),
                    _optional_text(event, "source"),
                    _optional_text(event, "transcript_path"),
                    now,
                    now,
                    now,
                ),
            )
        return {"session_id": session_id, "cwd": cwd, **git}

    def stop_session(self, event: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        session_id = _required_text(event, "session_id")
        cwd = _required_text(event, "cwd")
        git = discover_git(cwd)
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET cwd=?, repo_root=?, branch=?, git_head=?, ended_at=?, updated_at=?
                WHERE session_id=?
                """,
                (cwd, git["repo_root"], git["branch"], git["git_head"], now, now, session_id),
            )
        return {"session_id": session_id, "ended_at": now, **git}

    def record(self, item: KnowledgeInput) -> int:
        self.initialize()
        if item.kind not in VALID_KINDS:
            allowed = ", ".join(sorted(VALID_KINDS))
            raise ValueError(f"kind must be one of: {allowed}")
        if not item.title.strip() or not item.summary.strip():
            raise ValueError("title and summary are required")
        now = utc_now()
        tags = tuple(_clean_tag(tag) for tag in item.tags if _clean_tag(tag))
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO knowledge (
                    kind, title, summary, purpose, background, rationale, outcome,
                    tags_json, repo_root, session_id, source_url, observed_at,
                    effective_from, recorded_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.kind,
                    item.title.strip(),
                    item.summary.strip(),
                    item.purpose.strip(),
                    item.background.strip(),
                    item.rationale.strip(),
                    item.outcome.strip(),
                    json.dumps(tags, ensure_ascii=False),
                    normalize_repo_root(item.repo_root),
                    item.session_id,
                    item.source_url,
                    item.observed_at,
                    item.effective_from,
                    now,
                    now,
                ),
            )
            knowledge_id = int(cursor.lastrowid)
            for artifact in item.artifacts:
                excerpt = artifact.excerpt.strip() if artifact.excerpt else None
                excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else None
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        knowledge_id, path, symbol, line_start, line_end, git_commit, excerpt, excerpt_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        knowledge_id,
                        artifact.path,
                        artifact.symbol,
                        artifact.line_start,
                        artifact.line_end,
                        artifact.git_commit,
                        excerpt,
                        excerpt_hash,
                    ),
                )
        return knowledge_id

    def get(self, knowledge_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if row is None:
                return None
            return self._knowledge_dict(connection, row)

    def search(self, query: str, *, repo_root: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Search explicit fields first, then supplement FTS with substring matches.

        SQLite's stock tokenizer does not segment Japanese words. The LIKE pass
        deliberately covers partial Japanese queries such as "ログイン" against
        "ログイン障害" while FTS provides ranking for normal tokenized text.
        """
        self.initialize()
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("query is required")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        repo_root = normalize_repo_root(repo_root)

        tokens = _search_tokens(cleaned)
        fts_query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        candidates: dict[int, tuple[float, sqlite3.Row]] = {}
        with self.connection() as connection:
            if fts_query:
                where, params = _repo_filter(repo_root)
                rows = connection.execute(
                    f"""
                    SELECT k.*, bm25(knowledge_fts, 4.0, 3.0, 4.0, 4.0, 3.0, 2.0, 1.0) AS score
                    FROM knowledge_fts
                    JOIN knowledge k ON k.id = knowledge_fts.rowid
                    {where} {'AND' if where else 'WHERE'} knowledge_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (*params, fts_query, limit * 4),
                ).fetchall()
                for row in rows:
                    # bm25 scores are negative; higher is better after negation.
                    candidates[int(row["id"])] = (-float(row["score"]), row)

            # A substring pass is intentionally limited and parameterized. It
            # makes vague partial queries useful without parsing a transcript.
            where, params = _repo_filter(repo_root)
            text_fields = " || ' ' || ".join(
                ["title", "summary", "purpose", "background", "rationale", "outcome", "tags_json"]
            )
            like_clauses = " OR ".join(f"lower({text_fields}) LIKE lower(?)" for _ in tokens)
            if like_clauses:
                prefix = "WHERE" if not where else "AND"
                rows = connection.execute(
                    f"""
                    SELECT k.*, 0.0 AS score
                    FROM knowledge k
                    {where} {prefix} ({like_clauses})
                    ORDER BY recorded_at DESC
                    LIMIT ?
                    """,
                    (*params, *(f"%{token}%" for token in tokens), limit * 4),
                ).fetchall()
                for row in rows:
                    knowledge_id = int(row["id"])
                    fallback_score = _substring_score(row, tokens)
                    existing = candidates.get(knowledge_id)
                    if existing is None or fallback_score > existing[0]:
                        candidates[knowledge_id] = (fallback_score, row)

            artifact_fields = " || ' ' || ".join(
                ["a.path", "coalesce(a.symbol, '')", "coalesce(a.git_commit, '')", "coalesce(a.excerpt, '')"]
            )
            artifact_clauses = " OR ".join(f"lower({artifact_fields}) LIKE lower(?)" for _ in tokens)
            if artifact_clauses:
                where, params = _repo_filter(repo_root)
                prefix = "WHERE" if not where else "AND"
                rows = connection.execute(
                    f"""
                    SELECT k.*, 0.0 AS score
                    FROM artifacts a
                    JOIN knowledge k ON k.id = a.knowledge_id
                    {where} {prefix} ({artifact_clauses})
                    GROUP BY k.id
                    ORDER BY k.recorded_at DESC
                    LIMIT ?
                    """,
                    (*params, *(f"%{token}%" for token in tokens), limit * 4),
                ).fetchall()
                for row in rows:
                    knowledge_id = int(row["id"])
                    fallback_score = 6.0
                    existing = candidates.get(knowledge_id)
                    if existing is None or fallback_score > existing[0]:
                        candidates[knowledge_id] = (fallback_score, row)

            ordered = sorted(
                candidates.values(),
                key=lambda item: (item[0], item[1]["recorded_at"]),
                reverse=True,
            )[:limit]
            return [self._knowledge_dict(connection, row, score=score) for score, row in ordered]

    def recent_for_repo(self, repo_root: str | None, limit: int = 3) -> list[dict[str, Any]]:
        self.initialize()
        repo_root = normalize_repo_root(repo_root)
        if not repo_root:
            return []
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM knowledge
                WHERE repo_root = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (repo_root, limit),
            ).fetchall()
            return [self._knowledge_dict(connection, row) for row in rows]

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            sessions = int(connection.execute("SELECT count(*) FROM sessions").fetchone()[0])
            knowledge = int(connection.execute("SELECT count(*) FROM knowledge").fetchone()[0])
        return {"home": str(self.home), "database": str(self.db_path), "sessions": sessions, "knowledge": knowledge}

    def _knowledge_dict(self, connection: sqlite3.Connection, row: sqlite3.Row, *, score: float | None = None) -> dict[str, Any]:
        artifacts = [
            dict(artifact)
            for artifact in connection.execute(
                "SELECT path, symbol, line_start, line_end, git_commit, excerpt, excerpt_hash FROM artifacts WHERE knowledge_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        result["artifacts"] = artifacts
        if score is not None:
            result["score"] = round(score, 4)
        return result

    @staticmethod
    def _normalize_existing_repo_roots(connection: sqlite3.Connection) -> None:
        """Migrate records written before path normalization was introduced."""
        for table in ("sessions", "knowledge"):
            rows = connection.execute(
                f"SELECT rowid AS internal_rowid, repo_root FROM {table} WHERE repo_root IS NOT NULL"
            ).fetchall()
            for row in rows:
                normalized = normalize_repo_root(row["repo_root"])
                if normalized and normalized != row["repo_root"]:
                    connection.execute(
                        f"UPDATE {table} SET repo_root = ? WHERE rowid = ?", (normalized, row["internal_rowid"])
                    )


def knowledge_from_mapping(data: dict[str, Any]) -> KnowledgeInput:
    """Convert CLI/MCP JSON into the explicit persisted schema."""
    raw_artifacts = data.get("artifacts") or []
    artifacts: list[Artifact] = []
    for artifact in raw_artifacts:
        if not isinstance(artifact, dict) or not str(artifact.get("path", "")).strip():
            raise ValueError("each artifact needs a path")
        artifacts.append(
            Artifact(
                path=str(artifact["path"]),
                symbol=_optional_text(artifact, "symbol"),
                line_start=_optional_int(artifact, "line_start"),
                line_end=_optional_int(artifact, "line_end"),
                git_commit=_optional_text(artifact, "git_commit"),
                excerpt=_optional_text(artifact, "excerpt"),
            )
        )
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be an array")
    return KnowledgeInput(
        kind=_required_text(data, "kind"),
        title=_required_text(data, "title"),
        summary=_required_text(data, "summary"),
        purpose=_optional_text(data, "purpose") or "",
        background=_optional_text(data, "background") or "",
        rationale=_optional_text(data, "rationale") or "",
        outcome=_optional_text(data, "outcome") or "",
        tags=tuple(str(tag) for tag in raw_tags),
        repo_root=_optional_text(data, "repo_root"),
        session_id=_optional_text(data, "session_id"),
        source_url=_optional_text(data, "source_url"),
        observed_at=_optional_text(data, "observed_at"),
        effective_from=_optional_text(data, "effective_from"),
        artifacts=tuple(artifacts),
    )


def as_jsonable(item: KnowledgeInput) -> dict[str, Any]:
    result = asdict(item)
    result["tags"] = list(item.tags)
    result["artifacts"] = [asdict(artifact) for artifact in item.artifacts]
    return result


def _required_text(data: dict[str, Any], key: str) -> str:
    value = _optional_text(data, key)
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} must be an integer") from error


def _clean_tag(tag: str) -> str:
    return " ".join(tag.strip().split())


def _search_tokens(query: str) -> list[str]:
    raw_tokens = re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff-]+", query, flags=re.UNICODE)
    tokens: list[str] = []
    for token in raw_tokens:
        tokens.append(token)
        # SQLite's stock tokenizer does not split Japanese. A full natural
        # language query such as "別プロジェクトの過去実装を検索したい" should
        # still recall records that mention "別プロジェクト" or "過去実装".
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", token):
            tokens.extend(part for part in re.split(r"[のをにはがとでやもへ]+", token) if len(part) >= 2)
    return list(dict.fromkeys(token for token in tokens if token)) or [query]


def _repo_filter(repo_root: str | None) -> tuple[str, tuple[str, ...]]:
    if repo_root:
        return "WHERE k.repo_root = ?", (repo_root,)
    return "", ()


def _substring_score(row: sqlite3.Row, tokens: Iterable[str]) -> float:
    fields = ("title", "summary", "purpose", "background", "rationale", "outcome", "tags_json")
    weights = {"title": 5.0, "summary": 4.0, "purpose": 4.0, "background": 4.0, "rationale": 3.0, "outcome": 2.0, "tags_json": 2.0}
    score = 0.0
    for token in tokens:
        token_lower = token.lower()
        for field in fields:
            value = (row[field] or "").lower()
            if token_lower in value:
                score += weights[field]
    return score
