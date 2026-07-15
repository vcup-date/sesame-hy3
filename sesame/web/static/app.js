import { md, toolBody } from "/md.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const api = async (path, body) => {
  const r = await fetch(path, body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {});
  return r.json();
};
const tok = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(".0", "") + "M"
  : n >= 1e5 ? Math.round(n / 1e3) + "k"
  : n >= 1e3 ? (n / 1e3).toFixed(1).replace(".0", "") + "k" : String(n);
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
};

const EFFORTS = ["low", "medium", "high", "max"];
const state = { busy: false, cfg: {}, lastId: 0, tools: new Map(), answer: null, reason: null,
                chars: 0, streamStart: 0 };

/* ── thread ─────────────────────────────────────────────────────────────── */
const thread = $("thread");
const scroll = $("scroll");
let pinned = true;

scroll.addEventListener("scroll", () => {
  pinned = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90;
  $("toBottom").classList.toggle("hidden", pinned);
});
const toBottom = () => { if (pinned) scroll.scrollTop = scroll.scrollHeight; };
$("toBottom").onclick = () => {
  pinned = true;
  scroll.scrollTop = scroll.scrollHeight;
  $("toBottom").classList.add("hidden");
};

function clearThread() {
  thread.innerHTML = "";
  state.tools.clear();
  state.answer = state.reason = null;
  $("empty").classList.remove("hidden");
}

function add(node) {
  $("empty").classList.add("hidden");
  const wrap = el("div", "block");
  wrap.appendChild(node);
  thread.appendChild(wrap);
  toBottom();
  return node;
}

function userBlock(text, steered) {
  const n = el("div", "user" + (steered ? " steered" : ""));
  const b = el("div", "bubble");
  if (steered) b.appendChild(el("span", "tag", "sent while it was working"));
  // a pasted file is shown folded: click to see it, rather than a screenful of it
  const lines = text.split("\n");
  if (lines.length > 14) {
    const head = lines.slice(0, 3).join("\n");
    const more = el("details", "folded");
    const sum = el("summary", "", `${lines.length} lines pasted`);
    more.appendChild(sum);
    more.appendChild(el("pre", "", "")).textContent = text;
    b.appendChild(document.createTextNode(head + "\n"));
    b.appendChild(more);
  } else {
    b.appendChild(document.createTextNode(text));
  }
  n.appendChild(b);
  return add(n);
}

function reasonBlock() {
  const n = el("div", "reason live");
  n.innerHTML = `<button class="reason-head"><span class="caret">▶</span>
    <span class="reason-label">Thinking</span></button><div class="reason-body"></div>`;
  n.querySelector(".reason-head").onclick = () => n.classList.toggle("open");
  return add(n);
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el("textarea");           // clipboard API needs https or localhost
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const was = btn.textContent;
  btn.textContent = "copied";
  setTimeout(() => { btn.textContent = was; }, 1200);
}

function answerBlock() {
  const n = el("div", "answer");
  const copy = el("button", "copy-btn", "copy");
  copy.onclick = () => copyText(n.dataset.raw || n.innerText, copy);
  n.appendChild(copy);
  return add(n);
}

function decorate(answer) {
  // the copy button is rewritten away every time the markdown re-renders
  if (!answer.querySelector(".copy-btn")) {
    const copy = el("button", "copy-btn", "copy");
    copy.onclick = () => copyText(answer.dataset.raw || answer.innerText, copy);
    answer.appendChild(copy);
  }
  answer.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(".copy-btn")) return;
    const copy = el("button", "copy-btn", "copy");
    copy.onclick = () => copyText(pre.innerText.replace(/copy$/, ""), copy);
    pre.appendChild(copy);
  });
}

function toolCard(ev) {
  const n = el("div", "tool");
  const arg = argOf(ev.name, ev.args);
  n.innerHTML = `<button class="tool-head">
      <span class="tool-dot run"></span>
      <span class="tool-name"></span>
      <span class="tool-arg"></span>
      <span class="tool-state">running</span>
    </button><div class="tool-body"></div>`;
  n.querySelector(".tool-name").textContent = ev.label || ev.name;
  n.querySelector(".tool-arg").textContent = arg;
  n.querySelector(".tool-head").onclick = () => n.classList.toggle("open");
  n.dataset.start = String(Date.now());
  add(n);
  state.tools.set(ev.call, n);
  return n;
}

