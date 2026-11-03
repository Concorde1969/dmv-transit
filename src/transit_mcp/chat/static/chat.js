// The assistant, in the panel's thread: questions answered by Claude from the agencies'
// data (/api/chat). It can also put trips on the map (the "show_trip" event).
import { track } from "/static/track.js";
import { announce, append, follow, isEmpty, showIntro, thread } from "/static/thread.js";

const $ = (id) => document.getElementById(id);
const svg = (body) => `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICON = {
  go: svg('<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>'),
  plan: svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4M8 14h3"/>'),
  ask: svg('<path d="M21 12a8 8 0 0 1-11.5 7.2L4 20l1-4.6A8 8 0 1 1 21 12z"/>'),
};

// What the welcome offers: a trip, planning a visit, and the questions people ask most.
const STARTERS = [
  { icon: "go", text: "Take me to the National Mall" },
  { icon: "plan", text: "Plan my visit, day by day", planner: true },
  { icon: "ask", text: "I've never used the Metro. How does it work?" },
  { icon: "ask", text: "How do I pay to ride the bus or the train?" },
  { icon: "ask", text: "Which stations have working elevators right now?" },
];

// Starters for trip planning (signed-in riders).
const PLAN_SUGGESTIONS = [
  "Help me plan my trip to DC",
  "I'm staying near Union Station from Friday to Sunday",
  "Find a good lunch spot near the National Mall",
  "How do I get around on my first day?",
];

const input = $("chatInput"), sendBtn = $("chatSend"), resetBtn = $("resetChat");
let sessionId = sessionStorage.getItem("tmcp_sid");
if (!sessionId) {
  sessionId = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
  sessionStorage.setItem("tmcp_sid", sessionId);
}
let streaming = false;

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function sourceLabel(url) {
  if (/^https?:\/\//.test(url)) return null;
  const m = url.match(/^(\w+):\/\/([^/]+)(?:\/(.*))?$/);
  if (!m) return url;
  const sys = m[1] === "wmata" ? "Metro (WMATA)" : m[1].toUpperCase();
  return `${sys} ${(m[2] || "").replace(/-/g, " ")} data`.replace(/\s+/g, " ").trim();
}

// Minimal, safe Markdown -> HTML for assistant text.
function md(src) {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let html = "", list = null;
  const inline = (t) => esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])((https?:\/\/[^\s)]+))(?=[\s).,]|$)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { closeList(); html += `<h3>${inline(m[2])}</h3>`; continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== "ul") { closeList(); list = "ul"; html += "<ul>"; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { closeList(); list = "ol"; html += "<ol>"; } html += `<li>${inline(m[1])}</li>`; continue; }
    if (line === "") { closeList(); continue; }
    closeList(); html += `<p>${inline(line)}</p>`;
  }
  closeList();
  return html || "<p></p>";
}

// A message in the thread. ``more`` continues the assistant's answer after a trip card.
function addMessage(role, { more = false } = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}${more ? " more" : ""}`;
  el.innerHTML = `${more ? "" : `<div class="who">${role === "user" ? "You" : "Assistant"}</div>`}<div class="bubble"></div>`;
  append(el);
  return el.querySelector(".bubble");
}

function renderTrace(turn) {
  if (!turn.sources.size) return;
  const d = document.createElement("details");
  d.className = "trace";
  d.innerHTML = "<summary>Where this answer comes from</summary>";
  const body = document.createElement("div");
  body.className = "trace-body";
  for (const src of turn.sources.values()) {
    const row = document.createElement("div");
    row.className = "src";
    const friendly = sourceLabel(src.url);
    row.innerHTML = (friendly ? esc(friendly)
      : `<a href="${esc(src.url)}" target="_blank" rel="noopener">${esc(src.url.replace(/^https?:\/\//, ""))}</a>`)
      + (src.stale ? '<span class="badge">may be out of date</span>' : "");
    body.appendChild(row);
  }
  d.appendChild(body);
  append(d);
}

