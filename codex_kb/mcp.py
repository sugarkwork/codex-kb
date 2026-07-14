"""A minimal dependency-free MCP stdio server for codex-kb."""

from __future__ import annotations

import json
import sys
from typing import Any

from .db import KnowledgeBase, knowledge_from_mapping


PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}


def serve(kb: KnowledgeBase) -> int:
    """Serve JSON-RPC messages over stdin/stdout without log output on stdout."""
    for raw_line in sys.stdin.buffer:
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            _write_message(_error(None, -32700, f"MCP stdio must be UTF-8: {error}"))
            continue
        if not line:
            continue
        request: dict[str, Any] | None = None
        try:
            request = json.loads(line)
            response = handle_request(kb, request)
        except Exception as error:  # Keep the transport alive after one bad call.
            request_id = request.get("id") if isinstance(request, dict) else None
            response = _error(request_id, -32603, str(error))
        if response is not None:
            _write_message(response)
    return 0


def handle_request(kb: KnowledgeBase, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        requested_version = params.get("protocolVersion")
        negotiated_version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return _result(
            request_id,
            {
                "protocolVersion": negotiated_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "codex-kb", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        try:
            result = call_tool(kb, params)
            return _result(request_id, result)
        except ValueError as error:
            return _result(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
    return _error(request_id, -32601, f"method not found: {method}")


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_knowledge",
            "description": "Search prior implementations, research, decisions, purposes, backgrounds, and rationale. Use before researching, designing, or changing code. Results include session IDs and source paths.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language problem, intent, error, feature, or keyword."},
                    "repo_root": {"type": "string", "description": "Optional Git repository root to narrow the search."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["query"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "get_knowledge",
            "description": "Get one knowledge record and its source artifacts by numeric ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "required": ["id"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "record_knowledge",
            "description": "Persist a reusable finding. Record why the work exists (purpose/background) and why this approach was chosen (rationale), not only what changed. Do not record secrets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["implementation", "decision", "research", "incident", "note"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "purpose": {"type": "string"},
                    "background": {"type": "string"},
                    "rationale": {"type": "string"},
                    "outcome": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "repo_root": {"type": "string"},
                    "session_id": {"type": "string"},
                    "source_url": {"type": "string"},
                    "observed_at": {"type": "string", "description": "ISO 8601 date/time when the fact was observed."},
                    "effective_from": {"type": "string", "description": "ISO 8601 date from which this decision/behavior applies."},
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "symbol": {"type": "string"},
                                "line_start": {"type": "integer"},
                                "line_end": {"type": "integer"},
                                "git_commit": {"type": "string"},
                                "excerpt": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["kind", "title", "summary"],
            },
        },
    ]


def call_tool(kb: KnowledgeBase, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "search_knowledge":
        results = kb.search(
            str(arguments.get("query", "")),
            repo_root=_optional_str(arguments.get("repo_root")),
            limit=int(arguments.get("limit", 5)),
        )
        return {"content": [{"type": "text", "text": json.dumps(results, ensure_ascii=False, indent=2)}]}
    if name == "get_knowledge":
        try:
            knowledge_id = int(arguments.get("id"))
        except (TypeError, ValueError) as error:
            raise ValueError("id must be an integer") from error
        result = kb.get(knowledge_id)
        if result is None:
            return {"content": [{"type": "text", "text": f"knowledge record {knowledge_id} was not found"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
    if name == "record_knowledge":
        knowledge_id = kb.record(knowledge_from_mapping(arguments))
        return {"content": [{"type": "text", "text": f"Recorded knowledge #{knowledge_id}."}]}
    raise ValueError(f"unknown tool: {name}")


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _optional_str(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _write_message(message: dict[str, Any]) -> None:
    """MCP stdio is UTF-8 regardless of the active Windows console code page."""
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