function argOf(name, args) {
  const a = args || {};
  const first = a.command || a.path || a.url || a.query || a.selector || a.prompt || "";
  const s = String(first).replace(/\s+/g, " ");
  return s.length > 120 ? s.slice(0, 120) + "…" : s;
}

function toolDone(ev) {
  const n = state.tools.get(ev.call);
  if (!n) return;
  const failed = /^(error|denied|browser error|missing required)/i.test(ev.content || "");
  const secs = (Date.now() - Number(n.dataset.start)) / 1000;
  n.querySelector(".tool-dot").className = "tool-dot " + (failed ? "bad" : "ok");
  n.querySelector(".tool-state").textContent =
    (failed ? "failed" : "done") + (secs > 0.4 ? ` · ${secs.toFixed(1)}s` : "");
  const body = n.querySelector(".tool-body");
  body.innerHTML = toolBody(ev.content || "(no output)");
  if (failed) n.classList.add("open");
  toBottom();
}

function askCard(ev) {
  const n = el("div", "ask");
  n.innerHTML = `<div class="ask-title">Allow ${ev.label || ev.name}?</div>
    <div class="ask-why"></div><div class="ask-cmd"></div>
    <div class="ask-btns">
      <button class="btn primary" data-a="yes">Allow once</button>
      <button class="btn" data-a="always">Always allow this</button>
      <button class="btn" data-a="no">Deny</button>
    </div>`;
  n.querySelector(".ask-why").textContent = ev.reason || "";
  n.querySelector(".ask-cmd").textContent = argOf(ev.name, ev.args) || JSON.stringify(ev.args);
  n.querySelectorAll("button[data-a]").forEach((b) => {
    b.onclick = async () => {
      await api("/api/approve", { ask: ev.ask, answer: b.dataset.a });
      n.classList.add("done");
      n.querySelector(".ask-why").textContent =
        { yes: "allowed once", always: "allowed, and remembered for this folder", no: "denied" }[b.dataset.a];
    };
  });
  return add(n);
}

/* ── events ─────────────────────────────────────────────────────────────── */
function render(ev) {
  switch (ev.t) {
    case "user":
      userBlock(ev.text, ev.steer);
      break;

    case "reasoning_delta": {
      countChars(ev.text);
      if (!state.reason) state.reason = reasonBlock();
      const body = state.reason.querySelector(".reason-body");
      body.textContent += ev.text;
      const lines = body.textContent.trim().split("\n").filter(Boolean);
      state.reason.querySelector(".reason-label").textContent =
        (lines[lines.length - 1] || "Thinking").slice(0, 70);
      toBottom();
      break;
    }
    case "reasoning_done": {
      const n = state.reason || (ev.text ? reasonBlock() : null);
      if (!n) break;
      n.classList.remove("live");
      n.querySelector(".reason-body").textContent = ev.text;
      const meta = [ev.secs ? `${ev.secs}s` : null, ev.tokens ? `~${tok(ev.tokens)} tokens` : null]
        .filter(Boolean).join(" · ");
      n.querySelector(".reason-label").textContent = "Thought" + (meta ? ` · ${meta}` : "");
      state.reason = null;
      break;
    }

    case "answer_delta":
      countChars(ev.text);
      if (!state.answer) state.answer = answerBlock();
      state.answer.dataset.raw = (state.answer.dataset.raw || "") + ev.text;
      state.answer.innerHTML = md(state.answer.dataset.raw);
      decorate(state.answer);
      toBottom();
      break;
    case "answer_done": {
      const n = state.answer || answerBlock();
      n.dataset.raw = ev.text;
      n.innerHTML = md(ev.text);
      decorate(n);
      state.answer = null;
      break;
    }

    case "tool": toolCard(ev); break;
    case "tool_result": toolDone(ev); break;
    case "approval": askCard(ev); break;

    case "status":
      $("statusText").textContent = ev.text;
      break;

    // a local model reads your context before it says anything. llama.cpp reports
    // how far it has got, so show that rather than a spinner that means nothing.
    case "prefill":
      $("prefill").classList.remove("hidden");
      $("prefill").textContent =
        `reading context · ${tok(ev.tokens)} tokens` +
        (ev.rate ? ` · ${tok(ev.rate)}/s` : "") +
        (ev.cached ? ` · ${tok(ev.cached)} cached` : "");
      break;
    case "notice":
      add(el("div", "notice", "")).textContent = ev.text;
      break;
    case "error":
      add(el("div", "err", "")).textContent = ev.text;
      break;

    case "busy":
      setBusy(ev.busy);
      break;
    case "stats":
      setStats(ev);
      break;
    case "config":
      state.cfg = ev;
      paintConfig();
      break;
    case "title":
      state.cfg.title = ev.title;
      state.cfg.session = ev.session;
      $("title").textContent = ev.title || "New chat";
      loadSessions();
      break;
    case "session":
      state.cfg.session = ev.name;
      state.cfg.title = null;
      $("title").textContent = "New chat";
      clearThread();
      (ev.history || []).forEach(render);
      loadSessions();
      break;
  }
}

