"""Command-line interface for codex-kb."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from . import e2e
from .db import Artifact, KnowledgeBase, KnowledgeInput, VALID_KINDS, discover_git, knowledge_from_mapping
from .mcp import serve
from .credentials import load_credentials, resolve_credentials_path, save_credentials
from .remote import (
    DEFAULT_REMOTE_URL,
    EncryptedKnowledgeClient,
    RemoteKnowledgeClient,
    create_remote_account,
    knowledge_payload,
    login_remote_account,
)


def _add_knowledge_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", choices=sorted(VALID_KINDS))
    parser.add_argument("--title")
    parser.add_argument("--summary")
    parser.add_argument("--purpose", default="", help="Why this work exists / the user value it serves.")
    parser.add_argument("--background", default="", help="Context or problem that motivated the work.")
    parser.add_argument("--rationale", default="", help="Why this approach was selected over alternatives.")
    parser.add_argument("--outcome", default="", help="Observed result, limitation, or follow-up.")
    parser.add_argument("--tag", action="append", default=[], help="Repeat for each tag.")
    parser.add_argument("--path", action="append", default=[], help="Repeat for each affected source path.")
    parser.add_argument("--symbol", help="Optional symbol applying to all --path values.")
    parser.add_argument("--line-start", type=int)
    parser.add_argument("--line-end", type=int)
    parser.add_argument("--git-commit")
    parser.add_argument("--excerpt", help="Short non-secret excerpt only; source code remains in Git.")
    parser.add_argument("--repo-root", help="Defaults to the current Git repository root when available.")
    parser.add_argument("--session-id")
    parser.add_argument("--source-url")
    parser.add_argument("--observed-at", help="ISO 8601 date/time when the fact was observed.")
    parser.add_argument("--effective-from", help="ISO 8601 date from which this applies.")
    parser.add_argument("--stdin-json", action="store_true", help="Read the complete record body as a JSON object from stdin.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-kb",
        description="A local, cross-project knowledge base for Codex.",
    )
    parser.add_argument("--home", type=Path, help="Database directory (default: ~/.codex-kb or CODEX_KB_HOME).")
    parser.add_argument("--version", action="version", version=f"codex-kb {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the database if it does not exist.")
    subparsers.add_parser("status", help="Show database location and record counts.")

    record = subparsers.add_parser("record", help="Record reusable knowledge.")
    _add_knowledge_options(record)

    update = subparsers.add_parser("update", help="Replace a complete existing knowledge record.")
    update.add_argument("id", type=int)
    _add_knowledge_options(update)

    search = subparsers.add_parser("search", help="Search by intent, background, rationale, code path, or keyword.")
    search.add_argument("query")
    search.add_argument("--repo-root", help="Restrict results to a repository.")
    search.add_argument("--current-repo", action="store_true", help="Restrict results to the Git repository of the current directory.")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="Show a full knowledge record.")
    show.add_argument("id", type=int)
    show.add_argument("--json", action="store_true")

    context = subparsers.add_parser("context", help="Show recent knowledge for a repository.")
    context.add_argument("--repo-root")
    context.add_argument("--current-repo", action="store_true")
    context.add_argument("--limit", type=int, default=3)
    context.add_argument("--json", action="store_true")

    session = subparsers.add_parser("session", help="Record stable metadata from a Codex lifecycle hook.")
    session_subparsers = session.add_subparsers(dest="session_command", required=True)
    session_subparsers.add_parser("start", help="Read a SessionStart hook event JSON from stdin.")
    session_subparsers.add_parser("stop", help="Read a Stop hook event JSON from stdin.")

    subparsers.add_parser("mcp", help="Run a local MCP stdio server.")

    remote = subparsers.add_parser("remote", help="Work with the authenticated shared codex-kb service.")
    remote.add_argument("--url", help="Service URL. Defaults to the saved credential profile, then https://kb.sugar-knight.com.")
    remote.add_argument("--token", default=os.environ.get("CODEX_KB_REMOTE_TOKEN"))
    remote.add_argument("--credentials", type=Path, help="Credential-file path. Defaults to .codex-kb-credentials.json in the current directory when present, otherwise ~/.codex-kb/remote-credentials.json.")
    remote_subparsers = remote.add_subparsers(dest="remote_command", required=True)
    remote_register = remote_subparsers.add_parser("register", help="Create an account and save a bearer token plus local E2E keys.")
    remote_register.add_argument("--username", required=True, help="3-32 lowercase letters, digits, '.', '_' or '-'.")
    remote_register.add_argument("--display-name", help="Shown to selected file recipients; defaults to the username.")
    remote_register.add_argument("--url", dest="url", default=argparse.SUPPRESS, help="Override the service URL for this registration.")
    remote_register.add_argument("--credentials", dest="credentials", type=Path, default=argparse.SUPPRESS, help="Save the new PC credential file at this path.")
    remote_login = remote_subparsers.add_parser("login", help="Sign in on this PC and save a new bearer token plus local E2E keys.")
    remote_login.add_argument("--username", required=True)
    remote_login.add_argument("--url", dest="url", default=argparse.SUPPRESS, help="Override the service URL for this login.")
    remote_login.add_argument("--credentials", dest="credentials", type=Path, default=argparse.SUPPRESS, help="Save this PC credential file at this path.")
    remote_subparsers.add_parser("logout", help="Revoke this PC's bearer token and remove its saved credential file.")
    remote_subparsers.add_parser("whoami", help="Verify remote credentials and show the signed-in user.")
    remote_key = remote_subparsers.add_parser("recipient-key", help="Show a registered user's X25519 public-key fingerprint for out-of-band verification.")
    remote_key.add_argument("username")
    remote_list = remote_subparsers.add_parser("list", help="List only your remote knowledge records.")
    remote_list.add_argument("--query", default="")
    remote_get = remote_subparsers.add_parser("get", help="Get one of your remote knowledge records.")
    remote_get.add_argument("id")
    remote_record = remote_subparsers.add_parser("record", help="Create a remote knowledge record.")
    _add_knowledge_options(remote_record)
    remote_update = remote_subparsers.add_parser("update", help="Replace a complete remote knowledge record.")
    remote_update.add_argument("id")
    _add_knowledge_options(remote_update)
    remote_delete = remote_subparsers.add_parser("delete", help="Delete one of your remote knowledge records.")
    remote_delete.add_argument("id")
    remote_import = remote_subparsers.add_parser("import-local", help="Encrypt and copy local knowledge to the remote account; safe to rerun.")
    remote_import.add_argument("--source-home", type=Path, help="Local codex-kb home to import; defaults to --home or ~/.codex-kb.")
    remote_import.add_argument("--dry-run", action="store_true", help="Report the migration plan without uploading records.")
    remote_file = remote_subparsers.add_parser("file", help="Upload, share, download, or remove end-to-end encrypted files.")
    remote_file_subparsers = remote_file.add_subparsers(dest="remote_file_command", required=True)
    remote_file_subparsers.add_parser("list", help="List files you own or that were shared with you.")
    remote_upload = remote_file_subparsers.add_parser("upload", help="Encrypt and upload a file.")
    remote_upload.add_argument("source", type=Path)
    remote_download = remote_file_subparsers.add_parser("download", help="Download and decrypt a file.")
    remote_download.add_argument("id")
    remote_download.add_argument("--output", type=Path, help="Output path; defaults to the encrypted metadata filename in the current directory.")
    remote_share = remote_file_subparsers.add_parser("share", help="Grant one registered user access by encrypting the file key for them.")
    remote_share.add_argument("id")
    remote_share.add_argument("username")
    remote_share.add_argument("--recipient-fingerprint", help="Optional expected X25519 public-key fingerprint, verified before sharing.")
    remote_revoke = remote_file_subparsers.add_parser("revoke", help="Revoke a recipient's future service access to a file.")
    remote_revoke.add_argument("id")
    remote_revoke.add_argument("username")
    remote_file_delete = remote_file_subparsers.add_parser("delete", help="Delete a file you own.")
    remote_file_delete.add_argument("id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    kb = KnowledgeBase(args.home)
    try:
        if args.command == "init":
            kb.initialize()
            print(f"Initialized {kb.db_path}")
            return 0
        if args.command == "status":
            print(json.dumps(kb.status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "record":
            return _record(kb, args)
        if args.command == "update":
            return _update(kb, args)
        if args.command == "search":
            return _search(kb, args)
        if args.command == "show":
            return _show(kb, args)
        if args.command == "context":
            return _context(kb, args)
        if args.command == "session":
            return _session(kb, args)
        if args.command == "mcp":
            kb.initialize()
            return serve(kb)
        if args.command == "remote":
            return _remote(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"codex-kb: {error}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


def _record(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    knowledge_id = kb.record(_knowledge_from_args(args))
    print(f"Recorded knowledge #{knowledge_id}.")
    return 0


def _update(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    kb.update(args.id, _knowledge_from_args(args))
    print(f"Updated knowledge #{args.id}.")
    return 0


def _knowledge_from_args(args: argparse.Namespace) -> KnowledgeInput:
    if args.stdin_json:
        return knowledge_from_mapping(_read_json_stdin())
    if not args.kind or not args.title or not args.summary:
        raise ValueError("--kind, --title, and --summary are required unless --stdin-json is used")
    repo_root = args.repo_root or discover_git(Path.cwd())["repo_root"]
    artifacts = tuple(
        Artifact(
            path=path,
            symbol=args.symbol,
            line_start=args.line_start,
            line_end=args.line_end,
            git_commit=args.git_commit,
            excerpt=args.excerpt,
        )
        for path in args.path
    )
    return KnowledgeInput(
        kind=args.kind,
        title=args.title,
        summary=args.summary,
        purpose=args.purpose,
        background=args.background,
        rationale=args.rationale,
        outcome=args.outcome,
        tags=tuple(args.tag),
        repo_root=repo_root,
        session_id=args.session_id,
        source_url=args.source_url,
        observed_at=args.observed_at,
        effective_from=args.effective_from,
        artifacts=artifacts,
    )


def _search(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    repo_root = args.repo_root
    if args.current_repo:
        repo_root = discover_git(Path.cwd())["repo_root"]
    results = kb.search(args.query, repo_root=repo_root, limit=args.limit)
    _print_results(results, as_json=args.json)
    return 0


def _show(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    result = kb.get(args.id)
    if result is None:
        print(f"Knowledge record #{args.id} was not found.", file=sys.stderr)
        return 1
    _print_results([result], as_json=args.json, detailed=True)
    return 0


def _context(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    repo_root = args.repo_root
    if args.current_repo:
        repo_root = discover_git(Path.cwd())["repo_root"]
    if not repo_root:
        raise ValueError("--repo-root or --current-repo is required")
    _print_results(kb.recent_for_repo(repo_root, limit=args.limit), as_json=args.json)
    return 0


def _session(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    event = _read_json_stdin()
    if args.session_command == "start":
        result = kb.start_session(event)
        context = (
            "codex-kb recorded this session. Before researching, designing, or changing code, "
            "use the codex-kb MCP search_knowledge tool to look for relevant prior knowledge. "
            f"Current session ID: {result['session_id']}."
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                },
                ensure_ascii=False,
            )
        )
    else:
        kb.stop_session(event)
    return 0


def _remote(args: argparse.Namespace) -> int:
    credential_path = resolve_credentials_path(args.credentials)
    if args.remote_command == "register":
        password = _prompt_new_secret("Account password")
        passphrase = _prompt_new_secret("Encryption passphrase")
        credentials = create_remote_account(_remote_url(args, {}), args.username, args.display_name or args.username, password, passphrase)
        save_credentials(credential_path, credentials)
        print(f"Registered {credentials['username']} and saved this PC's bearer token and E2E keys to {credential_path}.")
        return 0
    if args.remote_command == "login":
        password = _prompt_secret("Account password")
        passphrase = _prompt_secret("Encryption passphrase")
        credentials = login_remote_account(_remote_url(args, {}), args.username, password, passphrase)
        save_credentials(credential_path, credentials)
        print(f"Signed in as {credentials['username']} and saved this PC's bearer token and E2E keys to {credential_path}.")
        return 0

    credentials = load_credentials(credential_path) if credential_path.is_file() else {}
    client = RemoteKnowledgeClient(_remote_url(args, credentials), args.token or credentials.get("token", ""))
    if args.remote_command == "logout":
        client.delete("/api/auth/token")
        if not args.token and credential_path.is_file():
            credential_path.unlink()
            print(f"Revoked the bearer token and removed {credential_path}.")
        else:
            print("Revoked the supplied bearer token.")
        return 0
    if args.remote_command == "whoami":
        result = client.get("/api/auth/me")
    elif args.remote_command == "recipient-key":
        result = client.get(f"/api/users/{args.username}/encryption-key")
        result["fingerprint"] = e2e.public_key_fingerprint(result["public_key"])
    elif args.remote_command == "list":
        result = _encrypted_remote(client, credentials).list_knowledge(args.query)
    elif args.remote_command == "get":
        result = _encrypted_remote(client, credentials).get_knowledge(args.id)
    elif args.remote_command == "record":
        result = _encrypted_remote(client, credentials).create_knowledge(knowledge_payload(_knowledge_from_args(args)))
    elif args.remote_command == "update":
        result = _encrypted_remote(client, credentials).update_knowledge(args.id, knowledge_payload(_knowledge_from_args(args)))
    elif args.remote_command == "delete":
        _encrypted_remote(client, credentials).delete_knowledge(args.id)
        print(f"Deleted remote knowledge {args.id}.")
        return 0
    elif args.remote_command == "import-local":
        return _import_local_knowledge(_encrypted_remote(client, credentials), args)
    elif args.remote_command == "file":
        encrypted = _encrypted_remote(client, credentials)
        if args.remote_file_command == "list":
            result = encrypted.list_files()
        elif args.remote_file_command == "upload":
            result = encrypted.upload_file(args.source)
        elif args.remote_file_command == "download":
            output = encrypted.download_file(args.id, args.output)
            print(f"Downloaded and verified {args.id} to {output}.")
            return 0
        elif args.remote_file_command == "share":
            result = encrypted.share_file(args.id, args.username, args.recipient_fingerprint)
        elif args.remote_file_command == "revoke":
            encrypted.revoke_file_share(args.id, args.username)
            print(f"Revoked {args.username}'s service access to file {args.id}.")
            return 0
        elif args.remote_file_command == "delete":
            encrypted.delete_file(args.id)
            print(f"Deleted remote file {args.id}.")
            return 0
        else:
            raise ValueError(f"unknown remote file command: {args.remote_file_command}")
    else:  # argparse requires a subcommand; retain a defensive error for direct callers.
        raise ValueError(f"unknown remote command: {args.remote_command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _remote_url(args: argparse.Namespace, credentials: dict[str, Any]) -> str:
    return args.url or os.environ.get("CODEX_KB_REMOTE_URL") or str(credentials.get("server_url") or DEFAULT_REMOTE_URL)


def _encrypted_remote(client: RemoteKnowledgeClient, credentials: dict[str, Any]) -> EncryptedKnowledgeClient:
    if not credentials:
        raise ValueError("E2E operations require the saved credential file; run 'codex-kb remote login'")
    return EncryptedKnowledgeClient(client, credentials)


def _import_local_knowledge(remote: EncryptedKnowledgeClient, args: argparse.Namespace) -> int:
    """Copy complete local records once, using an encrypted origin tag as a resume marker."""
    source = KnowledgeBase(args.source_home or args.home)
    local_records = source.all_knowledge()
    remote_records = remote.list_knowledge()
    migrated_ids = {
        tag.removeprefix("local-id:")
        for record in remote_records
        for tag in record.get("tags", [])
        if isinstance(tag, str) and tag.startswith("local-id:")
    }
    pending = [record for record in local_records if str(record["id"]) not in migrated_ids]
    if args.dry_run:
        print(json.dumps({"local_records": len(local_records), "already_migrated": len(local_records) - len(pending), "would_upload": len(pending)}, ensure_ascii=False, indent=2))
        return 0
    for record in pending:
        remote.create_knowledge(_migration_payload(record))
    print(f"Imported {len(pending)} local knowledge record(s); skipped {len(local_records) - len(pending)} already migrated record(s).")
    return 0


def _migration_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Preserve all user fields while retaining a private, idempotent local origin ID."""
    tags = [str(tag) for tag in record.get("tags", [])]
    marker = f"local-id:{record['id']}"
    if marker not in tags:
        tags.append(marker)
    return {
        "kind": record["kind"],
        "title": record["title"],
        "summary": record["summary"],
        "purpose": record.get("purpose", ""),
        "background": record.get("background", ""),
        "rationale": record.get("rationale", ""),
        "outcome": record.get("outcome", ""),
        "tags": tags,
        "repo_root": record.get("repo_root") or "",
        "session_id": record.get("session_id") or "",
        "source_url": record.get("source_url") or "",
        "observed_at": record.get("observed_at") or "",
        "effective_from": record.get("effective_from") or "",
        "artifacts": record.get("artifacts", []),
    }


