"""Command-line interface for codex-kb."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .db import Artifact, KnowledgeBase, KnowledgeInput, VALID_KINDS, discover_git, knowledge_from_mapping
from .mcp import serve


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
    record.add_argument("--kind", choices=sorted(VALID_KINDS))
    record.add_argument("--title")
    record.add_argument("--summary")
    record.add_argument("--purpose", default="", help="Why this work exists / the user value it serves.")
    record.add_argument("--background", default="", help="Context or problem that motivated the work.")
    record.add_argument("--rationale", default="", help="Why this approach was selected over alternatives.")
    record.add_argument("--outcome", default="", help="Observed result, limitation, or follow-up.")
    record.add_argument("--tag", action="append", default=[], help="Repeat for each tag.")
    record.add_argument("--path", action="append", default=[], help="Repeat for each affected source path.")
    record.add_argument("--symbol", help="Optional symbol applying to all --path values.")
    record.add_argument("--line-start", type=int)
    record.add_argument("--line-end", type=int)
    record.add_argument("--git-commit")
    record.add_argument("--excerpt", help="Short non-secret excerpt only; source code remains in Git.")
    record.add_argument("--repo-root", help="Defaults to the current Git repository root when available.")
    record.add_argument("--session-id")
    record.add_argument("--source-url")
    record.add_argument("--observed-at", help="ISO 8601 date/time when the fact was observed.")
    record.add_argument("--effective-from", help="ISO 8601 date from which this applies.")
    record.add_argument("--stdin-json", action="store_true", help="Read the record body as a JSON object from stdin.")

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
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"codex-kb: {error}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


def _record(kb: KnowledgeBase, args: argparse.Namespace) -> int:
    if args.stdin_json:
        data = _read_json_stdin()
        knowledge_id = kb.record(knowledge_from_mapping(data))
    else:
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
        knowledge_id = kb.record(
            KnowledgeInput(
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
        )
    print(f"Recorded knowledge #{knowledge_id}.")
    return 0


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