function countChars(text) {
  // The first token that arrives is the moment prefill ended.
  if (!state.streamStart) {
    state.streamStart = performance.now();
    $("prefill").classList.add("hidden");
  }
  state.chars += (text || "").length;
  const secs = (performance.now() - state.streamStart) / 1000;
  if (secs > 0.5) {
    const rate = (state.chars / 4) / secs;         // ~4 chars a token, as everywhere else
    $("rate").textContent = `${rate.toFixed(0)} tok/s`;
  }
}

function setBusy(busy) {
  state.busy = busy;
  state.chars = 0;
  state.streamStart = 0;
  $("rate").textContent = "";
  $("prefill").classList.add("hidden");
  $("send").classList.toggle("hidden", busy);
  $("stop").classList.toggle("hidden", !busy);
  $("statusline").classList.toggle("hidden", !busy);
  $("hint").textContent = busy
    ? "type to steer it, it reads you at its next step · esc to stop"
    : "enter send · shift enter newline";
  if (busy) $("statusText").textContent = "working";
  else {
    state.answer = null;
    state.reason = null;
    loadSessions();
  }
}

function setStats(s) {
  const pct = s.window ? Math.min(100, (s.tokens / s.window) * 100) : 0;
  $("ctxFill").style.width = pct + "%";
  $("ctxText").textContent = `${tok(s.tokens || 0)}/${tok(s.window || 0)}`;
  $("costText").textContent = s.cost ? `$${s.cost.toFixed(4)}` : "$0";
}

/* ── send ───────────────────────────────────────────────────────────────── */
const input = $("input");

function grow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
}

async function send() {
  const typed = input.value.trim();
  if (!typed) return;
  input.value = "";
  grow();
  const text = expandPastes(typed);      // the model gets what you pasted
  const r = await api("/api/send", { text });
  if (r.error) render({ t: "error", text: r.error });
}

