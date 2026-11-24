"""OpenAI Cascade speech layer: bilingual ASR + low-latency streaming PCM TTS."""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
import threading
import time
import wave
from typing import Iterator
from urllib import error, parse, request

from . import connections

ASR_MODEL = "gpt-transcribe"
TTS_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_VOICE = "marin"
PCM_RATE = 24000
MAX_AUDIO_BYTES = 4 * 1024 * 1024
FORMATS = {
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
}


class SpeechError(Exception):
    pass


def transcribe(key: str, encoded: str, media: str, hotwords: list[str] | None = None) -> str:
    base_media = (media or "").split(";", 1)[0].strip().lower()
    if base_media not in FORMATS:
        raise ValueError("不支持的录音格式。")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("无效录音数据。") from exc
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("录音为空或超过长度上限。")

    boundary = "sparky-" + secrets.token_hex(16)
    fields = [
        ("model", os.getenv("SPARKY_ASR_MODEL", ASR_MODEL)),
        ("languages[]", "en"),
        ("languages[]", "zh"),
    ]
    if hotwords and os.getenv("SPARKY_ASR_PROMPT", "1") != "0":
        hint = "English and Mandarin child conversation. Mandarin words: " + ", ".join(hotwords[:14])
        fields.append(("prompt", hint[:240]))

    parts: list[bytes] = []
    for name, val in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode("utf-8"))
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="turn.{FORMATS[base_media]}"\r\n'
        f"Content-Type: {base_media}\r\n\r\n".encode("utf-8")
    )
    parts.extend([audio, f"\r\n--{boundary}--\r\n".encode("utf-8")])

    req = request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with connections.urlopen(req, timeout=35.0) as resp:
            result = json.loads(resp.read())
        text = result.get("text")
        if not isinstance(text, str) or len(text) > 1000:
            raise ValueError("Invalid ASR text")
        return text.strip()
    except error.HTTPError as exc:
        raise SpeechError(f"语音识别请求失败（HTTP {exc.code}）。") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise SpeechError("语音识别暂时不可用，请重试或使用文字输入。") from None


def _connect_ws(url: str, key: str):
    """Use websocket-client if installed, otherwise fall back to zero-dependency StdlibWebSocket."""
    try:
        import websocket  # type: ignore

        return websocket.create_connection(
            url,
            header={"Authorization": f"Bearer {key}"},
            timeout=15,
            sslopt={"context": connections.tls_context()},
        )
    except ImportError:
        return connections.StdlibWebSocket.connect(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=15.0,
        )


def _open_realtime_tts(key: str, model: str, voice: str):
    url = "wss://api.openai.com/v1/realtime?model=" + parse.quote(model, safe="")
    ws = _connect_ws(url, key)
    try:
        ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["audio"],
                        "audio": {"output": {"format": {"type": "audio/pcm", "rate": PCM_RATE}, "voice": voice}},
                        "tools": [],
                    },
                }
            )
        )
        return ws
    except Exception:
        ws.close()
        raise


