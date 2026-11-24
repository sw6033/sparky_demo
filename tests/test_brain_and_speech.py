import base64
import io
import json
import os
import struct
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sparky import brain, connections, speech


class BrainAndSpeechTests(unittest.TestCase):
    def setUp(self):
        connections.speech_pool.clear()
        self.addCleanup(connections.speech_pool.clear)

    def test_two_part_output_strips_parenthetical_pinyin_and_parses_meta(self):
        raw = (
            "Whoa, a dinosaur! In Chinese that is **恐龙** (kǒng lóng) — lóng means dragon!\n"
            "---META---\n"
            '{"move":"expose_in_context","cards":["voc_dino"],"observations":[],"memory_notes":["Likes T-Rex"]}'
        )
        spoken, meta = brain.parse_two_part_output(raw)
        self.assertNotIn("kǒng lóng", spoken)
        self.assertNotIn("**", spoken)
        self.assertIn("恐龙", spoken)
        self.assertEqual(meta["move"], "expose_in_context")
        self.assertEqual(meta["cards"], ["voc_dino"])

    def test_stream_turn_emits_first_sentence_before_meta_arrives(self):
        sse_lines = [
            b'data: {"choices":[{"delta":{"content":"First sentence is ready! "}}]}\n',
            'data: {"choices":[{"delta":{"content":"Second sentence has 恐龙. "}}]}\n'.encode("utf-8"),
            b'data: {"choices":[{"delta":{"content":"\\n---META---\\n{\\"move\\":\\"expose_in_context\\",\\"cards\\":[\\"voc_dino\\"]}"}}]}\n',
            b"data: [DONE]\n",
        ]

        class FakeResp:
            def __init__(self):
                self._lines = list(sse_lines)

            def readline(self):
                return self._lines.pop(0) if self._lines else b""

            def read(self):
                return b""

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        ctx = {
            "memory": {"child_name": "Leo", "confirmed_facts": [], "past_sessions": []},
            "recent_turns": [],
            "last_reply_delivery": {"delivery": "complete"},
            "focus": {"rhythm_mode": "bridge_ready", "primary_targets": []},
        }
        with patch.dict(os.environ, {"SPARKY_MODEL_URL": "https://api.openai.com/v1/chat/completions", "SPARKY_MODEL": "gpt-4.1", "SPARKY_MODEL_KEY": "k"}), patch(
            "sparky.connections.open_response", return_value=FakeResp()
        ):
            events = list(brain.stream_turn("Tell me about dinosaurs!", ctx))

        sentences = [e[1] for e in events if e[0] == "sentence"]
        done = [e for e in events if e[0] == "done"]
        self.assertEqual(sentences, ["First sentence is ready!", "Second sentence has 恐龙."])
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][2]["cards"], ["voc_dino"])

    @patch("sparky.speech.connections.urlopen")
    def test_asr_passes_both_languages_and_hotwords(self, mock_urlopen):
        mock_urlopen.return_value = io.BytesIO(json.dumps({"text": "我喜欢恐龙"}).encode("utf-8"))
        out = speech.transcribe(
            "test-key",
            base64.b64encode(b"fake-webm-bytes").decode("ascii"),
            "audio/webm;codecs=opus",
            hotwords=["你好", "恐龙"],
        )
        self.assertEqual(out, "我喜欢恐龙")
        req = mock_urlopen.call_args.args[0]
        self.assertIn(b"gpt-transcribe", req.data)
        self.assertEqual(req.data.count(b'name="languages[]"'), 2)
        self.assertIn("恐龙".encode("utf-8"), req.data)
        self.assertNotIn(b"test-key", req.data)

    @patch("sparky.speech.connections.urlopen")
    def test_asr_error_never_leaks_key(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError("", 401, "secret-token-xyz", {}, None)
        with self.assertRaises(speech.SpeechError) as ctx:
            speech.transcribe("secret-token-xyz", base64.b64encode(b"abc").decode(), "audio/wav")
        self.assertNotIn("secret-token-xyz", str(ctx.exception))

    def test_streaming_tts_yields_pcm_immediately_without_transcript_mute_bug(self):
        fake_ws = MagicMock()
        pcm_1 = b"\x01\x02" * 120
        pcm_2 = b"\x03\x04" * 120
        # Even if model's internal audio transcript differs slightly in punctuation/homophone, audio is NOT muted!
        ws_events = [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm_1).decode()},
            {"type": "response.output_audio_transcript.delta", "delta": "Slightly different transcript"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm_2).decode()},
            {"type": "response.done", "response": {"status": "completed"}},
        ]
        fake_ws.recv.side_effect = [json.dumps(e) for e in ws_events]
        with patch("sparky.speech._connect_ws", return_value=fake_ws):
            chunks = list(speech.stream_tts_pcm("key-1", "你好 Leo!"))
        self.assertEqual(chunks, [pcm_1, pcm_2])
        # Pooled for reuse!
        self.assertEqual(connections.speech_pool.available(("key-1", speech.TTS_MODEL, speech.DEFAULT_VOICE)), 1)

    def test_stdlib_websocket_frame_decode_and_ping_pong(self):
        class DummySock:
            def __init__(self, incoming: bytes):
                self.buf = io.BytesIO(incoming)
                self.sent = bytearray()

            def recv(self, n: int) -> bytes:
                return self.buf.read(n)

            def sendall(self, data: bytes) -> None:
                self.sent.extend(data)

            def close(self) -> None:
                pass

        ping_frame = bytes([0x89, 0x02]) + b"hi"
        hello_bytes = "你好".encode("utf-8")
        text_frame = bytes([0x81, len(hello_bytes)]) + hello_bytes
        sock = DummySock(ping_frame + text_frame)
        ws = connections.StdlibWebSocket(sock)  # type: ignore[arg-type]
        ws._buf = bytearray()
        msg = ws.recv()
        self.assertEqual(msg, "你好")
        # Verify pong (opcode 0xA -> 0x8A) was automatically sent back
        self.assertEqual(sock.sent[0], 0x8A)


if __name__ == "__main__":
    unittest.main()