def _prompt_secret(label: str) -> str:
    value = getpass.getpass(f"{label}: ")
    if not value:
        raise ValueError(f"{label.lower()} cannot be empty")
    return value


def _prompt_new_secret(label: str) -> str:
    value = _prompt_secret(label)
    confirm = getpass.getpass(f"Confirm {label.lower()}: ")
    if value != confirm:
        raise ValueError(f"{label.lower()} entries do not match")
    return value


def _read_json_stdin() -> dict[str, Any]:
    # Windows PowerShell can emit UTF-8 bytes while Python configures the
    # redirected console stream as cp932. Reading bytes keeps JSON input
    # portable for both CLI piping and Codex hook events.
    raw = sys.stdin.buffer.read()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        # A legacy cmd.exe caller may still send the active Japanese code page.
        value = json.loads(raw.decode("cp932"))
    if not isinstance(value, dict):
        raise ValueError("stdin JSON must be an object")
    return value


def _print_results(results: list[dict[str, Any]], *, as_json: bool, detailed: bool = False) -> None:
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("No knowledge records found.")
        return
    for result in results:
        print(f"#{result['id']} [{result['kind']}] {result['title']}")
        print(f"  Summary: {result['summary']}")
        for label, key in (("Purpose", "purpose"), ("Background", "background"), ("Rationale", "rationale"), ("Outcome", "outcome")):
            if result.get(key):
                print(f"  {label}: {result[key]}")
        if result.get("tags"):
            print(f"  Tags: {', '.join(result['tags'])}")
        if result.get("session_id"):
            print(f"  Session: {result['session_id']}")
        if result.get("repo_root"):
            print(f"  Repo: {result['repo_root']}")
        if result.get("source_url"):
            print(f"  Source: {result['source_url']}")
        if result.get("artifacts"):
            for artifact in result["artifacts"]:
                location = artifact["path"]
                if artifact.get("line_start"):
                    location += f":{artifact['line_start']}"
                symbol = f" ({artifact['symbol']})" if artifact.get("symbol") else ""
                print(f"  Path: {location}{symbol}")
        if detailed:
            print(f"  Recorded: {result['recorded_at']}")
            if result.get("observed_at"):
                print(f"  Observed: {result['observed_at']}")
            if result.get("effective_from"):
                print(f"  Effective from: {result['effective_from']}")
        print()