def stream_tts_pcm(key: str, text: str, _fresh: bool = False) -> Iterator[bytes]:
    """Stream raw 24kHz 16-bit mono PCM chunks immediately as OpenAI generates them."""
    clean_text = (text or "").strip()
    if not clean_text:
        return

    if os.getenv("SPARKY_TTS_BACKEND", "realtime") == "http":
        yield from _stream_http_speech_pcm(key, clean_text)
        return

    model = os.getenv("SPARKY_TTS_MODEL", TTS_MODEL)
    voice = os.getenv("SPARKY_TTS_VOICE", DEFAULT_VOICE)
    pool_key = (key, model, voice)
    ws = None if _fresh else connections.speech_pool.take(pool_key)
    reused = ws is not None
    yielded_any = False

    try:
        if not reused:
            ws = _open_realtime_tts(key, model, voice)
        deadline = time.monotonic() + 45.0
        requested = False
        total_bytes = 0

        while time.monotonic() < deadline:
            event = {"type": "session.updated"} if (reused and not requested) else json.loads(ws.recv())
            kind = event.get("type")
            if kind == "session.updated" and not requested:
                ws.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {
                                "conversation": "none",
                                "input": [
                                    {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": json.dumps({"script": clean_text}, ensure_ascii=False),
                                            }
                                        ],
                                    }
                                ],
                                "output_modalities": ["audio"],
                                "instructions": (
                                    "You are a warm, expressive bilingual voice actor reading Sparky's line to a 5-8 year old child. "
                                    "Read ONLY the exact 'script' value in the input JSON from first word to last. "
                                    "Never add extra greetings, explanations, or translations. "
                                    "Pronounce English naturally in English and Chinese characters clearly with accurate Mandarin tones."
                                ),
                            },
                        }
                    )
                )
                requested = True
            elif kind == "response.output_audio.delta":
                pcm = base64.b64decode(event["delta"], validate=True)
                if pcm:
                    total_bytes += len(pcm)
                    if total_bytes > 10 * 1024 * 1024:
                        raise SpeechError("语音输出超长已截断。")
                    yielded_any = True
                    yield pcm
            elif kind == "error":
                raise SpeechError("语音合成服务返回错误，请检查模型权限或额度。")
            elif kind == "response.done":
                status = (event.get("response") or {}).get("status")
                if status != "completed" or not yielded_any:
                    raise SpeechError("语音合成未返回音频。")
                connections.speech_pool.put(pool_key, ws)
                ws = None
                return
        raise SpeechError("语音合成超时。")
    except SpeechError:
        raise
    except Exception as exc:
        if reused and not yielded_any:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None
            yield from stream_tts_pcm(key, clean_text, _fresh=True)
            return
        raise SpeechError(f"语音合成连接中断（{type(exc).__name__}）。") from None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _stream_http_speech_pcm(key: str, text: str) -> Iterator[bytes]:
    """Optional standard /v1/audio/speech PCM streaming backend."""
    body = json.dumps(
        {
            "model": os.getenv("SPARKY_TTS_MODEL", "gpt-4o-mini-tts"),
            "voice": os.getenv("SPARKY_TTS_VOICE", "coral"),
            "input": text,
            "response_format": "pcm",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with connections.open_response(req, timeout=35.0) as resp:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    resp.close()
                    break
                yield chunk
    except error.HTTPError as exc:
        raise SpeechError(f"语音合成请求失败（HTTP {exc.code}）。") from None
    except OSError:
        raise SpeechError("语音合成网络异常。") from None


def synthesize_wav(key: str, text: str) -> bytes:
    pcm = b"".join(stream_tts_pcm(key, text))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(PCM_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


_warm_lock = threading.Lock()
_warming: set[str] = set()
_last_warm: dict[str, float] = {}


def warm(key: str) -> None:
    if not key:
        return
    with _warm_lock:
        if key in _warming or time.monotonic() - _last_warm.get(key, 0.0) < 25.0:
            return
        _warming.add(key)
        _last_warm[key] = time.monotonic()

    def _warm_http() -> None:
        req = request.Request("https://api.openai.com/v1/models", method="HEAD", headers={"Authorization": f"Bearer {key}"})
        with connections.urlopen(req, timeout=12.0) as resp:
            resp.read()

    def _warm_ws() -> None:
        if os.getenv("SPARKY_TTS_BACKEND", "realtime") == "http":
            return
        model = os.getenv("SPARKY_TTS_MODEL", TTS_MODEL)
        voice = os.getenv("SPARKY_TTS_VOICE", DEFAULT_VOICE)
        pool_key = (key, model, voice)
        ws = _open_realtime_tts(key, model, voice)
        try:
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                ev = json.loads(ws.recv())
                if ev.get("type") == "session.updated":
                    connections.speech_pool.put(pool_key, ws)
                    ws = None
                    return
                if ev.get("type") == "error":
                    return
        finally:
            if ws is not None:
                ws.close()

    def _runner() -> None:
        try:
            pool_key = (key, os.getenv("SPARKY_TTS_MODEL", TTS_MODEL), os.getenv("SPARKY_TTS_VOICE", DEFAULT_VOICE))
            missing = max(0, 2 - connections.speech_pool.available(pool_key))
            threads = [threading.Thread(target=_safe, args=(_warm_http,), daemon=True)]
            for _ in range(missing):
                threads.append(threading.Thread(target=_safe, args=(_warm_ws,), daemon=True))
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            with _warm_lock:
                _warming.discard(key)

    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass

    threading.Thread(target=_runner, daemon=True).start()