input.addEventListener("input", grow);
/* ── paste: a big paste is an object, not a wall of text ──────────────────── */
const PASTE = /\[paste #(\d+) · (\d+) lines[^\]]*\]/g;
const PASTE_MIN_LINES = 3;          // one or two lines is just typing
const pastes = new Map();
let pasteN = 0;

function pasteLabel(id, text) {
  const lines = text.split("\n").length;
  const first = (text.split("\n").find((l) => l.trim()) || "").trim();
  const preview = first.length > 32 ? first.slice(0, 32) + "…" : first;
  return `[paste #${id} · ${lines} lines · ${preview}]`;
}

function expandPastes(text) {
  const out = text.replace(PASTE, (whole, id) => pastes.get(Number(id)) ?? whole);
  pastes.clear();
  return out;
}

function takePaste(text) {
  if (!text) return;
  text = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");   // CR line breaks count too
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  let insert = text;
  if (text.split("\n").length >= PASTE_MIN_LINES) {
    const id = ++pasteN;
    pastes.set(id, text);
    insert = pasteLabel(id, text);
  }
  input.value = input.value.slice(0, start) + insert + input.value.slice(end);
  input.selectionStart = input.selectionEnd = start + insert.length;
  input.focus();
  grow();
}

input.addEventListener("paste", (e) => {
  const raw = (e.clipboardData || window.clipboardData).getData("text");
  const text = (raw || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!text || text.split("\n").length < PASTE_MIN_LINES) return;   // short: let it through
  e.preventDefault();
  takePaste(text);
});

// paste with the page focused, not the box. Otherwise cmd-v anywhere but the input
// goes nowhere, and you have to click the box first, which nobody remembers to do.
document.addEventListener("paste", (e) => {
  const el = document.activeElement;
  if (el === input || el.tagName === "INPUT" || el.isContentEditable) return;
  const text = (e.clipboardData || window.clipboardData).getData("text");
  if (!text) return;
  e.preventDefault();
  takePaste(text);
});

input.addEventListener("keydown", (e) => {
  // backspace on a paste removes the paste, not one character of its label
  if (e.key !== "Backspace" || input.selectionStart !== input.selectionEnd) return;
  const before = input.value.slice(0, input.selectionStart);
  const m = [...before.matchAll(PASTE)].find((x) => x.index + x[0].length === before.length);
  if (!m) return;
  e.preventDefault();
  pastes.delete(Number(m[1]));
  input.value = before.slice(0, m.index) + input.value.slice(input.selectionStart);
  input.selectionStart = input.selectionEnd = m.index;
  grow();
});

let composing = false;
input.addEventListener("compositionstart", () => { composing = true; });
input.addEventListener("compositionend", () => { composing = false; });
input.addEventListener("keydown", (e) => {
  // Enter while an IME is open chooses a candidate. Sending there would cut the
  // word in half and fire whatever was typed so far.
  if (composing || e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const settingsOpen = !$("modal").classList.contains("hidden");
  if (settingsOpen) {
    $("modal").classList.add("hidden");   // esc closes settings first
    return;
  }
  if (state.busy) api("/api/stop");       // then esc stops the turn
});
$("send").onclick = send;
$("stop").onclick = () => api("/api/stop");
$("newChat").onclick = () => api("/api/session/new", {});
$("undoBtn").onclick = async () => {
  const r = await api("/api/undo", {});
  if (!r.ok) render({ t: "notice", text: r.error || "nothing to undo" });
};
$("compactBtn").onclick = async () => {
  const r = await api("/api/compact", {});
  render({ t: "notice", text: r.ok ? `compacted ${tok(r.before)} to ${tok(r.after)} tokens`
                                   : "nothing worth compacting yet" });
};
document.querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => { input.value = c.dataset.q; grow(); send(); };
});

/* ── sessions ───────────────────────────────────────────────────────────── */
let allSessions = [];

$("sessionSearch").addEventListener("input", () => paintSessions());

function paintSessions() {
  const q = $("sessionSearch").value.trim().toLowerCase();
  const box = $("sessions");
  box.innerHTML = "";
  const shown = q
    ? allSessions.filter((s) => (s.title || s.name).toLowerCase().includes(q))
    : allSessions;
  if (!shown.length) {
    box.appendChild(el("div", "empty-list", q ? "No session matches." : "No sessions yet."));
    return;
  }
  shown.forEach((s) => {
    const b = el("button", "session" + (s.name === state.cfg.session ? " on" : ""));
    b.innerHTML = `<span class="s-name"></span><span class="s-meta"></span>`;
    b.querySelector(".s-name").textContent = s.title || s.name;
    b.querySelector(".s-meta").textContent =
      `${ago(s.updated)} · ${s.turns} turn${s.turns === 1 ? "" : "s"}` +
      (s.cost ? ` · $${s.cost.toFixed(3)}` : "");
    b.onclick = async () => {
      const r = await api("/api/session/resume", { name: s.name });
      if (r.state) { state.cfg = r.state; paintConfig(); }
    };
    const x = el("button", "s-del", "✕");
    x.title = "Delete this session";
    x.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${s.title || s.name}"? This cannot be undone.`)) return;
      await api("/api/session/delete", { name: s.name });
      loadSessions();
    };
    b.appendChild(x);
    box.appendChild(b);
  });
}

async function loadSessions() {
  const { sessions } = await api("/api/sessions");
  allSessions = sessions;
  paintSessions();
}

/* ── settings ───────────────────────────────────────────────────────────── */
const modal = $("modal");
const openSettings = () => { modal.classList.remove("hidden"); loadModels(); };
$("gearBtn").onclick = openSettings;
$("nowChip").onclick = openSettings;
$("openSettings").onclick = openSettings;
$("closeSettings").onclick = () => modal.classList.add("hidden");
modal.onclick = (e) => { if (e.target === modal) modal.classList.add("hidden"); };

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    document.querySelectorAll(".pane").forEach((p) =>
      p.classList.toggle("on", p.id === "pane-" + t.dataset.tab));
  };
});

