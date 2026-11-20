"use strict";

// Audio stays on the WebRTC media tracks. Only course requests enter the local pipeline.
class LiveVoice {
  constructor({ negotiate, coursework, heard, caption, status, stopped, greeting, isLessonRequest }) {
    Object.assign(this, { negotiate, coursework, heard, caption, status, stopped, greeting, isLessonRequest });
    this.audio = new Audio();
    this.fragments = [];
    this.heardText = "";
    this.tasks = new Set();
    this.queue = Promise.resolve();
    this.closed = false;
  }

  async play() {
    if (this.closed || !this.audio.srcObject) return;
    try {
      await this.audio.play();
      if (this.closed) return;
      this.blocked = false;
      this.greet();
    } catch (err) {
      if (this.closed) return;
      if (err.name !== "NotAllowedError") {
        this.stop("语音播放失败，请检查声音输出设备。");
        return;
      }
      this.blocked = true;
      this.status("浏览器暂停了声音。轻触页面任意位置，或按一下空格，就能听到 Sparky。");
    }
  }

  greet() {
    if (!this.started || this.audio.paused || this.blocked || this.greeted) return;
    this.greeted = true;
    this.send("session.commentary.append", this.greeting());
    this.status("语音已接通，可以直接说话。");
  }

  async start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
      throw new Error("当前浏览器无法使用实时麦克风，请用 Chrome 打开同一地址。");
    }
    this.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true}});
    if (this.closed) {
      this.stream.getTracks().forEach(track => track.stop());
      return;
    }
    const pc = this.peer = new RTCPeerConnection();
    this.unlock = () => { if(this.blocked) this.play(); };
    document.addEventListener("click", this.unlock);
    document.addEventListener("keydown", this.unlock);
    const mic = this.stream.getAudioTracks()[0];
    mic.onended = () => this.stop("麦克风已断开。");
    mic.onmute = () => this.status("麦克风暂停了采音，请检查系统声音输入。");
    mic.onunmute = () => { if (!this.blocked) this.status("麦克风已恢复，可以说话。"); };
    this.timer = setTimeout(() => this.stop("语音连接超时。"), 30000);
    pc.ontrack = event => {
      if (this.closed) return;
      this.audio.srcObject = event.streams[0] || new MediaStream([event.track]);
      this.play();
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") this.stop("语音连接已断开。");
    };
    for (const track of this.stream.getTracks()) pc.addTrack(track, this.stream);
    const channel = this.channel = pc.createDataChannel("oai-events");
    channel.onmessage = ({data}) => this.event(JSON.parse(data));
    channel.onclose = () => this.stop("语音连接已结束。");
    await pc.setLocalDescription(await pc.createOffer());
    if (this.closed) return;
    const {sdp} = await this.negotiate(pc.localDescription.sdp);
    if (!this.closed) await pc.setRemoteDescription({type:"answer", sdp});
  }

  send(type, content, delegation_id = null) {
    if (!this.closed && this.channel?.readyState === "open") {
      this.channel.send(JSON.stringify({type, content, delegation_id, event_id:crypto.randomUUID()}));
    }
  }

  event(e) {
    if (e.type === "session.closed") {
      this.stop("语音已结束。");
      this.peer?.close();
      return;
    }
    if (this.closed) return;
    if (e.type === "session.started") {
      clearTimeout(this.timer);
      this.started = true;
      this.greet();
      // A live track can still be sending only silence, even after permission was granted.
      this.inputCheck = setTimeout(async () => {
        try {
          const stats = await this.peer.getStats();
          const source = [...stats.values()].find(s => s.type==="media-source" && s.kind==="audio");
          if (!this.closed && !this.blocked && !this.hasHeard && source?.totalAudioEnergy===0) {
            this.status("麦克风已打开，但还没有收到声音。说声「你好」试试；如果仍然没有字幕，请检查系统选中的输入设备。");
          }
        } catch { /* Some browsers do not expose microphone energy. */ }
      }, 9000);
    } else if (e.type === "session.input_transcript.delta") {
      if (!this.hasHeard && !this.blocked) this.status("听到了。");
      this.hasHeard = true;
      const newTurn = this.inputEnd === undefined ||
        (this.outputEnd > this.inputEnd && e.start_ms - this.inputEnd > 500);
      if (newTurn) {
        this.heardText = "";
        this.inputStart = e.start_ms;
        this.lastEntryReply = null;
      }
      this.inputEnd = e.end_ms ?? e.start_ms;
      this.fragments.push({...e, newTurn});
      this.heardText = (this.heardText + e.delta).slice(-800);
      const heard = this.heardText;
      this.heard(heard);
      if (/(关闭语音|关闭麦克风)[。！!？?\s]*$/u.test(heard)) this.stop("麦克风已关闭。");
      clearTimeout(this.entryTimer);
      if (!this.closed && this.isLessonRequest?.(heard)) {
        // A voice model can reply conversationally without delegating a clear course choice.
        this.entryTimer=setTimeout(()=>{
          this.queue=this.queue.then(async()=>{
            const text=this.fragments.filter(f=>f.start_ms>=this.inputStart).map(f=>f.delta).join("").trim();
            if(this.closed || !this.isLessonRequest(text))return;
            this.fragments=[];
            const reply=await this.coursework(text,crypto.randomUUID());
            if(this.closed)return;
            this.lastEntryReply=reply;
            this.send("session.commentary.append",reply);
          }).catch(()=>{if(!this.closed)this.status("活动暂时没接上，可以再说一次。");});
        },900);
      }
    } else if (e.type === "session.output_transcript.delta") {
      const fresh = this.outputEnd === undefined || e.start_ms - this.outputEnd > 1500;
      this.output = ((fresh ? "" : this.output) + e.delta).slice(-800);
      this.outputEnd = e.end_ms;
      this.caption(this.output);
    } else if (e.type === "session.delegation.created" && !this.tasks.has(e.delegation.id)) {
      const id = e.delegation.id;
      this.tasks.add(id);
      this.send("session.thinking.append", "", id);
      this.queue = this.queue.then(async () => {
        // Captions can arrive a little after the delegation. Use timeline offsets to
        // leave a newer interruption available for the next course request.
        await new Promise(resolve => setTimeout(resolve, 300));
        if (this.closed) return;
        const before = f => f.start_ms <= e.offset_ms;
        const pending = this.fragments.filter(before).sort((a,b) => a.start_ms-b.start_ms);
        let start=0;
        for(let i=1;i<pending.length;i++){
          if(pending[i].newTurn) start=i;
        }
        const text = pending.slice(start).map(f => f.delta).join("").trim();
        this.fragments = this.fragments.filter(f => !before(f));
        const reply = text ? await this.coursework(text, id) : this.lastEntryReply || "刚才我没听清，你可以再说一次吗？";
        this.lastEntryReply=null;
        this.send("session.commentary.append", reply, id);
      }).catch(() => this.send("session.commentary.append", "刚才没接上，我们再说一次好吗？", id));
    } else if (e.type === "error") {
      this.stop("GPT-Live 返回了会话错误。");
    }
  }

  stop(message) {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.timer);
    clearTimeout(this.inputCheck);
    clearTimeout(this.entryTimer);
    if (this.unlock) {
      document.removeEventListener("click",this.unlock);
      document.removeEventListener("keydown",this.unlock);
    }
    this.stream?.getTracks().forEach(track => track.stop());
    this.audio.pause();
    this.audio.srcObject = null;
    if (this.channel?.readyState === "open") {
      this.channel.send(JSON.stringify({type:"session.close"}));
      setTimeout(() => this.peer.close(), 2000);
    } else {
      this.peer?.close();
    }
    this.stopped(message);
  }
}
