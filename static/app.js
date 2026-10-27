"use strict";
// hwcheck's page. plain javascript, nothing fetched from anywhere but the hwcheck server.

const $ = (id) => document.getElementById(id);

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) e.append(kid);
  return e;
}

const LABELS = {
  waiting: "waiting", running: "running", works: "works", "doesnt-work": "doesn't work", skip: "skipped",
};
const WHERE = {
  page: "tested on this page",
  live: "read from the system by hwcheck",
  hibernate: "runs last",
  manual: "look, then answer",
};

let state = null;   // the last state from the server
let shown = null;   // which check (and hibernate phase) the test area was built for
let test = null;    // the running test: {stop(), observed(), update(state)}
let busy = false;   // an answer is on its way
let events = null;  // the server-sent event stream
let listIds = "";
const drafts = {};  // notes typed but not saved yet, by check id
// this run's secret, from the address hwcheck printed; every api request carries it.
// it moves from the address bar into this tab's session storage, so it isn't left on show
// (or in a copied address); a reload of this tab still finds it.
const TOKEN_KEY = "hwcheck-token";
const TOKEN = takeToken();

function takeToken() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("t");
  if (fromUrl) {
    try { window.sessionStorage.setItem(TOKEN_KEY, fromUrl); } catch (e) { /* storage off: keep it in memory */ }
    params.delete("t");
    const rest = params.toString();
    try {
      window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
    } catch (e) { /* an old browser: the address keeps it */ }
    return fromUrl;
  }
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

const RESTARTED = "hwcheck was restarted, or this page is from an earlier run: open the address it printed last.";

async function api(path, body) {
  const headers = {"X-Hwcheck-Token": TOKEN};
  const opts = body === undefined
    ? {cache: "no-store", headers}
    : {method: "POST", headers: {...headers, "Content-Type": "application/json"}, body: JSON.stringify(body)};
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (res.status === 403 && TOKEN) throw new Error(RESTARTED);
  if (!res.ok) throw new Error(data.error || `hwcheck answered ${res.status}`);
  return data;
}

const clock = (iso) => (iso ? iso.slice(11, 19) : "");
const round2 = (x) => Math.round(x * 100) / 100;

// ------------------------------------------------------------------ rendering

function render(s) {
  if (state && s.instance === state.instance && s.version < state.version) return;
  if (state && s.instance !== state.instance) shown = null;
  state = s;
  $("machine").textContent = [s.machine, s.model].filter(Boolean).join(" · ");
  const c = s.counts;
  $("progress").textContent =
    `${c.works} work · ${c["doesnt-work"]} don't · ${c.skip} skipped · ${c.left} left`;
  renderList(s);
  if (s.finished) { renderDone(s); return; }
  const check = s.checks.find((x) => x.id === s.current);
  if (!check) return;
  const key = check.id + "|" + (check.kind === "hibernate" ? s.hibernate.phase : "");
  if (key !== shown) {
    showCheck(s, check);
    shown = key;
  }
  if (test && test.update) test.update(s);
  renderButtons(s, check);
}

function renderList(s) {
  const ol = $("check-list");
  const ids = s.checks.map((c) => c.id).join(",");
  if (ids !== listIds) {
    ol.replaceChildren(...s.checks.map((c) => el("li", {},
      el("button", {type: "button", onclick: () => select(c.id)},
        el("span", {class: "title", text: c.title}),
        el("span", {class: "status"})))));
    listIds = ids;
  }
  const underway = ["pending", "hibernating"].includes(s.hibernate.phase);
  s.checks.forEach((c, i) => {
    const li = ol.children[i];
    const btn = li.firstChild;
    const st = btn.lastChild;
    st.className = `status ${c.status}`;
    st.textContent = LABELS[c.status] || c.status;
    li.className = c.id === s.current ? "current" : "";
    const hibLocked = c.kind === "hibernate" && !s.hibernate.others_done;
    btn.disabled = Boolean(s.finished) || hibLocked || (underway && c.kind !== "hibernate");
    btn.title = hibLocked ? "runs last, after the other checks" : "";
    if (c.id === s.current) btn.setAttribute("aria-current", "step");
    else btn.removeAttribute("aria-current");
  });
}

