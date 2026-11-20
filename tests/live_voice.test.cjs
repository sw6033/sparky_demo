const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const ctx = vm.createContext({crypto:webcrypto, setTimeout, clearTimeout, Audio:class {pause() {}}});
const LiveVoice = vm.runInContext(fs.readFileSync(require.resolve("../web/live.js"), "utf8") + "\nLiveVoice", ctx);

test("delegate a spoken course choice once, preserve the interruption, then stop capturing", async () => {
  const requests = [], updates = [];
  let stopped = false;
  const call = new LiveVoice({
    heard() {}, caption() {}, status() {}, stopped() {},
    coursework(text) { requests.push(text); return "课程已切换"; }
  });
  call.stream = { getTracks:() => [{stop() {stopped=true;}}] };
  call.channel = {readyState:"open", send:s => updates.push(JSON.parse(s))};
  call.peer = {close() {}};
  function speech(delta, start_ms) {
    call.event({type:"session.input_transcript.delta", delta, start_ms});
  }
  function delegate(id, offset_ms) {
    call.event({type:"session.delegation.created", delegation:{id}, offset_ms});
  }
  speech("我想", 30);
  speech("画画", 250);
  delegate("first", 500);
  delegate("first", 500);
  speech("蓝色", 650);
  delegate("second", 900);
  await call.queue;
  assert.deepEqual(requests, ["我想画画","蓝色"]);
  assert.deepEqual(updates.filter(e => e.type === "session.commentary.append").map(e => e.delegation_id), ["first","second"]);
  speech("关闭麦克风", 950);
  assert.equal(stopped, true);
  assert.equal(updates.at(-1).type, "session.close");
});

test("blocked playback keeps the mic open and greets once after a gesture", async () => {
  const updates = [];
  let stopped = false;
  const call = new LiveVoice({greeting:()=>"嗨，我是 Sparky！",status() {},stopped() {stopped=true;}});
  call.started = true;
  call.channel = {readyState:"open", send:s=>updates.push(JSON.parse(s))};
  call.audio.srcObject = {};
  call.audio.paused = true;
  call.audio.play = async () => {throw Object.assign(new Error(), {name:"NotAllowedError"});};
  await call.play();
  assert.equal(stopped, false);
  assert.equal(call.blocked, true);
  assert.equal(updates.length, 0);
  call.audio.play = async () => {call.audio.paused=false;};
  await call.play();
  await call.play();
  assert.deepEqual(updates.map(e=>e.content), ["嗨，我是 Sparky！"]);
});
