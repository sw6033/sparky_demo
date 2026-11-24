"use strict";

async function readNdjsonStream(response, onEvent, signal) {
  if (!response.ok) {
    let msg = "请求失败，请稍后重试。";
    try {
      const err = await response.json();
      msg = err.error || msg;
    } catch {}
    throw Object.assign(new Error(msg), { code: response.status });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const onAbort = () => reader.cancel().catch(() => {});
  signal?.addEventListener("abort", onAbort, { once: true });

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "error") {
          throw Object.assign(new Error(ev.error || "服务处理异常。"), { code: ev.code || 500 });
        }
        onEvent(ev);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const ev = JSON.parse(buffer.trim());
      if (ev.type === "error") throw new Error(ev.error || "服务处理异常。");
      onEvent(ev);
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}

class PcmStreamPlayer {
  constructor({ onSpeakingChange = () => {}, onLevel = () => {}, onSegmentReceipt = () => {} } = {}) {
    this.onSpeakingChange = onSpeakingChange;
    this.onLevel = onLevel;
    this.onSegmentReceipt = onSegmentReceipt;
    this.ctx = null;
    this.analyser = null;
    this.sources = new Set();
    this.scheduledEnd = 0;
    this.speaking = false;
    this.replyId = null;
    this.startedSegments = new Set();
    this.segmentLastSource = new Map();
    this.finishedSegments = new Set();
    this.raf = null;
  }

  ensureContext() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    if (!this.ctx || this.ctx.state === "closed") {
      this.ctx = new AudioCtx();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.connect(this.ctx.destination);
    }
    if (this.ctx.state === "suspended") {
      this.ctx.resume().catch(() => {});
    }
    return this.ctx;
  }

  beginTurn(replyId = null) {
    this.stop(false);
    this.replyId = replyId;
    this.startedSegments.clear();
    this.segmentLastSource.clear();
    this.finishedSegments.clear();
  }

  attachReplyId(replyId) {
    this.replyId = replyId;
    for (const seg of this.startedSegments) {
      this.onSegmentReceipt(replyId, seg, "started");
    }
    for (const seg of this.finishedSegments) {
      this.onSegmentReceipt(replyId, seg, "complete");
    }
  }

  enqueuePcmBase64(b64, sampleRate = 24000, segment = 0) {
    const ctx = this.ensureContext();
    if (!ctx) return;
    const bin = atob(b64);
    const sampleCount = Math.floor(bin.length / 2);
    if (sampleCount <= 0) return;

    const floats = new Float32Array(sampleCount);
    for (let i = 0; i < sampleCount; i++) {
      const lo = bin.charCodeAt(i * 2);
      const hi = bin.charCodeAt(i * 2 + 1);
      let val = (hi << 8) | lo;
      if (val >= 0x8000) val -= 0x10000;
      floats[i] = val / 32768.0;
    }

    const audioBuf = ctx.createBuffer(1, sampleCount, sampleRate);
    audioBuf.getChannelData(0).set(floats);

    const src = ctx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(this.analyser);

    const startAt = Math.max(ctx.currentTime + 0.025, this.scheduledEnd);
    this.scheduledEnd = startAt + audioBuf.duration;
    this.sources.add(src);
    this.segmentLastSource.set(segment, src);

    if (!this.startedSegments.has(segment)) {
      this.startedSegments.add(segment);
      if (this.replyId) this.onSegmentReceipt(this.replyId, segment, "started");
    }

    if (!this.speaking) {
      this.speaking = true;
      this.onSpeakingChange(true);
      this._pumpMeter();
    }

    src.onended = () => {
      this.sources.delete(src);
      if (this.segmentLastSource.get(segment) === src && this.finishedSegments.has(`done_${segment}`)) {
        this.finishedSegments.add(segment);
        if (this.replyId) this.onSegmentReceipt(this.replyId, segment, "complete");
      }
      if (this.sources.size === 0 && this.speaking) {
        this.speaking = false;
        this.onLevel(0);
        this.onSpeakingChange(false);
      }
    };
    src.start(startAt);
  }

  markSegmentStreamDone(segment) {
    this.finishedSegments.add(`done_${segment}`);
    const lastSrc = this.segmentLastSource.get(segment);
    if (!lastSrc || !this.sources.has(lastSrc)) {
      this.finishedSegments.add(segment);
      if (this.replyId) this.onSegmentReceipt(this.replyId, segment, "complete");
    }
  }

  _pumpMeter() {
    if (!this.speaking || !this.analyser) return;
    const arr = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(arr);
    let sum = 0;
    for (let i = 0; i < arr.length; i++) sum += arr[i] * arr[i];
    const rms = Math.sqrt(sum / arr.length);
    this.onLevel(Math.min(1, rms * 6.5));
    this.raf = requestAnimationFrame(() => this._pumpMeter());
  }

  stop(interrupted = true) {
    if (this.raf) cancelAnimationFrame(this.raf);
    const hadActive = this.sources.size > 0;
    for (const src of this.sources) {
      src.onended = null;
      try { src.stop(); } catch {}
    }
    this.sources.clear();
    this.scheduledEnd = 0;
    if (hadActive && interrupted && this.replyId) {
      for (const seg of this.startedSegments) {
        if (!this.finishedSegments.has(seg)) {
          this.onSegmentReceipt(this.replyId, seg, "interrupted");
        }
      }
    }
    if (this.speaking) {
      this.speaking = false;
      this.onLevel(0);
      this.onSpeakingChange(false);
    }
  }
}

