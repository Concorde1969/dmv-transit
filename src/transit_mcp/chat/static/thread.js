// The conversation in the panel: trip searches (app.js) and the assistant's answers
// (chat.js) go into one thread, in the order they happen.
const $ = (id) => document.getElementById(id);

export const thread = $("thread");
const intro = $("chatIntro");
const announcer = $("announce");

// Is the rider reading the newest part? Then keep up with new text; if they scrolled
// back to read something, leave them there.
let stick = true;
thread.addEventListener("scroll", () => {
  stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
});

export function follow() {
  if (stick) thread.scrollTop = thread.scrollHeight;
}

// Adds an item at the end of the thread; ``top`` scrolls to its start instead of the end.
export function append(el, { top = false } = {}) {
  intro.hidden = true;
  thread.appendChild(el);
  if (top) {
    thread.scrollTop = el.offsetTop - thread.offsetTop - 8;
    stick = false;
  } else {
    stick = true;
    follow();
  }
  window.dispatchEvent(new CustomEvent("dmv:thread"));
  return el;
}

export function isEmpty() {
  return !thread.querySelector(".msg, .trip-block");
}

export function showIntro() {
  intro.hidden = false;
  thread.scrollTop = 0;
}

// A short spoken update for screen readers ("3 ways to go"), without reading out
// everything that streams into the thread.
export function announce(text) {
  announcer.textContent = "";
  setTimeout(() => { announcer.textContent = text; }, 60);
}
