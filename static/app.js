"use strict";

const $ = (id) => document.getElementById(id);
const APP_TITLE = "AutoClick";
const THEME_KEY = "autoclick:theme";
const darkQuery = matchMedia("(prefers-color-scheme: dark)");

let presets = [];
let activePresetId = null;
let settings = { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 };
let runnerState = null;
let recorderState = null;
let recordedSteps = [];
let recMode = "both";
let clickerTargetMode = "cursor";
let clickerPoints = []; // [{x, y}]
let pollTimer = null;
let lastSection = "home";
let latestVersion = null;

// Modal state for Sequence Builder step editing
let modalCtx = null; // { mode: "add"|"edit", groupKey?: string, parentSteps?: Array, index: number, depth: number }
let capturingKeys = false;
let dragState = null; // { fromIndex, groupKey }
let saveSeq = 0;

function moveItem(list, from, to) {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return false;
  const [item] = list.splice(from, 1);
  list.splice(to, 0, item);
  return true;
}

function attachRowDrag(tr, getList, idx, groupKey, onReorder) {
  tr.draggable = true;
  tr.addEventListener("dragstart", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    const insideControl = typeof e.target.closest === "function" && e.target.closest("input, button, select, textarea");
    if (tag === "input" || tag === "button" || tag === "select" || tag === "textarea" || insideControl) {
      e.preventDefault();
      return;
    }
    dragState = { fromIndex: idx, groupKey };
    tr.classList.add("dragging");
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(idx));
    }
  });
  tr.addEventListener("dragenter", (e) => {
    if (!dragState || dragState.groupKey !== groupKey) return;
    e.preventDefault();
  });
  tr.addEventListener("dragover", (e) => {
    if (!dragState || dragState.groupKey !== groupKey) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    const rect = tr.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    tr.classList.toggle("drag-over-top", !after);
    tr.classList.toggle("drag-over-bottom", after);
  });
  tr.addEventListener("dragleave", (e) => {
    if (e && e.relatedTarget && typeof tr.contains === "function" && tr.contains(e.relatedTarget)) return;
    tr.classList.remove("drag-over-top", "drag-over-bottom");
  });
  tr.addEventListener("drop", (e) => {
    tr.classList.remove("drag-over-top", "drag-over-bottom");
    if (!dragState || dragState.groupKey !== groupKey) return;
    e.preventDefault();
    const rect = tr.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    let targetIdx = after ? idx + 1 : idx;
    if (dragState.fromIndex < targetIdx) targetIdx -= 1;
    const fromIdx = dragState.fromIndex;
    dragState = null;
    const list = typeof getList === "function" ? getList() : getList;
    if (list && moveItem(list, fromIdx, targetIdx)) onReorder();
  });
  tr.addEventListener("dragend", () => {
    tr.classList.remove("dragging", "drag-over-top", "drag-over-bottom");
    document.querySelectorAll("tr.drag-over-top, tr.drag-over-bottom").forEach((el) =>
      el.classList.remove("drag-over-top", "drag-over-bottom"));
    dragState = null;
  });
}

function makeGrip() {
  const span = document.createElement("span");
  span.className = "drag-grip";
  span.title = "Drag to reorder";
  span.innerHTML = `<svg class="ic"><use href="#i-grip"/></svg>`;
  return span;
}

function highlightActiveSteps(activeStepId) {
  document.querySelectorAll("tr[data-step-id]").forEach((tr) => {
    tr.classList.toggle("active-step", Boolean(activeStepId) && tr.dataset.stepId === activeStepId);
  });
}

// ---------------------------------------------------------------- helpers

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("toast");
  $("toast-text").textContent = msg;
  $("toast-icon").setAttribute("href", isError ? "#i-alert" : "#i-check");
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 5500 : 2800);
}

function desktop() { return window.pywebview && window.pywebview.api; }
function uid() { return Math.random().toString(16).slice(2, 10); }
function activePreset() { return presets.find((p) => p.id === activePresetId) || presets[0]; }

function countLeaves(steps) {
  let n = 0;
  for (const s of steps || []) {
    if (s.enabled === false) continue;
    if (s.action === "group") n += countLeaves(s.steps || []) * (s.repeat || 1);
    else n += 1;
  }
  return n;
}

function maxDepth(steps, d = 1) {
  if (!steps || !steps.length) return 0;
  let best = d;
  for (const s of steps) {
    if (s.action === "group") {
      best = Math.max(best, maxDepth(s.steps || [], d + 1) || (d + 1));
    }
  }
  return best;
}

function fmtKey(k) {
  if (!k) return "";
  return k.length === 1 ? k.toUpperCase() : k.charAt(0).toUpperCase() + k.slice(1);
}

function renderActionCell(step) {
  const wrap = document.createElement("div");
  wrap.className = "step-detail";
  if (step.action === "hotkey") {
    (step.keys || []).forEach((k, idx) => {
      if (idx > 0) wrap.append("+");
      const kbd = document.createElement("kbd");
      kbd.textContent = fmtKey(k);
      wrap.append(kbd);
    });
  } else if (step.action === "key") {
    const kbd = document.createElement("kbd");
    kbd.textContent = fmtKey(step.key);
    wrap.append(kbd);
    if ((step.presses || 1) > 1) {
      const span = document.createElement("span");
      span.className = "muted";
      span.textContent = `×${step.presses}`;
      wrap.append(span);
    }
  } else if (step.action === "type") {
    const span = document.createElement("span");
    const txt = step.text || "";
    span.textContent = `"${txt.length > 36 ? txt.slice(0, 33) + "…" : txt}"`;
    wrap.append(span);
  } else if (step.action === "click") {
    const btn = fmtKey(step.button || "left");
    const kind = step.clicks === 2 ? "Double-click" : step.clicks > 2 ? `${step.clicks}× click` : "Click";
    const pos = step.x != null && step.y != null ? `(${step.x}, ${step.y})` : "at cursor";
    wrap.append(document.createTextNode(`${btn} ${kind} ${pos}`));
  } else if (step.action === "wait") {
    wrap.append(document.createTextNode(`Pause ${step.seconds}s`));
  } else if (step.action === "group") {
    const b = document.createElement("b");
    b.textContent = step.name || "Sub-steps";
    const badge = document.createElement("span");
    badge.className = "badge group";
    badge.textContent = `×${step.repeat || 1} · ${(step.steps || []).length} sub`;
    wrap.append(b, badge);
  }
  if (step.note) {
    const note = document.createElement("span");
    note.className = "step-note";
    note.textContent = `— ${step.note}`;
    wrap.append(note);
  }
  return wrap;
}

// ---------------------------------------------------------------- theme & menu

function themeChoice() { return localStorage.getItem(THEME_KEY) || "system"; }

function applyTheme(animate = false) {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && darkQuery.matches);
  if (animate) {
    document.body.classList.add("theme-anim");
    setTimeout(() => document.body.classList.remove("theme-anim"), 250);
  }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelectorAll("#theme-switch button").forEach((b) =>
    b.classList.toggle("active", b.dataset.themeChoice === choice));
  if (desktop()) desktop().set_dark_title_bar(dark).catch(() => {});
}

darkQuery.addEventListener("change", () => { if (themeChoice() === "system") applyTheme(true); });

function toggleMenu(open) {
  const show = open ?? $("menu").classList.contains("hidden");
  $("menu").classList.toggle("hidden", !show);
  $("menu-btn").setAttribute("aria-expanded", String(show));
}

// ---------------------------------------------------------------- navigation

const SECTIONS = ["home", "recorder", "clicker", "sequences"];