async function send(text) {
  text = (text || "").trim();
  if (streaming || !text) return;
  streaming = true; sendBtn.disabled = true;
  input.value = ""; autosize();
  window.dmvPanel?.expand();
  addMessage("user").textContent = text;
  let bubble = addMessage("assistant");
  const thinking = document.createElement("div");
  thinking.className = "thinking"; thinking.textContent = "Looking that up…";
  append(thinking);
  let answer = "";
  const turn = { sources: new Map() };
  const clearThinking = () => thinking.remove();
  try {
    const resp = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text, screen: window.dmvTrip?.screen() || null }),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", done = false;
    while (!done) {
      const { value, done: rdone } = await reader.read();
      if (rdone) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
        if (evt.type === "text") {
          answer += evt.text;
          if (answer.trim()) clearThinking();
          bubble.innerHTML = md(answer); follow();
        } else if (evt.type === "tool_result") {
          for (const s of evt.sources || []) if (s.url) turn.sources.set(s.url, s);
        } else if (evt.type === "show_trip") {
          // The trip card goes here in the thread; the rest of the answer follows it.
          if (!answer.trim()) bubble.closest(".msg").remove();
          window.dmvTrip?.show(evt);
          bubble = addMessage("assistant", { more: true }); answer = "";
          append(thinking);
        } else if (evt.type === "account_changed") {
          window.dmvAccount?.refresh();
        } else if (evt.type === "error") {
          clearThinking();
          const box = document.createElement("div");
          box.className = "chat-error"; box.textContent = evt.message;
          bubble.appendChild(box);
        } else if (evt.type === "done") { done = true; }
      }
    }
  } catch (_) {
    bubble.innerHTML = '<div class="chat-error">Couldn\'t reach the assistant. Check your connection and try again.</div>';
  }
  clearThinking();
  const msg = bubble.closest(".msg");
  if (!answer && !bubble.querySelector(".chat-error")) {
    if (msg.classList.contains("more")) msg.remove();
    else bubble.innerHTML = "<p>No answer came back. Try asking about a specific place, station, or route.</p>";
  }
  renderTrace(turn);
  announce("The assistant answered.");
  streaming = false; sendBtn.disabled = false;
  input.focus({ preventScroll: true });
}

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

// A note from the site itself (not the assistant's model): the planner's welcome, About.
function localNote(html) {
  const el = document.createElement("div");
  el.className = "msg assistant note";
  el.innerHTML = `<div class="who">Assistant</div><div class="bubble">${html}</div>`;
  return append(el);
}

function chipButton(text, onClick, icon = "") {
  const c = document.createElement("button");
  c.className = "chip"; c.type = "button";
  c.innerHTML = `${icon}<span>${esc(text)}</span>`;
  c.addEventListener("click", onClick);
  return c;
}

function focusComposer() {
  window.dmvPanel?.expand();
  setTimeout(() => input.focus(), 50);
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
sendBtn.addEventListener("click", () => send(input.value));

// "Start over": a new conversation; the trip on the map stays.
resetBtn.addEventListener("click", async () => {
  try {
    await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) });
  } catch (_) {}
  thread.querySelectorAll(".msg, details.trace, .thinking, .trip-block.past").forEach((n) => n.remove());
  if (isEmpty()) showIntro();
  track("conversation_reset");
  input.focus();
});
// Once a conversation starts, the panel keeps the fine print to one line.
function syncStarted() {
  resetBtn.hidden = isEmpty();
  $("panel").classList.toggle("started", !isEmpty());
}
window.addEventListener("dmv:thread", syncStarted);
syncStarted();   // a trip from a shared link may already be in the thread

const chips = $("chips");
for (const s of STARTERS) {
  chips.appendChild(chipButton(s.text, () => {
    track("starter_used", { detail: { kind: s.icon } });
    s.planner ? planner() : send(s.text);
  }, ICON[s.icon]));
}

// "Plan my visit": signed-in riders get the planner's starters; others sign in first.
function planner() {
  track("planner_opened", { detail: { signed_in: Boolean(window.dmvAccount?.signedIn()) } });
  if (!window.dmvAccount?.signedIn()) { window.dmvAccount?.open("signin", { reason: "plan" }); return; }
  const note = localNote(`<p><strong>Let's plan your trip.</strong> Tell me where you're
    staying, which days you're here and what you'd like to do. I'll find places to eat and
    visit (from Google Maps), keep your plan day by day, and walk you through getting
    around.</p><div class="starters"></div>`);
  const box = note.querySelector(".starters");
  for (const s of PLAN_SUGGESTIONS) box.appendChild(chipButton(s, () => send(s)));
  focusComposer();
}

window.dmvChat = {
  ask(text) { send(text); },
  planner,
  about() {
    track("about_opened");
    localNote($("aboutTemplate").innerHTML);
  },
  close() {},
};
