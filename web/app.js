"use strict";

const $ = id => document.getElementById(id);

let appData = {
  profiles: [],
  state: null,
  traces: [],
  curriculum: null,
  capabilities: {},
};
let activeAbort = null;
let activeTurnId = null;
let pendingReceipts = [];

const status = msg => {
  $("statusLine").textContent = msg || "";
};

async function apiJson(path, body, signal) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal }
    : { signal };
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "请求失败");
  return data;
}

function setStageVisual(mode, labelText) {
  $("companionStage").dataset.state = mode;
  const labels = {
    idle: "Ready to hang out",
    listening: "Listening to you…",
    thinking: "Sparky is thinking…",
    speaking: "Sparky is talking…",
  };
  $("stateLabel").textContent = labelText || labels[mode] || "Ready";
}

function renderHighlightedBubble(text) {
  const container = $("replyBubble");
  container.replaceChildren();
  const content = text || "Hey! What would you like to chat about today?";
  // Highlight contiguous Chinese character phrases (including middle punctuation like '，' inside short Chinese spans)
  const regex = /([\u3400-\u9fff]+(?:[，、][\u3400-\u9fff]+)*)/gu;
  let last = 0;
  for (const match of content.matchAll(regex)) {
    if (match.index > last) {
      container.append(document.createTextNode(content.slice(last, match.index)));
    }
    const chip = document.createElement("span");
    chip.className = "zh-chip";
    chip.textContent = match[0];
    container.append(chip);
    last = match.index + match[0].length;
  }
  if (last < content.length) {
    container.append(document.createTextNode(content.slice(last)));
  }
}

function renderCompanionScreen() {
  const state = appData.state;
  if (!state) return;

  $("sessionBadge").textContent = `Session ${state.session.session_num} · ${state.profile.name}`;
  const voiceTag = appData.capabilities.voice_enabled ? "OpenAI Cascade" : "Offline Demo";
  $("modeBadge").textContent = `${voiceTag} (${appData.capabilities.model || "local"})`;

  renderHighlightedBubble(state.session.reply?.text);

  // Active Playlet Banner
  const playlet = state.session.active_playlet;
  if (playlet) {
    $("playletBanner").hidden = false;
    $("playletTitle").textContent = `🎲 ${playlet.title}`;
  } else {
    $("playletBanner").hidden = true;
  }

  // Controllable Floating Visual Cards (形态 A)
  const cards = state.session.ui_cards || [];
  const tray = $("cardTray");
  tray.replaceChildren();
  $("cardsArea").hidden = cards.length === 0;
  for (const card of cards) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "vocab-card";
    const speakSample = card.example || card.zh.replace(/…/g, "");
    btn.onclick = () => sendTurn(speakSample, false);

    const top = document.createElement("div");
    top.className = "card-top";
    const em = document.createElement("span");
    em.className = "emoji";
    em.textContent = card.emoji || "✨";
    const titles = document.createElement("div");
    const zh = document.createElement("div");
    zh.className = "zh";
    zh.textContent = card.zh;
    const py = document.createElement("div");
    py.className = "pinyin";
    py.textContent = card.pinyin;
    titles.append(zh, py);
    top.append(em, titles);

    const en = document.createElement("div");
    en.className = "en";
    en.textContent = card.en;
    btn.append(top, en);

    if (card.hook) {
      const hook = document.createElement("div");
      hook.className = "hook";
      hook.textContent = card.hook;
      btn.append(hook);
    }
    tray.append(btn);
  }

  // Collectible Chinese Stars Strip
  const stars = state.stars || [];
  $("starsBar").hidden = stars.length === 0;
  const starList = $("starList");
  starList.replaceChildren();
  for (const st of stars) {
    const pill = document.createElement("span");
    pill.className = "star-pill";
    pill.dataset.stage = st.stage;
    pill.title = `${st.en} (${st.stage})`;
    pill.textContent = `${st.emoji} ${st.zh}`;
    starList.append(pill);
  }
}

