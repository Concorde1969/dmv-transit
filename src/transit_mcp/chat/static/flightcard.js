// A flight card in the conversation: a flight to or from IAD, DCA or BWI from FlightAware
// (the assistant's track_flight / get_to_flight tools), with when to be at the airport
// or when the rider will be ready to leave it.
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const PLANE = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/></svg>';

// The clock time as the airport reports it ("2026-11-15T16:35-06:00" -> "4:35 PM"):
// each time is already local to its own airport, so it isn't converted again.
function clock(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return "";
  const h = +m[1];
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? "AM" : "PM"}`;
}

function day(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return "";
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  const today = new Date();
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return "";
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  return same(d, tomorrow) ? "tomorrow" : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function when(side) {
  const best = side.actual || side.estimated || side.scheduled;
  const moved = side.scheduled && best && clock(best) !== clock(side.scheduled);
  const d = day(best);
  return `<span class="fc-time">${esc(clock(best))}</span>${d ? ` <small>${esc(d)}</small>` : ""}${moved ? ` <s>${esc(clock(side.scheduled))}</s>` : ""}${side.actual ? ' <small>(actual)</small>' : ""}`;
}

function where(side, bags) {
  const bits = [];
  if (side.terminal) bits.push(`Terminal ${side.terminal}`);
  if (side.gate) bits.push(`gate ${side.gate}`);
  if (bags) bits.push(`bags at claim ${bags}`);
  return bits.length ? `<small>${esc(bits.join(", "))}</small>` : "";
}

function statusOf(f) {
  if (f.cancelled) return { cls: "bad", text: "Cancelled" };
  if (f.diverted) return { cls: "bad", text: "Diverted" };
  const late = Math.max(f.departure_delay_min || 0, f.arrival_delay_min || 0);
  if (late >= 15) return { cls: "late", text: `${late} min late` };
  return { cls: "ok", text: f.status || "Scheduled" };
}

export function flightCard({ flight: f, guidance: g }) {
  const s = statusOf(f);
  const el = document.createElement("section");
  el.className = `flight-card${f.cancelled ? " cancelled" : ""}`;
  el.setAttribute("aria-label", `Flight ${f.flight}`);
  const label = (side, dir) => (side.ours ? dir : `${dir}, ${side.city || side.code} time`);
  let plan = "";
  if (g?.kind === "departure") {
    plan = `<p class="fc-plan">Be at ${esc(g.airport)} by <strong>${esc(clock(g.be_at_airport_by))}</strong>${day(g.be_at_airport_by) ? ` ${esc(day(g.be_at_airport_by))}` : ""}
      <small>${Math.round(g.minutes_before / 60)} hours before departure. Your airline's own advice comes first.</small></p>`;
  } else if (g?.kind === "arrival") {
    plan = `<p class="fc-plan">Ready to leave ${esc(g.airport)} about <strong>${esc(clock(g.ready_to_leave_at))}</strong>
      <small>Time to get off the plane and collect bags${f.international ? ", and through customs" : ""}.</small></p>`;
  }
  el.innerHTML = `<div class="fc-head">${PLANE}<strong>${esc(f.flight)}</strong>
      <span class="fc-status ${s.cls}">${esc(s.text)}</span></div>
    <div class="fc-route"><span><b>${esc(f.origin.code)}</b><small>${esc(f.origin.city || "")}</small></span>
      <span class="fc-arrow" aria-hidden="true"></span>
      <span><b>${esc(f.destination.code)}</b><small>${esc(f.destination.city || "")}</small></span></div>
    <dl class="fc-times">
      <div><dt>${esc(label(f.origin, "Departs"))}</dt><dd>${when(f.departure)}${where(f.departure)}</dd></div>
      <div><dt>${esc(label(f.destination, "Arrives"))}</dt><dd>${when(f.arrival)}${where(f.arrival, f.arrival.baggage_claim)}</dd></div>
    </dl>
    ${plan}
    <p class="fc-src">Flight status from FlightAware. Confirm with your airline.</p>`;
  return el;
}
