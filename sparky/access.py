"""Access gate for a temporary HTTPS demo; no API key is sent to the browser."""
import hashlib
import hmac
import json
import time
from collections import deque
from http.cookies import SimpleCookie
from threading import Lock
from urllib.parse import urlsplit

LOGIN=b'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sparky - \xe8\xaf\x95\xe7\x94\xa8</title><style>body{font:18px system-ui;max-width:32rem;margin:12vh auto;padding:24px;color:#203e35}input,button{font:inherit;padding:12px;margin:8px 0;box-sizing:border-box;width:100%}button{background:#246b51;color:white;border:0;border-radius:10px}</style><h1>Sparky</h1><p>Temporary private demo</p><form><label>\xe8\xaf\xb7\xe8\xbe\x93\xe5\x85\xa5\xe8\xaf\x95\xe7\x94\xa8\xe5\x8f\xa3\xe4\xbb\xa4<input type="password" autocomplete="current-password" required maxlength="128"></label><button>\xe8\xbf\x9b\xe5\x85\xa5\xe5\xb0\x8f\xe5\xb1\x8b</button></form><p id="status" role="status"></p><script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/api/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.querySelector('input').value})});if(!r.ok)throw new Error((await r.json()).error);location.replace('/');}catch(e){document.getElementById('status').textContent=e.message;}};</script></html>'''

class Gate:
    def __init__(self,public_url="",token=""):
        parsed=urlsplit(public_url)
        if public_url and (parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("","/") or parsed.query or parsed.fragment or len(token)<16):
            raise ValueError("公开试用需要 HTTPS 根地址和至少 16 字符的口令")
        self.origin=public_url.rstrip("/")
        self.host=parsed.netloc
        self.token=token
        self.cookie=hmac.new(token.encode(),b"sparky-demo-session",hashlib.sha256).hexdigest() if token else ""
        self.calls={};self.lock=Lock()

    def limited(self,bucket,count,seconds):
        now=time.monotonic()
        with self.lock:
            calls=self.calls.setdefault(bucket,deque())
            while calls and calls[0]<=now-seconds:calls.popleft()
            if len(calls)>=count:return True
            calls.append(now);return False

    def allow(self,handler,path):
        if not self.origin:return True
        cookies=SimpleCookie(handler.headers.get("Cookie",""))
        value=cookies["sparky_access"].value if "sparky_access" in cookies else ""
        if path=="/api/unlock" and handler.command=="POST":
            if handler.headers.get("Origin")!=self.origin:raise PermissionError("来源无效。")
            if self.limited("unlock",20,60):handler.respond(429,{"error":"尝试过于频繁，请稍后再试。"});return False
            size=int(handler.headers.get("Content-Length","0"))
            if not 0<size<=1024:raise ValueError("无效请求")
            supplied=json.loads(handler.rfile.read(size)).get("token","")
            if not isinstance(supplied,str) or not hmac.compare_digest(supplied.encode(),self.token.encode()):raise PermissionError("试用口令不正确。")
            handler.access_cookie=self.cookie
            handler.respond(200,{"ok":True});return False
        if not hmac.compare_digest(value.encode(),self.cookie.encode()):
            if path=="/" and handler.command=="GET":handler.respond(200,LOGIN,"text/html; charset=utf-8")
            else:handler.respond(401,{"error":"请先回到首页输入试用口令。"})
            return False
        if handler.command=="POST" and path in {"/api/event","/api/transcribe","/api/speech"}:
            if self.limited("minute",60,60) or self.limited("hour",600,3600):
                handler.respond(429,{"error":"试用请求较多，请稍后再试。"});return False
        return True