function renderInspector() {
  const state = appData.state;
  if (!state) return;

  // 1. Profiles
  const sel = $("profileSelect");
  sel.replaceChildren();
  for (const p of appData.profiles) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.name} (Session ${p.session_num})`;
    opt.selected = p.id === state.profile.id;
    sel.append(opt);
  }
  $("langModeSelect").value = state.language_mode || "bilingual";

  // 2. SessionFocus
  const focus = state.inspector?.focus || {};
  const targetsTxt = (focus.primary_targets || [])
    .map(t => `${t.emoji} ${t.zh} [${t.current_stage} → ${(t.allowed_moves || []).join("/")}]`)
    .join(" · ") || "当前轮仅纯陪伴倾听";
  const snowballTxt = (focus.snowball_familiar_bank || []).map(s => s.zh).join("、") || "暂无熟词";
  const focusBox = $("focusSummary");
  focusBox.replaceChildren();
  for (const line of [
    `节奏档位: ${focus.rhythm_mode || "—"}`,
    `当前主攻单元: ${focus.active_unit?.title || "—"}`,
    `本轮候选目标: ${targetsTxt}`,
    `滚雪球熟词池: ${snowballTxt}`,
  ]) {
    const d = document.createElement("div");
    d.textContent = line;
    focusBox.append(d);
  }

  // 3. Syllabus & Mastery Map
  const prog = state.inspector?.progress || { units: [] };
  const tree = $("syllabusTree");
  tree.replaceChildren();
  for (const u of prog.units) {
    const box = document.createElement("div");
    box.className = "unit-block" + (u.active ? " active" : "");
    const title = document.createElement("div");
    title.className = "unit-title";
    const statusBadge = u.completed ? "✅ 已通关" : u.active ? "🎯 主攻中" : u.unlocked ? "🔓 已解锁" : "🔒 待解锁";
    title.innerHTML = `<span>${u.order}. ${u.title_zh}</span><span>${statusBadge}</span>`;
    const chips = document.createElement("div");
    chips.className = "target-chips";
    for (const t of u.targets) {
      const ch = document.createElement("span");
      ch.className = "t-chip";
      ch.dataset.stage = t.stage;
      ch.textContent = `${t.emoji}${t.zh} (${t.stage})`;
      chips.append(ch);
    }
    box.append(title, chips);
    tree.append(box);
  }
  if (prog.personal_lexicon?.length) {
    const pBox = document.createElement("div");
    pBox.className = "unit-block";
    const pt = document.createElement("div");
    pt.className = "unit-title";
    pt.textContent = "✨ 动态生成的孩子个人兴趣词库";
    const chips = document.createElement("div");
    chips.className = "target-chips";
    for (const d of prog.personal_lexicon) {
      const ch = document.createElement("span");
      ch.className = "t-chip";
      ch.dataset.stage = d.stage;
      ch.textContent = `${d.emoji}${d.zh} · ${d.en} (${d.stage})`;
      chips.append(ch);
    }
    pBox.append(pt, chips);
    tree.append(pBox);
  }

  // 4. Memory
  const mem = state.inspector?.memory || {};
  const memBox = $("memoryBox");
  memBox.replaceChildren();
  const nameLine = document.createElement("div");
  nameLine.textContent = `识别到的称呼: ${mem.child_name || "尚未告知"}`;
  memBox.append(nameLine);
  for (const f of mem.facts || []) {
    const row = document.createElement("div");
    row.className = "fact-row";
    const sp = document.createElement("span");
    sp.textContent = `• ${f.text} (S${f.session})`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "mini-link";
    del.textContent = "×";
    del.onclick = async () => {
      const res = await apiJson("/api/memory", { profile: state.profile.id, op: "remove_fact", value: f.id });
      appData.state = res.state;
      renderAll();
    };
    row.append(sp, del);
    memBox.append(row);
  }
  for (const ep of mem.episodic_summaries || []) {
    const epDiv = document.createElement("div");
    epDiv.style.fontSize = "12px";
    epDiv.style.color = "#536960";
    epDiv.textContent = `📖 S${ep.session_num} 归档: ${ep.summary}`;
    memBox.append(epDiv);
  }

  // 5. Turn Traces
  const traceList = $("traceList");
  traceList.replaceChildren();
  for (const tr of (appData.traces || []).slice(0, 12)) {
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    const t = tr.timing || {};
    const firstAudio = t.client_first_audio_ms ?? t.server_first_pcm_ms ?? "—";
    sum.textContent = `S${tr.session_num}#${tr.turn_index} · ${tr.move} · 大脑 ${t.brain_ms ?? "—"}ms · 首音 ${firstAudio}ms`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(
      {
        child: tr.user_text,
        sparky: tr.reply_text,
        observations: tr.meta?.applied_observations,
        delivery: tr.delivery?.delivery,
        timing: tr.timing,
      },
      null,
      2
    );
    det.append(sum, pre);
    traceList.append(det);
  }
}

function renderAll() {
  renderCompanionScreen();
  renderInspector();
  updateMicButton();
}

