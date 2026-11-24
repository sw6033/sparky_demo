import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import request,error
from server import make_server
from sparky.access import Gate

class AccessTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.token="fixture-private-access-1234"
        self.origin="https://demo.example.test"
        self.server=make_server(0,Path(tmp.name)/"demo.db",public_url=self.origin,access_token=self.token)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base=f"http://127.0.0.1:{self.server.server_port}"
    def call(self,path,body=None,cookie="",origin=None,host="demo.example.test"):
        headers={"Host":host,"Cookie":cookie,"Content-Type":"application/json"}
        if origin is not None:headers["Origin"]=origin
        return request.urlopen(request.Request(self.base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None))
    def test_gate_blocks_apis_and_unlock_cookie_is_secure(self):
        with self.call("/") as r:self.assertIn(b'password',r.read())
        with self.assertRaises(error.HTTPError) as e:self.call("/api/home")
        self.assertEqual(e.exception.code,401)
        with self.call("/api/unlock",{"token":self.token},origin=self.origin) as r:
            cookie=r.headers["Set-Cookie"]
            self.assertIn("Secure",cookie);self.assertIn("HttpOnly",cookie)
            self.assertNotIn(self.token,cookie)
        with self.call("/api/home",cookie=cookie.split(";",1)[0]) as r:self.assertEqual(json.load(r)["profiles"],[])
    def test_wrong_origin_host_or_key_cannot_unlock(self):
        for body,origin,host in [({"token":self.token},"https://evil.test","demo.example.test"),({"token":"wrong"},self.origin,"demo.example.test"),({"token":self.token},self.origin,"evil.test")]:
            with self.assertRaises(error.HTTPError) as e:self.call("/api/unlock",body,origin=origin,host=host)
            self.assertEqual(e.exception.code,403)
    def test_configuration_and_rate_limits(self):
        for url in ('http://demo.test','https://demo.test/path','https://user:pass@demo.test'):
            with self.assertRaises(ValueError):Gate(url,self.token)
        with self.assertRaises(ValueError):Gate(self.origin,"short")
        gate=self.server.gate
        self.assertFalse(gate.limited("test",1,60));self.assertTrue(gate.limited("test",1,60))