function showCheck(s, check) {
  stopTest();
  $("check-where").textContent = WHERE[check.where] || "";
  $("check-title").textContent = check.title;
  const dev = check.device;
  $("check-device").textContent = dev ? `for: ${dev.name}${dev.path && dev.path !== dev.name ? ` (${dev.path})` : ""}` : "";
  $("check-instructions").textContent = check.instructions;
  $("check-expect").textContent = check.expect ? `expected: ${check.expect}` : "";
  if (!(check.id in drafts)) drafts[check.id] = check.note || "";
  $("note").value = drafts[check.id];
  $("answer-msg").textContent = "";
  const area = $("test");
  area.replaceChildren();
  const build = BUILDERS[check.kind] || manual;
  try {
    test = build(area, check, s) || null;
  } catch (e) {
    area.append(el("p", {class: "warn", text: `this test couldn't start: ${e.message}. answer by what you see.`}));
    test = null;
  }
}

function stopTest() {
  if (test && test.stop) {
    try { test.stop(); } catch (e) { /* already stopped */ }
  }
  test = null;
}

// the buttons follow `busy` even when the state that arrives is older than the one shown
function refreshButtons() {
  const check = state && !state.finished && state.checks.find((x) => x.id === state.current);
  if (check) renderButtons(state, check);
}

function renderButtons(s, check) {
  const works = $("btn-works");
  const bad = $("btn-doesnt-work");
  const skip = $("btn-skip");
  works.hidden = bad.hidden = skip.hidden = false;
  works.disabled = bad.disabled = skip.disabled = busy;
  works.title = "";
  bad.textContent = "doesn't work";
  if (check.preset) {
    // detection recorded this before the run began (an expected device missing, say)
    works.disabled = true;
    works.title = "recorded by detection: it can be doesn't work or skip";
  }
  if (check.kind === "hibernate") {
    const ph = s.hibernate.phase;
    if (ph === "fresh-boot") {
      works.hidden = skip.hidden = true;
      bad.textContent = "save (doesn't work)";
    } else if (["idle", "failed"].includes(ph)) {
      works.disabled = true;
      works.title = "run hibernate first";
    } else if (["pending", "hibernating"].includes(ph)) {
      works.disabled = true;
      works.title = "the machine hasn't hibernated and come back yet";
    }
  }
}

function renderDone(s) {
  stopTest();
  shown = null;
  $("run").hidden = true;
  $("done").hidden = false;
  const c = s.counts;
  $("done-counts").textContent =
    `${c.works} work, ${c["doesnt-work"]} don't work, ${c.skip} skipped.`;
  $("done-path").textContent = s.results_path;
  $("conn").textContent = "";
  if (events) { events.close(); events = null; }
}

// ------------------------------------------------------------------ actions

async function answer(value) {
  if (!state || busy || state.finished) return;
  const check = state.checks.find((x) => x.id === state.current);
  if (!check) return;
  let observed = null;
  try { observed = test && test.observed ? test.observed() : null; } catch (e) { observed = null; }
  busy = true;
  renderButtons(state, check);
  try {
    const s = await api("/api/answer", {id: check.id, answer: value, note: $("note").value, observed});
    delete drafts[check.id];
    busy = false;
    render(s);
  } catch (e) {
    busy = false;
    $("answer-msg").textContent = e.message;
  }
  refreshButtons();
}

async function select(id) {
  if (!state || state.finished || busy || id === state.current) return;
  try {
    render(await api("/api/select", {id}));
  } catch (e) {
    $("answer-msg").textContent = e.message;
  }
}

function connect() {
  events = new EventSource(`/api/events?t=${encodeURIComponent(TOKEN)}`);
  events.onmessage = (ev) => {
    $("conn").textContent = "";
    try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  events.onerror = () => {
    if (state && state.finished) { if (events) events.close(); return; }
    if (events && events.readyState === EventSource.CLOSED) {
      // a refused stream (a 403: another run's secret) is never retried by the browser
      $("conn").textContent = RESTARTED;
      return;
    }
    $("conn").textContent = "lost touch with hwcheck; retrying…";
  };
}

// ------------------------------------------------------------------ tests on the page

function tones(area, headphones) {
  let ctx = null;
  const played = new Set();
  function play(pan, name, freq) {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
    const t = ctx.currentTime + 0.02;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = freq;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.35, t + 0.04);
    gain.gain.setValueAtTime(0.35, t + 1.1);
    gain.gain.linearRampToValueAtTime(0, t + 1.2);
    let node = osc.connect(gain);
    if (ctx.createStereoPanner) {
      const panner = ctx.createStereoPanner();
      panner.pan.value = pan;
      node = node.connect(panner);
    }
    node.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 1.25);
    played.add(name);
  }
  area.append(
    el("p", {class: "msg", text: headphones
      ? "plug headphones into the jack, then play each tone. left should be only in the left ear, right only in the right. unplug them afterwards: the sound should move back to the speakers."
      : "play each tone. left should come only from the left speaker, right only from the right, both from both."}),
    el("div", {class: "row"},
      el("button", {type: "button", onclick: () => play(-1, "left", 440)}, "left"),
      el("button", {type: "button", onclick: () => play(1, "right", 660)}, "right"),
      el("button", {type: "button", onclick: () => play(0, "both", 550)}, "both")));
  return {
    stop() { if (ctx) { ctx.close(); ctx = null; } },
    observed: () => ({tones_played: [...played]}),
  };
}

