from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codex_kb import e2e
from codex_kb.webapp import Settings, create_app


class SharedWebServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        self.app = create_app(Settings(data_dir=self.data_dir, max_upload_bytes=1024 * 1024))
        transport = httpx.ASGITransport(app=self.app)
        self.alice = httpx.AsyncClient(transport=transport, base_url="https://testserver")
        self.bob = httpx.AsyncClient(transport=transport, base_url="https://testserver")

    async def asyncTearDown(self) -> None:
        await self.alice.aclose()
        await self.bob.aclose()
        self.tempdir.cleanup()

    async def register(self, client: httpx.AsyncClient, username: str) -> dict[str, object]:
        vault_key, private_key, vault_envelope, encrypted_private_key, public_key = e2e.create_vault_material("long encryption test passphrase")
        response = await client.post(
            "/api/auth/register",
            json={
                "username": username,
                "display_name": username.title(),
                "password": "a safe test password",
                "vault_envelope": vault_envelope,
                "encrypted_private_key": encrypted_private_key,
                "public_key": public_key,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return {"token": response.json()["token"], "vault_key": vault_key, "private_key": private_key, "public_key": public_key}

    @staticmethod
    def auth(account: dict[str, object]) -> dict[str, str]:
        return {"Authorization": f"Bearer {account['token']}"}

    async def test_registration_needs_no_invite_and_knowledge_stays_opaque_and_owner_scoped(self) -> None:
        alice = await self.register(self.alice, "alice")
        bob = await self.register(self.bob, "bob")
        body = {"kind": "decision", "title": "VPS dump must not reveal this", "summary": "client encryption", "tags": ["e2e"]}
        ciphertext = e2e.encrypt_json(alice["vault_key"], body, aad=b"codex-kb knowledge v1")
        self.assertNotIn(body["title"], ciphertext)
        created = await self.alice.post("/api/knowledge", headers=self.auth(alice), json={"ciphertext": ciphertext})
        self.assertEqual(created.status_code, 201, created.text)
        knowledge_id = created.json()["id"]

        bob_list = await self.bob.get("/api/knowledge", headers=self.auth(bob))
        self.assertEqual(bob_list.status_code, 200)
        self.assertEqual(bob_list.json(), [])
        bob_read = await self.bob.get(f"/api/knowledge/{knowledge_id}", headers=self.auth(bob))
        self.assertEqual(bob_read.status_code, 404)

        listed = await self.alice.get("/api/knowledge", headers=self.auth(alice))
        self.assertEqual(listed.status_code, 200)
        stored = listed.json()[0]
        self.assertNotIn(body["title"], stored["ciphertext"])
        self.assertEqual(e2e.decrypt_json(alice["vault_key"], stored["ciphertext"], aad=b"codex-kb knowledge v1"), body)
        self.assertNotIn(body["title"].encode("utf-8"), (self.data_dir / "kb-service.sqlite3").read_bytes())

    async def test_secret_is_opaque_and_owner_scoped(self) -> None:
        alice = await self.register(self.alice, "alice")
        bob = await self.register(self.bob, "bob")
        secret_id = str(uuid.uuid4())
        value = {"name": "OPENAI_API_KEY", "value": "test-secret-value-that-must-not-reach-the-server"}
        ciphertext = e2e.encrypt_json(alice["vault_key"], value, aad=f"codex-kb secret v1 {secret_id}".encode())

        created = await self.alice.post("/api/secrets", headers=self.auth(alice), json={"id": secret_id, "ciphertext": ciphertext})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn(value["value"], ciphertext)

        bob_list = await self.bob.get("/api/secrets", headers=self.auth(bob))
        self.assertEqual(bob_list.status_code, 200)
        self.assertEqual(bob_list.json(), [])
        bob_read = await self.bob.get(f"/api/secrets/{secret_id}", headers=self.auth(bob))
        self.assertEqual(bob_read.status_code, 404)

        listed = await self.alice.get("/api/secrets", headers=self.auth(alice))
        self.assertEqual(listed.status_code, 200)
        stored = listed.json()[0]
        self.assertEqual(e2e.decrypt_json(alice["vault_key"], stored["ciphertext"], aad=f"codex-kb secret v1 {secret_id}".encode()), value)
        self.assertNotIn(value["value"].encode("utf-8"), (self.data_dir / "kb-service.sqlite3").read_bytes())

        updated_value = {"name": "OPENAI_API_KEY", "value": "rotated-test-secret-value"}
        updated_ciphertext = e2e.encrypt_json(alice["vault_key"], updated_value, aad=f"codex-kb secret v1 {secret_id}".encode())
        updated = await self.alice.put(f"/api/secrets/{secret_id}", headers=self.auth(alice), json={"ciphertext": updated_ciphertext})
        self.assertEqual(updated.status_code, 200, updated.text)
        removed = await self.alice.delete(f"/api/secrets/{secret_id}", headers=self.auth(alice))
        self.assertEqual(removed.status_code, 204, removed.text)

    async def test_file_is_opaque_and_requires_a_client_key_share(self) -> None:
        alice = await self.register(self.alice, "alice")
        bob = await self.register(self.bob, "bob")
        file_id = str(uuid.uuid4())
        file_key = b"x" * 32
        plaintext = b"a file that must stay opaque to the service"
        nonce = b"n" * 12
        encrypted_content = nonce + AESGCM(file_key).encrypt(nonce, plaintext, None)
        metadata = {"name": "private-plan.txt", "size_bytes": len(plaintext), "sha256": hashlib.sha256(plaintext).hexdigest()}
        metadata_ciphertext = e2e.encrypt_json(file_key, metadata, aad=f"codex-kb file metadata {file_id}".encode())
        reservation = await self.alice.post(
            "/api/files",
            headers=self.auth(alice),
            json={
                "id": file_id,
                "metadata_ciphertext": metadata_ciphertext,
                "owner_key_envelope": e2e.seal_for_recipient(file_key, alice["public_key"], file_id=file_id),
                "ciphertext_size": len(encrypted_content),
            },
        )
        self.assertEqual(reservation.status_code, 201, reservation.text)
        upload = await self.alice.put(f"/api/files/{file_id}/content", headers=self.auth(alice), content=encrypted_content)
        self.assertEqual(upload.status_code, 204, upload.text)

        denied = await self.bob.get(f"/api/files/{file_id}/content", headers=self.auth(bob))
        self.assertEqual(denied.status_code, 404)
        key_lookup = await self.alice.get("/api/users/bob/encryption-key", headers=self.auth(alice))
        shared = await self.alice.post(
            f"/api/files/{file_id}/shares",
            headers=self.auth(alice),
            json={"recipient_username": "bob", "key_envelope": e2e.seal_for_recipient(file_key, key_lookup.json()["public_key"], file_id=file_id)},
        )
        self.assertEqual(shared.status_code, 201, shared.text)
        downloaded = await self.bob.get(f"/api/files/{file_id}/content", headers=self.auth(bob))
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, encrypted_content)
        row = (await self.bob.get("/api/files", headers=self.auth(bob))).json()[0]
        opened_key = e2e.open_shared_file_key(row["key_envelope"], bob["private_key"], file_id=file_id)
        self.assertEqual(opened_key, file_key)
        self.assertEqual(e2e.decrypt_json(opened_key, row["metadata_ciphertext"], aad=f"codex-kb file metadata {file_id}".encode()), metadata)
        self.assertNotIn(plaintext, (self.data_dir / "files" / file_id.replace("-", "")).read_bytes())

        revoked = await self.alice.delete(f"/api/files/{file_id}/shares/bob", headers=self.auth(alice))
        self.assertEqual(revoked.status_code, 204)
        denied_after_revoke = await self.bob.get(f"/api/files/{file_id}/content", headers=self.auth(bob))
        self.assertEqual(denied_after_revoke.status_code, 404)
