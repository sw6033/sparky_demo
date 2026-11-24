"use strict";

class CascadeVoice {
  constructor({transcribe, turn, speech, pipeline, delivery, status, changed, heard, metrics=()=>{}, cancel=()=>{}, continuous=false, silenceMs=1500}) {
    Object.assign(this, {transcribe, turn, speech, pipeline, delivery, status, changed, heard, metrics, cancel, continuous, silenceMs});
    this.epoch = 0;
    this.audio = new Audio();
    this.recording = false;
  }

  current(token) { return token === this.epoch; }

  stop(message = "语音已停止，可以打字或重新开启。") {
    this.epoch++;
    clearTimeout(this.autoListen);
    this.controller?.abort();
    if(this.turnId && this.processing){
      const turn=this.turnId;
      this.cancelPending=Promise.resolve().then(()=>this.cancel(turn)).catch(()=>{});
    }
    this.processing=false;
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
    this.turnId=crypto.randomUUID();
    this.recordedAt=Date.now();
    this.lastSpeech=0;
    this.timing={};
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
          if(this.pipeline){await this.runPipeline({audio:btoa(binary),media},token);return;}
          const result = await this.transcribe(btoa(binary), media, this.controller.signal, this.turnId);
          if (!this.current(token)) return;
          if (!result.text.trim()) { this.stop("没有听清，请重新开启麦克风或打字。"); return; }
          this.heard(result.text);
          if (/(关闭语音|关闭麦克风)[。！!？?\s]*$/u.test(result.text)) { this.stop("麦克风已关闭。"); return; }
          await this.run(result.text, "voice", token);
        } catch (error) { this.fail(error, token); }
      };
      recorder.start(250);
      this.recordedAt=Date.now();
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
        let lastSpeech = 0, noiseFloor = 0.004;
        this.meter = setInterval(()=>{
          analyser.getFloatTimeDomainData(samples);
          const energy = Math.sqrt(samples.reduce((sum,value)=>sum+value*value,0)/samples.length);
          const threshold=Math.max(0.012,noiseFloor*3);
          if (energy > threshold) { lastSpeech = this.lastSpeech = Date.now(); this.detectedSpeech = true; }
          else noiseFloor=0.95*noiseFloor+0.05*Math.min(energy,0.02);
          const pause=Math.max(this.silenceMs,lastSpeech-this.recordedAt<1200?2000:0);
          if (lastSpeech && Date.now()-lastSpeech > pause) this.finish();
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
    const now=Date.now();
    this.turnAt=this.lastSpeech || now;
    this.timing={recording_ms:now-this.recordedAt,endpoint_wait_ms:now-this.turnAt};
    clearInterval(this.meter);
    clearTimeout(this.limit);
    if (this.recorder?.state === "recording") this.recorder.stop();
  }

  async text(text) {
    this.stop("");
    this.turnId=crypto.randomUUID();
    this.turnAt=Date.now();
    this.timing={};
    const token = this.epoch;
    this.controller = new AbortController();
    try { if(this.pipeline)await this.runPipeline({text},token);else await this.run(text, "text", token); }
    catch (error) { this.fail(error, token); }
  }

  async run(text, source, token) {
    if (!this.current(token)) return;
    if(this.cancelPending)await this.cancelPending;
    if (!this.current(token)) return;
    this.processing=true;
    this.status("Sparky 正在想…");
    const result = await this.turn(text, source, ()=>this.current(token), this.turnId, this.controller.signal);
    if (!this.current(token)) return;
    await this.play(result.state.reply, token);
    if(this.current(token))this.processing=false;
  }

  async runPipeline(input,token){
    if(this.cancelPending)await this.cancelPending;
    if(!this.current(token))return;
    this.processing=true;
    const started=Date.now();
    let next;
    this.status(input.audio?"正在识别你说的话…":"Sparky 正在想…");
    const result=await this.pipeline(input,this.controller.signal,this.turnId,event=>{
      if(!this.current(token))return;
      if(event.type==="transcript"){
        this.heard(event.text);this.timing.pipeline_transcript_ms=Date.now()-started;
        this.status("Sparky 正在想…");
      }
      if(event.type==="state"){
        this.timing.pipeline_state_ms=Date.now()-started;
        this.status("正在准备声音…");
        const reply=event.state.reply;
        // Prepare the next sentence while the server is still making the first.
        if(reply.speech_mode!=="whole" && reply.segments?.length>1){
          next=Promise.resolve().then(()=>this.current(token)?this.speech(reply.id,this.controller.signal,reply.segments[1].index,this.turnId):null)
            .then(blob=>({blob}),error=>({error}));
        }
      }
      if(event.type==="audio")this.timing.pipeline_audio_ms=Date.now()-started;
      if(event.type==="queue")this.timing.client_queue_ms=event.ms;
    },()=>this.current(token));
    if(!this.current(token))return;
    if(result.stopped){this.processing=false;this.stop(result.stopped);return;}
    this.timing.pipeline_request_ms=Date.now()-started;
    await this.play(result.state.reply,token,result.audio,next);
    if(this.current(token))this.processing=false;
  }

  async play(reply, token, firstAudio, nextAudio) {
    this.status("正在准备声音…");
    this.parts=reply.speech_mode==="whole" ? [{text:reply.text}] :
      reply.segments?.length ? reply.segments : [{text:reply.text}];
    this.reply=reply;
    this.prefetched=new Map();
    this.lastPartEnded=null;
    if(firstAudio)this.prefetched.set(0,Promise.resolve({blob:firstAudio}));
    if(nextAudio)this.prefetched.set(1,nextAudio);
    await this.playPart(0,token);
  }

  prepare(index,token) {
    if (!this.prefetched.has(index)) {
      // Wrap failures so a cancelled prefetch never rejects without a handler.
      const pending=Promise.resolve().then(()=> {
        if(!this.current(token))return null;
        return this.speech(this.reply.id,this.controller.signal,this.parts[index].index,this.turnId);
      }).then(blob=>({blob}),error=>({error}));
      this.prefetched.set(index,pending);
    }
    return this.prefetched.get(index);
  }

  async playPart(index,token) {
    const prepared=await this.prepare(index,token);
    if(!this.current(token))return;
    if(prepared.error){
      this.delivery({id:this.reply.id,segment:this.parts[index].index,attempt:crypto.randomUUID()},"failed");
      throw prepared.error;
    }
    if(index+1<this.parts.length)this.prepare(index+1,token);
    if(index+2<this.parts.length)this.prepare(index+2,token);
    if(this.url)URL.revokeObjectURL(this.url);
    this.url=URL.createObjectURL(prepared.blob);
    const playback=this.playback={id:this.reply.id,segment:this.parts[index].index,attempt:crypto.randomUUID(),started:false};
    const playRequestedAt=Date.now();
    this.audio.src=this.url;
    this.audio.onplaying=()=>{
      if(!this.current(token)||this.playback!==playback||playback.started)return;
      playback.started=true;
      this.timing.audio_start_ms=(this.timing.audio_start_ms||0)+Date.now()-playRequestedAt;
      if(this.lastPartEnded!==null)this.timing.sentence_gap_ms=(this.timing.sentence_gap_ms||0)+Date.now()-this.lastPartEnded;
      this.delivery(playback,"started");
      if(index===0){
        this.timing.first_audio_ms=Date.now()-this.turnAt;
        this.metrics(this.turnId,{...this.timing});
      }
      this.status("Sparky 正在说话，点击「打断并说话」可接话。");
    };
    this.audio.onended=()=>{
      if(!this.current(token)||this.playback!==playback)return;
      if(playback.started)this.delivery(playback,"complete");
      this.lastPartEnded=Date.now();
      this.playback=null;
      if(index+1<this.parts.length){
        this.playPart(index+1,token).catch(error=>this.fail(error,token));
      }else{
        this.metrics(this.turnId,{...this.timing,total_turn_ms:Date.now()-this.turnAt});
        this.stop(this.continuous?"轮到你啦，马上继续听。":"轮到你啦，点击「开始说话」。");
        const nextToken=this.epoch;
        if(this.continuous)this.autoListen=setTimeout(()=>{
          if(this.current(nextToken)&&this.continuous&&!document.hidden)this.start();
        },350);
      }
    };
    this.audio.onerror=()=>{
      if(!this.current(token)||this.playback!==playback)return;
      if(!playback.started)this.delivery(playback,"failed");
      this.stop("声音播放失败，文字回复已保留。");
    };
    try {await this.audio.play();}
    catch(error){if(this.current(token)&&!playback.started)this.delivery(playback,"failed");throw error;}
  }

  fail(error, token) {
    if (!this.current(token)) return;
    const message = error.name === "NotAllowedError" ? "请允许麦克风和声音播放，然后点击按钮重试。" : error.message;
    this.stop(message || "语音暂时不可用，可以继续打字。");
  }
}
