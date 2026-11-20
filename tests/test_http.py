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

