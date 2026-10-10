import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database.db import Base, get_db


class FakeLLMResponse:
    def __init__(self, lines: list[str], fail_after_first: bool = False) -> None:
        self.lines = lines
        self.fail_after_first = fail_after_first

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for index, line in enumerate(self.lines):
            yield line
            if self.fail_after_first and index == 0:
                raise httpx.ReadError("simulated interrupted stream")


class FakeAsyncClient:
    request_payload: dict | None = None
    fail_after_first = False

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, _method: str, _url: str, *, json: dict, **_kwargs):
        type(self).request_payload = json
        chunks = [
            {"choices": [{"delta": {"content": "Bonjour"}}]},
            {"choices": [{"delta": {"content": " !"}}]},
            {"choices": [], "usage": {"total_tokens": 7}},
        ]
        lines = [f"data: {__import__('json').dumps(chunk)}" for chunk in chunks]
        lines.append("data: [DONE]")
        return FakeLLMResponse(lines, type(self).fail_after_first)


class ChatEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)

        def override_get_db():
            with self.sessions() as db:
                yield db

        main.app.dependency_overrides[get_db] = override_get_db
        self.session_local_patch = patch.object(main, "SessionLocal", self.sessions)
        self.session_local_patch.start()
        self.api_key_patch = patch.object(main, "RODIUMAI_API_KEY", "test-key")
        self.api_key_patch.start()
        self.client_patch = patch.object(main.httpx, "AsyncClient", FakeAsyncClient)
        self.client_patch.start()
        FakeAsyncClient.request_payload = None
        FakeAsyncClient.fail_after_first = False
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()
        main.app.dependency_overrides.clear()
        self.client_patch.stop()
        self.api_key_patch.stop()
        self.session_local_patch.stop()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def create_conversation(self) -> int:
        response = self.client.post("/conversations")
        self.assertEqual(response.status_code, 201)
        return response.json()["conversation_id"]

    def test_models_are_exposed_and_unlisted_model_is_rejected(self) -> None:
        models = self.client.get("/models").json()
        self.assertGreaterEqual(len(models["models"]), 2)
        self.assertIn(models["default"], models["models"])

        response = self.client.post(
            "/chat",
            json={
                "conversation_id": self.create_conversation(),
                "message": "Bonjour",
                "model": "untrusted/model",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_completed_stream_persists_turn_but_filters_private_note(self) -> None:
        conversation_id = self.create_conversation()
        note_response = self.client.post(
            f"/conversations/{conversation_id}/notes",
            json={"content": "À revoir après le cours"},
        )
        self.assertEqual(note_response.status_code, 201)

        response = self.client.post(
            "/chat",
            json={
                "conversation_id": conversation_id,
                "message": "Bonjour",
                "model": main.DEFAULT_MODEL,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("data: [DONE]", response.text)
        self.assertIn("event: usage", response.text)
        self.assertIn('{"content": "Bonjour"}', response.text)
        self.assertIn('{"content": " !"}', response.text)
        self.assertNotIn(
            "À revoir après le cours",
            json.dumps(FakeAsyncClient.request_payload["messages"]),
        )

        messages = self.client.get(f"/conversations/{conversation_id}/messages").json()
        self.assertEqual([message["role"] for message in messages], ["note", "user", "assistant"])
        self.assertEqual(messages[-1]["content"], "Bonjour !")

    def test_interrupted_stream_does_not_persist_partial_turn(self) -> None:
        conversation_id = self.create_conversation()
        FakeAsyncClient.fail_after_first = True

        response = self.client.post(
            "/chat",
            json={
                "conversation_id": conversation_id,
                "message": "Question interrompue",
                "model": main.DEFAULT_MODEL,
            },
        )
        self.assertIn("event: error", response.text)
        self.assertIn("Bonjour", response.text)
        self.assertEqual(
            self.client.get(f"/conversations/{conversation_id}/messages").json(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
