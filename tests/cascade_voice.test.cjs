const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

function fixture(overrides={}) {
  const receipts=[], statuses=[], calls=[];
  let pendingMic;
  let stopped=0;
  class Audio {
    pause() {}
    removeAttribute() {}
    async play() { this.onplaying?.(); }
  }
  class Recorder {
    static isTypeSupported() { return true; }
    constructor() { this.state="inactive"; }
    start() { this.state="recording"; }
    stop() {
      this.state="inactive";
      this.ondataavailable?.({data:new Blob(["audio bytes"])});
      this.done=this.onstop?.();
    }
  }
  const context=vm.createContext({Audio,MediaRecorder:Recorder,window:{MediaRecorder:Recorder},
    navigator:{mediaDevices:{getUserMedia:()=>pendingMic || Promise.resolve({getTracks:()=>[{stop(){stopped++;}}]})}},
    crypto:webcrypto,AbortController,Blob,Uint8Array,Float32Array,btoa,Date,
    setTimeout,clearTimeout,setInterval,clearInterval,
    URL:{createObjectURL:()=>"blob:fixture",revokeObjectURL(){}},...overrides.context});
  const CascadeVoice=vm.runInContext(fs.readFileSync(require.resolve("../web/cascade.js"),"utf8")+"\nCascadeVoice",context);
  const voice=new CascadeVoice({
    transcribe:async()=>({text:"我想画画"}),
    turn:async(text,source)=>{calls.push({text,source});return {state:{reply:{id:"reply",text:"好的"}}};},
    speech:async()=>new Blob(["wave"]),
    delivery:(playback,phase)=>receipts.push({id:playback.id,phase}),
    status:message=>statuses.push(message),changed(){},heard(){},...overrides
  });
  return {voice,receipts,statuses,calls,stopCount:()=>stopped,setMic:value=>{pendingMic=value;}};
}

test("recording passes ASR text through the brain before speech and receipts",async()=>{
  const {voice,receipts,calls}=fixture();
  await voice.start();
  assert.equal(voice.recording,true);
  voice.finish();
  await voice.recorder.done;
  assert.deepEqual(calls,[{text:"我想画画",source:"voice"}]);
  assert.deepEqual(receipts,[{id:"reply",phase:"started"}]);
  voice.audio.onended();
  assert.deepEqual(receipts.map(value=>value.phase),["started","complete"]);
});

test("stopping pending ASR never calls the brain",async()=>{
  let resolveASR;
  const {voice,calls}=fixture({transcribe:()=>new Promise(resolve=>{resolveASR=resolve;})});
  await voice.start();
  voice.finish();
  await new Promise(resolve=>setImmediate(resolve));
  voice.stop();
  resolveASR({text:"我想画画"});
  await voice.recorder.done;
  assert.equal(calls.length,0);
});

test("stopping pending TTS prevents late playback",async()=>{
  let resolveSpeech;
  const {voice,receipts}=fixture({speech:()=>new Promise(resolve=>{resolveSpeech=resolve;})});
  const pending=voice.text("你好");
  await new Promise(resolve=>setImmediate(resolve));
  voice.stop();
  resolveSpeech(new Blob(["late"]));
  await pending;
  assert.equal(receipts.length,0);
  assert.equal(voice.playback,null);
});

test("interrupt records interrupted once and ignores late completion",async()=>{
  const {voice,receipts}=fixture();
  await voice.text("你好");
  const lateEnd=voice.audio.onended;
  voice.stop();
  voice.stop();
  lateEnd();
  assert.deepEqual(receipts.map(value=>value.phase),["started","interrupted"]);
});

test("microphone permission arriving after stop releases tracks",async()=>{
  const {voice,setMic}=fixture();
  let resolveMic,stopped=0;
  setMic(new Promise(resolve=>{resolveMic=resolve;}));
  const pending=voice.start();
  voice.stop();
  resolveMic({getTracks:()=>[{stop(){stopped++;}}]});
  await pending;
  assert.equal(stopped,1);
  assert.equal(voice.recording,false);
});

test("playback failure does not claim completed delivery",async()=>{
  const {voice,receipts}=fixture();
  voice.audio.play=async()=>{throw Object.assign(new Error("blocked"),{name:"NotAllowedError"});};
  await voice.text("你好");
  assert.deepEqual(receipts,[]);
});

test("queued brain work receives an invalidation check and cannot trigger late speech",async()=>{
  let finishTurn, isCurrent;
  const {voice,receipts}=fixture({turn:(text,source,current)=>{
    isCurrent=current;
    return new Promise(resolve=>{finishTurn=resolve;});
  }});
  const pending=voice.text("我想画画");
  assert.equal(isCurrent(),true);
  voice.stop();
  assert.equal(isCurrent(),false);
  finishTurn({state:{reply:{id:"old",text:"过期回复"}}});
  await pending;
  assert.deepEqual(receipts,[]);
});
