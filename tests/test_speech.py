import base64
import io
import json
import types
import unittest
import wave
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sparky import speech


class SpeechTests(unittest.TestCase):
    def setUp(self):
        speech.connections.speech_pool.clear()
        self.addCleanup(speech.connections.speech_pool.clear)

    @patch("sparky.speech.connections.urlopen")
    def test_asr_uploads_audio_and_both_language_hints(self, upstream):
        upstream.return_value = io.BytesIO(json.dumps({"text": "我想画画 with blue"}).encode())
        result = speech.transcribe("fixture-key", base64.b64encode(b"recording").decode(), "audio/webm;codecs=opus")
        self.assertEqual(result, "我想画画 with blue")
        sent = upstream.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertIn(b"gpt-transcribe", sent.data)
        self.assertIn(b'filename="turn.webm"', sent.data)
        self.assertEqual(sent.data.count(b'name="languages[]"'), 2)
        self.assertNotIn(b"fixture-key", sent.data)

    @patch("sparky.speech.connections.urlopen")
    def test_bad_audio_never_reaches_upstream(self, upstream):
        for encoded, media in [("not base64!", "audio/wav"), ("", "audio/wav"), ("YWJj", "text/html")]:
            with self.assertRaises(ValueError):
                speech.transcribe("fixture", encoded, media)
        upstream.assert_not_called()

    @patch("sparky.speech.connections.urlopen")
    def test_asr_errors_do_not_leak_upstream_details(self, upstream):
        upstream.side_effect = HTTPError("", 401, "secret-key", {}, None)
        with self.assertRaises(speech.SpeechError) as caught:
            speech.transcribe("fixture", "YWJj", "audio/wav")
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def synthesize_fixture(self, spoken, status="completed"):
        connection = MagicMock()
        events = [
            {"type": "session.created"}, {"type": "session.updated"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x00\x01"*100).decode()},
            {"type": "response.output_audio_transcript.delta", "delta": spoken},
            {"type": "response.done", "response": {"status": status}},
        ]
        connection.recv.side_effect = [json.dumps(event) for event in events]
        module = types.SimpleNamespace(create_connection=MagicMock(return_value=connection), WebSocketException=ConnectionError)
        return connection, module

    def test_tts_reads_confirmed_text_and_returns_valid_wave(self):
        connection, module = self.synthesize_fixture("你想选蓝色吗？")
        with patch.dict("sys.modules", {"websocket": module}):
            result = speech.synthesize("fixture", "你想选蓝色吗？")
        with wave.open(io.BytesIO(result)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(sent[0]["session"]["tools"], [])
        self.assertEqual(sent[1]["response"]["conversation"], "none")
        script=sent[1]["response"]["input"][0]
        self.assertEqual(script["role"],"user")
        self.assertEqual(json.loads(script["content"][0]["text"]),{"script":"你想选蓝色吗？"})
        self.assertNotIn("你想选蓝色吗", sent[1]["response"]["instructions"])
        self.assertIn(speech.TTS_MODEL, module.create_connection.call_args.args[0])
        connection.close.assert_not_called()

    def test_tts_rejects_rewritten_or_missing_transcript(self):
        for spoken in ("你已经学会了！", "", "好的，我来读一下。你想选蓝色吗？"):
            connection, module = self.synthesize_fixture(spoken)
            with patch.dict("sys.modules", {"websocket": module}), self.assertRaises(speech.SpeechError):
                speech.synthesize("fixture", "你想选蓝色吗？")
            connection.close.assert_called_once()

    def test_failed_tts_is_not_delivered(self):
        connection, module = self.synthesize_fixture("你好", "failed")
        with patch.dict("sys.modules", {"websocket": module}), self.assertRaises(speech.SpeechError):
            speech.synthesize("fixture", "你好")
        connection.close.assert_called_once()

    def test_reuses_tts_connection_without_conversation_history(self):
        connection,module=self.synthesize_fixture("Hello.")
        def response(text):return [json.dumps(e) for e in [
            {"type":"response.output_audio.delta","delta":base64.b64encode(b'\x00\x01'*100).decode()},
            {"type":"response.output_audio_transcript.delta","delta":text},
            {"type":"response.done","response":{"status":"completed"}}]]
        connection.recv.side_effect=[json.dumps({"type":"session.updated"})]+response("Hello.")+response("你好。")
        with patch.dict("sys.modules",{"websocket":module}):
            speech.synthesize('fixture','Hello.');speech.synthesize('fixture','你好。')
        self.assertEqual(module.create_connection.call_count,1)
        sent=[json.loads(call.args[0]) for call in connection.send.call_args_list]
        creates=[e['response'] for e in sent if e['type']=='response.create']
        self.assertEqual(len(creates),2)
        for r in creates:self.assertEqual(r['conversation'],'none');self.assertEqual(len(r['input']),1)
        self.assertEqual(json.loads(creates[1]['input'][0]['content'][0]['text']),{'script':'你好。'})

    def test_closed_idle_tts_connection_reconnects_once(self):
        old=MagicMock();old.send.side_effect=ConnectionResetError('closed')
        speech.connections.speech_pool.put(('fixture',speech.TTS_MODEL,'marin'),old)
        fresh,module=self.synthesize_fixture('Hello.')
        with patch.dict('sys.modules',{'websocket':module}):audio=speech.synthesize('fixture','Hello.')
        self.assertEqual(audio[:4],b'RIFF')
        old.close.assert_called_once();module.create_connection.assert_called_once()

    def test_warming_only_configures_session_and_first_reply_reuses_it(self):
        connection,module=self.synthesize_fixture('Hello.')
        with patch.dict('sys.modules',{'websocket':module}):
            speech.warm_speech('fixture')
            self.assertEqual([json.loads(c.args[0])['type'] for c in connection.send.call_args_list],['session.update'])
            speech.synthesize('fixture','Hello.')
        self.assertEqual(module.create_connection.call_count,1)
