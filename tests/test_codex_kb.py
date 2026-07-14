from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_kb.db import Artifact, KnowledgeBase, KnowledgeInput
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

    def test_session_lifecycle_keeps_stable_metadata(self) -> None:
        started = self.kb.start_session(
            {"session_id": "session-123", "cwd": self.temp_dir.name, "model": "gpt-test", "source": "startup"}
        )
        stopped = self.kb.stop_session({"session_id": "session-123", "cwd": self.temp_dir.name})

        self.assertEqual(started["session_id"], "session-123")
        self.assertEqual(stopped["session_id"], "session-123")
        self.assertEqual(self.kb.status()["sessions"], 1)

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