const player = new PcmStreamPlayer({
  onSpeakingChange: speaking => {
    if (!speaking) {
      document.documentElement.style.setProperty("--mouth-open", "0px");
      if (voice.active) {
        setStageVisual("listening");
        voice.setProcessing(false);
      } else {
        setStageVisual("idle");
      }
    } else {
      setStageVisual("speaking");
    }
    updateMicButton();
  },
  onLevel: level => {
    document.documentElement.style.setProperty("--mouth-open", `${Math.round(level * 14)}px`);
    document.documentElement.style.setProperty("--energy-scale", (1 + level * 0.25).toFixed(2));
  },
  onSegmentReceipt: (replyId, segment, phase) => {
    const pid = appData.state?.profile?.id;
    if (!pid || !replyId) return;
    pendingReceipts.push({ reply: replyId, segment, status: phase });
    apiJson("/api/delivery", { profile: pid, reply: replyId, segment, status: phase }).catch(() => {});
  },
});

const voice = new VoiceController({
  player,
  onUtterance: async (audioB64, media, vadMs) => {
    await sendTurn({ audio: audioB64, media, vadMs }, true);
  },
  onBargeIn: () => {
    cancelInFlightTurn();
    status("检测到你开口，已立即打断 Sparky 并继续听你说…");
  },
  onMicLevel: level => {
    if (!player.speaking) {
      document.documentElement.style.setProperty("--energy-scale", (1 + level * 0.28).toFixed(2));
    }
  },
  onStateChange: st => {
    if (!player.speaking) setStageVisual(st);
    updateMicButton();
  },
  onError: err => {
    status(err.message || "麦克风状态异常。");
    updateMicButton();
  },
});

function updateMicButton() {
  const btn = $("micBtn");
  if (!appData.capabilities.voice_enabled) {
    btn.textContent = "🎙 未配置 OpenAI Key (当前可用文字体验)";
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  if (player.speaking && voice.active) {
    btn.textContent = "✋ 打断并说话";
    btn.classList.add("active");
  } else if (voice.active) {
    btn.textContent = voice.handsFree ? "⏹ 停止语音监听" : "✅ 说完了（点击发送）";
    btn.classList.add("active");
  } else {
    btn.textContent = voice.handsFree ? "🎙 开启连续语音对话" : "🎙 点击开始说话";
    btn.classList.remove("active");
  }
}

function cancelInFlightTurn() {
  player.stop(true);
  if (activeAbort) {
    activeAbort.abort();
    activeAbort = null;
  }
  if (activeTurnId && appData.state?.profile?.id) {
    const tid = activeTurnId;
    activeTurnId = null;
    apiJson("/api/cancel", { profile: appData.state.profile.id, turn: tid }).catch(() => {});
  }
}

async function refreshTraces() {
  const pid = appData.state?.profile?.id;
  if (!pid) return;
  try {
    const res = await apiJson(`/api/state/${pid}`);
    appData.state = res.state;
    appData.traces = res.traces;
    renderAll();
  } catch {}
}

async function sendTurn(input, isAudioPayload = false) {
  const pid = appData.state?.profile?.id;
  if (!pid) return;
  cancelInFlightTurn();

  const turnId = crypto.randomUUID();
  activeTurnId = turnId;
  const abortCtrl = new AbortController();
  activeAbort = abortCtrl;
  voice.setProcessing(true);

  const startedAt = performance.now();
  let firstAudioRecorded = false;
  let streamingSentences = [];
  const receipts = pendingReceipts.splice(0, pendingReceipts.length);

  const payload = {
    profile: pid,
    turn: turnId,
    want_audio: Boolean(appData.capabilities.voice_enabled),
    receipts,
    ...(isAudioPayload ? { audio: input.audio, media: input.media } : { text: String(input) }),
  };

  if (!isAudioPayload) {
    $("heardPill").hidden = false;
    $("heardPill").textContent = `你说："${input}"`;
  }
  setStageVisual("thinking");
  status(isAudioPayload ? "正在识别语音…" : "Sparky 正在思考…");
  player.beginTurn(null);

  try {
    const res = await fetch("/api/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: abortCtrl.signal,
    });

    await readNdjsonStream(
      res,
      ev => {
        if (ev.type === "transcript") {
          $("heardPill").hidden = false;
          $("heardPill").textContent = `听到你说："${ev.text}"`;
          status("Sparky 正在接话…");
        } else if (ev.type === "empty") {
          status(ev.message || "没听清，再说一次吧。");
          voice.setProcessing(false);
          setStageVisual(voice.active ? "listening" : "idle");
        } else if (ev.type === "sentence") {
          streamingSentences.push(ev.text);
          renderHighlightedBubble(streamingSentences.join(" "));
        } else if (ev.type === "audio_pcm") {
          if (!firstAudioRecorded) {
            firstAudioRecorded = true;
            const firstAudioMs = Math.round(performance.now() - startedAt);
            apiJson("/api/metrics", {
              profile: pid,
              turn: turnId,
              metrics: {
                client_first_audio_ms: firstAudioMs,
                ...(isAudioPayload && input.vadMs ? { vad_endpoint_ms: input.vadMs } : {}),
              },
            }).catch(() => {});
          }
          player.enqueuePcmBase64(ev.pcm, ev.rate || 24000, ev.segment ?? 0);
          status("");
        } else if (ev.type === "segment_done") {
          player.markSegmentStreamDone(ev.segment ?? 0);
        } else if (ev.type === "state") {
          appData.state = ev.state;
          player.attachReplyId(ev.state.session.reply.id);
          if (!appData.capabilities.voice_enabled) {
            apiJson("/api/delivery", {
              profile: pid,
              reply: ev.state.session.reply.id,
              status: "complete",
            }).catch(() => {});
          }
          renderCompanionScreen();
          renderInspector();
        } else if (ev.type === "tts_warning") {
          status(`文字已更新（语音提示：${ev.error}）`);
        }
      },
      abortCtrl.signal
    );

    if (activeTurnId === turnId) {
      activeTurnId = null;
      activeAbort = null;
      if (!player.speaking) {
        voice.setProcessing(false);
        setStageVisual(voice.active ? "listening" : "idle");
      }
      await refreshTraces();
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    status(err.message || "对话请求中断。");
    voice.setProcessing(false);
    setStageVisual(voice.active ? "listening" : "idle");
  }
}

async function speakCurrentReply() {
  const pid = appData.state?.profile?.id;
  const replyId = appData.state?.session?.reply?.id;
  if (!pid || !replyId || !appData.capabilities.voice_enabled) return;

  cancelInFlightTurn();
  const abortCtrl = new AbortController();
  activeAbort = abortCtrl;
  player.beginTurn(replyId);
  status("正在合成 Sparky 的开场语音…");

  try {
    const res = await fetch("/api/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: pid }),
      signal: abortCtrl.signal,
    });
    await readNdjsonStream(
      res,
      ev => {
        if (ev.type === "audio_pcm") {
          player.enqueuePcmBase64(ev.pcm, ev.rate || 24000, ev.segment ?? 0);
          status("");
        } else if (ev.type === "segment_done") {
          player.markSegmentStreamDone(ev.segment ?? 0);
        } else if (ev.type === "tts_warning") {
          status(ev.error || "");
        }
      },
      abortCtrl.signal
    );
  } catch (err) {
    if (err.name !== "AbortError") status(err.message);
  }
}

