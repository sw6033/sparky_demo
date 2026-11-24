"""Run locally; --cascade additionally requires requirements.txt."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from sparky import brain, live, speech, segments, diagnostics, access
from sparky.content import Catalog, ROOT
from sparky.store import Store, Missing, Conflict

ACTIONS={"start","resume","pause","finish","select","help","meaning","refuse","utterance","silence"}
def string(obj,key,limit,blank=False):
    v=obj.get(key)
    if not isinstance(v,str) or len(v)>limit or (not blank and not v.strip()):
        raise ValueError("字段无效："+key)
    return v.strip()

def validate_event(e):
    string(e,"id",100)
    if type(e.get("revision")) is not int or e["revision"]<0 or e.get("type") not in ACTIONS:
        raise ValueError("事件类型或版本无效。")
    if e["type"]=="start":
        if e.get("mode") not in ("A","B"):
            raise ValueError("活动类型无效。")
        string(e,"lesson",80)
    if e["type"]=="select":
        string(e,"item",80)
    if e["type"]=="utterance":
        string(e,"text",800,True)
        if e.get("source","text") not in ("text","voice"):
            raise ValueError("输入来源无效。")
        confidence=e.get("confidence",1)
        if type(confidence) not in (float,int) or not 0<=confidence<=1 or e.get("speaker","child") not in ("child","unknown"):
            raise ValueError("输入来源或质量无效。")

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def cascade_turn(self,family,pid,b):
        store=self.server.store
        turn=string(b,"turn",100)
        event={"id":turn,"revision":b.get("revision"),"type":"utterance","text":"", "source":"voice" if "audio" in b else "text"}
        validate_event(event)
        store.current_turn(family,pid,turn)
        if "audio" in b:
            encoded=string(b,"audio",4*1024*1024);media=string(b,"media",100)
        else:event["text"]=string(b,"text",800)
        receipts=b.get("receipts",[])
        if not isinstance(receipts,list) or len(receipts)>128:raise ValueError("无效播放回执。")
        for receipt in receipts:
            if not isinstance(receipt,dict):raise ValueError("无效播放回执。")
            string(receipt,"reply",64);string(receipt,"attempt",100)
            if receipt.get("status") not in {"started","complete","interrupted","failed"}:raise ValueError("无效播放回执。")
            part=receipt.get("segment")
            if part is not None and (type(part) is not int or part<0):raise ValueError("无效播放回执。")
        for receipt in receipts:
            store.delivery(family,pid,receipt["reply"],receipt["status"],receipt["attempt"],receipt.get("segment"))
        self.protocol_version="HTTP/1.1"
        self.close_connection=True
        self.send_response(200)
        self.send_header("Content-Type","application/x-ndjson")
        self.send_header("Transfer-Encoding","chunked")
        self.send_header("Connection","close")
        self.send_header("Cache-Control","no-store, no-transform")
        self.send_header("X-Content-Type-Options","nosniff")
        self.end_headers()
        def emit(value):
            data=(json.dumps(value,ensure_ascii=False)+"\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode()+data+b"\r\n");self.wfile.flush()
        started=time.monotonic()
        try:
            emit({"type":"stage","stage":"asr" if "audio" in b else "brain"})
            if "audio" in b:
                phase=time.monotonic()
                try:event["text"]=speech.transcribe(self.server.cascade_key,encoded,media)
                except speech.SpeechError:
                    store.trace(family,pid,turn,{"asr_ms":diagnostics.elapsed(phase),"asr_status":"failed"});raise
                store.trace(family,pid,turn,{"asr_ms":diagnostics.elapsed(phase),"asr_status":"ok"})
                store.current_turn(family,pid,turn)
                emit({"type":"transcript","text":event["text"]})
                if not event["text"].strip():
                    emit({"type":"stop","message":"没有听清，请重新开启麦克风或打字。"})
                elif re.search(r"(关闭语音|关闭麦克风)[。！!？?\s]*$",event["text"]):
                    emit({"type":"stop","message":"麦克风已关闭。"})
                else:event["source"]="voice"
                if not event["text"].strip() or re.search(r"(关闭语音|关闭麦克风)[。！!？?\s]*$",event["text"]):
                    self.wfile.write(b"0\r\n\r\n");self.wfile.flush();return
            validate_event(event)
            state,duplicate=store.event(family,pid,event)
            reply=segments.attach(state["reply"])
            emit({"type":"state","state":state,"duplicate":duplicate})
            part=None if reply["speech_mode"]=="whole" else 0
            text=reply["text"] if part is None else reply["segments"][0]["text"]
            store.current_turn(family,pid,turn,reply["id"])
            phase=time.monotonic();stage="tts" if part is None else "tts_0"
            try:audio=speech.synthesize(self.server.cascade_key,text)
            except speech.SpeechError:
                store.trace(family,pid,turn,{stage+"_ms":diagnostics.elapsed(phase),stage+"_status":"failed"});raise
            store.current_turn(family,pid,turn,reply["id"])
            store.trace(family,pid,turn,{stage+"_ms":diagnostics.elapsed(phase),stage+"_status":"ok","pipeline_ready_ms":diagnostics.elapsed(started)})
            emit({"type":"audio","reply":reply["id"],"segment":part,"audio":base64.b64encode(audio).decode()})
        except (Missing,Conflict,speech.SpeechError) as exc:
            emit({"type":"error","error":str(exc) or "档案不存在。","code":404 if isinstance(exc,Missing) else 409 if isinstance(exc,Conflict) else 502})
        except (ValueError,KeyError,TypeError):
            emit({"type":"error","error":"语音输入无效，请重试。","code":400})
        self.wfile.write(b"0\r\n\r\n");self.wfile.flush()

    def respond(self,status,data,media="application/json; charset=utf-8"):
        body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",media)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("X-Frame-Options","DENY")
        secure="; Secure" if self.server.gate.origin else ""
        if getattr(self,"access_cookie",None):
            self.send_header("Set-Cookie","sparky_access="+self.access_cookie+"; HttpOnly; SameSite=Strict; Path=/; Max-Age=7200"+secure)
        if getattr(self,"new_cookie",None):
            self.send_header("Set-Cookie","sparky_family="+self.new_cookie+"; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"+secure)
        self.end_headers()
        self.wfile.write(body)

    def family(self):
        c=SimpleCookie(self.headers.get("Cookie",""))
        token=c["sparky_family"].value if "sparky_family" in c else ""
        if not re.fullmatch(r"[a-f0-9]{64}",token):
            token=self.new_cookie=secrets.token_hex(32)
        return hashlib.sha256(token.encode()).hexdigest()

    def parent_check(self):
        pin=os.getenv("SPARKY_PARENT_PIN","")
        if pin and not hmac.compare_digest(self.headers.get("X-Parent-Pin","").encode(),pin.encode()):
            raise PermissionError("需要正确的家长 PIN。")

    def route(self):
        host=self.headers.get("Host","")
        gate=self.server.gate
        if host!=gate.host and urlsplit("//"+host).hostname not in {"localhost","127.0.0.1","::1"}:
            raise PermissionError("仅允许本机访问。")
        path=urlsplit(self.path).path
        origin=gate.origin if host==gate.host else "http://"+host
        if not gate.allow(self,path):return
        store=self.server.store
        if self.command=="GET":
            if path=="/api/home":
                if self.server.prewarm and self.server.cascade_key:speech.warm(self.server.cascade_key)
                family=self.family()
                return self.respond(200,{"lessons":store.catalog.listing(),"profiles":store.profiles(family),
                    "model":brain.configured(),"live":bool(self.server.live_key),
                    "cascade":bool(self.server.cascade_key),"pin":bool(os.getenv("SPARKY_PARENT_PIN"))})
            if match:=re.fullmatch(r"/api/state/([a-f0-9]{32})",path):
                return self.respond(200,store.state(self.family(),match[1]))
            assets={"/debug":("debug.html","text/html"),"/debug.js":("debug.js","application/javascript"),"/parent":("parent.html","text/html"),"/parent.js":("parent.js","application/javascript"),"/":("index.html","text/html"),"/app.js":("app.js","application/javascript"),
                "/transport.js":("transport.js","application/javascript"),"/live.js":("live.js","application/javascript"),"/cascade.js":("cascade.js","application/javascript"),"/style.css":("style.css","text/css")}
            if path in assets:
                name,mime=assets[path]
                return self.respond(200,(ROOT/"web"/name).read_bytes(),mime+"; charset=utf-8")
            raise Missing()
        if self.headers.get("Origin") and self.headers["Origin"]!=origin:
            raise PermissionError("来源无效。")
        length=int(self.headers.get("Content-Length","0"))
        limit=4*1024*1024+65536 if path in {"/api/transcribe","/api/cascade"} else (65536 if path=="/api/live" else 16384)
        if length<=0 or length>limit or self.headers.get_content_type()!="application/json":
            raise ValueError("请求格式无效。")
        b=json.loads(self.rfile.read(length))
        if not isinstance(b,dict):
            raise ValueError("请求必须是对象。")
        family=self.family()
        if path=="/api/profile":
            self.parent_check()
            return self.respond(201,{"id":store.create(family,string(b,"name",32))})
        pid=string(b,"profile",40)
        if path=="/api/cascade":
            if not self.server.cascade_key:return self.respond(503,{"error":"尚未开启 Cascade 语音。"})
            if self.headers.get("Origin")!=origin:raise PermissionError("来源无效。")
            return self.cascade_turn(family,pid,b)
        if path=="/api/cancel":
            store.cancel(family,pid,string(b,"turn",100))
            return self.respond(200,{"ok":True})
        if path in {"/api/transcribe", "/api/speech"}:
            if not self.server.cascade_key:
                return self.respond(503,{"error":"尚未开启 Cascade 语音。"})
            if self.headers.get("Origin")!=origin:
                raise PermissionError("来源无效。")
            snapshot=store.state(family,pid)
            turn=string(b,"turn",100) if b.get("turn") else secrets.token_hex(16)
            store.current_turn(family,pid,turn)
            started=time.monotonic()
            if path=="/api/transcribe":
                try:
                    text=speech.transcribe(self.server.cascade_key,string(b,"audio",4*1024*1024),string(b,"media",100))
                except speech.SpeechError:
                    store.trace(family,pid,turn,{"asr_ms":diagnostics.elapsed(started),"asr_status":"failed"})
                    raise
                values={"asr_ms":diagnostics.elapsed(started),"asr_status":"ok"}
                if os.getenv("SPARKY_DEBUG_TRANSCRIPTS")=="1":
                    values["asr_text"]=text
                store.trace(family,pid,turn,values)
                return self.respond(200,{"text":text})
            reply=string(b,"reply",64)
            if snapshot["reply"]["id"]!=reply:
                raise Conflict("回复已过期。")
            part=b.get("segment")
            parts=segments.attach(snapshot["reply"])["segments"]
            if part is not None and (type(part) is not int or not 0<=part<len(parts)):
                raise ValueError("无效语音分句。")
            text=snapshot["reply"]["text"] if part is None else parts[part]["text"]
            stage="tts" if part is None else f"tts_{part}"
            try:
                audio=speech.synthesize(self.server.cascade_key,text)
            except speech.SpeechError:
                store.trace(family,pid,turn,{stage+"_ms":diagnostics.elapsed(started),stage+"_status":"failed"})
                raise
            store.current_turn(family,pid,turn,reply)
            store.trace(family,pid,turn,{stage+"_ms":diagnostics.elapsed(started),stage+"_status":"ok"})
            return self.respond(200,audio,"audio/wav")
        if path=="/api/live":
            if not self.server.live_key:
                return self.respond(503,{"error":"本机尚未开启 GPT-Live 成人测试。"})
            if self.headers.get("Origin")!=origin:
                raise PermissionError("来源无效。")
            store.state(family,pid)
            sdp=b.get("sdp")
            if not isinstance(sdp,str) or not sdp.startswith("v=0\r\n"):
                raise ValueError("无效的语音连接请求。")
            return self.respond(*live.connect(self.server.live_key,sdp,store.catalog.conversation))
        if path=="/api/event":
            e=b.get("event")
            if not isinstance(e,dict):
                raise ValueError("缺少事件。")
            validate_event(e)
            state,duplicate=store.event(family,pid,e)
            return self.respond(200,{"state":state,"duplicate":duplicate})
        if path=="/api/delivery":
            if b.get("status") not in ("started","complete","interrupted","failed"):
                raise ValueError("播放状态无效。")
            store.delivery(family,pid,string(b,"reply",64),b["status"],string(b,"attempt",80),b.get("segment"))
            return self.respond(200,{"ok":True})
        if path=="/api/metrics":
            metrics=b.get("metrics")
            allowed={"endpoint_wait_ms","first_audio_ms","total_turn_ms","recording_ms","pipeline_transcript_ms","pipeline_state_ms","pipeline_audio_ms","pipeline_request_ms","audio_start_ms","sentence_gap_ms","sentence_gap_max_ms","client_queue_ms"}
            if not isinstance(metrics,dict) or any(k not in allowed or type(v) not in (int,float) or not 0<=v<=300000 for k,v in metrics.items()):
                raise ValueError("无效耗时数据。")
            store.trace(family,pid,string(b,"turn",100),{"client_metrics":metrics})
            return self.respond(200,{"ok":True})
        if path=="/api/debug":
            self.parent_check()
            return self.respond(200,store.debug(family,pid))
        if path=="/api/parent":
            self.parent_check()
            op=b.get("op","summary")
            if op not in ("summary","delete","remember","forget","forget_context","learner"):
                raise ValueError("未知操作。")
            value=b.get("value") if op=="learner" else string(b,"value",80) if op in ("remember","forget") else ""
            return self.respond(200,store.parent(family,pid,op,value))
        raise Missing()

    def handle_request(self):
        try:
            self.route()
        except Missing:
            self.respond(404,{"error":"记录不存在。"})
        except Conflict as e:
            self.respond(409,{"error":str(e)})
        except PermissionError as e:
            self.respond(403,{"error":str(e)})
        except speech.SpeechError as e:
            self.respond(502,{"error":str(e)})
        except (ValueError,KeyError,TypeError):
            self.respond(400,{"error":"操作或输入无效，请检查当前活动。"})
        except (BrokenPipeError,ConnectionResetError):
            pass
    do_GET=handle_request
    do_POST=handle_request

class LocalServer(ThreadingHTTPServer):
    def server_close(self):
        if getattr(self,"store",None):
            self.store.close()
        super().server_close()

def make_server(port,db_path,catalog=None,live_key=None,cascade_key=None,public_url="",access_token="",prewarm=False):
    gate=access.Gate(public_url,access_token)
    server=LocalServer(("127.0.0.1",port),Handler)
    server.gate=gate
    server.store=Store(db_path,catalog or Catalog())
    server.live_key=live_key
    server.cascade_key=cascade_key
    server.prewarm=prewarm
    return server

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int)
    parser.add_argument("--db")
    parser.add_argument("--public-url",default="",help="Quick Tunnel 的完整 HTTPS 根地址；将隐藏询问试用口令")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--live-test",action="store_true",help="成人模拟 GPT-Live 语音测试")
    modes.add_argument("--cascade",action="store_true",help="OpenAI ASR → C/A/B → TTS")
    parser.add_argument("--curriculum",default=str(ROOT/"curriculum"),type=type(ROOT))
    args=parser.parse_args()
    key=None
    if args.live_test or args.cascade:
        from getpass import getpass
        key=os.getenv("OPENAI_API_KEY") or getpass("输入新 OpenAI 项目密钥（隐藏输入）：").strip()
        if not key:
            parser.error("需要 OpenAI 项目密钥才能开启云端语音。")
    if args.cascade:
        try:
            import websocket
        except ImportError:
            parser.error("请先运行 python3 -m pip install -r requirements.txt。")
        if not os.getenv("SPARKY_MODEL_URL"):
            os.environ["SPARKY_MODEL_URL"]="https://api.openai.com/v1/chat/completions"
            os.environ.setdefault("SPARKY_MODEL","gpt-4.1")
            os.environ["SPARKY_MODEL_KEY"]=key
    port=args.port if args.port is not None else (8766 if args.live_test else 8765)
    db=args.db or ROOT/".data"/("adult-live-test.sqlite3" if args.live_test else "sparky.sqlite3")
    access_token=""
    if args.public_url:
        from getpass import getpass
        access_token=os.getenv("SPARKY_ACCESS_TOKEN") or getpass("试用访问口令（至少 16 字符，隐藏输入）：")
    server=make_server(port,db,Catalog(args.curriculum),live_key=key if args.live_test else None,
                       cascade_key=key if args.cascade else None,public_url=args.public_url,access_token=access_token,prewarm=args.cascade)
    print(f"Sparky  http://127.0.0.1:{server.server_port}",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