class VoiceController {
  constructor({ player, onUtterance, onBargeIn, onMicLevel, onStateChange, onError }) {
    this.player = player;
    this.onUtterance = onUtterance;
    this.onBargeIn = onBargeIn;
    this.onMicLevel = onMicLevel;
    this.onStateChange = onStateChange;
    this.onError = onError;

    this.handsFree = true;
    this.active = false;
    this.capturingUtterance = false;
    this.stream = null;
    this.audioCtx = null;
    this.meterTimer = null;
    this.recorder = null;
    this.mediaType = "";
    this.chunks = [];
    this.noiseFloor = 0.004;
    this.bargeFrames = 0;
  }

  async start() {
    if (this.active) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      throw new Error("当前浏览器不支持麦克风录音，请使用 Chrome 或 Safari。");
    }
    this.player.ensureContext();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.mediaType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t =>
      MediaRecorder.isTypeSupported(t)
    ) || "";
    if (!this.mediaType) {
      this.stop();
      throw new Error("当前浏览器缺少兼容录音编码格式。");
    }

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    this.audioCtx = new AudioCtx();
    const source = this.audioCtx.createMediaStreamSource(this.stream);
    const analyser = this.audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);

    this.active = true;
    this.onStateChange("listening");

    if (this.handsFree) {
      this._beginSegmentRecorder(false);
    } else {
      this._beginSegmentRecorder(true);
    }

    this.meterTimer = setInterval(() => {
      if (!this.active) return;
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
      const rms = Math.sqrt(sum / samples.length);
      this.onMicLevel(Math.min(1, rms * 8));

      const speechThreshold = Math.max(0.015, this.noiseFloor * 3.2);
      const bargeThreshold = Math.max(0.032, this.noiseFloor * 5.0);

      if (this.player.speaking) {
        // Acoustic Barge-In detection while Sparky is speaking (echo-cancelled mic)
        if (this.handsFree && rms > bargeThreshold) {
          this.bargeFrames += 1;
          if (this.bargeFrames >= 3) {
            this.bargeFrames = 0;
            this.onBargeIn();
            this._beginSegmentRecorder(true);
          }
        } else {
          this.bargeFrames = Math.max(0, this.bargeFrames - 1);
        }
        return;
      }

      this.bargeFrames = 0;
      if (!this.recorder || this.recorder.state !== "recording") {
        if (this.handsFree && !this.processingTurn) {
          this._beginSegmentRecorder(false);
        }
        return;
      }

      const now = Date.now();
      if (rms > speechThreshold) {
        if (!this.speechStartedAt) this.speechStartedAt = now;
        this.lastVoiceAt = now;
      } else {
        this.noiseFloor = 0.96 * this.noiseFloor + 0.04 * Math.min(rms, 0.02);
      }

      if (this.speechStartedAt) {
        const spokenMs = this.lastVoiceAt - this.speechStartedAt;
        const silenceMs = now - this.lastVoiceAt;
        const endpointMs = spokenMs < 700 ? 900 : 800;
        if (silenceMs >= endpointMs) {
          this.finishUtterance();
        } else if (now - this.speechStartedAt > 18000) {
          this.finishUtterance();
        }
      } else if (this.handsFree && now - this.recorderStartedAt > 12000) {
        // Recycle idle MediaRecorder periodically so container header stays tiny and fresh
        this._recycleIdleRecorder();
      }
    }, 90);
  }

  _beginSegmentRecorder(immediateSpeech = false) {
    if (!this.active || !this.stream) return;
    if (this.recorder && this.recorder.state === "recording") return;
    this.chunks = [];
    this.speechStartedAt = immediateSpeech ? Date.now() : 0;
    this.lastVoiceAt = immediateSpeech ? Date.now() : 0;
    this.recorderStartedAt = Date.now();
    this.discardOnStop = false;

    const rec = new MediaRecorder(this.stream, { mimeType: this.mediaType });
    this.recorder = rec;
    rec.ondataavailable = ev => {
      if (ev.data && ev.data.size > 0) this.chunks.push(ev.data);
    };
    rec.onstop = async () => {
      if (this.discardOnStop || !this.active) return;
      const hadSpeech = this.speechStartedAt > 0 && (this.lastVoiceAt - this.speechStartedAt >= 140 || !this.handsFree);
      if (!hadSpeech || !this.chunks.length) {
        if (this.handsFree && this.active && !this.processingTurn) this._beginSegmentRecorder(false);
        return;
      }
      try {
        const blob = new Blob(this.chunks, { type: this.mediaType });
        const bytes = new Uint8Array(await blob.arrayBuffer());
        if (bytes.length < 400) {
          if (this.handsFree && this.active) this._beginSegmentRecorder(false);
          return;
        }
        let bin = "";
        for (let i = 0; i < bytes.length; i += 8192) {
          bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
        }
        const vadMs = Date.now() - (this.lastVoiceAt || Date.now());
        if (!this.handsFree) {
          this.stop();
        }
        await this.onUtterance(btoa(bin), this.mediaType, vadMs);
      } catch (err) {
        this.onError(err);
      }
    };
    rec.start(200);
  }

  _recycleIdleRecorder() {
    if (!this.recorder || this.recorder.state !== "recording") return;
    this.discardOnStop = true;
    const old = this.recorder;
    this.recorder = null;
    try { old.stop(); } catch {}
    setTimeout(() => {
      if (this.active && this.handsFree && !this.processingTurn && !this.player.speaking) {
        this._beginSegmentRecorder(false);
      }
    }, 60);
  }

  finishUtterance() {
    if (!this.recorder || this.recorder.state !== "recording") return;
    if (!this.speechStartedAt) this.speechStartedAt = Date.now() - 300;
    if (!this.lastVoiceAt) this.lastVoiceAt = Date.now();
    const old = this.recorder;
    this.recorder = null;
    try { old.stop(); } catch {}
  }

  setProcessing(flag) {
    this.processingTurn = flag;
    if (!flag && this.active && this.handsFree && !this.player.speaking) {
      this._beginSegmentRecorder(false);
    }
  }

  stop() {
    this.active = false;
    this.discardOnStop = true;
    clearInterval(this.meterTimer);
    this.meterTimer = null;
    if (this.recorder && this.recorder.state === "recording") {
      try { this.recorder.stop(); } catch {}
    }
    this.recorder = null;
    this.stream?.getTracks().forEach(t => t.stop());
    this.stream = null;
    this.audioCtx?.close().catch(() => {});
    this.audioCtx = null;
    this.onMicLevel(0);
    this.onStateChange("idle");
  }
}

if (typeof module !== "undefined") {
  module.exports = { readNdjsonStream, PcmStreamPlayer, VoiceController };
}