function showSection(id) {
  lastSection = id;
  for (const s of SECTIONS) $(s).classList.toggle("hidden", s !== id);
  $("home-btn").classList.toggle("hidden", id === "home");
  $("workspaces").classList.toggle("hidden", id === "home");
  document.querySelectorAll("[data-workspace]").forEach((b) =>
    b.classList.toggle("active", b.dataset.workspace === id));
  const titles = {
    home: APP_TITLE,
    recorder: `Record & Replay · ${APP_TITLE}`,
    clicker: `Auto Clicker · ${APP_TITLE}`,
    sequences: `Sequences · ${APP_TITLE}`,
  };
  document.title = titles[id] || APP_TITLE;
  if (id === "home") renderHomePresets();
  if (id === "sequences") renderSequenceWorkspace();
  if (id === "clicker") renderClickerPoints();
}

// ---------------------------------------------------------------- home

function renderHomePresets() {
  const tbody = $("home-presets-body");
  tbody.replaceChildren();
  $("home-preset-count").textContent = `${presets.length} saved`;
  for (const p of presets) {
    const tr = document.createElement("tr");
    const leaves = countLeaves(p.steps);
    const depth = maxDepth(p.steps) || 1;
    const tdName = document.createElement("td");
    const b = document.createElement("b");
    b.textContent = p.name;
    tdName.append(b);

    const tdSteps = document.createElement("td");
    tdSteps.textContent = `${leaves} step${leaves === 1 ? "" : "s"} · Depth ${depth}`;

    const tdCool = document.createElement("td");
    tdCool.textContent = `${p.start_delay}s`;

    const tdIter = document.createElement("td");
    tdIter.textContent = p.infinite ? "Until stopped" : `${p.iterations}×`;

    const tdAct = document.createElement("td");
    tdAct.className = "actions";
    const btnOpen = document.createElement("button");
    btnOpen.textContent = "Edit";
    btnOpen.addEventListener("click", async () => {
      activePresetId = p.id;
      await api("POST", "/api/presets/select", { id: p.id }).catch(() => {});
      showSection("sequences");
    });
    const btnRun = document.createElement("button");
    btnRun.className = "go";
    btnRun.textContent = "Run";
    btnRun.addEventListener("click", async () => {
      activePresetId = p.id;
      await api("POST", "/api/presets/select", { id: p.id }).catch(() => {});
      showSection("sequences");
      startSequenceRun();
    });
    tdAct.append(btnOpen, btnRun);

    tr.append(tdName, tdSteps, tdCool, tdIter, tdAct);
    tbody.append(tr);
  }
}

// ---------------------------------------------------------------- settings sync

function syncSettingsUI() {
  $("set-stop-key").value = settings.stop_key || "f8";
  $("rec-stop-key").value = settings.record_stop_key || "f9";
  $("set-failsafe").checked = !!settings.failsafe;
  $("set-minimize").checked = !!settings.minimize_on_start;
  $("rec-minimize").checked = !!settings.minimize_on_start;
  $("seq-speed").value = settings.speed || 1;
  $("seq-speed-val").textContent = `${Number(settings.speed || 1).toFixed(2).replace(/\.00$/, ".0")}×`;
  $("rec-speed").value = settings.speed || 1;
  $("rec-speed-val").textContent = `${Number(settings.speed || 1).toFixed(2).replace(/\.00$/, ".0")}×`;

  const stopLbl = (settings.stop_key || "f8").toUpperCase();
  const recLbl = (settings.record_stop_key || "f9").toUpperCase();
  $("home-stop-key").textContent = stopLbl;
  $("home-rec-key").textContent = recLbl;
  document.querySelectorAll(".lbl-stop-key").forEach((el) => { el.textContent = stopLbl; });
  document.querySelectorAll(".lbl-rec-key").forEach((el) => { el.textContent = recLbl; });
}

