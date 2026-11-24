"""Exercise incremental HTTP delivery and cancellation across model stages."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError
from server import make_server
from sparky.speech import SpeechError

class StreamTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{"SPARKY_MODEL_URL":"","SPARKY_PARENT_PIN":""})
        env.start();self.addCleanup(env.stop)
        self.server=make_server(0,Path(tmp.name)/"stream.db",cascade_key="fixture")
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        self.client=request.build_opener(request.HTTPCookieProcessor(CookieJar()))
        with self.post("/api/profile",{"name":"stream fixture"}) as r:self.pid=json.load(r)["id"]
        with self.server.store.db() as db:self.family=db.execute("SELECT family FROM profiles WHERE id=?",(self.pid,)).fetchone()[0]
        self.payload={"profile":self.pid,"turn":"stream-turn","revision":0,"audio":"YWJj","media":"audio/webm"}
    def post(self,path,data,origin=None):
        return self.client.open(request.Request(self.base+path,data=json.dumps(data).encode(),headers={"Content-Type":"application/json","Origin":origin or self.base}),timeout=5)
    def records(self):
        with self.post("/api/cascade",self.payload) as r:
            self.assertEqual(r.headers["Transfer-Encoding"],"chunked")
            return [json.loads(line) for line in r]
    def cancel(self):self.server.store.cancel(self.family,self.pid,self.payload["turn"])

    @patch("server.speech.transcribe",return_value="Speak English")
    def test_state_arrives_before_tts_finishes_and_audio_is_committed_text(self,asr):
        entered=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        def synth(key,text):
            entered.set();release.wait(3);return b"RIFF-fixture"
        with patch("server.speech.synthesize",side_effect=synth) as tts:
            with self.post("/api/cascade",self.payload) as r:
                self.assertEqual(json.loads(r.readline())["type"],"stage")
                self.assertEqual(json.loads(r.readline())["type"],"transcript")
                state=json.loads(r.readline())["state"]
                self.assertTrue(entered.wait(1));self.assertFalse(release.is_set())
                self.assertEqual(state["reply"]["delivery"],"pending")
                release.set();audio=json.loads(r.readline())
                self.assertEqual(base64.b64decode(audio["audio"]),b"RIFF-fixture")
                self.assertEqual(audio["reply"],state["reply"]["id"])
                self.assertIsNone(audio["segment"])
                self.assertEqual(r.read(),b"")
            tts.assert_called_once_with("fixture",state["reply"]["text"])

    @patch("server.speech.synthesize")
    def test_cancel_during_asr_prevents_brain_commit_and_audio(self,tts):
        def transcribe(*args):self.cancel();return "Speak English"
        with patch("server.speech.transcribe",side_effect=transcribe):rows=self.records()
        self.assertEqual(rows[-1]["code"],409)
        self.assertFalse(any(x["type"]=="state" for x in rows))
        self.assertEqual(self.server.store.state(self.family,self.pid)["revision"],0)
        tts.assert_not_called()

    @patch("server.speech.transcribe",return_value="Speak English")
    def test_cancel_during_tts_discards_audio(self,asr):
        def synth(*args):self.cancel();return b"late audio"
        with patch("server.speech.synthesize",side_effect=synth):rows=self.records()
        self.assertEqual(rows[-1]["code"],409)
        self.assertFalse(any(x["type"]=="audio" for x in rows))
        self.assertEqual(self.server.store.state(self.family,self.pid)["reply"]["delivery"],"pending")

    @patch("server.speech.transcribe",return_value="Speak English")
    @patch("server.speech.synthesize",side_effect=SpeechError("speech mismatch"))
    def test_unverified_audio_is_never_delivered(self,tts,asr):
        rows=self.records()
        self.assertEqual(rows[-1]["code"],502)
        self.assertFalse(any(x["type"]=="audio" for x in rows))

    @patch("server.speech.synthesize")
    def test_silence_or_stop_does_not_create_reply(self,tts):
        for text in ("","关闭麦克风。"):
            with patch("server.speech.transcribe",return_value=text):rows=self.records()
            self.assertEqual(rows[-1]["type"],"stop")
        self.assertEqual(self.server.store.state(self.family,self.pid)["revision"],0)
        tts.assert_not_called()

    @patch("server.speech.transcribe")
    def test_rejects_foreign_origin_profile_and_cancelled_turn_before_asr(self,asr):
        with self.assertRaises(HTTPError) as err:self.post("/api/cascade",self.payload,"https://other.example")
        self.assertEqual(err.exception.code,403)
        with self.assertRaises(HTTPError) as err:self.post("/api/cascade",{**self.payload,"profile":"0"*32})
        self.assertEqual(err.exception.code,404)
        self.cancel()
        with self.assertRaises(HTTPError) as err:self.records()
        self.assertEqual(err.exception.code,409)
        asr.assert_not_called()

    @patch("server.speech.synthesize",return_value=b"RIFF-fixture")
    @patch("server.speech.transcribe",return_value="Speak English")
    def test_piggybacked_receipts_arrive_before_brain_and_late_duplicates_do_not_regress(self,asr,tts):
        first=next(r["state"] for r in self.records() if r["type"]=="state")
        rid=first["reply"]["id"]
        self.payload.update(turn="next",revision=first["revision"],receipts=[
            {"reply":rid,"attempt":"play","status":"started"},
            {"reply":rid,"attempt":"play","status":"interrupted"}])
        def check_before_brain(*args):
            self.assertEqual(self.server.store.state(self.family,self.pid)["reply"]["delivery"],"interrupted")
            self.server.store.delivery(self.family,self.pid,rid,"started","play")
            self.assertEqual(self.server.store.state(self.family,self.pid)["reply"]["delivery"],"interrupted")
            return "Speak English"
        asr.side_effect=check_before_brain
        self.assertEqual(self.records()[-1]["type"],"audio")
