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
    document:{hidden:false},URL:{createObjectURL:()=>"blob:fixture",revokeObjectURL(){}},...overrides.context});
  const CascadeVoice=vm.runInContext(fs.readFileSync(require.resolve("../web/cascade.js"),"utf8")+"\nCascadeVoice",context);
  const voice=new CascadeVoice({
    transcribe:async()=>({text:"我想画画"}),
    turn:async(text,source)=>{calls.push({text,source});return {state:{reply:{id:"reply",text:"好的"}}};},
    speech:async()=>new Blob(["wave"]),
    delivery:(playback,phase)=>receipts.push({id:playback.id,phase,...(playback.segment===undefined?{}:{segment:playback.segment})}),
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

test("interrupt aborts pending brain request and sends cancellation before next turn",async()=>{
  const cancelled=[], order=[];
  let signal;
  const {voice}=fixture({
    cancel:async id=>{cancelled.push(id);order.push("cancel");},
    turn:(text,source,current,id,s)=>{
      signal=s;order.push(text);
      if(text==="old")return new Promise((resolve,reject)=>s.addEventListener("abort",()=>reject(Object.assign(new Error("cancelled"),{name:"AbortError"}))));
      return Promise.resolve({state:{reply:{id:"new",text:"I'm listening."}}});
    }
  });
  const old=voice.text("old");const oldId=voice.turnId;
  voice.stop();assert.equal(signal.aborted,true);
  await voice.text("new");await old;
  assert.deepEqual(cancelled,[oldId]);
  assert.deepEqual(order,["old","cancel","new"]);
  voice.stop();
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
  assert.deepEqual(receipts,[{id:"reply",phase:"failed"}]);
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

test("short whole audio makes one request and interruption never claims partial completion",async()=>{
  const requested=[];
  const {voice,receipts}=fixture({
    turn:async()=>({state:{reply:{id:"r",text:"Hi! Take your time.",speech_mode:"whole",segments:[{index:0,text:"Hi!"},{index:1,text:"Take your time."}]}}}),
    speech:async(id,signal,index)=>{requested.push(index);return new Blob(["whole"]);}
  });
  await voice.text("Hi");
  assert.deepEqual(requested,[undefined]);
  const lateEnd=voice.audio.onended;
  voice.stop();lateEnd();
  assert.deepEqual(receipts,[{id:"r",phase:"started"},{id:"r",phase:"interrupted"}]);
});

test("first sentence plays while the second is pending, with separate receipts",async()=>{
  let resolveSecond;
  const requested=[];
  const {voice,receipts}=fixture({
    turn:async()=>({state:{reply:{id:"r",text:"Hi! Try this.",segments:[{index:0,text:"Hi!"},{index:1,text:"Try this."}]}}}),
    speech:async(id,signal,index)=>{requested.push(index);return index===0?new Blob(["first"]):new Promise(resolve=>{resolveSecond=resolve;});}
  });
  await voice.text("Hi");
  assert.deepEqual(requested,[0,1]);
  assert.deepEqual(receipts,[{id:"r",segment:0,phase:"started"}]);
  const oldEnd=voice.audio.onended;
  voice.audio.onended();
  assert.equal(receipts.at(-1).phase,"complete");
  resolveSecond(new Blob(["second"]));
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(receipts.at(-1),{id:"r",segment:1,phase:"started"});
  oldEnd();
  assert.equal(receipts.length,3);
  voice.stop();
  assert.deepEqual(receipts.at(-1),{id:"r",segment:1,phase:"interrupted"});
});

test("interrupt drops a prefetched sentence and does not reopen microphone",async()=>{
  let resolveSecond;
  const {voice,receipts}=fixture({continuous:true,
    turn:async()=>({state:{reply:{id:"r",segments:[{index:0,text:"One."},{index:1,text:"Two."}]}}}),
    speech:async(id,signal,index)=>index===0?new Blob(["one"]):new Promise(resolve=>{resolveSecond=resolve;})});
  await voice.text("Hi");
  const lateEnd=voice.audio.onended;
  voice.stop();resolveSecond(new Blob(["two"]));lateEnd();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(receipts.some(r=>r.segment===1),false);
  assert.equal(voice.recording,false);
});

test("continuous conversation rearms only while enabled and visible",async()=>{
  const {voice}=fixture({continuous:true});
  await voice.text("Hi");voice.audio.onended();
  await new Promise(resolve=>setTimeout(resolve,400));
  assert.equal(voice.recording,true);
  voice.stop();
  await voice.text("Hi");voice.audio.onended();voice.continuous=false;
  await new Promise(resolve=>setTimeout(resolve,400));
  assert.equal(voice.recording,false);
  voice.stop();
});

test("short utterance gets hesitation time and silence uses the selected threshold",async()=>{
  let now=1000,energy=0,measure;
  class AudioContext{
    createAnalyser(){return {getFloatTimeDomainData(samples){samples.fill(energy);}};}
    createMediaStreamSource(){return {connect(){}};}
    async close(){}
  }
  const {voice}=fixture({silenceMs:1000,context:{window:{AudioContext,MediaRecorder:true},Date:{now:()=>now},
    setInterval:callback=>{measure=callback;return 1;},clearInterval(){},setTimeout:()=>1,clearTimeout(){}}});
  await voice.start();
  now=1100;energy=.05;measure();
  now=2600;energy=0;measure();
  assert.equal(voice.recording,true);
  now=3200;measure();
  await voice.recorder.done;
  assert.equal(voice.recording,false);
  assert.equal(voice.timing.endpoint_wait_ms,2100);
  voice.stop();
});

test("combined recording uses returned audio without legacy ASR, turn or first TTS roundtrips",async()=>{
  const inputs=[];
  const {voice,calls,receipts}=fixture({
    transcribe:()=>assert.fail("legacy ASR called"),speech:()=>assert.fail("duplicate TTS called"),
    pipeline:async(input,signal,turn,onRecord)=>{
      inputs.push(input);
      const state={reply:{id:"combined",text:"One. Two. Three.",speech_mode:"whole"}};
      onRecord({type:"transcript",text:"Hello"});onRecord({type:"state",state});onRecord({type:"audio"});
      return {state,audio:new Blob(["whole"])};
    }
  });
  await voice.start();voice.finish();await voice.recorder.done;
  assert.equal(inputs.length,1);assert.equal(atob(inputs[0].audio),"audio bytes");
  assert.equal(calls.length,0);assert.deepEqual(receipts,[{id:"combined",phase:"started"}]);
  voice.audio.onended();assert.equal(receipts.at(-1).phase,"complete");
});

test("long reply prefetch starts before first audio arrives and cancellation prevents late playback",async()=>{
  let finish;const requested=[];
  const {voice,receipts}=fixture({
    pipeline:async(input,signal,turn,onRecord)=>{
      const state={reply:{id:"r",speech_mode:"sentences",segments:[{index:0,text:"One."},{index:1,text:"Two."}]}};
      onRecord({type:"state",state});await new Promise(resolve=>{finish=resolve;});
      return {state,audio:new Blob(["late first"])};
    },
    speech:async(id,signal,index)=>{requested.push(index);return new Blob(["second"]);}
  });
  const pending=voice.text("Hi");await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(requested,[1]);assert.equal(receipts.length,0);
  voice.stop();finish();await pending;assert.equal(receipts.length,0);
});
