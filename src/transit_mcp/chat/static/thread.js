// The conversation in the panel: trip searches (app.js) and the assistant's answers
// (chat.js) go into one thread, in the order they happen.
const $ = (id) => document.getElementById(id);

export const thread = $("thread");
const intro = $("chatIntro");
const announcer = $("announce");

// Is the rider reading the newest part? Then keep up with new text; if they scrolled
// back to read something, leave them there.
let stick = true;
// A trip card being shown: the thread holds it in view until the rider scrolls or sends
// something new, even while the assistant's answer keeps arriving below it.
let pinned = null;
thread.addEventListener("scroll", () => {
  stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
});
for (const kind of ["wheel", "touchmove", "keydown"]) {
  thread.addEventListener(kind, () => { pinned = null; }, { passive: true });
}

function toTop(el) {
  thread.scrollTo({ top: Math.max(0, el.offsetTop - thread.offsetTop - 8),
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

export function follow() {
  if (pinned) return;
  if (stick) thread.scrollTop = thread.scrollHeight;
}

// Brings an item (a trip card) to the top of the thread and keeps it there.
export function pin(el) {
  pinned = el;
  toTop(el);
}

// Adds an item at the end of the thread. ``top`` pins it in view; ``force`` (the rider's
// own message) always scrolls to it.
export function append(el, { top = false, force = false } = {}) {
  intro.hidden = true;
  thread.appendChild(el);
  if (top) pin(el);
  else if (force) { pinned = null; stick = true; follow(); }
  else follow();
  window.dispatchEvent(new CustomEvent("dmv:thread"));
  return el;
}

export function isEmpty() {
  return !thread.querySelector(".msg, .trip-block");
}

export function showIntro() {
  pinned = null;
  intro.hidden = false;
  thread.scrollTop = 0;
}

// A short spoken update for screen readers ("3 ways to go"), without reading out
// everything that streams into the thread.
export function announce(text) {
  announcer.textContent = "";
  setTimeout(() => { announcer.textContent = text; }, 60);
}
