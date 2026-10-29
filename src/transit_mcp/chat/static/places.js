// Saved places (account.js keeps the list; app.js shows it as quick picks and map pins).
// A hotel counts while the stay lasts; a dated visit until its day has passed.

const svg = (body) => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const KIND = {
  hotel: { name: "Hotel", icon: svg('<path d="M2 19V6M2 15h20v4M22 15v-2a3 3 0 0 0-3-3h-8v5"/><circle cx="6.5" cy="11.5" r="2"/>') },
  home: { name: "Home", icon: svg('<path d="M3 11l9-7 9 7M5 10v10h14V10M10 20v-5h4v5"/>') },
  attraction: { name: "Place to visit", icon: svg('<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.8l6.1-.9z"/>') },
  work: { name: "Work", icon: svg('<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 13h18"/>') },
  other: { name: "Other", icon: svg('<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>') },
};

export const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export function isoDay(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const day = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
export const shortDate = (iso) => day(iso).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

// {active, past, badge, rank}: active places get a quick pick; past ones only stay listed.
export function placeStatus(p, today = isoDay()) {
  if (p.kind === "hotel" && p.start_date) {
    const end = p.end_date || p.start_date;
    const eve = isoDay(new Date(day(p.start_date).getTime() - 86400000));
    if (today > end) return { active: false, past: true, badge: "Past stay", rank: 9 };
    if (today < eve) return { active: false, past: false, badge: `Stay from ${shortDate(p.start_date)}`, rank: 8 };
    return { active: true, past: false, badge: "Your stay", rank: 0 };
  }
  if (p.kind === "attraction" && p.start_date) {
    if (p.start_date < today) return { active: false, past: true, badge: `Visited ${shortDate(p.start_date)}`, rank: 9 };
    if (p.start_date === today) return { active: true, past: false, badge: "Today", rank: 1 };
    return { active: true, past: false, badge: shortDate(p.start_date), rank: 2 };
  }
  const rank = { hotel: 0, home: 0, attraction: 3, work: 4, other: 5 }[p.kind] ?? 5;
  return { active: true, past: false, badge: null, rank };
}

// Places in the order quick picks show them: your stay or home, today's visits, then the rest.
export function sortPlaces(places) {
  return places.map((p) => ({ p, s: placeStatus(p) }))
    .sort((a, b) => a.s.rank - b.s.rank || (a.p.start_date || "9").localeCompare(b.p.start_date || "9") || a.p.id - b.p.id);
}

// A trip end for /api/trip. Saved places are logged without their names (source "saved").
export const savedPoint = (p) => ({ lat: p.lat, lon: p.lon, label: p.name, source: "saved", kind: p.kind, saved_id: p.id });

// A sensible type for a place picked from the address suggestions.
export function guessKind(kind) {
  const k = String(kind || "").toLowerCase();
  if (/lodging|hotel|motel|hostel|inn|resort/.test(k)) return "hotel";
  if (/museum|tourist|attraction|park|zoo|monument|memorial|landmark|gallery|aquarium|church|stadium|theat|historic|garden|viewpoint|castle/.test(k)) return "attraction";
  return "other";
}