function microphone(area) {
  let stream = null, ctx = null, raf = 0, rec = null, url = null, closed = false;
  let peak = 0, recorded = false, played = false, device = "";
  const audio = el("audio");
  audio.addEventListener("play", () => { played = true; });
  const status = el("p", {class: "msg", text: "press start microphone; the bar should move when you speak."});
  const fill = el("div", {class: "meter-fill"});
  const meter = el("div", {class: "meter", role: "meter", "aria-label": "microphone level",
    "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0"}, fill);
  const startBtn = el("button", {type: "button"}, "start microphone");
  const recBtn = el("button", {type: "button"}, "record 3 s");
  const playBtn = el("button", {type: "button"}, "play it back");
  recBtn.disabled = playBtn.disabled = true;
  area.append(status, el("div", {class: "row"}, meter), el("div", {class: "row"}, startBtn, recBtn, playBtn));

  function stopStream() {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  }
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia(
        {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
    } catch (e) {
      status.textContent = `the microphone didn't open: ${e.message}`;
      status.className = "warn";
      startBtn.disabled = false;
      return;
    }
    if (closed) { stopStream(); return; }
    const track = stream.getAudioTracks()[0];
    device = track ? track.label : "";
    status.className = "msg";
    status.textContent = `listening${device ? ` on ${device}` : ""}. speak; the bar should move.`;
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const tick = () => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const rms = Math.sqrt(sum / buf.length);
      const level = Math.max(0, Math.min(1, (20 * Math.log10(rms + 1e-9) + 60) / 60));
      peak = Math.max(peak, level);
      fill.style.width = `${(level * 100).toFixed(0)}%`;
      meter.setAttribute("aria-valuenow", (level * 100).toFixed(0));
      raf = requestAnimationFrame(tick);
    };
    tick();
    if (typeof MediaRecorder === "undefined") status.textContent += " (this browser can't record)";
    else recBtn.disabled = false;
  });
  recBtn.addEventListener("click", () => {
    if (!stream) return;
    recBtn.disabled = playBtn.disabled = true;
    const chunks = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      if (closed) return;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(new Blob(chunks, {type: rec.mimeType || "audio/webm"}));
      audio.src = url;
      recorded = true;
      recBtn.disabled = playBtn.disabled = false;
      status.textContent = "recorded 3 s; playing it back. you should hear yourself clearly.";
      audio.play().catch(() => { status.textContent = "recorded 3 s. press play it back."; });
    };
    rec.start();
    status.textContent = "recording 3 s… speak now.";
    setTimeout(() => { if (rec && rec.state === "recording") rec.stop(); }, 3000);
  });
  playBtn.addEventListener("click", () => { audio.currentTime = 0; audio.play().catch(() => {}); });
  return {
    stop() {
      closed = true;
      cancelAnimationFrame(raf);
      if (rec && rec.state !== "inactive") { try { rec.stop(); } catch (e) { /* done */ } }
      stopStream();
      if (ctx) { ctx.close(); ctx = null; }
      audio.pause();
      audio.removeAttribute("src");
      if (url) { URL.revokeObjectURL(url); url = null; }
    },
    observed: () => ({device, peak_level: round2(peak), recorded, played_back: played}),
  };
}

