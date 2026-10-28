// Rider reports: a short form to warn other riders, and the latest reports (/api/reports).
import { track } from "/static/track.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const drawer = $("reportDrawer"), btn = $("reportBtn"), form = $("reportForm");
const statusEl = $("reportStatus"), sendBtn = $("reportSend");
const text = $("rText"), count = $("rCount"), dateInput = $("rDate"), list = $("recentReports");

export const CATEGORY = {
  delay: "Delay", cancellation: "Cancelled", crowding: "Crowded",
  elevator_escalator: "Elevator or escalator", accessibility: "Accessibility",
  safety: "Safety", cleanliness: "Cleanliness", detour: "Detour",
  station_issue: "Station", tip: "Tip", other: "Report",
};

export function ago(minutes) {
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const h = Math.floor(minutes / 60);
  return `${h} hour${h === 1 ? "" : "s"} ago`;
}

function where(r) {
  const parts = [r.line, r.station && `at ${r.station}`].filter(Boolean);
  return parts.length ? `${parts.join(" ")}, ` : "";
}

export function reportItem(r) {
  return `<li class="recent-item sev-${esc(r.severity || "info")}">
    <span class="cat">${esc(CATEGORY[r.category] || "Report")}</span>
    <span class="what">${esc(r.summary)}</span>
    <small>${esc(where(r))}${esc(ago(r.minutes_ago))}</small>
  </li>`;
}

function isoDay(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function setDates() {
  const today = new Date();
  const earliest = new Date(); earliest.setDate(today.getDate() - 60);
  dateInput.max = isoDay(today); dateInput.min = isoDay(earliest);
  if (!dateInput.value) dateInput.value = isoDay(today);
}

function prefillTrip() {
  // Start from the trip on the map, if the rider has one.
  const from = $("fromInput")?.value?.trim(), to = $("toInput")?.value?.trim();
  if (from && from !== "My location" && !$("rOrigin").value) $("rOrigin").value = from;
  if (to && !$("rDestination").value) $("rDestination").value = to;
}

async function loadRecent() {
  try {
    const data = await (await fetch("/api/reports/recent")).json();
    const rows = data.reports || [];
    list.innerHTML = rows.length ? rows.slice(0, 6).map(reportItem).join("")
      : `<li class="recent-empty">No reports in the last ${data.hours || 3} hours. Be the first to tell other riders.</li>`;
  } catch (_) {
    list.innerHTML = `<li class="recent-empty">Couldn't load the latest reports.</li>`;
  }
}

function updateCount() {
  const left = 2000 - text.value.length;
  count.textContent = left < 300 ? `${left} characters left` : "";
}

function say(msg, ok = false) {
  statusEl.textContent = msg;
  statusEl.className = `report-status ${ok ? "ok" : "err"}`;
}

function open() {
  if (!drawer.classList.contains("open")) track("report_form_opened");
  window.dmvChat?.close?.(true);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  btn.setAttribute("aria-expanded", "true");
  setDates(); prefillTrip(); loadRecent();
  setTimeout(() => $("rStation").focus(), 50);
}

function close(quiet = false) {
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  btn.setAttribute("aria-expanded", "false");
  if (!quiet) btn.focus();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(form).entries());
  for (const k of Object.keys(body)) body[k] = String(body[k]).trim();
  if (!body.origin || !body.destination) return say("Please fill in where your trip started and where you were going.");
  if (!body.travel_date) return say("Please pick the date of travel.");
  if (body.text.length < 10) return say("Please describe what happened (at least a few words).");
  if (!body.station) delete body.station;
  if (!body.line) delete body.line;
  sendBtn.disabled = true; say("Sending…", true);
  try {
    const r = await fetch("/api/reports", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      say(data.message || "Thank you! Your report will help other riders.", true);
      for (const id of ["rStation", "rLine", "rText"]) $(id).value = "";
      updateCount();
      setTimeout(loadRecent, 5000);   // after it has been checked
    } else if (r.status === 422 && !data.message) {
      say("Please check the form: the description needs at least 10 characters.");
    } else {
      say(data.message || "Sorry, your report couldn't be sent. Please try again.");
    }
  } catch (_) {
    say("Couldn't reach the site. Check your connection and try again.");
  } finally {
    sendBtn.disabled = false;
  }
});

text.addEventListener("input", updateCount);
btn.addEventListener("click", () => (drawer.classList.contains("open") ? close() : open()));
$("closeReport").addEventListener("click", () => close());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) close(); });

window.dmvReports = { open, close };
