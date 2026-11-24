"""Exercise the real HTTP and storage stack, with a local model protocol fixture."""
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError
from server import make_server

class ModelFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data=json.loads(payload["messages"][-1]["content"])
        self.server.last=data
        proposal={"intent":"choose","item":"dog" if data["utterance"]=="会汪汪叫的那只" else "dragon"}
        body=json.dumps({"choices":[{"message":{"content":json.dumps(proposal)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):
        pass

class HTTPTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        def serve(server):
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server
        self.model=serve(ThreadingHTTPServer(("127.0.0.1",0),ModelFixture))
        self.server=serve(make_server(0,Path(tmp.name)/"app.sqlite3"))
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        env=patch.dict(os.environ,{"SPARKY_MODEL":"contract-test","SPARKY_MODEL_URL":f"http://127.0.0.1:{self.model.server_port}/generate","SPARKY_PARENT_PIN":"1234"})
        env.start()
        self.addCleanup(env.stop)
        self.client=request.build_opener(request.HTTPCookieProcessor(CookieJar()))
    def call(self,path,data=None,pin="1234"):
        r=request.Request(self.base+path,headers={"Content-Type":"application/json","X-Parent-Pin":pin},
                          data=json.dumps(data).encode() if data is not None else None)
        with self.client.open(r) as response:
            return json.load(response)
    def test_real_http_model_actions_evidence_replay_and_delete(self):
        home=self.call("/api/home")
        self.assertEqual(len(home["lessons"]),4)
        with self.assertRaises(HTTPError) as exc:
            self.call("/api/profile",{"name":"测试"},"wrong")
        self.assertEqual(exc.exception.code,403)
        pid=self.call("/api/profile",{"name":"测试"})["id"]
        self.call("/api/parent",{"profile":pid,"op":"remember","value":"喜欢小狗"})
        def send(**fields):
            rev=self.call("/api/state/"+pid)["revision"]
            payload={"profile":pid,"event":{"id":uuid.uuid4().hex,"revision":rev,**fields}}
            value=self.call("/api/event",payload)
            self.assertTrue(self.call("/api/event",payload)["duplicate"])
            return value["state"]
        send(type="start",lesson="animals",mode="B")
        rejected=send(type="utterance",text="会喷火的那只")
        self.assertEqual(rejected["active"]["step"],1)
        accepted=send(type="utterance",text="会汪汪叫的那只",source="voice",confidence=.92)
        self.assertEqual(accepted["active"]["options"][0]["id"],"dog")
        self.assertIn("喜欢小狗",self.model.last["context"]["confirmed_preferences"])
        help_id=send(type="help")["reply"]["id"]
        for status in ("started","complete"):
            self.call("/api/delivery",{"profile":pid,"reply":help_id,"status":status,"attempt":"playback"})
        send(type="utterance",text="这是小狗")
        summary=self.call("/api/parent",{"profile":pid})["observations"]
        self.assertEqual({r["kind"] for r in summary},{"uncertain","interpreted","imitation"})
        self.call("/api/parent",{"profile":pid,"op":"delete"})
        self.assertEqual(self.call("/api/home")["profiles"],[])

    def audio_call(self, path, data, origin=None):
        headers={"Content-Type":"application/json", "Origin":origin or self.base}
        req=request.Request(self.base+path,data=json.dumps(data).encode(),headers=headers)
        return self.client.open(req)

    @patch("server.speech.synthesize", return_value=b"RIFF-fixture")
    @patch("server.speech.transcribe", return_value="我想画画")
    def test_cascade_uses_committed_reply_and_preserves_delivery_evidence(self, asr, tts):
        self.server.cascade_key="fixture"
        self.assertTrue(self.call("/api/home")["cascade"])
        profile=self.call("/api/profile",{"name":"成人模拟"})["id"]
        with self.audio_call("/api/transcribe",{"profile":profile,"audio":"YWJj","media":"audio/webm"}) as response:
            text=json.load(response)["text"]
        state=self.call("/api/state/"+profile)
        self.assertIsNone(state["active"])
        state=self.call("/api/event",{"profile":profile,"event":{"id":"recorded-turn","revision":state["revision"],
            "type":"utterance","source":"voice","text":text}})["state"]
        self.assertEqual(state["active"]["lesson"],"color")
        reply=state["reply"]
        with self.audio_call("/api/speech",{"profile":profile,"reply":reply["id"],"text":"untrusted override"}) as response:
            self.assertEqual(response.headers["Content-Type"],"audio/wav")
            self.assertEqual(response.read(),b"RIFF-fixture")
        tts.assert_called_once_with("fixture",reply["text"])
        self.assertEqual(self.call("/api/state/"+profile)["reply"]["delivery"],"pending")
        for phase in ("started","interrupted"):
            self.call("/api/delivery",{"profile":profile,"reply":reply["id"],"attempt":"play-once","status":phase})
        self.assertEqual(self.call("/api/state/"+profile)["reply"]["delivery"],"interrupted")

    @patch("server.speech.synthesize",return_value=b"RIFF-fixture")
    @patch("server.speech.transcribe",return_value="No, I would like you to speak English.")
    def test_cascade_language_request_reaches_tts_in_english(self, asr, tts):
        self.server.cascade_key="fixture"
        pid=self.call("/api/profile",{"name":"语言回归"})["id"]
        for _ in range(2):
            with self.audio_call("/api/transcribe",{"profile":pid,"audio":"YWJj","media":"audio/webm"}) as response:
                text=json.load(response)["text"]
            state=self.call("/api/state/"+pid)
            state=self.call("/api/event",{"profile":pid,"event":{
                "id":uuid.uuid4().hex,"revision":state["revision"],"type":"utterance","source":"voice","text":text}})["state"]
            self.assertEqual(state["language_preference"],"english")
            self.assertIsNone(state["active"])
            with self.audio_call("/api/speech",{"profile":pid,"reply":state["reply"]["id"]}) as response:
                self.assertEqual(response.read(),b"RIFF-fixture")
            self.assertEqual(tts.call_args.args,("fixture",state["reply"]["text"]))
            self.assertTrue(tts.call_args.args[1].isascii())
            self.assertIn("I'll speak English",tts.call_args.args[1])
        self.assertEqual(self.call("/api/parent",{"profile":pid})["observations"],[])

    @patch("server.speech.synthesize",return_value=b"RIFF-part")
    def test_segment_speech_and_debug_auth(self, tts):
        self.server.cascade_key="fixture"
        pid=self.call("/api/profile",{"name":"分句测试"})["id"]
        state=self.call("/api/state/"+pid)
        state=self.call("/api/event",{"profile":pid,"event":{"id":"turn-debug","revision":state["revision"],"type":"utterance","text":"Speak English"}})["state"]
        reply=state["reply"]
        with self.audio_call("/api/speech",{"profile":pid,"reply":reply["id"],"segment":0,"turn":"turn-debug"}) as response:
            self.assertEqual(response.read(),b"RIFF-part")
        tts.assert_called_once_with("fixture",reply["segments"][0]["text"])
        with self.assertRaises(HTTPError) as exc:
            self.audio_call("/api/speech",{"profile":pid,"reply":reply["id"],"segment":100})
        self.assertEqual(exc.exception.code,400)
        self.call("/api/metrics",{"profile":pid,"turn":"turn-debug","metrics":{"first_audio_ms":321}})
        with self.assertRaises(HTTPError) as exc:
            self.call("/api/debug",{"profile":pid},"wrong")
        self.assertEqual(exc.exception.code,403)
        debug=self.call("/api/debug",{"profile":pid})
        self.assertIn("你是 Sparky",debug["prompt"])
        row=next(r for r in debug["turns"] if r["turn"]=="turn-debug")
        self.assertIn("tts_0_ms",row)
        self.assertEqual(row["client_metrics"]["first_audio_ms"],321)
        self.assertNotIn("input",row)
        self.assertNotIn("reply",row)
        foreign=request.build_opener(request.HTTPCookieProcessor(CookieJar()))
        req=request.Request(self.base+"/api/debug",data=json.dumps({"profile":pid}).encode(),headers={"Content-Type":"application/json","X-Parent-Pin":"1234"})
        with self.assertRaises(HTTPError) as exc:
            foreign.open(req)
        self.assertEqual(exc.exception.code,404)

    @patch("server.speech.synthesize")
    def test_cascade_rejects_bad_origin_foreign_profile_and_stale_reply(self, tts):
        self.server.cascade_key="fixture"
        profile=self.call("/api/profile",{"name":"成人模拟"})["id"]
        reply=self.call("/api/state/"+profile)["reply"]["id"]
        for data,origin,expected in [
            ({"profile":profile,"reply":reply},"https://example.com",403),
            ({"profile":"0"*32,"reply":reply},self.base,404),
            ({"profile":profile,"reply":"obsolete"},self.base,409),
        ]:
            with self.assertRaises(HTTPError) as caught:
                self.audio_call("/api/speech",data,origin)
            self.assertEqual(caught.exception.code,expected)
        tts.assert_not_called()

    @patch("server.speech.synthesize")
    def test_reply_changed_during_synthesis_is_discarded(self, tts):
        self.server.cascade_key="fixture"
        profile=self.call("/api/profile",{"name":"成人模拟"})["id"]
        snapshot=self.call("/api/state/"+profile)
        def change_reply(*args):
            self.call("/api/event",{"profile":profile,"event":{"id":"new-input","revision":snapshot["revision"],
                "type":"utterance","text":"你好"}})
            return b"stale-audio"
        tts.side_effect=change_reply
        with self.assertRaises(HTTPError) as caught:
            self.audio_call("/api/speech",{"profile":profile,"reply":snapshot["reply"]["id"]})
        self.assertEqual(caught.exception.code,409)

