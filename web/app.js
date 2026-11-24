"use strict";
const $ = id => document.getElementById(id);
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const synth = window.speechSynthesis;
let home, profile, state, recognition, playback, busy = false, ready = false;
let voice = Boolean(Recognition), quietTurns = 0, listenTimer, pendingReceipt = Promise.resolve();
let liveCall, creatingProfile, turnQueue = Promise.resolve();
let resumeLive = false;
let cascade;
let resyncAfterCancel=false;
const cascadeReceipts=[];
const status = message => { $("status").textContent = message; };

async function api(path, body, signal) {
  const options = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {};
  const response = await fetch(path, {...options,signal});
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || "请稍后再试。"), {code:response.status});
  return data;
}
function render() {
  $("title").textContent = state?.active?.title || (state?.paused ? "接着上次聊" : "今天想聊什么？");
  $("reply").textContent = state?.reply?.text || "Hi, I'm Sparky! What would you like to do today?";
}
async function ensureProfile() {
  if (profile) return;
  if (home.pin) throw new Error("请家长先在家长空间创建档案。");
  if (!creatingProfile) creatingProfile=(async()=>{
    const created = await api("/api/profile", {name:home.live?"成人语音测试":"小朋友"});
    const snapshot = await api("/api/state/"+created.id);
    profile = created.id;
    localStorage.setItem("sparky.profile", profile);
    state = snapshot;
  })().finally(()=>{creatingProfile=null;});
  await creatingProfile;
}
function courseTurn(text, source, confidence, id=crypto.randomUUID(), isCurrent=()=>true, signal) {
  const next = turnQueue.then(async () => {
    await pendingReceipt;
    if (!isCurrent()) throw new DOMException("本轮已取消。","AbortError");
    await ensureProfile();
    if (!isCurrent()) throw new DOMException("本轮已取消。","AbortError");
    if(resyncAfterCancel){state=await api("/api/state/"+profile,undefined,signal);resyncAfterCancel=false;}
    const event={id,revision:state.revision,type:"utterance",text,source,confidence};
    const send=()=>api("/api/event",{profile,event},signal);
    let result;
    try { result=await send(); } catch(err) {
      if(err.name==="AbortError" || !isCurrent())throw err;
      if(err.code===409) {state=await api("/api/state/"+profile);render();}
      if(err.code) throw err;
      result=await send();
    }
    if (!isCurrent()) throw new DOMException("本轮已取消。","AbortError");
    state=result.state;
    render();
    return result;
  });
  turnQueue=next.catch(()=>{});
  return next;
}
async function startLive() {
  if (liveCall || document.hidden) return;
  voice=false;
  const call = new LiveVoice({
    greeting:()=>state?.revision ? state.reply.text : "嗨，我是 Sparky！你今天想做什么呀？",
    isLessonRequest:text=>{
      const phrase=text.replace(/[\s，。！？,.!?]/gu,"").replace(/^(?:我想|我要|我们来|想)/u,"");
      return !state?.active && !state?.paused && home.lessons.some(l=>l.entry_phrases?.includes(phrase));
    },
    status, negotiate:sdp=>api("/api/live",{profile,sdp}),
    coursework:async (text,id)=>(await courseTurn(text,"voice",undefined,id)).state.reply.text,
    heard:text=>{$("heard").textContent="你说「"+text+"」";},
    caption:text=>{$("reply").textContent=text;},
    stopped:message=>{
      if(liveCall!==call)return;
      liveCall=null;
      status(message+" 可打字，或输入「开启语音」重连。");
    }
  });
  liveCall=call;
  status("正在连接 GPT-Live，请允许浏览器使用麦克风。");
  try {
    await ensureProfile();
    if(!call.closed) await call.start();
  } catch(err) {
    const reasons={NotAllowedError:"这个页面没有麦克风权限。请允许麦克风；如果内置浏览器没有权限入口，可用 Chrome 打开同一地址。",
      NotFoundError:"没有找到麦克风，请检查系统的声音输入设备。",
      NotReadableError:"麦克风暂时无法采音，请检查系统声音输入。"};
    const reason=reasons[err.name] || err.message;
    call.stop(reason || "无法连接语音。");
  }
}
function receipt(p, phase) {
  if (!p.id || !p.profile) return;
  if(cascade){
    cascadeReceipts.push({profile:p.profile,reply:p.id,attempt:p.attempt,status:phase,segment:p.segment});
    if(cascadeReceipts.length>128)cascadeReceipts.splice(0,cascadeReceipts.length-128);
  }
  pendingReceipt = pendingReceipt.then(() => api("/api/delivery", {
    profile:p.profile, reply:p.id, attempt:p.attempt, status:phase, segment:p.segment
  })).catch(()=>{});
}
function stopListening() {
  clearTimeout(listenTimer);
  const old = recognition;
  recognition = null;
  if (old) old.abort();
}
function stopSpeaking() {
  if (!playback) return;
  const old = playback;
  playback = null;
  clearTimeout(old.timeout);
  synth.cancel();
  receipt(old, "interrupted");
}
function listenSoon(delay=150) {
  clearTimeout(listenTimer);
  if (voice && ready && !document.hidden) listenTimer=setTimeout(listen, delay);
}
function speakReply() {
  if (!synth || !voice || document.hidden) { listenSoon(); return; }
  stopListening();
  stopSpeaking();
  const utterance = new SpeechSynthesisUtterance($("reply").textContent);
  utterance.lang = "zh-CN"; utterance.rate = 0.86;
  const p = {id:state?.reply.id, profile, attempt:crypto.randomUUID(), utterance};
  playback = p;
  function done(phase) {
    if (p!==playback) return;
    clearTimeout(p.timeout);
    playback = null;
    receipt(p,phase);
    listenSoon();
  }
  // Some browsers silently block autoplay. Never leave the microphone waiting on it.
  p.timeout = setTimeout(()=>{if (p===playback){stopSpeaking(); listenSoon();}},2200);
  utterance.onstart=()=>{
    if (p!==playback) return;
    clearTimeout(p.timeout);
    p.timeout=setTimeout(()=>{if(p===playback){stopSpeaking();listenSoon();}},45000);
    receipt(p,"started"); status("Sparky 正在说话…");
  };
  utterance.onend=()=>done("complete");
  utterance.onerror=()=>done("interrupted");
  synth.speak(utterance);
}
function listen() {
  if (!Recognition || !voice || !ready || busy || playback || recognition || document.hidden) return;
  const rec=new Recognition();
  recognition=rec;
  rec.lang="zh-CN"; rec.interimResults=true; rec.continuous=false;
  rec.onstart=()=>{if(rec===recognition)status("正在听，可以直接说话。");};
  rec.onresult=e=>{
    if(rec!==recognition)return;
    const result=e.results[e.results.length-1], heard=result[0];
    if(!heard.transcript.trim())return;
    status("听到「"+heard.transcript+"」");
    if(result.isFinal) submit(heard.transcript,"voice",Number.isFinite(heard.confidence)?heard.confidence:0.5);
  };
  rec.onerror=e=>{
    if(rec!==recognition || e.error==="no-speech")return;
    voice=false;
    stopListening();
    const reasons={"not-allowed":"浏览器尚未允许麦克风。","service-not-allowed":"当前浏览器未启用语音识别服务。","audio-capture":"没有检测到麦克风。","network":"浏览器连不上语音识别服务。"};
    status((reasons[e.error] || "语音暂时不可用。")+"可打字并按回车；输入「开启语音」可重试。");
  };
  rec.onend=()=>{
    if(rec!==recognition)return;
    recognition=null;
    if(++quietTurns>=3){voice=false;status("麦克风已休息。输入「开启语音」可继续，也可以打字。");}
    else listenSoon(500);
  };
  try {rec.start();} catch {recognition=null;voice=false;status("输入「开启语音」可启用麦克风，也可以直接打字。");}
}
async function submit(raw,source,confidence=1) {
  const text=raw.trim();
  if (ready && cascade && text) {
    if (/^(开启语音|打开麦克风|开始语音)[。！!]?$/u.test(text)) return cascade.start();
    if (/^(关闭语音|关闭麦克风)[。！!]?$/u.test(text)) return cascade.stop("麦克风已关闭。");
    return cascade.text(text);
  }
  if(!ready || busy || !text)return;
  if(/^(开启语音|打开麦克风|开始语音)[。！!]?$/u.test(text)){
    if(home.live){await startLive();return;}
    if(!Recognition){status("当前浏览器不支持语音识别，可打字并按回车。");return;}
    voice=true;quietTurns=0;stopSpeaking();listen();return;
  }
  if(/^(关闭语音|关闭麦克风)[。！!]?$/u.test(text)){
    liveCall?.stop("麦克风已关闭。");
    voice=false;stopListening();stopSpeaking();status("麦克风已关闭。");return;
  }
  busy=true;quietTurns=0;stopListening();stopSpeaking();
  $("heard").textContent="你说「"+text+"」";
  status("Sparky 正在想…");
  try{
    const result=await courseTurn(text,source,confidence);
    status("");busy=false;
    if(liveCall)liveCall.send("session.commentary.append",state.reply.text);
    else if(voice && !result.duplicate)speakReply();else listenSoon();
  }catch(err){
    status(err.message);busy=false;
  }finally{busy=false;}
}
$("chat").onsubmit=e=>{e.preventDefault();if(!busy && ready){const text=$("input").value;$("input").value="";submit(text,"text");}};
function suspend(){cascade?.stop("语音已暂停，返回后点击「开始说话」。");resumeLive=Boolean(liveCall)||resumeLive;liveCall?.stop("语音已暂停。");stopListening();stopSpeaking();}
function resume(){
  if(home?.live && ready && resumeLive && !document.hidden){resumeLive=false;startLive();}
  else listenSoon();
}
document.addEventListener("visibilitychange",()=>{if(document.hidden)suspend();else resume();});
window.addEventListener("pageshow",resume);
window.addEventListener("pagehide",suspend);
(async()=>{
  home=await api("/api/home");
  $("mode").textContent=home.cascade?"Cascade · ASR → C → TTS":home.live?"成人模拟测试 · GPT-Live":home.model?"已配置对话模型":"本地课程模式";
  const saved=localStorage.getItem("sparky.profile");
  profile=(home.profiles.find(p=>p.id===saved)||home.profiles[0])?.id;
  if(profile)state=await api("/api/state/"+profile);
  ready=true;render();
  if (home.cascade) {
    voice=false;
    async function speechRequest(path, data, signal) {
      await ensureProfile();
      const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({profile,...data}),signal});
      if (!response.ok){
        let message="语音连接暂时不稳定，请重试。";
        try{message=(await response.json()).error||message;}catch{}
        throw Object.assign(new Error(message),{code:response.status});
      }
      return response;
    }
    function cascadeTurn(input,signal,turn,onRecord,isCurrent){
      const queuedAt=Date.now();
      const next=turnQueue.then(async()=>{
        await ensureProfile();
        if(!isCurrent())throw new DOMException("本轮已取消。","AbortError");
        if(resyncAfterCancel){state=await api("/api/state/"+profile,undefined,signal);resyncAfterCancel=false;}
        onRecord({type:"queue",ms:Date.now()-queuedAt});
        const receipts=cascadeReceipts.filter(r=>r.profile===profile && r.reply===state.reply.id).map(({profile,...r})=>r);
        try{
          const response=await speechRequest("/api/cascade",{...input,turn,revision:state.revision,receipts},signal);
          return await readCascade(response,event=>{
            if(!isCurrent())throw new DOMException("本轮已取消。","AbortError");
            if(event.type==="state"){state=event.state;render();}
            onRecord(event);
          });
        }catch(error){if(error.code===409)resyncAfterCancel=true;throw error;}
      });
      turnQueue=next.catch(()=>{});return next;
    }
    cascade=new CascadeVoice({
      pipeline:cascadeTurn,
      transcribe:async(audio,media,signal,turn)=>(await speechRequest("/api/transcribe",{audio,media,turn},signal)).json(),
      speech:async(reply,signal,segment,turn)=>(await speechRequest("/api/speech",{reply,segment,turn},signal)).blob(),
      turn:(text,source,isCurrent,turn,signal)=>courseTurn(text,source,undefined,turn,isCurrent,signal),
      cancel:turn=>{resyncAfterCancel=true;return profile?api("/api/cancel",{profile,turn}):Promise.resolve();},
      metrics:(turn,metrics)=>api("/api/metrics",{profile,turn,metrics}).catch(()=>{}),
      delivery:(playback,phase)=>receipt({...playback,profile},phase),
      status, heard:text=>{$("heard").textContent="你说「"+text+"」";},
      changed:recording=>{$("record").textContent=recording?"说完了":"开始说话";}
    });
    $("cascadeControls").hidden=false;
    $("continuous").onchange=()=>{cascade.continuous=$("continuous").checked;};
    $("pauseLength").onchange=()=>{cascade.silenceMs=Number($("pauseLength").value);};
    $("record").onclick=()=>cascade.recording?cascade.finish():cascade.start();
    $("interrupt").onclick=()=>cascade.start();
    $("stopVoice").onclick=()=>cascade.stop();
    status("点击「开始说话」开启录音，也可以打字。先用成人模拟语音测试。");
    return;
  }
  if(home.live){resumeLive=document.hidden;await startLive();}
  else if(voice){status("允许浏览器使用麦克风后，就可以直接和 Sparky 说话。");speakReply();}
  else status("当前浏览器可用文字对话，按回车发送。");
})().catch(e=>status("小屋暂时无法打开。"+e.message));
