from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_kb.cli import _environment_secrets, _migration_payload, build_parser
from codex_kb.credentials import load_credentials, save_credentials
from codex_kb.db import Artifact, KnowledgeBase, KnowledgeInput, discover_git
from codex_kb import e2e
from codex_kb.mcp import call_tool, handle_request
from codex_kb.remote import EncryptedKnowledgeClient


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

    def test_remote_login_can_read_both_secrets_from_explicit_environment_names(self) -> None:
        args = build_parser().parse_args(
            [
                "remote",
                "login",
                "--username",
                "alice",
                "--password-env",
                "TEST_ACCOUNT_PASSWORD",
                "--passphrase-env",
                "TEST_ENCRYPTION_PASSPHRASE",
            ]
        )
        with patch.dict(
            "os.environ",
            {"TEST_ACCOUNT_PASSWORD": "test password", "TEST_ENCRYPTION_PASSPHRASE": "test passphrase"},
            clear=False,
        ):
            self.assertEqual(_environment_secrets(args), ("test password", "test passphrase"))

        args.passphrase_env = None
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            _environment_secrets(args)

    def test_remote_secret_parser_requires_non_argument_secret_sources(self) -> None:
        parsed = build_parser().parse_args(["remote", "secret", "set", "OPENAI_API_KEY", "--value-env", "TEMP_API_KEY"])
        self.assertEqual(parsed.remote_secret_command, "set")
        self.assertEqual(parsed.name, "OPENAI_API_KEY")
        self.assertEqual(parsed.value_env, "TEMP_API_KEY")
        run = build_parser().parse_args(["remote", "secret", "run", "OPENAI_API_KEY", "--", "python", "-V"])
        self.assertEqual(run.child_command, ["python", "-V"])

    def test_credentials_round_trip_without_plaintext_on_windows(self) -> None:
        path = Path(self.temp_dir.name) / "remote-credentials.json"
        credentials = {
            "server_url": "https://kb.example.test",
            "token": "unit-test-token-that-must-not-be-plain",
            "vault_key": "vault-key",
            "private_key": "private-key",
            "public_key": "public-key",
            "username": "alice",
        }
        save_credentials(path, credentials)
        self.assertEqual(load_credentials(path), credentials)
        if os.name == "nt":
            self.assertNotIn(credentials["token"].encode("utf-8"), path.read_bytes())

    def test_remote_secret_values_are_encrypted_and_not_returned_by_list(self) -> None:
        class FakeApi:
            def __init__(self) -> None:
                self.rows: dict[str, dict[str, str]] = {}

            def get(self, path: str) -> list[dict[str, str]]:
                self.assert_path(path)
                return list(self.rows.values())

            def post(self, path: str, payload: dict[str, str]) -> dict[str, str]:
                self.assert_path(path)
                secret_id = payload["id"]
                self.rows[secret_id] = {"id": secret_id, "ciphertext": payload["ciphertext"], "created_at": "now", "updated_at": "now"}
                return {"id": secret_id, "created_at": "now", "updated_at": "now"}

            def put(self, path: str, payload: dict[str, str]) -> dict[str, str]:
                secret_id = path.rsplit("/", 1)[-1]
                self.rows[secret_id]["ciphertext"] = payload["ciphertext"]
                self.rows[secret_id]["updated_at"] = "later"
                return {"id": secret_id, "updated_at": "later"}

            def delete(self, path: str) -> None:
                secret_id = path.rsplit("/", 1)[-1]
                del self.rows[secret_id]

            @staticmethod
            def assert_path(path: str) -> None:
                if path != "/api/secrets":
                    raise AssertionError(path)

        vault_key = b"v" * 32
        private_key = b"p" * 32
        api = FakeApi()
        remote = EncryptedKnowledgeClient(
            api,
            {"vault_key": e2e.b64encode(vault_key), "private_key": e2e.b64encode(private_key), "public_key": e2e.b64encode(b"q" * 32)},
        )

        stored = remote.set_secret("OPENAI_API_KEY", "unit-test-api-key-value")
        self.assertEqual(stored["name"], "OPENAI_API_KEY")
        self.assertNotIn("unit-test-api-key-value", next(iter(api.rows.values()))["ciphertext"])
        self.assertEqual(remote.list_secrets()[0]["name"], "OPENAI_API_KEY")
        self.assertNotIn("value", remote.list_secrets()[0])
        self.assertEqual(remote.secret_value("OPENAI_API_KEY"), "unit-test-api-key-value")
        remote.delete_secret("OPENAI_API_KEY")
        self.assertEqual(api.rows, {})

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
