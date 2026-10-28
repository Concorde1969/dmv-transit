// The assistant drawer: questions answered by Claude from the agencies' data (/api/chat).
import { track } from "/static/track.js";
const $ = (id) => document.getElementById(id);
const SUGGESTIONS = [
  "I've never used the Metro. How does it work?",
  "How do I pay to ride the bus or the train?",
  "What will it cost to go from Shady Grove to downtown DC?",
  "Which stations have working elevators right now?",
];

const drawer = $("drawer"), log = $("chatLog"), intro = $("chatIntro");
const input = $("chatInput"), sendBtn = $("chatSend"), askBtn = $("askBtn");
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

const scrollDown = () => { log.scrollTop = log.scrollHeight; };

function addMessage(role) {
  intro.hidden = true;
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="who">${role === "user" ? "You asked" : "Assistant"}</div><div class="bubble"></div>`;
  log.appendChild(el);
  scrollDown();
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
  log.appendChild(d);
}

async function send(text) {
  text = (text || "").trim();
  if (streaming || !text) return;
  streaming = true; sendBtn.disabled = true;
  input.value = ""; autosize();
  addMessage("user").textContent = text;
  const bubble = addMessage("assistant");
  const thinking = document.createElement("div");
  thinking.className = "thinking"; thinking.textContent = "Looking that up…";
  log.appendChild(thinking); scrollDown();
  let answer = "";
  const turn = { sources: new Map() };
  const clearThinking = () => thinking.remove();
  try {
    const resp = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
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
          bubble.innerHTML = md(answer); scrollDown();
        } else if (evt.type === "tool_result") {
          for (const s of evt.sources || []) if (s.url) turn.sources.set(s.url, s);
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
  if (!answer && !bubble.querySelector(".chat-error")) {
    bubble.innerHTML = "<p>No answer came back. Try asking about a specific place, station, or route.</p>";
  }
  renderTrace(turn);
  streaming = false; sendBtn.disabled = false;
  input.focus();
}

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function open() {
  if (!drawer.classList.contains("open")) track("assistant_opened");
  window.dmvReports?.close(true);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  askBtn.setAttribute("aria-expanded", "true");
  setTimeout(() => input.focus(), 50);
}
function close(quiet = false) {
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  askBtn.setAttribute("aria-expanded", "false");
  if (!quiet) askBtn.focus();
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
sendBtn.addEventListener("click", () => send(input.value));
askBtn.addEventListener("click", () => (drawer.classList.contains("open") ? close() : open()));
$("closeDrawer").addEventListener("click", () => close());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) close(); });
$("resetChat").addEventListener("click", async () => {
  try {
    await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) });
  } catch (_) {}
  log.querySelectorAll(".msg, details.trace, .thinking").forEach((n) => n.remove());
  intro.hidden = false;
  input.focus();
});

const chips = $("chips");
for (const s of SUGGESTIONS) {
  const c = document.createElement("button");
  c.className = "chip"; c.type = "button"; c.textContent = s;
  c.addEventListener("click", () => send(s));
  chips.appendChild(c);
}

window.dmvChat = {
  ask(text) { open(); send(text); },
  about() { open(); intro.hidden = false; $("aboutText").hidden = false; },
  close,
};