async function saveSettings(patch) {
  settings = { ...settings, ...patch };
  syncSettingsUI();
  try {
    const res = await api("POST", "/api/settings", patch);
    settings = res.settings;
    syncSettingsUI();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- workspace 1: record & replay

function renderRecordedSteps() {
  const tbody = $("rec-steps-body");
  tbody.replaceChildren();
  $("rec-count-badge").textContent = recordedSteps.length;
  $("rec-t-steps").textContent = recordedSteps.length;
  $("rec-empty").classList.toggle("hidden", recordedSteps.length > 0);
  const canUse = recordedSteps.length > 0 && !(recorderState && recorderState.active);
  $("rec-replay").disabled = !canUse || (runnerState && runnerState.active);
  $("rec-save-btn").disabled = !canUse;
  $("rec-open-builder").disabled = !canUse;
  $("rec-clear").disabled = !canUse;

  recordedSteps.forEach((step, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.stepId = step.id || "";
    if (runnerState && runnerState.active && runnerState.step_id === step.id) tr.classList.add("active-step");
    attachRowDrag(tr, () => recordedSteps, idx, "rec", () => renderRecordedSteps());

    const tdNum = document.createElement("td");
    tdNum.append(makeGrip(), document.createTextNode(String(idx + 1)));

    const tdType = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${step.action}`;
    badge.textContent = fmtKey(step.action);
    tdType.append(badge);

    const tdAct = document.createElement("td");
    tdAct.append(renderActionCell(step));

    const tdHold = document.createElement("td");
    if (step.action === "hotkey" || step.action === "key" || step.action === "click") {
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = "0";
      inp.max = "10000";
      inp.step = "10";
      inp.value = step.hold_ms || 0;
      inp.className = "inline-num";
      inp.title = "Press hold depth in milliseconds";
      inp.addEventListener("change", () => { step.hold_ms = Math.max(0, parseInt(inp.value, 10) || 0); });
      tdHold.append(inp, " ms");
    } else {
      tdHold.textContent = "—";
    }

    const tdDelay = document.createElement("td");
    const dInp = document.createElement("input");
    dInp.type = "number";
    dInp.min = "0";
    dInp.max = "3600";
    dInp.step = "0.05";
    dInp.value = step.action === "wait" ? step.seconds : (step.delay ?? 0.1);
    dInp.className = "inline-num";
    dInp.addEventListener("change", () => {
      const v = Math.max(0, parseFloat(dInp.value) || 0);
      if (step.action === "wait") step.seconds = Math.max(0.01, v);
      else step.delay = v;
    });
    tdDelay.append(dInp, " s");

    const tdOps = document.createElement("td");
    tdOps.className = "col-act";
    const upBtn = makeIconBtn("i-up", "Move up", () => {
      if (moveItem(recordedSteps, idx, idx - 1)) renderRecordedSteps();
    }, idx === 0);
    const downBtn = makeIconBtn("i-down", "Move down", () => {
      if (moveItem(recordedSteps, idx, idx + 1)) renderRecordedSteps();
    }, idx >= recordedSteps.length - 1);
    const delBtn = makeIconBtn("i-trash", "Remove step", () => {
      recordedSteps.splice(idx, 1);
      renderRecordedSteps();
    }, false, true);
    tdOps.append(upBtn, downBtn, delBtn);

    tr.append(tdNum, tdType, tdAct, tdHold, tdDelay, tdOps);
    tbody.append(tr);
  });
}

async function startRecording() {
  try {
    const cooloff = Math.max(0, parseFloat($("rec-cooloff").value) || 0);
    const stopKey = $("rec-stop-key").value;
    await saveSettings({ record_stop_key: stopKey, minimize_on_start: $("rec-minimize").checked });
    const res = await api("POST", "/api/record/start", {
      mode: recMode,
      cooloff,
      stop_key: stopKey,
      capture_delays: $("rec-delays").checked,
      minimize_on_start: $("rec-minimize").checked,
    });
    recorderState = res.recorder;
    recordedSteps = [];
    renderRecordedSteps();
    updateLiveUI();
    ensurePolling();
  } catch (e) {
    toast(e.message, true);
  }
}

async function stopRecording() {
  try {
    const res = await api("POST", "/api/record/stop");
    recorderState = res.recorder;
    if (recorderState && Array.isArray(recorderState.steps)) {
      recordedSteps = recorderState.steps.map((s) => ({ ...s }));
      renderRecordedSteps();
    }
    updateLiveUI();
    if (recorderState && recorderState.active) ensurePolling();
  } catch (e) {
    toast(e.message, true);
  }
}

async function replayRecorded() {
  if (!recordedSteps.length) return toast("Record at least one step first.", true);
  const preset = {
    id: "recorded-temp",
    name: $("rec-save-name").value.trim() || "Recorded Macro",
    start_delay: Math.max(0, parseFloat($("rec-replay-cooloff").value) || 0),
    iterations: Math.max(1, parseInt($("rec-iterations").value, 10) || 1),
    infinite: $("rec-infinite").checked,
    steps: recordedSteps,
  };
  try {
    const res = await api("POST", "/api/run/start", {
      preset,
      save: false,
      minimize_on_start: $("rec-minimize").checked,
      speed: parseFloat($("rec-speed").value) || 1.0,
    });
    runnerState = res.runner;
    updateLiveUI();
    ensurePolling();
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveRecordedAsPreset(openInBuilder = false) {
  if (!recordedSteps.length) return;
  const name = $("rec-save-name").value.trim() || `Recorded ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  try {
    const res = await api("POST", "/api/presets/create", {
      name,
      start_delay: Math.max(0, parseFloat($("rec-replay-cooloff").value) || 5),
      iterations: Math.max(1, parseInt($("rec-iterations").value, 10) || 10),
      infinite: $("rec-infinite").checked,
      steps: recordedSteps,
    });
    ++saveSeq;
    presets = res.presets;
    activePresetId = res.active_preset_id;
    toast(`Saved "${name}" to Sequences`);
    if (openInBuilder) showSection("sequences");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- workspace 2: auto clicker

function renderClickerPoints() {
  $("clk-single-box").classList.toggle("hidden", clickerTargetMode !== "single");
  $("clk-multi-box").classList.toggle("hidden", clickerTargetMode !== "multi");
  $("clk-points-clear").classList.toggle("hidden", clickerTargetMode !== "multi" || !clickerPoints.length);

  const tbody = $("clk-points-body");
  tbody.replaceChildren();
  const btn = fmtKey($("clk-button").value);
  const clicks = parseInt($("clk-clicks").value, 10) || 1;
  const hold = parseInt($("clk-hold").value, 10) || 0;
  const interval = parseFloat($("clk-interval").value) || 0.5;
  const kind = clicks === 2 ? "Double" : clicks === 3 ? "Triple" : "Single";
  const depthTxt = hold > 0 ? ` · hold ${hold}ms` : "";

  if (clickerTargetMode === "cursor") {
    $("clk-points-badge").textContent = "Cursor";
    $("clk-points-empty").classList.remove("hidden");
    return;
  }
  if (clickerTargetMode === "single") {
    const x = $("clk-x").value;
    const y = $("clk-y").value;
    $("clk-points-badge").textContent = x && y ? `(${x}, ${y})` : "Pick point";
    $("clk-points-empty").classList.add("hidden");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>1</td><td><b>${x && y ? `(${x}, ${y})` : "Current Cursor (set X/Y or Pick)"}</b></td><td>${btn} ${kind}${depthTxt}</td><td>${interval}s</td><td></td>`;
    tbody.append(tr);
    return;
  }

  $("clk-points-badge").textContent = `${clickerPoints.length} point${clickerPoints.length === 1 ? "" : "s"}`;
  $("clk-points-empty").classList.toggle("hidden", clickerPoints.length > 0);
  if (!clickerPoints.length) {
    $("clk-points-empty").innerHTML = `No points added yet. Click <b>Pick next point (3s)</b> to capture screen coordinates.`;
    return;
  }
  clickerPoints.forEach((pt, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.stepId = `clk-${idx}`;
    attachRowDrag(tr, () => clickerPoints, idx, "clk", () => renderClickerPoints());

    const tdNum = document.createElement("td");
    tdNum.append(makeGrip(), document.createTextNode(String(idx + 1)));
    const tdPos = document.createElement("td");
    const bPos = document.createElement("b");
    bPos.textContent = `(${pt.x}, ${pt.y})`;
    tdPos.append(bPos);
    const tdBtn = document.createElement("td");
    tdBtn.textContent = `${btn} ${kind}${depthTxt}`;
    const tdInt = document.createElement("td");
    tdInt.textContent = `${interval}s`;

    const tdOps = document.createElement("td");
    tdOps.className = "col-act";
    const upBtn = makeIconBtn("i-up", "Move up", () => {
      if (moveItem(clickerPoints, idx, idx - 1)) renderClickerPoints();
    }, idx === 0);
    const downBtn = makeIconBtn("i-down", "Move down", () => {
      if (moveItem(clickerPoints, idx, idx + 1)) renderClickerPoints();
    }, idx >= clickerPoints.length - 1);
    const del = makeIconBtn("i-trash", "Remove point", () => {
      clickerPoints.splice(idx, 1);
      renderClickerPoints();
    }, false, true);
    tdOps.append(upBtn, downBtn, del);
    tr.append(tdNum, tdPos, tdBtn, tdInt, tdOps);
    tbody.append(tr);
  });
}

function buildClickerPreset() {
  const button = $("clk-button").value;
  const clicks = Math.max(1, parseInt($("clk-clicks").value, 10) || 1);
  const hold_ms = Math.max(0, parseInt($("clk-hold").value, 10) || 0);
  const delay = Math.max(0.01, parseFloat($("clk-interval").value) || 0.5);
  let steps = [];

  if (clickerTargetMode === "multi") {
    if (!clickerPoints.length) throw new Error("Pick at least one screen point in Multi-Point mode.");
    steps = clickerPoints.map((pt, i) => ({
      id: `clk-${i}`, action: "click", x: pt.x, y: pt.y, button, clicks, hold_ms, delay, enabled: true, note: `Point ${i + 1}`,
    }));
  } else if (clickerTargetMode === "single") {
    const x = $("clk-x").value !== "" ? parseInt($("clk-x").value, 10) : null;
    const y = $("clk-y").value !== "" ? parseInt($("clk-y").value, 10) : null;
    steps = [{ id: "clk-0", action: "click", x, y, button, clicks, hold_ms, delay, enabled: true, note: "Target point" }];
  } else {
    steps = [{ id: "clk-0", action: "click", x: null, y: null, button, clicks, hold_ms, delay, enabled: true, note: "Current cursor" }];
  }

  return {
    id: "clicker-temp",
    name: `Auto Clicker (${clickerTargetMode})`,
    start_delay: Math.max(0, parseFloat($("clk-cooloff").value) || 0),
    iterations: Math.max(1, parseInt($("clk-iterations").value, 10) || 1),
    infinite: $("clk-infinite").checked,
    steps,
  };
}

async function pickScreenPoint(buttonEl, onPicked) {
  const orig = buttonEl.innerHTML;
  buttonEl.disabled = true;
  buttonEl.textContent = "Move mouse to target… (3s)";
  try {
    const res = await api("POST", "/api/cursor/capture", { delay: 3 });
    onPicked(res.x, res.y);
    toast(`Captured point (${res.x}, ${res.y})`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    buttonEl.disabled = false;
    buttonEl.innerHTML = orig;
  }
}

// ---------------------------------------------------------------- workspace 3: sequences

function findGroupSteps(steps, targetGroupId) {
  for (const s of steps || []) {
    if (s.action === "group") {
      if (s.id === targetGroupId) {
        if (!Array.isArray(s.steps)) s.steps = [];
        return s.steps;
      }
      const nested = findGroupSteps(s.steps, targetGroupId);
      if (nested) return nested;
    }
  }
  return null;
}

function resolveSequenceList(groupKey) {
  const p = activePreset();
  if (!p) return null;
  if (!Array.isArray(p.steps)) p.steps = [];
  if (!groupKey || groupKey === "root") return p.steps;
  if (groupKey.startsWith("grp-")) return findGroupSteps(p.steps, groupKey.slice(4));
  return null;
}

let saveDebounce = null;
function scheduleSavePreset() {
  const ticket = ++saveSeq;
  const targetId = activePresetId;
  clearTimeout(saveDebounce);
  saveDebounce = setTimeout(async () => {
    const p = presets.find((x) => x.id === targetId) || activePreset();
    if (!p) return;
    try {
      const res = await api("POST", "/api/presets/save", { preset: p });
      // Ignore stale responses if the user made newer edits while this save was in flight.
      if (ticket !== saveSeq) return;
      presets = res.presets;
      if (!activePresetId || activePresetId === targetId) {
        activePresetId = res.active_preset_id;
      }
    } catch (e) {
      toast(e.message, true);
    }
  }, 150);
}

function renderSequenceWorkspace() {
  const p = activePreset();
  if (!p) return;

  const sel = $("seq-select");
  sel.replaceChildren();
  for (const item of presets) {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.name;
    opt.selected = item.id === p.id;
    sel.append(opt);
  }
  $("seq-del").disabled = presets.length <= 1;
  $("seq-del").title = presets.length <= 1 ? "At least one sequence must remain" : "Delete sequence";
  $("seq-name").value = p.name;
  $("seq-cooloff").value = p.start_delay;
  $("seq-iterations").value = p.iterations;
  $("seq-infinite").checked = !!p.infinite;
  $("seq-iterations").disabled = !!p.infinite;

  const leaves = countLeaves(p.steps);
  const depth = maxDepth(p.steps) || 1;
  $("seq-step-count").textContent = leaves;
  $("seq-depth-badge").textContent = `Max Depth ${depth}`;

  const tbody = $("seq-steps-body");
  tbody.replaceChildren();
  $("seq-empty").classList.toggle("hidden", p.steps.length > 0);

  let rowNum = 0;
  function appendRows(list, depthLevel, groupKey) {
    list.forEach((step, idx) => {
      rowNum += 1;
      const tr = document.createElement("tr");
      tr.dataset.stepId = step.id || "";
      if (step.enabled === false) tr.classList.add("disabled-step");
      if (step.action === "group") tr.classList.add("group-row");
      if (runnerState && runnerState.active && runnerState.step_id === step.id) tr.classList.add("active-step");
      attachRowDrag(tr, () => resolveSequenceList(groupKey), idx, groupKey, () => {
        renderSequenceWorkspace();
        scheduleSavePreset();
      });

      const tdChk = document.createElement("td");
      tdChk.className = "col-chk";
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = step.enabled !== false;
      chk.addEventListener("change", () => {
        const liveList = resolveSequenceList(groupKey);
        if (!liveList || !liveList[idx]) return;
        liveList[idx].enabled = chk.checked;
        renderSequenceWorkspace();
        scheduleSavePreset();
      });
      tdChk.append(chk);

      const tdNum = document.createElement("td");
      tdNum.append(makeGrip(), document.createTextNode(String(rowNum)));

      const tdType = document.createElement("td");
      const indent = document.createElement("span");
      indent.className = "depth-indent";
      indent.style.paddingLeft = `${(depthLevel - 1) * 16}px`;
      if (depthLevel > 1) {
        const dt = document.createElement("span");
        dt.className = "depth-tag";
        dt.textContent = `L${depthLevel}`;
        indent.append(dt);
      }
      const badge = document.createElement("span");
      badge.className = `badge ${step.action}`;
      badge.textContent = step.action === "group" ? "Group" : fmtKey(step.action);
      indent.append(badge);
      tdType.append(indent);

      const tdDetail = document.createElement("td");
      tdDetail.append(renderActionCell(step));

      const tdHold = document.createElement("td");
      tdHold.textContent = (step.hold_ms && step.hold_ms > 0) ? `${step.hold_ms} ms` : "—";

      const tdDelay = document.createElement("td");
      tdDelay.textContent = step.action === "wait" ? `${step.seconds}s` : `${step.delay ?? 0}s`;

      const tdAct = document.createElement("td");
      tdAct.className = "col-act";

      if (step.action === "group" && depthLevel < 5) {
        const addSub = document.createElement("button");
        addSub.type = "button";
        addSub.className = "btn ghost sm";
        addSub.textContent = "+ Sub";
        addSub.title = "Add nested sub-step inside this group";
        addSub.addEventListener("click", (e) => {
          e.stopPropagation();
          const subKey = `grp-${step.id}`;
          const subList = resolveSequenceList(subKey);
          if (!subList) return;
          openStepModal({ mode: "add", groupKey: subKey, index: subList.length, depth: depthLevel + 1 }, "hotkey");
        });
        tdAct.append(addSub);
      }

      const editBtn = makeIconBtn("i-edit", "Edit step", () => openStepModal({ mode: "edit", groupKey, index: idx, depth: depthLevel }));
      const upBtn = makeIconBtn("i-up", "Move up", () => {
        const liveList = resolveSequenceList(groupKey);
        if (liveList && moveItem(liveList, idx, idx - 1)) { renderSequenceWorkspace(); scheduleSavePreset(); }
      }, idx === 0);
      const downBtn = makeIconBtn("i-down", "Move down", () => {
        const liveList = resolveSequenceList(groupKey);
        if (liveList && moveItem(liveList, idx, idx + 1)) { renderSequenceWorkspace(); scheduleSavePreset(); }
      }, idx >= list.length - 1);
      const delBtn = makeIconBtn("i-trash", "Delete step", () => {
        const liveList = resolveSequenceList(groupKey);
        if (!liveList || idx >= liveList.length) return;
        liveList.splice(idx, 1);
        renderSequenceWorkspace();
        scheduleSavePreset();
      }, false, true);

      tdAct.append(editBtn, upBtn, downBtn, delBtn);
      tr.append(tdChk, tdNum, tdType, tdDetail, tdHold, tdDelay, tdAct);
      tbody.append(tr);

      if (step.action === "group" && Array.isArray(step.steps)) {
        appendRows(step.steps, depthLevel + 1, `grp-${step.id}`);
      }
    });
  }
  appendRows(p.steps, 1, "root");
}

function makeIconBtn(iconId, title, onClick, disabled = false, danger = false) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `icon-btn sm${danger ? " danger" : ""}`;
  b.title = title;
  b.disabled = disabled;
  b.innerHTML = `<svg class="ic"><use href="#${iconId}"/></svg>`;
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick(e);
  });
  return b;
}

async function startSequenceRun() {
  const p = activePreset();
  if (!p) return;
  try {
    const res = await api("POST", "/api/run/start", { preset: p, save: true });
    runnerState = res.runner;
    updateLiveUI();
    ensurePolling();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- step editor modal

function setModalType(type) {
  document.querySelectorAll("#m-type button").forEach((b) => b.classList.toggle("active", b.dataset.type === type));
  document.querySelectorAll(".m-pane").forEach((el) => el.classList.toggle("hidden", el.dataset.pane !== type));
  const hasHold = type === "hotkey" || type === "key" || type === "click";
  const hasDelay = type !== "wait";
  $("m-hold-wrap").classList.toggle("hidden", !hasHold);
  $("m-delay-wrap").classList.toggle("hidden", !hasDelay);
  $("m-hold-delay-row").classList.toggle("hidden", !hasHold && !hasDelay);
}

function currentModalType() {
  return document.querySelector("#m-type button.active").dataset.type;
}

function openStepModal(ctx, initialType = "hotkey") {
  const parentSteps = ctx.groupKey ? resolveSequenceList(ctx.groupKey) : ctx.parentSteps;
  if (!parentSteps) return;
  const isEdit = ctx.mode === "edit";
  const step = isEdit ? parentSteps[ctx.index] : null;
  if (isEdit && !step) return;

  modalCtx = ctx;
  capturingKeys = false;
  $("m-capture-keys").textContent = "Capture";
  const groupBtn = document.querySelector('#m-type button[data-type="group"]');
  groupBtn.disabled = ctx.depth >= 5;

  const type = isEdit ? step.action : initialType;

  $("modal-title").textContent = isEdit
    ? `Edit Step (Depth ${ctx.depth})`
    : ctx.depth > 1 ? `Add Sub-Step (Depth ${ctx.depth})` : "Add Step";
  setModalType(type);

  $("m-keys").value = (step && step.keys) ? step.keys.join("+") : "ctrl+c";
  $("m-key").value = (step && step.key) ? step.key : "enter";
  $("m-presses").value = (step && step.presses) ? step.presses : 1;
  $("m-text").value = (step && step.text) ? step.text : "";
  $("m-interval").value = (step && step.interval != null) ? step.interval : 0;
  $("m-x").value = (step && step.x != null) ? step.x : "";
  $("m-y").value = (step && step.y != null) ? step.y : "";
  $("m-button").value = (step && step.button) ? step.button : "left";
  $("m-clicks").value = (step && step.clicks) ? step.clicks : 1;
  $("m-seconds").value = (step && step.seconds != null) ? step.seconds : 1.0;
  $("m-group-name").value = (step && step.name) ? step.name : "Sub-steps";
  $("m-group-repeat").value = (step && step.repeat) ? step.repeat : 3;
  $("m-hold").value = (step && step.hold_ms != null) ? step.hold_ms : 0;
  $("m-delay").value = (step && step.delay != null) ? step.delay : 0.1;
  $("m-note").value = (step && step.note) ? step.note : "";

  $("step-modal").classList.remove("hidden");
}

function closeStepModal() {
  capturingKeys = false;
  $("step-modal").classList.add("hidden");
  modalCtx = null;
}

function commitStepModal() {
  if (!modalCtx) return;
  const parentSteps = modalCtx.groupKey ? resolveSequenceList(modalCtx.groupKey) : modalCtx.parentSteps;
  if (!parentSteps) { closeStepModal(); return; }
  const type = currentModalType();
  const existing = modalCtx.mode === "edit" ? parentSteps[modalCtx.index] : null;
  if (modalCtx.mode === "edit" && !existing) { closeStepModal(); return; }
  const base = {
    id: existing ? existing.id : uid(),
    action: type,
    enabled: existing ? existing.enabled !== false : true,
    note: $("m-note").value.trim(),
  };

  if (type === "hotkey") {
    const keys = $("m-keys").value.split(/[+,]/).map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (!keys.length) return toast("Enter at least one key for the hotkey.", true);
    base.keys = keys;
    base.hold_ms = Math.max(0, parseInt($("m-hold").value, 10) || 0);
    base.delay = Math.max(0, parseFloat($("m-delay").value) || 0);
  } else if (type === "key") {
    const key = $("m-key").value.trim().toLowerCase();
    if (!key) return toast("Enter a key name.", true);
    base.key = key;
    base.presses = Math.max(1, parseInt($("m-presses").value, 10) || 1);
    base.hold_ms = Math.max(0, parseInt($("m-hold").value, 10) || 0);
    base.delay = Math.max(0, parseFloat($("m-delay").value) || 0);
  } else if (type === "type") {
    base.text = $("m-text").value;
    base.interval = Math.max(0, parseFloat($("m-interval").value) || 0);
    base.delay = Math.max(0, parseFloat($("m-delay").value) || 0);
  } else if (type === "click") {
    base.x = $("m-x").value !== "" ? parseInt($("m-x").value, 10) : null;
    base.y = $("m-y").value !== "" ? parseInt($("m-y").value, 10) : null;
    base.button = $("m-button").value;
    base.clicks = Math.max(1, parseInt($("m-clicks").value, 10) || 1);
    base.hold_ms = Math.max(0, parseInt($("m-hold").value, 10) || 0);
    base.delay = Math.max(0, parseFloat($("m-delay").value) || 0);
  } else if (type === "wait") {
    base.seconds = Math.max(0.01, parseFloat($("m-seconds").value) || 1.0);
  } else if (type === "group") {
    base.name = $("m-group-name").value.trim() || "Sub-steps";
    base.repeat = Math.max(1, parseInt($("m-group-repeat").value, 10) || 1);
    base.delay = Math.max(0, parseFloat($("m-delay").value) || 0);
    base.steps = (existing && existing.action === "group" && Array.isArray(existing.steps)) ? existing.steps : [];
  }

  if (modalCtx.mode === "edit") parentSteps[modalCtx.index] = base;
  else parentSteps.splice(Math.min(modalCtx.index, parentSteps.length), 0, base);

  closeStepModal();
  renderSequenceWorkspace();
  scheduleSavePreset();
}

// ---------------------------------------------------------------- live polling & status rendering

function ensurePolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollStatus, 180);
}

function stopPolling() {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

async function pollStatus() {
  try {
    const res = await api("GET", "/api/run/status");
    const prevRecActive = recorderState && recorderState.active;
    runnerState = res.runner;
    recorderState = res.recorder;
    if ((recorderState.active && recorderState.step_count !== recordedSteps.length) ||
        (prevRecActive && !recorderState.active && recorderState.steps.length)) {
      recordedSteps = recorderState.steps.map((s) => ({ ...s }));
      renderRecordedSteps();
    }
    updateLiveUI();
    if (!runnerState.active && !recorderState.active) {
      stopPolling();
    }
  } catch (_) {
    stopPolling();
  }
}

function setLog(el, lines) {
  const txt = (lines || []).join("\n");
  if (el.textContent !== txt) {
    el.textContent = txt;
    el.scrollTop = el.scrollHeight;
  }
}

function updateLiveUI() {
  const run = runnerState || { status: "idle", active: false, iteration: 0, total_iterations: 0, logs: [] };
  const rec = recorderState || { status: "idle", active: false, step_count: 0, logs: [] };

  // Topbar live pill
  const anyActive = run.active || rec.active;
  $("topbar-live").classList.toggle("hidden", !anyActive);
  if (rec.active) {
    $("topbar-live-pill").className = `live-pill ${rec.status === "cooloff" ? "cooloff" : "rec"}`;
    $("topbar-live-pill").textContent = rec.status === "cooloff"
      ? `● Cool-off ${rec.cooloff_remaining.toFixed(1)}s`
      : `● Recording · ${rec.step_count} step${rec.step_count === 1 ? "" : "s"}`;
  } else if (run.active) {
    $("topbar-live-pill").className = `live-pill ${run.status === "countdown" ? "cooloff" : ""}`;
    $("topbar-live-pill").textContent = run.status === "countdown"
      ? `● Cool-off ${run.countdown_remaining.toFixed(1)}s`
      : run.status === "paused"
        ? `● Paused (${run.iteration} / ${run.infinite ? "∞" : run.total_iterations})`
        : `● Running ${run.iteration} / ${run.infinite ? "∞" : run.total_iterations}`;
  }

  // ---- Recorder workspace ----
  $("rec-start").classList.toggle("hidden", rec.active);
  $("rec-start").disabled = run.active;
  $("rec-stop").classList.toggle("hidden", !rec.active);
  $("rec-replay").classList.toggle("hidden", run.active);
  $("rec-replay").disabled = rec.active || !recordedSteps.length;
  $("rec-pause").classList.toggle("hidden", !run.active || run.status === "countdown");
  $("rec-pause").innerHTML = run.status === "paused"
    ? `<svg class="ic"><use href="#i-play"/></svg>Resume`
    : `<svg class="ic"><use href="#i-pause"/></svg>Pause`;
  $("rec-run-stop").classList.toggle("hidden", !run.active);

  const recBanner = $("rec-banner");
  if (rec.status === "cooloff") {
    recBanner.className = "callout";
    recBanner.textContent = `Cool-off: ${rec.cooloff_remaining.toFixed(1)}s — switch to your target window and prepare the scene…`;
  } else if (rec.status === "recording") {
    recBanner.className = "callout rec";
    recBanner.textContent = `● Recording live! Perform your clicks & key presses now. Press ${(rec.stop_key || "F9").toUpperCase()} when finished.`;
  } else if (run.status === "countdown") {
    recBanner.className = "callout";
    recBanner.textContent = `Replay cool-off: ${run.countdown_remaining.toFixed(1)}s — focus your target window…`;
  } else {
    recBanner.classList.add("hidden");
  }

  const recStatusText = rec.active
    ? (rec.status === "cooloff" ? `Cool-off ${rec.cooloff_remaining.toFixed(1)}s` : "Recording")
    : run.active
      ? (run.status === "countdown" ? `Cool-off ${run.countdown_remaining.toFixed(1)}s` : fmtKey(run.status))
      : (recordedSteps.length ? "Ready to Replay" : "Ready");
  $("rec-t-status").textContent = recStatusText;
  $("rec-t-steps").textContent = rec.active ? rec.step_count : recordedSteps.length;
  const recIterTotal = $("rec-infinite").checked ? "∞" : ($("rec-iterations").value || "10");
  $("rec-t-iter").textContent = run.active || run.iteration > 0
    ? `${run.iteration} / ${run.infinite ? "∞" : run.total_iterations}`
    : `0 / ${recIterTotal}`;
  $("rec-t-time").textContent = rec.active
    ? `${(rec.status === "cooloff" ? rec.cooloff_remaining : rec.elapsed).toFixed(1)}s`
    : `${(run.elapsed || 0).toFixed(1)}s`;

  const recBar = $("rec-prog-bar");
  if (rec.status === "cooloff") {
    const total = Math.max(0.1, parseFloat($("rec-cooloff").value) || 5);
    recBar.className = "progress-bar cooloff";
    recBar.style.width = `${Math.min(100, (1 - rec.cooloff_remaining / total) * 100)}%`;
    $("rec-prog-title").textContent = "Setting up scene (cool-off)";
    $("rec-prog-detail").textContent = `${rec.cooloff_remaining.toFixed(1)}s left`;
  } else if (rec.status === "recording") {
    recBar.className = "progress-bar recording";
    recBar.style.width = "100%";
    $("rec-prog-title").textContent = `Recording clicks & keys… (${rec.step_count} captured)`;
    $("rec-prog-detail").textContent = `Press ${(rec.stop_key || "F9").toUpperCase()} to stop`;
  } else if (run.active) {
    applyRunProgress($("rec-prog-bar"), $("rec-prog-title"), $("rec-prog-detail"), run);
  } else {
    recBar.className = run.status === "completed" ? "progress-bar done" : "progress-bar";
    recBar.style.width = run.status === "completed" ? "100%" : "0%";
    $("rec-prog-title").textContent = run.step_label || (recordedSteps.length ? `${recordedSteps.length} step(s) captured and ready to replay` : "Set cool-off time and click Start Recording");
    $("rec-prog-detail").textContent = "";
  }
  setLog($("rec-log"), rec.logs.length && (rec.active || !run.logs.length) ? rec.logs : [...rec.logs, ...run.logs]);

  // ---- Clicker workspace ----
  $("clk-start").classList.toggle("hidden", run.active);
  $("clk-start").disabled = rec.active;
  $("clk-pause").classList.toggle("hidden", !run.active || run.status === "countdown");
  $("clk-pause").innerHTML = run.status === "paused"
    ? `<svg class="ic"><use href="#i-play"/></svg>Resume`
    : `<svg class="ic"><use href="#i-pause"/></svg>Pause`;
  $("clk-stop").classList.toggle("hidden", !run.active);

  $("clk-t-status").textContent = run.status === "countdown" ? `Cool-off ${run.countdown_remaining.toFixed(1)}s` : fmtKey(run.status === "idle" ? "Ready" : run.status);
  const clkTotal = $("clk-infinite").checked ? "∞" : ($("clk-iterations").value || "20");
  $("clk-t-iter").textContent = run.active || run.iteration > 0
    ? `${run.iteration} / ${run.infinite ? "∞" : run.total_iterations}`
    : `0 / ${clkTotal}`;
  $("clk-t-step").textContent = run.step_label || "—";
  $("clk-t-time").textContent = `${(run.elapsed || 0).toFixed(1)}s`;
  applyRunProgress($("clk-prog-bar"), $("clk-prog-title"), $("clk-prog-detail"), run);
  setLog($("clk-log"), run.logs);

  // ---- Sequences workspace ----
  $("seq-start").classList.toggle("hidden", run.active);
  $("seq-start").disabled = rec.active;
  $("seq-pause").classList.toggle("hidden", !run.active || run.status === "countdown");
  $("seq-pause").innerHTML = run.status === "paused"
    ? `<svg class="ic"><use href="#i-play"/></svg>Resume`
    : `<svg class="ic"><use href="#i-pause"/></svg>Pause`;
  $("seq-stop").classList.toggle("hidden", !run.active);

  const p = activePreset();
  $("seq-t-status").textContent = run.status === "countdown" ? `Cool-off ${run.countdown_remaining.toFixed(1)}s` : fmtKey(run.status === "idle" ? "Ready" : run.status);
  $("seq-t-iter").textContent = run.active || run.iteration > 0
    ? `${run.iteration} / ${run.infinite ? "∞" : run.total_iterations}`
    : `0 / ${p ? (p.infinite ? "∞" : p.iterations) : 0}`;
  $("seq-t-step").textContent = run.step_label ? `${run.step_depth > 1 ? `[L${run.step_depth}] ` : ""}${run.step_label}` : "—";
  $("seq-t-time").textContent = `${(run.elapsed || 0).toFixed(1)}s`;
  applyRunProgress($("seq-prog-bar"), $("seq-prog-title"), $("seq-prog-detail"), run);
  setLog($("seq-log"), run.logs);

  // Highlight the active step in-place without rebuilding DOM rows (preserves button clicks during run/pause).
  highlightActiveSteps(run.active ? run.step_id : null);
}

function applyRunProgress(bar, titleEl, detailEl, run) {
  if (run.status === "countdown") {
    bar.className = "progress-bar cooloff";
    bar.style.width = "100%";
    titleEl.textContent = "Cool-off countdown — focus target window";
    detailEl.textContent = `${run.countdown_remaining.toFixed(1)}s`;
  } else if (run.active) {
    bar.className = "progress-bar";
    const totalSteps = !run.infinite && run.total_iterations && run.total_steps ? run.total_iterations * run.total_steps : 0;
    const pct = totalSteps ? Math.min(100, (run.steps_completed / totalSteps) * 100) : 100;
    bar.style.width = `${pct}%`;
    titleEl.textContent = run.step_label || "Running…";
    detailEl.textContent = totalSteps ? `${Math.round(pct)}%` : `${run.steps_completed} steps run`;
  } else {
    bar.className = run.status === "completed" ? "progress-bar done" : "progress-bar";
    bar.style.width = run.status === "completed" ? "100%" : "0%";
    titleEl.textContent = run.step_label || "Ready";
    detailEl.textContent = "";
  }
}

// ---------------------------------------------------------------- updates

function renderUpdateState(info) {
  const el = $("home-update-state");
  el.classList.toggle("hidden", !!info.error);
  if (info.error) return;
  el.className = `status-pill ${info.available ? "new" : "ok"}`;
  el.textContent = info.available ? `● ${info.latest} available` : "● Up to date";
}

function showUpdateBanner(info) {
  latestVersion = info.latest;
  $("update-text").textContent = `New update available: Version ${info.latest} (you have ${info.current}).`;
  $("update-install").classList.toggle("hidden", !info.can_install);
  $("update-install").disabled = false;
  $("update-link").href = info.url;
  $("update-link").classList.remove("hidden");
  $("update-dismiss").classList.remove("hidden");
  $("update-banner").classList.remove("hidden");
  $("menu-dot").classList.remove("hidden");
}

async function checkUpdate(manual = false, force = false) {
  const buttons = [$("menu-update"), $("home-update")];
  if (manual) buttons.forEach((b) => { b.disabled = true; b.lastElementChild.textContent = "Checking…"; });
  let info;
  try {
    info = await api("GET", (manual || force) ? "/api/update?force=1" : "/api/update");
  } catch (e) {
    info = { error: e.message };
  } finally {
    buttons.forEach((b) => { b.disabled = false; b.lastElementChild.textContent = "Check for updates"; });
  }
  renderUpdateState(info);
  if (!info.available && !info.error) {
    $("update-banner").classList.add("hidden");
    $("menu-dot").classList.add("hidden");
  }
  if (!manual) {
    if (info.available && sessionStorage.getItem("updateDismissed") !== info.latest) showUpdateBanner(info);
    return;
  }
  toggleMenu(false);
  if (info.error) return toast(info.error, true);
  if (!info.available) return toast(`You're up to date. Version ${info.current} is the latest.`);
  sessionStorage.removeItem("updateDismissed");
  showUpdateBanner(info);
  toast(`Version ${info.latest} is available`);
}

// ---------------------------------------------------------------- wire up events

$("home-btn").addEventListener("click", () => showSection("home"));
document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => showSection(b.dataset.mode)));
document.querySelectorAll("[data-workspace]").forEach((b) => b.addEventListener("click", () => showSection(b.dataset.workspace)));

$("menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
$("menu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => toggleMenu(false));
document.querySelectorAll("#theme-switch button").forEach((b) =>
  b.addEventListener("click", () => { localStorage.setItem(THEME_KEY, b.dataset.themeChoice); applyTheme(true); }));
$("menu-update").addEventListener("click", () => checkUpdate(true));
$("home-update").addEventListener("click", () => checkUpdate(true));

$("update-install").addEventListener("click", async () => {
  const btn = $("update-install");
  btn.disabled = true;
  $("update-text").textContent = `Downloading version ${latestVersion}…`;
  try {
    await api("POST", "/api/update/install");
    $("update-text").textContent = "Installing the update. AutoClick will close and reopen automatically.";
    $("update-link").classList.add("hidden");
    $("update-dismiss").classList.add("hidden");
  } catch (e) {
    btn.disabled = false;
    $("update-text").textContent = `Version ${latestVersion} is available.`;
    toast(e.message, true);
  }
});
$("update-dismiss").addEventListener("click", () => {
  sessionStorage.setItem("updateDismissed", latestVersion);
  $("update-banner").classList.add("hidden");
});

$("menu-export").addEventListener("click", () => {
  toggleMenu(false);
  const blob = new Blob([JSON.stringify({ presets }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "autoclick-sequences.json";
  a.click();
  URL.revokeObjectURL(a.href);
  toast(`Exported ${presets.length} sequence(s)`);
});
$("menu-import").addEventListener("click", () => { toggleMenu(false); $("import-file").click(); });
$("import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    const list = Array.isArray(parsed) ? parsed : (parsed.presets || parsed);
    const res = await api("POST", "/api/presets/import", { presets: list });
    ++saveSeq;
    clearTimeout(saveDebounce);
    presets = res.presets;
    activePresetId = res.active_preset_id;
    renderHomePresets();
    renderSequenceWorkspace();
    toast(`Imported ${res.imported} sequence(s)`);
  } catch (err) {
    toast(`Import failed: ${err.message}`, true);
  }
});

// Topbar universal stop
$("topbar-stop").addEventListener("click", async () => {
  if (recorderState && recorderState.active) await stopRecording();
  if (runnerState && runnerState.active) await api("POST", "/api/run/stop").catch(() => {});
  pollStatus();
});

// Recorder events
document.querySelectorAll("#rec-mode button").forEach((b) =>
  b.addEventListener("click", () => {
    recMode = b.dataset.recMode;
    document.querySelectorAll("#rec-mode button").forEach((x) => x.classList.toggle("active", x === b));
  }));
$("rec-stop-key").addEventListener("change", (e) => saveSettings({ record_stop_key: e.target.value }));
$("rec-minimize").addEventListener("change", (e) => saveSettings({ minimize_on_start: e.target.checked }));
$("rec-infinite").addEventListener("change", (e) => { $("rec-iterations").disabled = e.target.checked; updateLiveUI(); });
$("rec-speed").addEventListener("input", (e) => {
  $("rec-speed-val").textContent = `${Number(e.target.value).toFixed(2).replace(/\.00$/, ".0")}×`;
});
$("rec-speed").addEventListener("change", (e) => saveSettings({ speed: parseFloat(e.target.value) }));
$("rec-start").addEventListener("click", startRecording);
$("rec-stop").addEventListener("click", stopRecording);
$("rec-replay").addEventListener("click", replayRecorded);
$("rec-pause").addEventListener("click", async () => {
  const url = runnerState && runnerState.status === "paused" ? "/api/run/resume" : "/api/run/pause";
  await api("POST", url).catch((e) => toast(e.message, true));
  pollStatus();
});
$("rec-run-stop").addEventListener("click", async () => {
  await api("POST", "/api/run/stop").catch(() => {});
  pollStatus();
});
$("rec-clear").addEventListener("click", async () => {
  recordedSteps = [];
  await api("POST", "/api/record/clear").catch(() => {});
  renderRecordedSteps();
  updateLiveUI();
});
$("rec-save-btn").addEventListener("click", () => saveRecordedAsPreset(false));
$("rec-open-builder").addEventListener("click", () => saveRecordedAsPreset(true));
$("rec-log-clear").addEventListener("click", async () => {
  await api("POST", "/api/run/clear-log").catch(() => {});
  await api("POST", "/api/record/clear", { logs_only: true }).catch(() => {});
  pollStatus();
});

// Clicker events
document.querySelectorAll("#clk-target-mode button").forEach((b) =>
  b.addEventListener("click", () => {
    clickerTargetMode = b.dataset.target;
    document.querySelectorAll("#clk-target-mode button").forEach((x) => x.classList.toggle("active", x === b));
    renderClickerPoints();
  }));
["clk-x", "clk-y", "clk-button", "clk-clicks", "clk-hold", "clk-interval"].forEach((id) =>
  $(id).addEventListener("input", renderClickerPoints));
$("clk-infinite").addEventListener("change", (e) => { $("clk-iterations").disabled = e.target.checked; updateLiveUI(); });
$("clk-pick-single").addEventListener("click", () => pickScreenPoint($("clk-pick-single"), (x, y) => {
  $("clk-x").value = x;
  $("clk-y").value = y;
  renderClickerPoints();
}));
$("clk-pick-multi").addEventListener("click", () => pickScreenPoint($("clk-pick-multi"), (x, y) => {
  clickerPoints.push({ x, y });
  renderClickerPoints();
}));
$("clk-points-clear").addEventListener("click", () => { clickerPoints = []; renderClickerPoints(); });
$("clk-start").addEventListener("click", async () => {
  try {
    const preset = buildClickerPreset();
    const res = await api("POST", "/api/run/start", { preset, save: false });
    runnerState = res.runner;
    updateLiveUI();
    ensurePolling();
  } catch (e) {
    toast(e.message, true);
  }
});
$("clk-pause").addEventListener("click", async () => {
  const url = runnerState && runnerState.status === "paused" ? "/api/run/resume" : "/api/run/pause";
  await api("POST", url).catch((e) => toast(e.message, true));
  pollStatus();
});
$("clk-stop").addEventListener("click", async () => {
  await api("POST", "/api/run/stop").catch(() => {});
  pollStatus();
});
$("clk-save-preset").addEventListener("click", async () => {
  try {
    const preset = buildClickerPreset();
    const res = await api("POST", "/api/presets/create", preset);
    ++saveSeq;
    presets = res.presets;
    activePresetId = res.active_preset_id;
    toast("Saved clicker setup to Sequences");
  } catch (e) {
    toast(e.message, true);
  }
});
$("clk-log-clear").addEventListener("click", async () => {
  await api("POST", "/api/run/clear-log").catch(() => {});
  pollStatus();
});

// Sequence Builder events
$("seq-select").addEventListener("change", async (e) => {
  activePresetId = e.target.value;
  await api("POST", "/api/presets/select", { id: activePresetId }).catch(() => {});
  renderSequenceWorkspace();
});
$("seq-new").addEventListener("click", async () => {
  const res = await api("POST", "/api/presets/create", { name: `Sequence ${presets.length + 1}` });
  ++saveSeq;
  presets = res.presets;
  activePresetId = res.active_preset_id;
  renderSequenceWorkspace();
  toast("Created new sequence");
});
$("seq-dup").addEventListener("click", async () => {
  const p = activePreset();
  if (!p) return;
  const res = await api("POST", "/api/presets/create", { name: `${p.name} (Copy)`, clone_from: p.id });
  ++saveSeq;
  presets = res.presets;
  activePresetId = res.active_preset_id;
  renderSequenceWorkspace();
  toast("Duplicated sequence");
});
$("seq-del").addEventListener("click", async () => {
  const p = activePreset();
  if (!p || presets.length <= 1) return;
  try {
    if (runnerState && runnerState.active) {
      await api("POST", "/api/run/stop").catch(() => {});
    }
    const res = await api("DELETE", `/api/presets/${encodeURIComponent(p.id)}`);
    ++saveSeq;
    clearTimeout(saveDebounce);
    presets = res.presets;
    activePresetId = res.active_preset_id;
    renderSequenceWorkspace();
    toast("Deleted sequence");
  } catch (e) {
    toast(e.message, true);
  }
});
$("seq-reset-v05").addEventListener("click", async () => {
  const res = await api("POST", "/api/presets/reset");
  ++saveSeq;
  clearTimeout(saveDebounce);
  presets = res.presets;
  activePresetId = res.active_preset_id;
  renderSequenceWorkspace();
  toast("Restored Copy Tab URLs (v0.5) preset");
});
$("seq-name").addEventListener("input", (e) => {
  const p = activePreset();
  if (!p) return;
  p.name = e.target.value;
  const opt = $("seq-select").querySelector(`option[value="${p.id}"]`);
  if (opt) opt.textContent = p.name || "Untitled";
  scheduleSavePreset();
});
$("seq-cooloff").addEventListener("change", (e) => {
  const p = activePreset();
  if (!p) return;
  p.start_delay = Math.max(0, parseFloat(e.target.value) || 0);
  scheduleSavePreset();
});
$("seq-iterations").addEventListener("change", (e) => {
  const p = activePreset();
  if (!p) return;
  p.iterations = Math.max(1, parseInt(e.target.value, 10) || 1);
  updateLiveUI();
  scheduleSavePreset();
});
$("seq-infinite").addEventListener("change", (e) => {
  const p = activePreset();
  if (!p) return;
  p.infinite = e.target.checked;
  $("seq-iterations").disabled = p.infinite;
  updateLiveUI();
  scheduleSavePreset();
});
$("seq-speed").addEventListener("input", (e) => {
  $("seq-speed-val").textContent = `${Number(e.target.value).toFixed(2).replace(/\.00$/, ".0")}×`;
});
$("seq-speed").addEventListener("change", (e) => saveSettings({ speed: parseFloat(e.target.value) }));
$("set-stop-key").addEventListener("change", (e) => saveSettings({ stop_key: e.target.value }));
$("set-failsafe").addEventListener("change", (e) => saveSettings({ failsafe: e.target.checked }));
$("set-minimize").addEventListener("change", (e) => saveSettings({ minimize_on_start: e.target.checked }));

$("seq-add-step").addEventListener("click", () => {
  const p = activePreset();
  if (p) openStepModal({ mode: "add", groupKey: "root", index: p.steps.length, depth: 1 }, "hotkey");
});
$("seq-add-group").addEventListener("click", () => {
  const p = activePreset();
  if (p) openStepModal({ mode: "add", groupKey: "root", index: p.steps.length, depth: 1 }, "group");
});
$("seq-start").addEventListener("click", startSequenceRun);
$("seq-pause").addEventListener("click", async () => {
  const url = runnerState && runnerState.status === "paused" ? "/api/run/resume" : "/api/run/pause";
  await api("POST", url).catch((e) => toast(e.message, true));
  pollStatus();
});
$("seq-stop").addEventListener("click", async () => {
  await api("POST", "/api/run/stop").catch(() => {});
  pollStatus();
});
$("seq-log-clear").addEventListener("click", async () => {
  await api("POST", "/api/run/clear-log").catch(() => {});
  pollStatus();
});

// Copy log buttons
document.querySelectorAll("[data-log-copy]").forEach((b) =>
  b.addEventListener("click", async () => {
    const txt = $(b.dataset.logCopy).textContent;
    if (!txt) return;
    try { await navigator.clipboard.writeText(txt); toast("Activity log copied"); } catch (_) {}
  }));

// Modal events
document.querySelectorAll("#m-type button").forEach((b) =>
  b.addEventListener("click", () => { if (!b.disabled) setModalType(b.dataset.type); }));
document.querySelectorAll("[data-quick-keys]").forEach((b) =>
  b.addEventListener("click", () => { $("m-keys").value = b.dataset.quickKeys; }));
$("m-capture-keys").addEventListener("click", () => {
  capturingKeys = !capturingKeys;
  $("m-capture-keys").textContent = capturingKeys ? "Press keys…" : "Capture";
});
document.addEventListener("keydown", (e) => {
  if (!capturingKeys) {
    if (e.key === "Escape") {
      toggleMenu(false);
      if (!$("step-modal").classList.contains("hidden")) closeStepModal();
    }
    return;
  }
  e.preventDefault();
  e.stopPropagation();
  const mods = [];
  if (e.ctrlKey) mods.push("ctrl");
  if (e.altKey) mods.push("alt");
  if (e.shiftKey) mods.push("shift");
  if (e.metaKey) mods.push("win");
  const k = e.key.toLowerCase();
  if (["control", "alt", "shift", "meta"].includes(k)) return;
  const map = { " ": "space", escape: "esc", arrowup: "up", arrowdown: "down", arrowleft: "left", arrowright: "right" };
  const finalKey = map[k] || k;
  $("m-keys").value = [...mods, finalKey].join("+");
  capturingKeys = false;
  $("m-capture-keys").textContent = "Capture";
});
$("m-pick-pos").addEventListener("click", () => pickScreenPoint($("m-pick-pos"), (x, y) => {
  $("m-x").value = x;
  $("m-y").value = y;
}));
$("modal-close").addEventListener("click", closeStepModal);
$("modal-cancel").addEventListener("click", closeStepModal);
$("modal-save").addEventListener("click", commitStepModal);
$("step-modal").addEventListener("click", (e) => { if (e.target === $("step-modal")) closeStepModal(); });

// ---------------------------------------------------------------- init

function onDesktopReady() {
  document.body.classList.add("desktop");
  applyTheme();
}
if (desktop()) onDesktopReady();
else window.addEventListener("pywebviewready", onDesktopReady);

async function init() {
  applyTheme();
  const data = await api("GET", "/api/state");
  presets = data.presets || [];
  activePresetId = data.active_preset_id || (presets[0] && presets[0].id);
  settings = data.settings || settings;
  runnerState = data.runner;
  recorderState = data.recorder;
  recordedSteps = (recorderState && recorderState.steps) || [];
  syncSettingsUI();
  renderHomePresets();
  renderRecordedSteps();
  renderClickerPoints();
  renderSequenceWorkspace();
  updateLiveUI();
  if ((runnerState && runnerState.active) || (recorderState && recorderState.active)) {
    ensurePolling();
  }
  checkUpdate();
  setInterval(() => checkUpdate(false, true), 30 * 60 * 1000);
}

init().catch((e) => toast(e.message, true));