async function triggerNextSession() {
  const pid = appData.state?.profile?.id;
  if (!pid) return;
  cancelInFlightTurn();
  status("正在归档本场记忆与掌握度，准备开启下一场对话…");
  setStageVisual("thinking", "Archiving memory…");
  try {
    const res = await apiJson("/api/session/next", { profile: pid });
    appData.profiles = res.profiles;
    appData.state = res.state;
    appData.traces = res.traces;
    $("heardPill").hidden = true;
    renderAll();
    status(`已进入 Session ${res.state.session.session_num}！注意看 Sparky 如何结合上场记忆和熟词主动打招呼。`);
    if (appData.capabilities.voice_enabled) {
      await speakCurrentReply();
    } else {
      setStageVisual("idle");
    }
  } catch (err) {
    status(err.message);
    setStageVisual("idle");
  }
}

// Wire UI Controls
$("handsFreeToggle").onchange = () => {
  voice.handsFree = $("handsFreeToggle").checked;
  if (voice.active) {
    voice.stop();
  }
  updateMicButton();
};

$("micBtn").onclick = async () => {
  player.ensureContext();
  if (player.speaking && voice.active) {
    cancelInFlightTurn();
    voice._beginSegmentRecorder(true);
    status("已打断，请直接说话…");
    return;
  }
  if (voice.active) {
    if (voice.handsFree) {
      voice.stop();
      status("语音监听已暂停。");
    } else {
      voice.finishUtterance();
    }
    return;
  }
  try {
    await voice.start();
    status(voice.handsFree ? "正在连续监听（说完停顿即自动发送，Sparky 说话时你可直接插话）" : "正在录音，说完再点一次发送。");
  } catch (err) {
    status(err.message);
  }
};

$("replayBtn").onclick = () => {
  if (appData.capabilities.voice_enabled) {
    speakCurrentReply();
  } else {
    status("离线演示模式下未启用云端语音合成。");
  }
};