function camera(area) {
  let stream = null, closed = false, devices = [], index = 0;
  const seen = new Set();
  const video = el("video", {class: "camera", autoplay: "", playsinline: ""});
  video.muted = true;
  const status = el("p", {class: "msg", text: "press start camera; you should see a live picture."});
  const startBtn = el("button", {type: "button"}, "start camera");
  const nextBtn = el("button", {type: "button"}, "next camera");
  nextBtn.hidden = true;
  area.append(status, el("div", {class: "row"}, startBtn, nextBtn), video);

  function stopStream() {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    video.srcObject = null;
  }
  async function open(deviceId) {
    startBtn.disabled = nextBtn.disabled = true;
    stopStream();
    try {
      stream = await navigator.mediaDevices.getUserMedia(
        {video: deviceId ? {deviceId: {exact: deviceId}} : true, audio: false});
    } catch (e) {
      status.textContent = `the camera didn't open: ${e.message}`;
      status.className = "warn";
      startBtn.disabled = nextBtn.disabled = false;
      return;
    }
    if (closed) { stopStream(); return; }
    video.srcObject = stream;
    const track = stream.getVideoTracks()[0];
    const st = track && track.getSettings ? track.getSettings() : {};
    const label = `${(track && track.label) || "camera"}${st.width ? ` (${st.width}×${st.height})` : ""}`;
    seen.add(label);
    status.className = "msg";
    status.textContent = `showing ${label}.`;
    try {
      devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    } catch (e) { devices = []; }
    index = Math.max(0, devices.findIndex((d) => d.deviceId === st.deviceId));
    nextBtn.hidden = devices.length < 2;
    if (devices.length > 1) status.textContent += ` ${devices.length} cameras found; next camera switches.`;
    startBtn.disabled = nextBtn.disabled = false;
  }
  startBtn.addEventListener("click", () => open(null));
  nextBtn.addEventListener("click", () => {
    if (devices.length < 2) return;
    index = (index + 1) % devices.length;
    open(devices[index].deviceId);
  });
  return {
    stop() { closed = true; stopStream(); },
    observed: () => ({cameras_seen: [...seen], cameras_found: devices.length}),
  };
}

function touch(area) {
  const field = el("div", {class: "field"});
  const target = el("div", {class: "target", text: "drop here"});
  const box = el("div", {class: "box", text: "drag me"});
  field.append(target, box);
  const count = el("p", {class: "readout"});
  area.append(
    el("p", {class: "msg", text: "drag the blue box into the dashed square with a finger. then put several fingers on the grey area to see how many touches it counts."}),
    field, count);
  const active = new Set();
  let most = 0, reached = false, draggedWith = null, drag = null;
  const show = () => {
    count.textContent = `touches now: ${active.size} · most at once: ${most} · the screen reports up to ${navigator.maxTouchPoints || 0}`;
  };
  show();
  field.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") return;
    active.add(e.pointerId);
    most = Math.max(most, active.size);
    show();
  });
  const lift = (e) => { if (active.delete(e.pointerId)) show(); };
  field.addEventListener("pointerup", lift);
  field.addEventListener("pointercancel", lift);
  box.addEventListener("pointerdown", (e) => {
    if (drag) return;
    e.preventDefault();
    const b = box.getBoundingClientRect();
    drag = {id: e.pointerId, dx: e.clientX - b.left, dy: e.clientY - b.top, type: e.pointerType};
    box.setPointerCapture(e.pointerId);
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const f = field.getBoundingClientRect();
    const x = Math.max(0, Math.min(f.width - box.offsetWidth, e.clientX - f.left - drag.dx));
    const y = Math.max(0, Math.min(f.height - box.offsetHeight, e.clientY - f.top - drag.dy));
    box.style.left = `${x}px`;
    box.style.top = `${y}px`;
  });
  const drop = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const b = box.getBoundingClientRect();
    const t = target.getBoundingClientRect();
    const cx = b.left + b.width / 2;
    const cy = b.top + b.height / 2;
    if (cx >= t.left && cx <= t.right && cy >= t.top && cy <= t.bottom) {
      reached = true;
      draggedWith = drag.type;
      target.classList.add("hit");
      target.textContent = `reached (${drag.type})`;
    }
    drag = null;
  };
  box.addEventListener("pointerup", drop);
  box.addEventListener("pointercancel", drop);
  return {
    observed: () => ({max_touches_seen: most, reported_max_touch_points: navigator.maxTouchPoints || 0,
      reached_target: reached, dragged_with: draggedWith}),
  };
}