function paintConfig() {
  const c = state.cfg;
  $("nowModel").textContent = c.model || "no model";
  $("nowWhere").textContent = c.local ? "local" : (c.provider || "");
  $("nowProfile").textContent = c.profile || "";
  $("nowProfile").classList.toggle("hidden", !c.profile);
  $("railModel").textContent = c.model || "no model";
  $("railProvider").textContent =
    (c.profile ? `${c.profile} · ` : "") + (c.provider || "") + (c.local ? " · local" : "");
  $("railFolder").textContent = c.workdir || "";
  $("railFolder").title = c.workdir || "";
  $("title").textContent = c.title || "New chat";
  setStats({ ...(c.stats || {}), window: c.window });

  const sel = $("providerSel");
  sel.innerHTML = "";
  (c.providers || []).concat(c.provider === "custom" ? ["custom"] : []).forEach((p) => {
    const o = el("option");
    o.value = p;
    o.textContent = p;
    if (p === c.provider) o.selected = true;
    sel.appendChild(o);
  });

  $("keyState").textContent = c.local ? "not needed for a local model"
    : c.hasKey ? `set (${c.keyHint})` : "not set";
  $("modelSource").textContent = `from ${c.provider || "your provider"}`;
  $("profileNow").textContent = `${c.model} · ${c.baseUrl}`;
  $("effort").value = String(Math.max(0, EFFORTS.indexOf(c.effort || "high")));
  paintEffort();
  $("confirmDanger").checked = !!c.confirmDanger;
  $("confirmAll").checked = !!c.confirmAll;
  $("browserHeaded").checked = !!c.browserHeaded;
  $("toolBudget").value = c.toolBudget || 50;
  $("maxTokens").value = c.maxTokens || 8192;
  $("temperature").value = c.temperature == null ? "" : c.temperature;
  $("wdSub").textContent = c.workdir || "";
  $("wdInput").value = c.workdir || "";
  $("wdFiles").textContent = (c.projectFiles || []).join(", ") || "none found";
  $("wdGit").textContent = c.git || "not a repository";

  // profiles
  const pl = $("profileList");
  pl.innerHTML = "";
  const profs = Object.entries(c.profiles || {});
  if (!profs.length) pl.appendChild(el("div", "empty-list", "No saved setups yet."));
  profs.forEach(([name, p]) => {
    const row = el("div", "item" + (name === c.profile ? " on" : ""));
    row.innerHTML = `<div><div class="item-name"></div><div class="item-sub"></div></div>
      <div class="item-actions">
        <button class="btn ghost" data-use>Use</button>
        <button class="btn ghost" data-del>Delete</button>
      </div>`;
    row.querySelector(".item-name").textContent = name;
    row.querySelector(".item-sub").textContent =
      `${p.model} · ${p.wire} · ${p.contextWindow ? tok(p.contextWindow) + " ctx" : ""}`;
    row.querySelector("[data-use]").onclick = async () => {
      const r = await api("/api/profile", { action: "use", name });
      if (r.state) { state.cfg = r.state; paintConfig(); }
    };
    row.querySelector("[data-del]").onclick = async () => {
      const r = await api("/api/profile", { action: "delete", name });
      if (r.state) { state.cfg = r.state; paintConfig(); }
    };
    pl.appendChild(row);
  });

  // permissions
  const perm = $("permList");
  perm.innerHTML = "";
  const rows = [
    ...(c.permissions?.tools || []).map((t) => ["tools", t, "any use of this tool"]),
    ...(c.permissions?.prefixes || []).map((p) => ["prefixes", p, "this exact command"]),
  ];
  if (!rows.length) perm.appendChild(el("div", "empty-list", "Nothing yet. It asks every time."));
  rows.forEach(([kind, rule, what]) => {
    const row = el("div", "item");
    row.innerHTML = `<div><div class="item-name"></div><div class="item-sub"></div></div>
      <button class="btn ghost" data-x>Remove</button>`;
    row.querySelector(".item-name").textContent = rule;
    row.querySelector(".item-sub").textContent = what;
    row.querySelector("[data-x]").onclick = async () => {
      const r = await api("/api/permissions", { action: "remove", kind, rule });
      state.cfg.permissions = r.permissions;
      paintConfig();
    };
    perm.appendChild(row);
  });

  // undo
  const ul = $("undoList");
  ul.innerHTML = "";
  const turns = c.undo || [];
  if (!turns.length) ul.appendChild(el("div", "empty-list", "No file changes to undo."));
  turns.slice().reverse().forEach((t) => {
    const row = el("div", "item");
    row.innerHTML = `<div><div class="item-name"></div><div class="item-sub"></div></div>
      <button class="btn ghost" data-r>Restore</button>`;
    row.querySelector(".item-name").textContent = `Turn ${t.turn}`;
    row.querySelector(".item-sub").textContent = (t.files || []).join(", ") || "no files";
    row.querySelector("[data-r]").onclick = async () => {
      const r = await api("/api/undo", { turn: t.turn });
      render({ t: "notice", text: r.ok ? `restored turn ${t.turn}` : (r.error || "could not undo") });
      modal.classList.add("hidden");
    };
    ul.appendChild(row);
  });
}

