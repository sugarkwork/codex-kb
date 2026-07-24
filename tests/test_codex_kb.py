from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_kb.cli import _migration_payload
from codex_kb.db import Artifact, KnowledgeBase, KnowledgeInput, discover_git
from codex_kb.mcp import call_tool, handle_request


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.kb = KnowledgeBase(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_finds_background_and_partial_japanese_phrase(self) -> None:
        record_id = self.kb.record(
            KnowledgeInput(
                kind="implementation",
                title="OAuth refresh token を単一フライト化",
                summary="期限切れ時の重複更新を抑える",
                purpose="ユーザーのログイン障害を減らす",
                background="複数タブが同時にログイン更新を実行していた",
                rationale="共有 Promise が最小の変更範囲で競合を防ぐ",
                tags=("auth", "oauth"),
                artifacts=(Artifact(path="src/auth/refresh.ts", symbol="refreshToken"),),
            )
        )

        results = self.kb.search("ログイン")

        self.assertEqual([item["id"] for item in results], [record_id])
        self.assertEqual(results[0]["purpose"], "ユーザーのログイン障害を減らす")
        self.assertEqual(results[0]["artifacts"][0]["symbol"], "refreshToken")

        symbol_results = self.kb.search("refreshToken")
        self.assertEqual([item["id"] for item in symbol_results], [record_id])

        intent_results = self.kb.search("ログイン障害を減らしたい")
        self.assertEqual([item["id"] for item in intent_results], [record_id])

    def test_session_lifecycle_keeps_stable_metadata(self) -> None:
        started = self.kb.start_session(
            {"session_id": "session-123", "cwd": self.temp_dir.name, "model": "gpt-test", "source": "startup"}
        )
        stopped = self.kb.stop_session({"session_id": "session-123", "cwd": self.temp_dir.name})

        self.assertEqual(started["session_id"], "session-123")
        self.assertEqual(stopped["session_id"], "session-123")
        self.assertEqual(self.kb.status()["sessions"], 1)

    def test_repository_filter_normalizes_windows_git_path_spellings(self) -> None:
        self.kb.record(
            KnowledgeInput(
                kind="note",
                title="リポジトリパスの正規化",
                summary="PowerShell と Git の区切り文字の違いを吸収する",
                repo_root=r"F:\\workspace\\example",
            )
        )
        # Simulate the v0.1.0 database entry written before normalization.
        with self.kb.connection() as connection:
            connection.execute("UPDATE knowledge SET repo_root = ?", (r"F:\\workspace\\example",))

        results = self.kb.search("正規化", repo_root="F:/workspace/example")

        self.assertEqual(len(results), 1)

    def test_discover_git_requests_utf8_output(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="F:/workspace/日本語\n",
            stderr="",
        )
        with patch("codex_kb.db.subprocess.run", return_value=completed) as run:
            metadata = discover_git("F:/workspace/日本語")

        self.assertIsNotNone(metadata["repo_root"])
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(call.kwargs["encoding"] == "utf-8" for call in run.call_args_list))
        self.assertTrue(all(call.kwargs["errors"] == "strict" for call in run.call_args_list))

    def test_discover_git_treats_decode_failure_as_missing_metadata(self) -> None:
        decode_error = UnicodeDecodeError("utf-8", b"\x81", 0, 1, "invalid start byte")
        with patch("codex_kb.db.subprocess.run", side_effect=decode_error):
            metadata = discover_git("F:/workspace")

        self.assertEqual(metadata, {"repo_root": None, "branch": None, "git_head": None})

    def test_update_replaces_knowledge_fields_and_artifacts(self) -> None:
        record_id = self.kb.record(
            KnowledgeInput(
                kind="implementation",
                title="初期タイトル",
                summary="初期要約",
                purpose="初期目的",
                artifacts=(Artifact(path="src/old.py", symbol="old"),),
            )
        )

        self.kb.update(
            record_id,
            KnowledgeInput(
                kind="decision",
                title="更新後タイトル",
                summary="更新後要約",
                purpose="更新後目的",
                background="更新理由",
                rationale="採用理由",
                outcome="検証済み",
                tags=("updated",),
                source_url="https://example.test/decision",
                artifacts=(Artifact(path="src/new.py", symbol="new"),),
            ),
        )

        updated = self.kb.get(record_id)
        self.assertEqual(updated["kind"], "decision")
        self.assertEqual(updated["title"], "更新後タイトル")
        self.assertEqual(updated["tags"], ["updated"])
        self.assertEqual(updated["artifacts"][0]["path"], "src/new.py")

    def test_all_knowledge_and_migration_payload_are_complete_and_stable(self) -> None:
        first = self.kb.record(
            KnowledgeInput(kind="note", title="最初", summary="一件目", tags=("one",), artifacts=(Artifact(path="first.py"),))
        )
        second = self.kb.record(KnowledgeInput(kind="decision", title="次", summary="二件目"))

        records = self.kb.all_knowledge()

        self.assertEqual([record["id"] for record in records], [first, second])
        payload = _migration_payload(records[0])
        self.assertEqual(payload["title"], "最初")
        self.assertIn(f"local-id:{first}", payload["tags"])
        self.assertEqual(payload["artifacts"][0]["path"], "first.py")

    def test_mcp_tools_search_and_record(self) -> None:
        response = handle_request(
            self.kb,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        )
        self.assertEqual(response["result"]["serverInfo"]["name"], "codex-kb")

        recorded = call_tool(
            self.kb,
            {
                "name": "record_knowledge",
                "arguments": {
                    "kind": "decision",
                    "title": "キャッシュを短く保つ",
                    "summary": "期限切れデータを避ける",
                    "purpose": "設定変更をすぐ反映する",
                    "background": "利用者は即時反映を期待する",
                    "rationale": "長いTTLより一貫性を優先する",
                },
            },
        )
        self.assertIn("Recorded knowledge #1", recorded["content"][0]["text"])

        found = call_tool(self.kb, {"name": "search_knowledge", "arguments": {"query": "即時反映"}})
        payload = json.loads(found["content"][0]["text"])
        self.assertEqual(payload[0]["title"], "キャッシュを短く保つ")


if __name__ == "__main__":
    unittest.main()
