"""Sparky POC v2 Server: Streaming Cascade (ASR -> Streaming Brain -> Streaming PCM TTS)."""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from sparky import brain, diagnostics, segments, speech
from sparky.curriculum import Curriculum, ROOT
from sparky.store import Conflict, Missing, Store


def _str(obj: dict, key: str, limit: int, allow_blank: bool = False) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or len(val) > limit or (not allow_blank and not val.strip()):
        raise ValueError(f"字段无效: {key}")
    return val.strip()


class SparkyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def respond_json(self, status: int, data: object) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _begin_ndjson(self) -> callable:
        self.protocol_version = "HTTP/1.1"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.end_headers()
        write_lock = threading.Lock()

        def emit(payload: dict) -> None:
            raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            with write_lock:
                self.wfile.write(f"{len(raw):x}\r\n".encode("ascii") + raw + b"\r\n")
                self.wfile.flush()

        return emit

    def _finish_ndjson(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream_turn(self, body: dict) -> None:
        store: Store = self.server.store
        speech_key: str = self.server.speech_key
        pid = _str(body, "profile", 64)
        turn_id = _str(body, "turn", 100) if body.get("turn") else uuid.uuid4().hex
        want_audio = bool(body.get("want_audio", True) and speech_key)

        # Apply piggybacked playback receipts from the previous reply first
        receipts = body.get("receipts") or []
        if isinstance(receipts, list):
            for rec in receipts[:64]:
                if isinstance(rec, dict) and isinstance(rec.get("reply"), str) and isinstance(rec.get("status"), str):
                    seg = rec.get("segment")
                    store.record_delivery(pid, rec["reply"], rec["status"], seg if type(seg) is int else None)

        store.ensure_not_cancelled(pid, turn_id)
        has_audio = "audio" in body and body["audio"]
        if has_audio:
            encoded_audio = _str(body, "audio", 6 * 1024 * 1024)
            media = _str(body, "media", 100)
            user_text = ""
            source = "voice"
        else:
            encoded_audio = ""
            media = ""
            user_text = _str(body, "text", 800)
            source = "text"

        emit = self._begin_ndjson()
        turn_started = time.monotonic()
        timing: dict[str, float] = {}

        try:
            if has_audio:
                if not speech_key:
                    raise speech.SpeechError("服务端未配置 OpenAI 密钥，无法进行语音识别。")
                emit({"type": "stage", "stage": "asr"})
                pre_ctx = store.prepare_turn_context(pid, turn_id, "")
                t0 = time.monotonic()
                user_text = speech.transcribe(speech_key, encoded_audio, media, hotwords=pre_ctx.get("hotwords"))
                timing["asr_ms"] = diagnostics.elapsed(t0)
                store.ensure_not_cancelled(pid, turn_id)
                emit({"type": "transcript", "text": user_text, "asr_ms": timing["asr_ms"]})
                if not user_text.strip():
                    emit({"type": "empty", "message": "没有听清，再说一次吧。"})
                    self._finish_ndjson()
                    return

            ctx = store.prepare_turn_context(pid, turn_id, user_text)
            if ctx.get("duplicate"):
                emit({"type": "state", "state": ctx["public"], "duplicate": True})
                self._finish_ndjson()
                return

            emit({"type": "stage", "stage": "thinking", "focus": ctx["focus"]})

            # Overlap sentence-by-sentence TTS synthesis in a worker thread while Brain SSE finishes!
            tts_queue: queue.Queue[tuple[int, str] | None] = queue.Queue()
            cancelled_flag = threading.Event()
            tts_worker_error: list[str] = []

            def _tts_worker() -> None:
                first_pcm_logged = False
                while not cancelled_flag.is_set():
                    item = tts_queue.get()
                    if item is None:
                        return
                    seg_idx, seg_text = item
                    seg_t0 = time.monotonic()
                    try:
                        store.ensure_not_cancelled(pid, turn_id)
                        for pcm_chunk in speech.stream_tts_pcm(speech_key, seg_text):
                            if cancelled_flag.is_set():
                                return
                            store.ensure_not_cancelled(pid, turn_id)
                            if not first_pcm_logged:
                                first_pcm_logged = True
                                timing["server_first_pcm_ms"] = diagnostics.elapsed(turn_started)
                            emit(
                                {
                                    "type": "audio_pcm",
                                    "segment": seg_idx,
                                    "rate": speech.PCM_RATE,
                                    "pcm": base64.b64encode(pcm_chunk).decode("ascii"),
                                }
                            )
                        timing[f"tts_seg_{seg_idx}_ms"] = diagnostics.elapsed(seg_t0)
                        emit({"type": "segment_done", "segment": seg_idx})
                    except Conflict:
                        cancelled_flag.set()
                        return
                    except speech.SpeechError as exc:
                        tts_worker_error.append(str(exc))
                        emit({"type": "tts_warning", "segment": seg_idx, "error": str(exc)})
                        return

            worker = threading.Thread(target=_tts_worker, daemon=True) if want_audio else None
            if worker:
                worker.start()

            seg_counter = 0
            final_reply = ""
            final_meta: dict = {}
            brain_stats: dict = {}

            try:
                for event_kind, *payload in brain.stream_turn(user_text, ctx):
                    store.ensure_not_cancelled(pid, turn_id)
                    if event_kind == "sentence":
                        sent_text = payload[0]
                        emit({"type": "sentence", "segment": seg_counter, "text": sent_text})
                        if worker:
                            tts_queue.put((seg_counter, sent_text))
                        seg_counter += 1
                    elif event_kind == "done":
                        final_reply, final_meta, brain_stats = payload
            finally:
                if worker:
                    tts_queue.put(None)

            timing.update({k: v for k, v in brain_stats.items() if isinstance(v, (int, float))})
            committed_state = store.commit_turn(
                pid=pid,
                turn_id=turn_id,
                user_text=user_text,
                source=source,
                reply_text=final_reply,
                meta={**final_meta, "brain_source": brain_stats.get("source")},
                focus=ctx["focus"],
                timing=timing,
            )
            emit({"type": "state", "state": committed_state, "turn_id": turn_id})

            if worker:
                worker.join(timeout=45.0)
                if timing:
                    store.update_timing(pid, turn_id, timing)
            emit({"type": "complete", "turn_id": turn_id, "timing": timing})
        except Conflict as exc:
            cancelled_flag.set() if "cancelled_flag" in locals() else None
            emit({"type": "cancelled", "error": str(exc)})
        except (Missing, speech.SpeechError) as exc:
            emit({"type": "error", "error": str(exc), "code": 404 if isinstance(exc, Missing) else 502})
        except (ValueError, KeyError, TypeError) as exc:
            emit({"type": "error", "error": str(exc) or "输入参数无效。", "code": 400})
        except (BrokenPipeError, ConnectionResetError):
            return
        self._finish_ndjson()

    def _stream_greeting_speech(self, body: dict) -> None:
        store: Store = self.server.store
        speech_key: str = self.server.speech_key
        if not speech_key:
            return self.respond_json(503, {"error": "未配置语音合成密钥。"})
        pid = _str(body, "profile", 64)
        state = store.public_state(pid)
        reply = state["session"]["reply"]
        emit = self._begin_ndjson()
        try:
            for seg in reply.get("segments", []):
                idx = seg["index"]
                emit({"type": "sentence", "segment": idx, "text": seg["text"]})
                for pcm_chunk in speech.stream_tts_pcm(speech_key, seg["text"]):
                    emit(
                        {
                            "type": "audio_pcm",
                            "segment": idx,
                            "rate": speech.PCM_RATE,
                            "pcm": base64.b64encode(pcm_chunk).decode("ascii"),
                        }
                    )
                emit({"type": "segment_done", "segment": idx})
            emit({"type": "complete", "reply_id": reply["id"]})
        except speech.SpeechError as exc:
            emit({"type": "tts_warning", "error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        self._finish_ndjson()

    def route(self) -> None:
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise PermissionError("此 POC 服务默认仅监听本机。")

        path = urlsplit(self.path).path
        store: Store = self.server.store
        curriculum: Curriculum = self.server.curriculum

        if self.command == "GET":
            if path == "/api/bootstrap":
                if self.server.prewarm and self.server.speech_key:
                    speech.warm(self.server.speech_key)
                profiles = store.list_profiles()
                if not profiles:
                    initial_state = store.create_profile("Leo", preset="fresh")
                    profiles = store.list_profiles()
                else:
                    initial_state = store.public_state(profiles[0]["id"])
                _, model_name, _ = brain.model_config()
                return self.respond_json(
                    200,
                    {
                        "profiles": profiles,
                        "state": initial_state,
                        "traces": store.recent_traces(initial_state["profile"]["id"], limit=20),
                        "curriculum": curriculum.summary(),
                        "capabilities": {
                            "voice_enabled": bool(self.server.speech_key),
                            "brain_configured": brain.configured(),
                            "model": model_name if brain.configured() else "offline-fallback",
                            "asr_model": os.getenv("SPARKY_ASR_MODEL", speech.ASR_MODEL),
                            "tts_model": os.getenv("SPARKY_TTS_MODEL", speech.TTS_MODEL),
                        },
                    },
                )
            if m := re.fullmatch(r"/api/state/([a-f0-9]{32})", path):
                pid = m.group(1)
                return self.respond_json(
                    200,
                    {
                        "state": store.public_state(pid),
                        "traces": store.recent_traces(pid, limit=25),
                    },
                )
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
                "/voice.js": ("voice.js", "application/javascript; charset=utf-8"),
                "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            }
            if path in static_files:
                fname, mime = static_files[path]
                return self.respond_bytes(200, (ROOT / "web" / fname).read_bytes(), mime)
            raise Missing("路径不存在。")

        length = int(self.headers.get("Content-Length", "0"))
        max_len = 7 * 1024 * 1024 if path == "/api/turn" else 65536
        if length <= 0 or length > max_len:
            raise ValueError("请求体长度无效。")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象。")

        if path == "/api/turn":
            return self._stream_turn(body)
        if path == "/api/speech":
            return self._stream_greeting_speech(body)
        if path == "/api/profile":
            op = body.get("op", "create")
            if op == "delete":
                store.delete_profile(_str(body, "profile", 64))
                profiles = store.list_profiles()
                if not profiles:
                    state = store.create_profile("Leo", preset="fresh")
                    profiles = store.list_profiles()
                else:
                    state = store.public_state(profiles[0]["id"])
                return self.respond_json(200, {"profiles": profiles, "state": state, "traces": store.recent_traces(state["profile"]["id"])})
            preset = body.get("preset", "fresh")
            name = str(body.get("name", "")).strip()[:32]
            state = store.create_profile(name, preset=preset if preset in {"fresh", "returning_dino"} else "fresh")
            return self.respond_json(
                201,
                {
                    "profiles": store.list_profiles(),
                    "state": state,
                    "traces": store.recent_traces(state["profile"]["id"]),
                },
            )
        if path == "/api/session/next":
            pid = _str(body, "profile", 64)
            state = store.start_next_session(
                pid,
                summarizer=brain.summarize_session,
                opening_generator=brain.generate_session_opening,
            )
            return self.respond_json(
                200,
                {
                    "profiles": store.list_profiles(),
                    "state": state,
                    "traces": store.recent_traces(pid, limit=25),
                },
            )
        if path == "/api/cancel":
            store.cancel_turn(_str(body, "profile", 64), _str(body, "turn", 100))
            return self.respond_json(200, {"ok": True})
        if path == "/api/delivery":
            seg = body.get("segment")
            store.record_delivery(
                _str(body, "profile", 64),
                _str(body, "reply", 64),
                _str(body, "status", 24),
                seg if type(seg) is int else None,
            )
            return self.respond_json(200, {"ok": True})
        if path == "/api/metrics":
            metrics = body.get("metrics")
            if not isinstance(metrics, dict):
                raise ValueError("无效指标。")
            clean_metrics = {k: round(float(v), 1) for k, v in metrics.items() if isinstance(v, (int, float)) and 0 <= v <= 300000}
            store.update_timing(_str(body, "profile", 64), _str(body, "turn", 100), clean_metrics)
            return self.respond_json(200, {"ok": True})
        if path == "/api/memory":
            pid = _str(body, "profile", 64)
            op = _str(body, "op", 32)
            val = str(body.get("value", ""))[:120]
            state = store.mutate_memory(pid, op, val)
            return self.respond_json(200, {"state": state})
        if path == "/api/curriculum/reload":
            summary = curriculum.reload()
            pid = body.get("profile")
            state = store.public_state(pid) if isinstance(pid, str) and len(pid) == 32 else None
            return self.respond_json(200, {"curriculum": summary, "state": state})
        raise Missing("未知接口。")

    def handle_http(self) -> None:
        try:
            self.route()
        except Missing as exc:
            self.respond_json(404, {"error": str(exc) or "未找到资源。"})
        except Conflict as exc:
            self.respond_json(409, {"error": str(exc)})
        except PermissionError as exc:
            self.respond_json(403, {"error": str(exc)})
        except speech.SpeechError as exc:
            self.respond_json(502, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self.respond_json(400, {"error": str(exc) or "请求格式有误。"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = handle_http
    do_POST = handle_http


class SparkyServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self) -> None:
        if getattr(self, "store", None):
            self.store.close()
        super().server_close()


def make_server(
    port: int,
    db_path: Path | str,
    curriculum: Curriculum | None = None,
    speech_key: str = "",
    prewarm: bool = False,
) -> SparkyServer:
    curr = curriculum or Curriculum()
    srv = SparkyServer(("127.0.0.1", port), SparkyHandler)
    srv.curriculum = curr
    srv.store = Store(db_path, curr)
    srv.speech_key = speech_key
    srv.prewarm = prewarm
    return srv


def main() -> None:
    parser = argparse.ArgumentParser(description="Sparky v2 双语陪伴+教学 POC 服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / ".data" / "sparky_v2.sqlite3")
    parser.add_argument("--curriculum", type=Path, default=ROOT / "curriculum")
    parser.add_argument("--offline", action="store_true", help="强制离线规则模式（不调用 OpenAI）")
    args = parser.parse_args()

    key = ""
    if not args.offline:
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key and not brain.configured():
            try:
                from getpass import getpass

                entered = getpass("输入 OpenAI API Key（直接回车则使用本地离线演示模式）：").strip()
                if entered:
                    key = entered
            except (EOFError, KeyboardInterrupt):
                key = ""
        if key:
            os.environ.setdefault("OPENAI_API_KEY", key)
            os.environ.setdefault("SPARKY_MODEL_URL", "https://api.openai.com/v1/chat/completions")
            os.environ.setdefault("SPARKY_MODEL", "gpt-4.1")
            os.environ.setdefault("SPARKY_MODEL_KEY", key)
    else:
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["SPARKY_MODEL_URL"] = ""

    srv = make_server(args.port, args.db, Curriculum(args.curriculum), speech_key=key, prewarm=bool(key))
    mode_label = "OpenAI Streaming Cascade (ASR -> GPT-4.1 -> PCM TTS)" if key else "Local Offline Fallback"
    print(f"Sparky POC v2 running at http://127.0.0.1:{srv.server_port}  [{mode_label}]", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