function paintEffort() {
  const i = Number($("effort").value);
  document.querySelectorAll(".ticks span").forEach((s, n) => s.classList.toggle("on", n === i));
  const words = {
    low: "Answers fast, thinks little.",
    medium: "Balanced.",
    high: "Thinks harder on tricky work.",
    max: "Thinks as long as it needs.",
  };
  $("effortSub").textContent = words[EFFORTS[i]];
}

$("effort").oninput = paintEffort;
$("effort").onchange = async () => {
  const r = await api("/api/config", { action: "effort", effort: EFFORTS[Number($("effort").value)] });
  if (r.state) state.cfg = r.state;
};

$("providerSel").onchange = async () => {
  const r = await api("/api/config", { action: "provider", provider: $("providerSel").value });
  if (r.needKey) {
    $("keyState").textContent = "this provider needs a key. Add it below, then pick it again.";
    return;
  }
  if (r.state) { state.cfg = r.state; paintConfig(); loadModels(); }
};

$("keySave").onclick = async () => {
  const key = $("keyInput").value.trim();
  if (!key) return;
  const r = await api("/api/config", { action: "key", apiKey: key });
  $("keyInput").value = "";
  if (r.state) { state.cfg = r.state; paintConfig(); loadModels(); }
};

["confirmDanger", "confirmAll", "browserHeaded"].forEach((id) => {
  $(id).onchange = async () => {
    const body = { action: "behavior" };
    body[id] = $(id).checked;
    const r = await api("/api/config", body);
    if (r.state) state.cfg = r.state;
  };
});
["toolBudget", "maxTokens", "temperature"].forEach((id) => {
  $(id).onchange = async () => {
    const map = { toolBudget: "toolCallBudget", maxTokens: "maxTokens", temperature: "temperature" };
    const body = { action: "behavior" };
    body[map[id]] = $(id).value;
    const r = await api("/api/config", body);
    if (r.state) state.cfg = r.state;
  };
});

$("permReset").onclick = async () => {
  const r = await api("/api/permissions", { action: "reset" });
  state.cfg.permissions = r.permissions;
  paintConfig();
};

$("wdSave").onclick = async () => {
  const r = await api("/api/config", { action: "workdir", path: $("wdInput").value.trim() });
  if (!r.ok) { $("wdSub").textContent = r.error; return; }
  state.cfg = r.state;
  paintConfig();
};

$("profileSave").onclick = async () => {
  const name = $("profileName").value.trim();
  if (!name) return;
  const r = await api("/api/profile", { action: "save", name });
  $("profileName").value = "";
  if (r.state) { state.cfg = r.state; paintConfig(); }
};

$("cxDetect").onclick = async () => {
  $("cxStatus").textContent = "asking the server…";
  const r = await api("/api/config", {
    action: "custom", baseUrl: $("cxUrl").value.trim(), model: $("cxModel").value.trim(),
    apiKey: $("cxKey").value.trim(), wire: $("cxWire").value,
  });
  if (r.state) {
    state.cfg = r.state;
    $("cxWindow").value = r.state.window;
    $("cxStatus").textContent = `connected · ${tok(r.state.window)} context`;
    paintConfig();
  }
};