function pen(area) {
  const canvas = el("canvas", {class: "pad"});
  const readout = el("p", {class: "readout", text: "touch the pad with the pen."});
  const clear = el("button", {type: "button"}, "clear");
  area.append(
    el("p", {class: "msg", text: "draw with the pen: lines get thicker as you press harder. tilt the pen and watch the numbers; try the eraser end and the side button if it has them."}),
    canvas, readout, el("div", {class: "row"}, clear));
  const ctx = canvas.getContext("2d");
  const size = () => {
    const r = canvas.getBoundingClientRect();
    const d = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * d));
    canvas.height = Math.max(1, Math.round(r.height * d));
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.lineCap = ctx.lineJoin = "round";
  };
  size();
  window.addEventListener("resize", size);
  clear.addEventListener("click", () => { ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.restore(); });
  const types = new Set();
  const levels = new Set();
  let maxPressure = 0, maxTilt = 0, eraser = false, hover = false, barrel = false, last = null;
  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return {x: e.clientX - r.left, y: e.clientY - r.top};
  };
  const info = (e) => {
    types.add(e.pointerType);
    if (e.pointerType === "pen") {
      if (e.pressure > 0) levels.add(Math.round(e.pressure * 100));
      maxPressure = Math.max(maxPressure, e.pressure);
      maxTilt = Math.max(maxTilt, Math.abs(e.tiltX || 0), Math.abs(e.tiltY || 0));
      if ((e.buttons & 32) || e.button === 5) eraser = true;
      if ((e.buttons & 2) || e.button === 2) barrel = true;
    }
    readout.textContent = `${e.pointerType} · pressure ${(e.pressure || 0).toFixed(2)} · tilt x ${e.tiltX || 0}° y ${e.tiltY || 0}° · twist ${e.twist || 0}° · buttons ${e.buttons}`;
  };
  const stroke = (a, b, e) => {
    const erase = (e.buttons & 32) !== 0;
    const p = e.pressure > 0 ? e.pressure : 0.5;
    ctx.globalCompositeOperation = erase ? "destination-out" : "source-over";
    ctx.strokeStyle = "#1d1f21";
    ctx.lineWidth = erase ? 20 : 1 + p * 9;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  };
  canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    last = pos(e);
    info(e);
  });
  canvas.addEventListener("pointermove", (e) => {
    info(e);
    if (e.pointerType === "pen" && e.buttons === 0) hover = true;  // moving above the pad, not touching
    if (!last || !(e.buttons & (1 | 32))) return;
    const list = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    for (const ce of list.length ? list : [e]) {
      const p = pos(ce);
      stroke(last, p, ce);
      last = p;
    }
  });
  const up = (e) => { last = null; info(e); };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  return {
    stop() { window.removeEventListener("resize", size); },
    observed: () => ({pointer_types: [...types], max_pressure: round2(maxPressure),
      pressure_levels_seen: levels.size, max_tilt_degrees: maxTilt, eraser_seen: eraser,
      side_button_seen: barrel, hover_seen: hover}),
  };
}

function keys(area) {
  const big = el("p", {class: "big", text: "press a key"});
  const countEl = el("p", {class: "readout", text: "0 different keys"});
  const chips = el("div", {class: "chips"});
  area.append(
    el("p", {class: "msg", text: "press every key, including the function row and the arrows. each key shows below once it's been pressed. keys for volume, brightness and the like act on the system instead: check them by watching the screen. typing in the note isn't counted."}),
    big, countEl, chips);
  const seen = new Map();
  let presses = 0, fresh = null;
  const onKey = (e) => {
    if (e.target && e.target.closest && e.target.closest("textarea, input")) return;
    e.preventDefault();
    presses += 1;
    const code = e.code || e.key || "unknown";
    big.textContent = `${e.key === " " ? "space" : e.key}  (${code})`;
    let chip = seen.get(code);
    if (!chip) {
      chip = el("span", {class: "chip", text: code});
      seen.set(code, chip);
      chips.append(chip);
    }
    if (fresh) fresh.classList.remove("fresh");
    chip.classList.add("fresh");
    fresh = chip;
    countEl.textContent = `${seen.size} different keys, ${presses} presses`;
  };
  document.addEventListener("keydown", onKey, true);
  return {
    stop() { document.removeEventListener("keydown", onKey, true); },
    observed: () => ({keys_seen: seen.size, presses, codes: [...seen.keys()].slice(0, 200)}),
  };
}