$("textForm").onsubmit = ev => {
  ev.preventDefault();
  const val = $("textInput").value.trim();
  if (!val) return;
  $("textInput").value = "";
  player.ensureContext();
  sendTurn(val, false);
};

$("exitPlaylet").onclick = () => sendTurn("Let's stop the game and chat about something else.", false);
$("nextSessionQuick").onclick = triggerNextSession;
$("nextSessionBtn").onclick = triggerNextSession;

$("toggleInspector").onclick = () => $("layout").classList.toggle("inspector-closed");
$("closeInspector").onclick = () => $("layout").classList.add("inspector-closed");

$("profileSelect").onchange = async () => {
  cancelInFlightTurn();
  const pid = $("profileSelect").value;
  localStorage.setItem("sparky.v2.profile", pid);
  const res = await apiJson(`/api/state/${pid}`);
  appData.state = res.state;
  appData.traces = res.traces;
  $("heardPill").hidden = true;
  renderAll();
};

$("newFreshBtn").onclick = async () => {
  cancelInFlightTurn();
  const name = window.prompt("输入新测试儿童称呼：", "Leo");
  if (name === null) return;
  const res = await apiJson("/api/profile", { op: "create", name: name || "Leo", preset: "fresh" });
  appData.profiles = res.profiles;
  appData.state = res.state;
  appData.traces = res.traces;
  localStorage.setItem("sparky.v2.profile", res.state.profile.id);
  $("heardPill").hidden = true;
  renderAll();
  status("已创建零基础 Session 1 画像。");
};

$("newReturningBtn").onclick = async () => {
  cancelInFlightTurn();
  const res = await apiJson("/api/profile", { op: "create", name: "Maya", preset: "returning_dino" });
  appData.profiles = res.profiles;
  appData.state = res.state;
  appData.traces = res.traces;
  localStorage.setItem("sparky.v2.profile", res.state.profile.id);
  $("heardPill").hidden = true;
  renderAll();
  status("已加载回归老朋友 Maya (Session 2，已解锁第2单元『我喜欢…』且带恐龙记忆)！");
  if (appData.capabilities.voice_enabled) speakCurrentReply();
};

$("deleteProfileBtn").onclick = async () => {
  const pid = appData.state?.profile?.id;
  if (!pid || !window.confirm("确定删除该测试档案吗？")) return;
  cancelInFlightTurn();
  const res = await apiJson("/api/profile", { op: "delete", profile: pid });
  appData.profiles = res.profiles;
  appData.state = res.state;
  appData.traces = res.traces;
  localStorage.setItem("sparky.v2.profile", res.state.profile.id);
  renderAll();
};

$("langModeSelect").onchange = async () => {
  const pid = appData.state?.profile?.id;
  if (!pid) return;
  const res = await apiJson("/api/memory", { profile: pid, op: "set_language", value: $("langModeSelect").value });
  appData.state = res.state;
  renderAll();
};

$("addFactForm").onsubmit = async ev => {
  ev.preventDefault();
  const val = $("factInput").value.trim();
  const pid = appData.state?.profile?.id;
  if (!val || !pid) return;
  $("factInput").value = "";
  const res = await apiJson("/api/memory", { profile: pid, op: "add_fact", value: val });
  appData.state = res.state;
  renderAll();
};

$("reloadCurrBtn").onclick = async () => {
  try {
    const res = await apiJson("/api/curriculum/reload", { profile: appData.state?.profile?.id });
    appData.curriculum = res.curriculum;
    if (res.state) appData.state = res.state;
    renderAll();
    status(`课程配置热重载成功 (digest: ${res.curriculum.digest})！`);
  } catch (err) {
    status(err.message);
  }
};

// Bootstrap
(async () => {
  try {
    const boot = await apiJson("/api/bootstrap");
    appData = boot;
    const saved = localStorage.getItem("sparky.v2.profile");
    if (saved && boot.profiles.some(p => p.id === saved) && saved !== boot.state.profile.id) {
      const switched = await apiJson(`/api/state/${saved}`);
      appData.state = switched.state;
      appData.traces = switched.traces;
    }
    renderAll();
    status(
      boot.capabilities.voice_enabled
        ? "点击「开启连续语音对话」直接开口，或在下方输入一句话体验陪伴+教学闭环。"
        : "当前未设置 OPENAI_API_KEY，已进入本地离线演示模式（可完整测试大纲进阶、词卡与跨场记忆）。"
    );
  } catch (err) {
    status("初始化失败：" + err.message);
  }
})();