$("cxConnect").onclick = async () => {
  const url = $("cxUrl").value.trim();
  if (!url) { $("cxStatus").textContent = "a base URL is needed"; return; }
  $("cxStatus").textContent = "connecting…";
  const r = await api("/api/config", {
    action: "custom", baseUrl: url, model: $("cxModel").value.trim(),
    apiKey: $("cxKey").value.trim(), wire: $("cxWire").value,
    window: Number($("cxWindow").value || 0),
  });
  if (r.ok) {
    state.cfg = r.state;
    $("cxStatus").textContent = `connected · ${r.state.model} · ${tok(r.state.window)} context`;
    paintConfig();
    loadModels();
  } else {
    $("cxStatus").textContent = r.error || "could not connect";
  }
};

async function loadModels() {
  const box = $("modelList");
  box.innerHTML = `<div class="empty-list">asking ${state.cfg.provider || "the provider"}…</div>`;
  const r = await api("/api/models");
  box.innerHTML = "";
  $("modelSource").textContent = `from ${r.source}`;
  if (!r.models.length) {
    box.appendChild(el("div", "empty-list", "No models. Check the key or the endpoint."));
    return;
  }
  r.models.forEach((m) => {
    const row = el("div", "item" + (m.id === state.cfg.model ? " on" : ""));
    row.innerHTML = `<div><div class="item-name"></div><div class="item-sub"></div></div>`;
    row.querySelector(".item-name").textContent = m.id;
    row.querySelector(".item-sub").textContent =
      [m.window ? tok(m.window) + " ctx" : null,
       m.in != null ? `$${m.in}/$${m.out} per Mtok` : null].filter(Boolean).join(" · ");
    row.onclick = async () => {
      const res = await api("/api/config", { action: "model", model: m.id });
      if (res.needKey) { $("keyState").textContent = "this model's provider needs a key first."; return; }
      if (res.state) { state.cfg = res.state; paintConfig(); loadModels(); }
    };
    box.appendChild(row);
  });
}

$("refreshModels").onclick = () => loadModels();

/* ── appearance ─────────────────────────────────────────────────────────── */
const dark = window.matchMedia("(prefers-color-scheme: dark)");

function applyTheme(choice) {
  const real = choice === "system" ? (dark.matches ? "dark" : "light") : choice;
  document.documentElement.dataset.theme = real;
  localStorage.setItem("sesame-theme", choice);
  document.querySelectorAll("[data-theme-set]").forEach((b) =>
    b.classList.toggle("on", b.dataset.themeSet === choice));
}

function applySize(pct) {
  document.documentElement.style.setProperty("--ui-scale", pct / 100);
  localStorage.setItem("sesame-size", String(pct));
  $("uiSize").value = String(pct);
  $("uiSizeSub").textContent = pct === 100
    ? "Scales the whole interface, not just the text."
    : `${pct}% of the default size.`;
}

applyTheme(localStorage.getItem("sesame-theme") || "dark");
applySize(Number(localStorage.getItem("sesame-size") || 100));
dark.addEventListener("change", () => {
  if (localStorage.getItem("sesame-theme") === "system") applyTheme("system");
});

$("themeBtn").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
document.querySelectorAll("[data-theme-set]").forEach((b) => {
  b.onclick = () => applyTheme(b.dataset.themeSet);
});
$("uiSize").oninput = () => applySize(Number($("uiSize").value));
$("uiSizeReset").onclick = () => applySize(100);

/* ── boot ───────────────────────────────────────────────────────────────── */
async function boot() {
  state.cfg = await api("/api/state");
  state.lastId = state.cfg.lastEvent || 0;
  paintConfig();
  setBusy(state.cfg.busy);
  const { history } = await api("/api/history");
  clearThread();
  (history || []).forEach(render);
  await loadSessions();

  // from the tip, not from zero: the history above is already on screen
  const es = new EventSource("/api/events?since=" + (state.cfg.lastEvent || 0));
  es.onopen = () => $("offline").classList.add("hidden");
  es.onerror = () => {
    // the browser retries on its own; say so instead of going quietly dead
    $("offline").classList.remove("hidden");
  };
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.id <= state.lastId) return;   // a replayed event, already on screen
    state.lastId = ev.id;
    render(ev);
  };
}

boot();
