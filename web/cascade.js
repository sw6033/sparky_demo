"use strict";

class CascadeVoice {
  constructor({transcribe, turn, speech, delivery, status, changed, heard}) {
    Object.assign(this, {transcribe, turn, speech, delivery, status, changed, heard});
    this.epoch = 0;
    this.audio = new Audio();
    this.recording = false;
  }

  current(token) { return token === this.epoch; }

  stop(message = "语音已停止，可以打字或重新开启。") {
    this.epoch++;
    this.controller?.abort();
    clearInterval(this.meter);
    clearTimeout(this.limit);
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.stream?.getTracks().forEach(track => track.stop());
    this.context?.close().catch(()=>{});
    this.audio.onended = null;
    this.audio.onplaying = null;
    this.audio.onerror = null;
    this.audio.pause();
    this.audio.removeAttribute("src");
    if (this.playback?.started) this.delivery(this.playback, "interrupted");
    this.playback = null;
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
    this.recording = false;
    this.changed(false);
    if (message) this.status(message);
  }

  async start() {
    this.stop("");
    const token = this.epoch;
    this.controller = new AbortController();
    this.detectedSpeech = false;
    this.status("正在开启麦克风…");
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new Error("当前浏览器不支持录音，请使用新版 Chrome 或 Safari。");
      }
      const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true}});
      if (!this.current(token)) { stream.getTracks().forEach(track=>track.stop()); return; }
      this.stream = stream;
      stream.getTracks().forEach(track=>{track.onended=()=>{if(this.current(token))this.stop("麦克风已断开，请重新开启。");};});
      const media = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(type=>MediaRecorder.isTypeSupported(type));
      if (!media) throw new Error("浏览器没有可用的录音格式，请换用 Chrome。");
      const recorder = this.recorder = new MediaRecorder(stream, {mimeType:media});
      const chunks = [];
      let size = 0;
      recorder.ondataavailable = event => {
        if (!this.current(token)) return;
        size += event.data.size;
        if (size > 3*1024*1024) { this.stop("录音过长，请分成短句。"); return; }
        if (event.data.size) chunks.push(event.data);
      };
      recorder.onerror = () => { if (this.current(token)) this.stop("录音失败，请重新开启麦克风。"); };
      recorder.onstop = async () => {
        stream.getTracks().forEach(track=>track.stop());
        if (!this.current(token)) return;
        clearInterval(this.meter);
        clearTimeout(this.limit);
        this.context?.close().catch(()=>{});
        this.recording = false;
        this.changed(false);
        this.status("正在识别你说的话…");
        try {
          const bytes = new Uint8Array(await new Blob(chunks, {type:media}).arrayBuffer());
          let binary = "";
          for (let offset=0; offset<bytes.length; offset+=8192) binary += String.fromCharCode(...bytes.subarray(offset,offset+8192));
          if (!this.current(token)) return;
          const result = await this.transcribe(btoa(binary), media, this.controller.signal);
          if (!this.current(token)) return;
          if (!result.text.trim()) { this.stop("没有听清，请重新开启麦克风或打字。"); return; }
          this.heard(result.text);
          if (/(关闭语音|关闭麦克风)[。！!？?\s]*$/u.test(result.text)) { this.stop("麦克风已关闭。"); return; }
          await this.run(result.text, "voice", token);
        } catch (error) { this.fail(error, token); }
      };
      recorder.start(250);
      this.recording = true;
      this.changed(true);
      this.status("正在听。说完后稍等，或点击「说完了」。");
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        const context = this.context = new AudioContext();
        const analyser = context.createAnalyser();
        analyser.fftSize = 2048;
        context.createMediaStreamSource(stream).connect(analyser);
        const samples = new Float32Array(analyser.fftSize);
        let lastSpeech = 0;
        this.meter = setInterval(()=>{
          analyser.getFloatTimeDomainData(samples);
          const energy = Math.sqrt(samples.reduce((sum,value)=>sum+value*value,0)/samples.length);
          if (energy > 0.02) { lastSpeech = Date.now(); this.detectedSpeech = true; }
          if (lastSpeech && Date.now()-lastSpeech > 1600) this.finish();
        },100);
      }
      this.limit = setTimeout(()=>{
        if (!this.current(token)) return;
        if (this.context && !this.detectedSpeech) this.stop("没有检测到声音，请重新开启麦克风。");
        else this.finish();
      },20000);
    } catch (error) { this.fail(error, token); }
  }

  finish() {
    clearInterval(this.meter);
    clearTimeout(this.limit);
    if (this.recorder?.state === "recording") this.recorder.stop();
  }

  async text(text) {
    this.stop("");
    const token = this.epoch;
    this.controller = new AbortController();
    try { await this.run(text, "text", token); }
    catch (error) { this.fail(error, token); }
  }

  async run(text, source, token) {
    if (!this.current(token)) return;
    this.status("Sparky 正在想…");
    const result = await this.turn(text, source, ()=>this.current(token));
    if (!this.current(token)) return;
    await this.play(result.state.reply, token);
  }

  async play(reply, token) {
    this.status("正在准备声音…");
    const blob = await this.speech(reply.id, this.controller.signal);
    if (!this.current(token)) return;
    this.url = URL.createObjectURL(blob);
    const playback = this.playback = {id:reply.id, attempt:crypto.randomUUID(), started:false};
    this.audio.src = this.url;
    this.audio.onplaying = () => {
      if (!this.current(token) || playback.started) return;
      playback.started = true;
      this.delivery(playback, "started");
      this.status("Sparky 正在说话，点击「打断并说话」可接话。");
    };
    this.audio.onended = () => {
      if (!this.current(token)) return;
      if (playback.started) this.delivery(playback, "complete");
      this.playback = null;
      this.stop("轮到你啦，点击「开始说话」。");
    };
    this.audio.onerror = () => { if (this.current(token)) this.stop("声音播放失败，文字回复已保留。"); };
    await this.audio.play();
  }

  fail(error, token) {
    if (!this.current(token)) return;
    const message = error.name === "NotAllowedError" ? "请允许麦克风和声音播放，然后点击按钮重试。" : error.message;
    this.stop(message || "语音暂时不可用，可以继续打字。");
  }
}
