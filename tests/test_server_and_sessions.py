import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import request

from server import make_server
from sparky.curriculum import Curriculum


class ServerAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "", "SPARKY_MODEL_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

        self.server = make_server(
            port=0,
            db_path=Path(self.tmp.name) / "server.db",
            curriculum=Curriculum(),
            speech_key="test-speech-key",
            prewarm=False,
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get(self, path: str) -> dict:
        with request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return json.loads(resp.read())

    def _post_json(self, path: str, body: dict) -> dict:
        req = request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _post_ndjson(self, path: str, body: dict) -> list[dict]:
        req = request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=8) as resp:
            lines = [line.strip() for line in resp.read().decode("utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_bootstrap_and_streaming_voice_turn_pipeline(self):
        boot = self._get("/api/bootstrap")
        pid = boot["state"]["profile"]["id"]
        self.assertEqual(boot["state"]["session"]["session_num"], 1)
        self.assertTrue(boot["capabilities"]["voice_enabled"])

        with patch("server.speech.transcribe", return_value="Hi! My name is Leo and I love dinosaurs!"), patch(
            "server.speech.stream_tts_pcm", return_value=iter([b"\x10\x00" * 80, b"\x20\x00" * 80])
        ):
            events = self._post_ndjson(
                "/api/turn",
                {
                    "profile": pid,
                    "turn": "turn-voice-1",
                    "audio": base64.b64encode(b"webm-audio").decode("ascii"),
                    "media": "audio/webm",
                    "want_audio": True,
                },
            )

        types = [e["type"] for e in events]
        self.assertIn("transcript", types)
        self.assertIn("sentence", types)
        self.assertIn("audio_pcm", types)
        self.assertIn("state", types)
        self.assertEqual(types[-1], "complete")

        state_ev = next(e["state"] for e in events if e["type"] == "state")
        self.assertEqual(state_ev["inspector"]["memory"]["child_name"], "Leo")
        self.assertTrue(any("dinosaurs" in f["text"].lower() for f in state_ev["inspector"]["memory"]["facts"]))

    def test_cross_session_archival_and_snowball_greeting(self):
        boot = self._get("/api/bootstrap")
        pid = boot["state"]["profile"]["id"]

        # Turn 1: Share interest & ask for Chinese
        self._post_ndjson(
            "/api/turn",
            {"profile": pid, "turn": "s1-t1", "text": "My name is Zoe and I love building space rockets! Teach me Chinese!", "want_audio": False},
        )
        # Complete delivery of exposed target
        st1 = self._get(f"/api/state/{pid}")["state"]
        self._post_json("/api/delivery", {"profile": pid, "reply": st1["session"]["reply"]["id"], "status": "complete"})

        # Turn 2: Practice 你好
        self._post_ndjson("/api/turn", {"profile": pid, "turn": "s1-t2", "text": "你好!", "want_audio": False})

        # Simulate Next Day -> Session 2
        next_res = self._post_json("/api/session/next", {"profile": pid})
        s2 = next_res["state"]
        self.assertEqual(s2["session"]["session_num"], 2)
        self.assertEqual(len(s2["inspector"]["memory"]["episodic_summaries"]), 1)
        # Opening greeting remembers Zoe & space rockets and snowballs 你好!
        opening = s2["session"]["reply"]["text"]
        self.assertIn("Zoe", opening)
        self.assertIn("你好", opening)

    def test_barge_in_cancel_prevents_commit(self):
        boot = self._get("/api/bootstrap")
        pid = boot["state"]["profile"]["id"]

        entered = threading.Event()
        release = threading.Event()

        def slow_asr(*args, **kwargs):
            entered.set()
            release.wait(2.0)
            return "Late utterance"

        results: list[list[dict]] = []

        def run_turn():
            with patch("server.speech.transcribe", side_effect=slow_asr):
                results.append(
                    self._post_ndjson(
                        "/api/turn",
                        {
                            "profile": pid,
                            "turn": "cancelled-turn",
                            "audio": base64.b64encode(b"audio").decode(),
                            "media": "audio/webm",
                        },
                    )
                )

        worker = threading.Thread(target=run_turn)
        worker.start()
        self.assertTrue(entered.wait(1.5))
        self._post_json("/api/cancel", {"profile": pid, "turn": "cancelled-turn"})
        release.set()
        worker.join(timeout=3.0)

        self.assertEqual(results[0][-1]["type"], "cancelled")
        after = self._get(f"/api/state/{pid}")["state"]
        self.assertEqual(after["session"]["turn_count"], 0)

    def test_returning_dino_preset_and_hot_reload_endpoint(self):
        created = self._post_json("/api/profile", {"op": "create", "name": "Maya", "preset": "returning_dino"})
        self.assertEqual(created["state"]["session"]["session_num"], 2)
        self.assertEqual(created["state"]["inspector"]["progress"]["active_unit_id"], "u2_favorites")

        reloaded = self._post_json("/api/curriculum/reload", {"profile": created["state"]["profile"]["id"]})
        self.assertEqual(len(reloaded["curriculum"]["units"]), 4)


if __name__ == "__main__":
    unittest.main()