function liveView(area, check) {
  const now = el("div", {class: "live-now"}, el("p", {text: "reading…"}));
  const seen = el("ol", {class: "seen"});
  area.append(now, el("p", {class: "msg", text: "seen during this check:"}), seen);
  return {
    update(s) {
      const lv = s.live && s.live.check === check.id ? s.live : null;
      const r = lv && lv.reading;
      if (!r) {
        now.className = "live-now";
        now.replaceChildren(el("p", {text: "reading…"}));
        seen.replaceChildren();
        return;
      }
      if (!r.read) {
        now.className = "live-now cant";
        now.replaceChildren(
          el("p", {text: `can't read: ${r.error}`}),
          el("p", {class: "msg", text: "this check stays manual: answer by what you see."}));
      } else {
        now.className = "live-now";
        now.replaceChildren(
          el("p", {}, el("strong", {text: r.summary})),
          el("ul", {}, ...r.lines.map((line) => el("li", {text: line}))),
          el("p", {class: "msg", text: `read by ${r.source} at ${clock(r.at)}`}));
      }
      seen.replaceChildren(...lv.seen.map((x) => el("li", {text: `${clock(x.at)}  ${x.summary}`})));
    },
  };
}

function hibernate(area, check, s) {
  const h = s.hibernate;
  const p = (text, cls) => el("p", {class: cls || "", text});
  if (check.preset) {
    // detection skipped it (too little swap, say): no button to start it
    area.append(p(`detection skipped this check: ${check.note || "no reason given"}`, "warn"));
    return null;
  }
  if (h.phase === "idle" || h.phase === "failed") {
    if (h.phase === "failed") area.append(p(`hibernate didn't start: ${h.error || "no reason given"}`, "warn"));
    area.append(p("save anything open in other windows first. the button records that hibernate is under way, then the machine hibernates. once the screen is dark and the machine is off, turn it back on with the power button; this page will ask how it went."));
    const btn = el("button", {type: "button"}, h.phase === "failed" ? "try hibernate again" : "hibernate now");
    btn.disabled = !h.others_done;
    if (!h.others_done) area.append(p("answer the other checks first.", "warn"));
    btn.addEventListener("click", async () => {
      if (!window.confirm("hibernate this machine now?")) return;
      btn.disabled = true;
      try {
        render(await api("/api/hibernate", {}));
      } catch (e) {
        $("answer-msg").textContent = e.message;
        btn.disabled = false;
      }
    });
    area.append(el("div", {class: "row"}, btn));
  } else if (h.phase === "pending" || h.phase === "hibernating") {
    area.append(
      p("hibernating… the machine should power off in a few seconds. once it's off, turn it back on with the power button. this page will ask how it went."),
      p("if nothing happens within a minute, answer doesn't work and say what you saw.", "msg"));
  } else if (h.phase === "resumed") {
    area.append(p(`the machine came back from hibernate${h.slept_seconds ? ` after ${h.slept_seconds} s` : ""}, in the same boot. check that everything came back: this page, wifi, sound, the keyboard, touch and the pen. then answer.`));
  } else if (h.phase === "unknown") {
    area.append(p("hwcheck can't read the boot id, so it can't tell whether the machine resumed. did everything come back where it left off? answer by what you see."));
  } else if (h.phase === "fresh-boot") {
    area.append(
      p("the machine started fresh instead of resuming from hibernate, so hibernate is recorded as doesn't work.", "warn"),
      p("in the note, say what you saw: did it power off? did it show an error, or start at the login screen? then press save."));
  }
  return null;
}

function manual(area) {
  area.append(el("p", {class: "msg", text: "follow the instructions above, then answer."}));
  return null;
}

const BUILDERS = {
  speakers: (area) => tones(area, false),
  headphones: (area) => tones(area, true),
  microphone,
  camera,
  touch,
  pen,
  keys,
  rotation: liveView,
  "tablet-mode": liveView,
  lid: liveView,
  power: liveView,
  "external-display": liveView,
  bluetooth: liveView,
  hibernate,
  manual,
};

// ------------------------------------------------------------------ start

document.addEventListener("DOMContentLoaded", async () => {
  $("btn-works").addEventListener("click", () => answer("works"));
  $("btn-doesnt-work").addEventListener("click", () => answer("doesnt-work"));
  $("btn-skip").addEventListener("click", () => answer("skip"));
  $("note").addEventListener("input", () => {
    if (state && state.current) drafts[state.current] = $("note").value;
  });
  window.addEventListener("pagehide", stopTest);
  if (!TOKEN) {
    $("conn").textContent = "open the address hwcheck printed, with its ?t= part.";
    return;
  }
  try {
    render(await api("/api/state"));
  } catch (e) {
    $("conn").textContent = `can't reach hwcheck: ${e.message}`;
    return;  // a wrong or stale address: retrying the stream wouldn't help
  }
  connect();
});
