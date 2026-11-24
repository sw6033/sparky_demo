"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { readNdjsonStream, PcmStreamPlayer } = require("../web/voice.js");

test("readNdjsonStream parses split UTF-8 NDJSON chunks cleanly", async () => {
  const encoder = new TextEncoder();
  const payload = [
    JSON.stringify({ type: "transcript", text: "我喜欢恐龙" }),
    JSON.stringify({ type: "sentence", segment: 0, text: "恐龙 is awesome!" }),
    JSON.stringify({ type: "complete", turn_id: "t1" }),
  ].join("\n") + "\n";

  const bytes = encoder.encode(payload);
  let offset = 0;
  const fakeResponse = {
    ok: true,
    body: {
      getReader() {
        return {
          async read() {
            if (offset >= bytes.length) return { done: true, value: undefined };
            const slice = bytes.slice(offset, offset + 13);
            offset += 13;
            return { done: false, value: slice };
          },
          async cancel() {},
          releaseLock() {},
        };
      },
    },
  };

  const events = [];
  await readNdjsonStream(fakeResponse, ev => events.push(ev));
  assert.equal(events.length, 3);
  assert.equal(events[0].text, "我喜欢恐龙");
  assert.equal(events[1].text, "恐龙 is awesome!");
  assert.equal(events[2].type, "complete");
});

test("PcmStreamPlayer schedules 16-bit PCM chunks and emits delivery & interruption receipts", () => {
  const createdSources = [];
  class FakeAudioContext {
    constructor() {
      this.state = "running";
      this.currentTime = 1.0;
      this.destination = {};
    }
    createAnalyser() {
      return {
        fftSize: 512,
        connect() {},
        getFloatTimeDomainData(arr) { arr.fill(0.05); },
      };
    }
    createBuffer(_ch, length, rate) {
      const data = new Float32Array(length);
      return {
        duration: length / rate,
        getChannelData: () => data,
      };
    }
    createBufferSource() {
      const src = {
        buffer: null,
        onended: null,
        startedAt: null,
        stopped: false,
        connect() {},
        start(when) { this.startedAt = when; },
        stop() { this.stopped = true; },
      };
      createdSources.push(src);
      return src;
    }
    resume() { return Promise.resolve(); }
  }

  global.window = { AudioContext: FakeAudioContext };
  global.atob = b64 => Buffer.from(b64, "base64").toString("binary");
  global.requestAnimationFrame = () => 1;
  global.cancelAnimationFrame = () => {};

  const receipts = [];
  const player = new PcmStreamPlayer({
    onSegmentReceipt: (replyId, seg, status) => receipts.push({ replyId, seg, status }),
  });

  player.beginTurn(null);
  const pcmChunk = Buffer.alloc(480, 0);
  pcmChunk.writeInt16LE(8192, 0);
  player.enqueuePcmBase64(pcmChunk.toString("base64"), 24000, 0);
  player.markSegmentStreamDone(0);

  // Reply ID arrives slightly after first PCM chunk (pipelined!) -> retroactively flushes started receipt
  player.attachReplyId("reply-123");
  assert.deepEqual(receipts, [{ replyId: "reply-123", seg: 0, status: "started" }]);

  // Simulate natural playback end of segment 0
  createdSources[0].onended();
  assert.deepEqual(receipts, [
    { replyId: "reply-123", seg: 0, status: "started" },
    { replyId: "reply-123", seg: 0, status: "complete" },
  ]);

  // Now enqueue segment 1 and interrupt mid-playback -> emits interrupted receipt!
  player.enqueuePcmBase64(pcmChunk.toString("base64"), 24000, 1);
  player.stop(true);
  assert.equal(receipts[receipts.length - 1].status, "interrupted");
  assert.equal(receipts[receipts.length - 1].seg, 1);
});
