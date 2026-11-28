// DMV Transit - map-first, leave-now trip planner.
// Data: /api/geocode, /api/trip, /api/trip/live (chat/trip_api.py). Map: MapLibre GL with a
// self-hosted Protomaps basemap (/tiles/dmv.pmtiles); all files are served by this site.
// Each search becomes a trip card in the panel's thread, next to the assistant's answers.
import * as ML from "/static/map/vendor/maplibre-gl.mjs";
import { KIND, savedPoint, sortPlaces } from "/static/places.js";
import { ago, reportItem } from "/static/reports.js";
import { announce, append, pin } from "/static/thread.js";
import { track } from "/static/track.js";

const maplibregl = ML.default || ML;
const $ = (id) => document.getElementById(id);
const ORIGIN = location.origin;
const DMV_CENTER = [-77.0369, 38.9072];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const isPhone = () => matchMedia("(max-width: 720px)").matches;

const state = {
  from: null,            // {lat, lon, label, source, kind} or null = "My location"
  to: null,
  me: null,              // last known position
  plan: null,
  selected: null,        // index into plan.options
  view: "list",          // list | detail
  searchId: null,        // the server's ID for the last search (usage log)
  saved: [],             // the signed-in visitor's saved places (account.js)
  accounts: false,       // whether accounts are available on this site
};
let map, youMarker, destMarker, liveTimer, savedMarkers = [], savedPopup;
// Following a trip on the map, step by step (see "follow on the map" below).
const journey = { on: false, opt: null, i: 0, steps: [], feats: [], watch: null, me: null };
let stepPins = [];

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const mins = (s) => Math.max(0, Math.round(s / 60));
const nowS = () => Date.now() / 1000;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const isRail = (leg) => ["subway", "rail", "tram", "monorail"].includes(leg.route_type);

function routeLabel(leg) {
  const short = (leg.route_short_name || "").trim();
  const long = (leg.route_long_name || "").trim();
  if (isRail(leg)) {
    if (long && short.length <= 2) return /line$/i.test(long) ? long : `${long} Line`;
    return short || long || leg.route_id;
  }
  return short || long || leg.route_id;
}

const ICONS = {
  bus: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="3" width="16" height="14" rx="3"/><path d="M4 11h16M8 21v-4M16 21v-4"/></svg>',
  rail: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3" width="14" height="14" rx="4"/><path d="M5 11h14M8 21l2-4M16 21l-2-4"/></svg>',
  alert: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/></svg>',
  walk: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="M10 22l2-7-3-3 1-5 4 3 3 1M9 12l-3 3"/></svg>',
  chev: '<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
  back: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>',
  flag: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 21V4M5 4h11l-2 4 2 4H5"/></svg>',
  route: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M8.5 19H15a3.5 3.5 0 0 0 0-7H9a3.5 3.5 0 0 1 0-7h6.5"/></svg>',
};

// Readable text on any route color: feeds' own text colors are sometimes unreadable
// (e.g. black on navy), so pick white or near-black by contrast.
function luminance(hex) {
  const n = parseInt(hex.slice(1), 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}
function textOn(bg) {
  if (!/^#[0-9a-f]{6}$/i.test(bg || "")) return "#ffffff";
  const L = luminance(bg);
  return (1.05 / (L + 0.05)) >= ((L + 0.05) / (luminance("#1B2621") + 0.05)) ? "#ffffff" : "#1B2621";
}

function pill(leg) {
  const style = `background:${esc(leg.color)};color:${textOn(leg.color)}`;
  if (isRail(leg)) {
    // Metro-style line letters ("R", "OR") get a bullet; other trains a train icon.
    const short = (leg.route_short_name || "").trim();
    const mark = short && short.length <= 2 ? `<span class="bullet">${esc(short)}</span>` : ICONS.rail;
    return `<span class="pill rail" style="${style}">${mark}${esc(routeLabel(leg))}</span>`;
  }
  return `<span class="pill" style="${style}">${ICONS.bus}${esc(routeLabel(leg))}</span>`;
}

const rides = (opt) => opt.legs.filter((l) => l.mode === "ride");

// ------------------------------------------------------------------ map
async function initMap() {
  let tiles = false;
  try { tiles = (await fetch("/tiles/dmv.pmtiles", { method: "HEAD" })).ok; } catch (_) {}
  const glyphs = `${ORIGIN}/static/map/fonts/{fontstack}/{range}.pbf`;
  let style = {
    version: 8, glyphs, sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#E7EEE6" } }],
  };
  if (tiles && window.pmtiles && window.basemaps) {
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    // Protomaps "light", tinted toward the D.C. Transit mint and turquoise.
    const flavor = {
      ...basemaps.namedFlavor("light"),
      background: "#E7EEE6", earth: "#EEF2EB", water: "#B4D9D3",
      park_a: "#D4E6D2", park_b: "#C9DFC6", wood_a: "#D4E6D2", wood_b: "#C9DFC6",
      buildings: "#DDE3DA", pedestrian: "#E8EDE5",
    };
    style = {
      version: 8, glyphs, sprite: `${ORIGIN}/static/map/sprites/light`,
      sources: {
        protomaps: {
          type: "vector", url: `pmtiles://${ORIGIN}/tiles/dmv.pmtiles`,
          attribution: '<a href="https://protomaps.com">Protomaps</a> &copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
        },
      },
      layers: basemaps.layers("protomaps", flavor, { lang: "en" }),
    };
  }
  map = new maplibregl.Map({
    container: "map", style, center: DMV_CENTER, zoom: 10.6, minZoom: 7, maxZoom: 18,
    attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("load", addTripLayers);
  window.dmvMap = map;    // explore.js draws the whole network on it
  applyPadding();
  addEventListener("resize", applyPadding);
}

// What covers the map: the step card while following a trip, the bar when the panel is
// minimized, otherwise the panel.
function cover() {
  if (!$("journey").hidden) return $("journey");
  return $("panel").classList.contains("min") ? $("peek") : $("panel");
}

let lastPadding = "";
function applyPadding() {
  if (!map) return;
  const el = cover(), box = el.getBoundingClientRect();
  const pad = isPhone()
    // Below the floating logo (with room for a pin), which sits under the status bar.
    ? { top: Math.round(document.querySelector(".brand").getBoundingClientRect().bottom) + 44,
        bottom: Math.round(innerHeight - box.top) + 20, left: 24, right: 64 }   // the zoom buttons
    : el.id === "peek"
      ? { top: 80, bottom: Math.round(innerHeight - box.top) + 30, left: 40, right: 80 }
      : { top: 80, bottom: 40, left: Math.round(box.right) + 40, right: 80 };
  // Setting the padding stops a camera move in progress, so only when it changes.
  const key = JSON.stringify(pad);
  if (key === lastPadding) return;
  lastPadding = key;
  map.setPadding(pad);
}

// ------------------------------------------------------------------ phone sheet
// On phones the panel is a sheet over the map: normal (the map shows above it) or tall
// (for reading answers). Questions make it tall; a new trip brings the map back.
const panel = $("panel"), sheetToggle = $("sheetToggle");
function setTall(tall) {
  if (!tall) document.body.classList.remove("fullscreen");
  panel.classList.toggle("tall", tall);
  document.body.classList.toggle("sheet-tall", tall);
  sheetToggle.setAttribute("aria-expanded", String(tall));
  sheetToggle.setAttribute("aria-label", tall ? "Show more of the map" : "Make the panel taller");
}
// Drag the sheet by its handle (phones): up for more panel, down for more map. Let go and
// it settles at the nearest stop: the bar at the bottom, the usual height, or tall (a
// flick carries on in its direction). A tap on the handle still toggles tall.
let drag = null, dragged = false;
sheetToggle.addEventListener("click", () => {
  if (dragged) { dragged = false; return; }
  setTall(!panel.classList.contains("tall"));
});
sheetToggle.addEventListener("pointerdown", (e) => {
  if (!isPhone()) return;
  drag = { y: e.clientY, t: performance.now(), h: panel.getBoundingClientRect().height, moved: false };
  try { sheetToggle.setPointerCapture(e.pointerId); } catch (_) { /* keep going without it */ }
});
sheetToggle.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dy = e.clientY - drag.y;
  if (!drag.moved && Math.abs(dy) < 6) return;
  drag.moved = true;
  panel.classList.add("dragging");
  panel.style.height = `${Math.max(56, Math.min(innerHeight, drag.h - dy))}px`;
});
function endDrag(e) {
  if (!drag) return;
  const d = drag; drag = null;
  if (!d.moved) return;                   // a tap: the click toggles tall
  dragged = true;
  setTimeout(() => { dragged = false; }, 400);
  const h = panel.getBoundingClientRect().height;
  const speed = (e.clientY - d.y) / Math.max(1, performance.now() - d.t);   // px/ms, + is down
  const aim = h - speed * 220;            // where a flick is heading
  panel.classList.remove("dragging");
  panel.style.height = "";
  const exploring = document.body.classList.contains("exploring");
  const stops = [["min", 0], ["normal", innerHeight * (exploring ? 0.38 : 0.62)], ["tall", innerHeight - 76]];
  const [name] = stops.reduce((best, s) => (Math.abs(s[1] - aim) < Math.abs(best[1] - aim) ? s : best));
  if (name !== "tall") document.activeElement?.blur?.();   // the keyboard goes too
  if (name === "min") { setTall(false); setMin(true, { focus: false }); return; }
  setMin(false, { focus: false });
  setTall(name === "tall");
}
sheetToggle.addEventListener("pointerup", endDrag);
sheetToggle.addEventListener("pointercancel", endDrag);
function sizeSheet() {
  // The map's corner controls (and its attribution) sit just above whatever covers it.
  document.documentElement.style.setProperty("--sheet", isPhone() ? `${Math.round(innerHeight - cover().getBoundingClientRect().top)}px` : "0px");
  applyPadding();
}

// Minimize: the panel folds down to one bar (#peek) so the map has the screen. The bar
// says what's on the map (the trip, or "Where to?") and brings the panel back.
const peek = $("peek"), panelMin = $("panelMin");
function setMin(min, { focus = true } = {}) {
  if (panel.classList.contains("min") !== min) track(min ? "panel_minimized" : "panel_restored");
  if (min) document.body.classList.remove("fullscreen");
  panel.classList.toggle("min", min);
  document.body.classList.toggle("minimized", min);
  peek.hidden = !min || journey.on;
  panelMin.setAttribute("aria-expanded", String(!min));
  updatePeek();
  sizeSheet();
  if (focus) (min ? peek : panelMin).focus({ preventScroll: true });
}
panelMin.addEventListener("click", () => setMin(true));
function openFromPeek(tall = false) {
  const trip = state.plan?.options?.[state.selected];
  setMin(false, { focus: !trip });
  if (tall) setTall(true);
  if (!trip) toInput.focus();            // "Where to?": straight to the search
}
// Pressing and dragging the bar up opens the panel (a long drag, tall). On a phone that's
// the only way with a finger, so a tap doesn't open it by accident; elsewhere a click
// does, and the keyboard (Enter or Space) always does.
let peekFrom = null, peekSwiped = false;
peek.addEventListener("pointerdown", (e) => { peekFrom = e.clientY; });
peek.addEventListener("pointerup", (e) => {
  const up = peekFrom == null ? 0 : peekFrom - e.clientY;
  peekFrom = null;
  if (up > 24) { peekSwiped = true; setTimeout(() => { peekSwiped = false; }, 400); openFromPeek(up > 200); }
});
peek.addEventListener("click", (e) => {
  if (peekSwiped) return;
  if (isPhone() && e.detail > 0) return;  // a tap on a phone: drag it up instead
  openFromPeek();
});

function updatePeek() {
  const opt = state.plan?.options?.[state.selected];
  if (opt && state.to) {
    $("peekTitle").textContent = `Leave by ${clock(opt.leave_at)}, arrive ${clock(opt.arrive_at)}`;
    $("peekSub").textContent = opt.walk_only ? `Walk to ${state.to.label}`
      : `${rides(opt).map(routeLabel).join(", then ")} to ${state.to.label}`;
  } else {
    $("peekTitle").textContent = "Where to?";
    $("peekSub").textContent = "Plan a trip or ask a question";
  }
}

for (const el of [panel, peek, $("journey")]) new ResizeObserver(sizeSheet).observe(el);
window.dmvPanel = {
  // An answer to read: the panel comes back, tall on a phone.
  expand: () => { setMin(false, { focus: false }); if (isPhone()) setTall(true); },
  // More map (Explore, a new trip): a minimized panel stays minimized.
  lower: () => isPhone() && setTall(false),
  show: () => setMin(false, { focus: false }),
  // Explore on a phone: the panel folds to its bar so the map is clear.
  minimize: () => isPhone() && setMin(true, { focus: false }),
  // Typing to the assistant (chat.js): the whole screen on a phone, until focus leaves.
  fullscreen: (on) => setFullscreen(on),
};

function addTripLayers() {
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["others", "trip", "stops"]) map.addSource(id, { type: "geojson", data: empty });
  map.addLayer({ id: "others-line", type: "line", source: "others",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 5, "line-opacity": 0.28 } });
  map.addLayer({ id: "trip-casing", type: "line", source: "trip", filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 12 } });
  map.addLayer({ id: "trip-ride", type: "line", source: "trip", filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 7 } });
  map.addLayer({ id: "trip-walk", type: "line", source: "trip", filter: ["==", ["get", "kind"], "walk"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#4A5651", "line-width": 4, "line-dasharray": [0.1, 1.8] } });
  map.addLayer({ id: "trip-shuttle-casing", type: "line", source: "trip", filter: ["==", ["get", "kind"], "shuttle"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 11 } });
  map.addLayer({ id: "trip-shuttle", type: "line", source: "trip", filter: ["==", ["get", "kind"], "shuttle"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": SHUTTLE_COLOR, "line-width": 6, "line-dasharray": [2, 1.2] } });
  map.addLayer({ id: "trip-shuttle-label", type: "symbol", source: "trip", filter: ["==", ["get", "kind"], "shuttle"],
    layout: { "symbol-placement": "line-center", "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"],
              "text-size": 14, "text-offset": [0, -1.1] },
    paint: { "text-color": "#1B2621", "text-halo-color": "#ffffff", "text-halo-width": 2 } });
  map.addLayer({ id: "stops-dot", type: "circle", source: "stops",
    paint: { "circle-radius": 7, "circle-color": "#ffffff", "circle-stroke-width": 4,
             "circle-stroke-color": ["get", "color"] } });
  map.addLayer({ id: "stops-label", type: "symbol", source: "stops",
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": 14,
              "text-offset": [0, 1.3], "text-anchor": "top", "text-max-width": 12 },
    paint: { "text-color": "#1B2621", "text-halo-color": "#ffffff", "text-halo-width": 2 } });
  if (state.plan) drawPlan();
  drawSaved();
}

const SHUTTLE_COLOR = "#0F7368";
const lineOf = (coords, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: coords } });
const pt = (p) => [p.lon, p.lat];

function optionFeatures(opt) {
  const feats = [], stops = [];
  const o = state.plan.origin, d = state.plan.destination;
  let prev = pt(o);
  for (const [step, leg] of opt.legs.entries()) {
    const line = (coords, props) => lineOf(coords, { ...props, step });
    if (leg.mode === "walk" && leg.airport?.mode === "shuttle") {
      // The airport shuttle between the terminal and the rail station, then any walk.
      const a = leg.airport, terminal = [a.terminal[1], a.terminal[0]];
      const name = `Free ${a.code} shuttle`;
      stops.push({ type: "Feature", properties: { name: `${a.name} terminal`, color: SHUTTLE_COLOR }, geometry: { type: "Point", coordinates: terminal } });
      if (leg.to) {   // terminal -> station
        feats.push(line([prev, terminal], { kind: "walk" }));
        feats.push(line([terminal, pt(leg.to)], { kind: "shuttle", name }));
        prev = pt(leg.to);
      } else {        // station -> terminal
        feats.push(line([leg.from ? pt(leg.from) : prev, terminal], { kind: "shuttle", name }));
        feats.push(line([terminal, pt(d)], { kind: "walk" }));
        prev = pt(d);
      }
    } else if (leg.mode === "walk") {
      const target = leg.to ? pt(leg.to) : pt(d);
      const from = leg.from ? pt(leg.from) : prev;
      // Over real streets once the walk is routed (loadWalks); a straight line until then.
      const path = leg.walk?.path?.length > 1 ? leg.walk.path : [from, target];
      feats.push(line(path, { kind: "walk" }));
      prev = target;
    } else {
      const coords = leg.geometry?.length > 1 ? leg.geometry : [pt(leg.board), pt(leg.alight)];
      feats.push(line(coords, { kind: "ride", color: leg.color }));
      stops.push({ type: "Feature", properties: { name: leg.board.name, color: leg.color }, geometry: { type: "Point", coordinates: pt(leg.board) } });
      stops.push({ type: "Feature", properties: { name: leg.alight.name, color: leg.color }, geometry: { type: "Point", coordinates: pt(leg.alight) } });
      prev = pt(leg.alight);
    }
  }
  return { feats, stops };
}

function drawPlan(fit = true) {
  if (!map || !map.getSource("trip") || !state.plan) return;
  const opts = state.plan.options;
  const others = [];
  opts.forEach((opt, i) => {
    if (i === state.selected) return;
    for (const leg of rides(opt)) {
      if (leg.geometry?.length > 1) others.push(lineOf(leg.geometry, { color: leg.color }));
    }
  });
  map.getSource("others").setData({ type: "FeatureCollection", features: others });
  const sel = opts[state.selected];
  const { feats, stops } = sel ? optionFeatures(sel) : { feats: [], stops: [] };
  map.getSource("trip").setData({ type: "FeatureCollection", features: feats });
  map.getSource("stops").setData({ type: "FeatureCollection", features: stops });
  drawStepPins(sel, feats);

  const o = state.plan.origin, d = state.plan.destination;
  if (!youMarker) {
    const el = document.createElement("div"); el.className = "you-dot";
    youMarker = new maplibregl.Marker({ element: el });
  }
  if (!destMarker) {
    const el = document.createElement("div"); el.className = "dest-pin";
    destMarker = new maplibregl.Marker({ element: el, anchor: "bottom", offset: [0, 4] });
  }
  youMarker.setLngLat(pt(o)).addTo(map);
  destMarker.setLngLat(pt(d)).addTo(map);

  const b = new maplibregl.LngLatBounds(pt(o), pt(o));
  b.extend(pt(d));
  for (const f of feats) for (const c of f.geometry.coordinates) b.extend(c);
  if (fit) map.fitBounds(b, { duration: reduceMotion ? 0 : 900, maxZoom: 16 });
}

// ------------------------------------------------------------------ walking directions
// The walks of the option a rider looks at, over real streets (/api/trip/walks,
// openrouteservice): the path for the map, the time at a gentle pace, turn-by-turn steps,
// and which station entrance to use. The planner's straight-line guess stays until then,
// and for good if routing isn't available.
const walkRequests = new WeakMap();
function loadWalks(opt) {
  if (!opt || walkRequests.has(opt)) return;
  const plan = state.plan, legs = opt.legs, asks = [], at = [];
  const stopRef = (ride, stop) => (ride && stop?.stop_id ? { agency: ride.agency, stop_id: stop.stop_id } : null);
  legs.forEach((leg, i) => {
    if (leg.mode !== "walk" || leg.airport) return;    // airports have their own directions
    const before = legs[i - 1], after = legs[i + 1];
    const from = leg.from || (before?.mode === "ride" ? before.alight : plan.origin);
    const to = leg.to || plan.destination;
    if (!from || !to) return;
    asks.push({ from: { lat: from.lat, lon: from.lon }, to: { lat: to.lat, lon: to.lon },
      from_stop: before?.mode === "ride" ? stopRef(before, from) : null,
      to_stop: after?.mode === "ride" ? stopRef(after, to) : null });
    at.push(i);
  });
  if (!asks.length) return;
  const req = fetch("/api/trip/walks", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ walks: asks }) }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  walkRequests.set(opt, req);
  req.then((data) => {
    if (!data?.walks) return;
    data.walks.forEach((w, k) => { if (w) applyWalk(opt, at[k], w, data.attribution); });
    if (state.plan !== plan) return;               // the rider has searched again
    if (plan.options[state.selected] === opt) drawPlan(false);
    render();
    if (journey.on && journey.opt === opt) refreshJourney();
  });
}

function applyWalk(opt, i, w, attribution) {
  const leg = opt.legs[i];
  leg.walk = { ...w, attribution };
  if (!w.routed) return;                           // entrance names only
  const delta = w.duration_s - leg.duration_s;
  leg.duration_s = w.duration_s;
  opt.walk_s += delta;
  if (opt.walk_only) opt.arrive_at += delta;
  else if (i === 0) { opt.leave_at -= delta; opt.wait_s -= delta; }
  else if (leg.transfer) leg.wait_s -= delta;
  else opt.arrive_at += delta;
  opt.duration_s = opt.arrive_at - Math.max(opt.leave_at, nowS());
}

// 0.4 mi, or feet when it's short.
function distance(m) {
  const ft = m * 3.281;
  if (ft < 528) return `${Math.max(50, Math.round(ft / 50) * 50)} ft`;
  return `${(m / 1609.34).toFixed(1).replace(/\.0$/, "")} mi`;
}

// The extra lines under a walk step: entrance, elevator, and the turn-by-turn steps.
function walkDetails(leg) {
  const w = leg.walk;
  if (!w) return "";
  const notes = [];
  if (w.exit) notes.push(`Leave the station by ${esc(w.exit)}.`);
  if (w.exit_elevator) notes.push(`Elevator: ${esc(w.exit_elevator)}.`);
  if (w.entrance) notes.push(`Go in at ${esc(w.entrance)}.`);
  if (w.elevator) notes.push(`Elevator: ${esc(w.elevator)}.`);
  const turns = (w.steps || []).map((s) => `<li>${esc(s.text)}${s.distance_m >= 15 ? ` <span class="walk-dist">${distance(s.distance_m)}</span>` : ""}</li>`).join("");
  return (notes.length ? `<br><span class="walk-entrance">${notes.join(" ")}</span>` : "")
    + (turns ? `<details class="walk-turns"><summary>Walking directions</summary><ol>${turns}</ol>
        <p class="walk-credit">${esc(w.attribution || "")}</p></details>` : "");
}

function walkTitle(leg, where) {
  const far = leg.walk?.routed ? ` (${distance(leg.walk.distance_m)})` : "";
  return `Walk ${mins(leg.duration_s)} min${far} to ${where}`;
}

// ------------------------------------------------------------------ search
const fromInput = $("fromInput"), toInput = $("toInput"), suggest = $("suggest");
let activeField = null, suggestItems = [], suggestIndex = -1, searchTimer;
// One round of typing and its pick share a token (Google bills them as one session).
let placesSession = null;
const newSession = () => (crypto.randomUUID ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));

function showSuggestions(field, items, credit = "") {
  activeField = field; suggestItems = items; suggestIndex = -1;
  suggest.innerHTML = items.map((it, i) =>
    `<li role="option" id="sg${i}" aria-selected="false"${it.saved ? ` class="saved kind-${esc(it.saved.kind)}"` : ""}>${it.saved ? `<span class="sg-icon">${KIND[it.saved.kind]?.icon || ""}</span>` : ""}${esc(it.label)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</li>`).join("")
    + (credit && items.length ? `<li class="credit-line" role="presentation">Results from ${esc(credit)}</li>` : "");
  suggest.hidden = !items.length;
  field.setAttribute("aria-expanded", String(!!items.length));
  [...suggest.children].slice(0, items.length).forEach((li, i) => li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); }));
}

function hideSuggestions() {
  suggest.hidden = true; suggestItems = [];
  fromInput.setAttribute("aria-expanded", "false"); toInput.setAttribute("aria-expanded", "false");
}

// Saved places as suggestions: all of them before typing, the matching ones after.
function savedSuggestions(q = "") {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return sortPlaces(state.saved)
    .filter(({ p, s }) => !s.past && words.every((w) => `${p.name} ${p.address || ""}`.toLowerCase().includes(w)))
    .slice(0, q ? 3 : 8)
    .map(({ p, s }) => ({ label: p.name, sub: [KIND[p.kind]?.name, s.badge].filter(Boolean).join(", "), saved: p }));
}

function onType(field) {
  clearTimeout(searchTimer);
  const q = field.value.trim();
  if (field === fromInput) state.from = q.toLowerCase() === "my location" ? null : state.from;
  if (q.length < 3 || q.toLowerCase() === "my location") {   // nothing typed yet
    const mine = savedSuggestions();
    showSuggestions(field, field === fromInput ? [{ label: "My location", me: true }, ...mine] : mine);
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      placesSession ||= newSession();
      const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}&session=${placesSession}`);
      const data = await r.json();
      // Google results bring their own second line; geocode.maps.co ones get one here.
      const items = (data.results || []).map((x) => ({ ...x, sub: "sub" in x ? x.sub : x.full_label && x.full_label !== x.label ? x.full_label.split(",").slice(-4, -2).join(",").trim() : "" }));
      const found = items.length;
      items.unshift(...savedSuggestions(q));
      if (field === fromInput) items.unshift({ label: "My location", me: true });
      if (!items.length && data.message) items.push({ label: data.message, disabled: true });
      if (document.activeElement === field) showSuggestions(field, items, found ? data.attribution : "");
    } catch (_) { /* keep typing; nothing to show */ }
  }, 380);
}

async function pick(i) {
  const it = suggestItems[i];
  if (!it || it.disabled) return;
  const field = activeField;
  hideSuggestions();
  if (it.saved) {
    placesSession = null;
    track("saved_place_used", { detail: { field: field === fromInput ? "from" : "to", kind: it.saved.kind, from: "suggestion" } });
    return field === fromInput ? startFrom(savedPoint(it.saved)) : goTo(savedPoint(it.saved));
  }
  if (!it.me && it.place_id && it.lat == null) {
    // A Places suggestion: ask where it is (this also closes the typing session).
    try {
      const r = await fetch(`/api/place?id=${encodeURIComponent(it.place_id)}&session=${placesSession || ""}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.message);
      Object.assign(it, { lat: data.lat, lon: data.lon, kind: it.kind || data.kind });
    } catch (err) {
      showSuggestions(field, [{ label: err.message || "Couldn't find that place. Please try another suggestion.", disabled: true }]);
      return;
    }
  }
  placesSession = null;
  if (field === fromInput) {
    if (it.me) { state.from = null; fromInput.value = "My location"; }
    else { state.from = { lat: it.lat, lon: it.lon, label: it.label, source: "suggestion", kind: it.kind, address: it.full_label }; fromInput.value = it.label; }
    track("suggestion_picked", { detail: { field: "from", kind: it.me ? "my_location" : (it.kind || "") } });
    if (state.to) planTrip(); else toInput.focus();
  } else {
    state.to = { lat: it.lat, lon: it.lon, label: it.label, source: "suggestion", kind: it.kind, address: it.full_label };
    track("suggestion_picked", { detail: { field: "to", kind: it.kind || "" } });
    toInput.value = it.label;
    toInput.blur();
    planTrip();
  }
}

function onKey(e) {
  if (suggest.hidden || !suggestItems.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    suggestIndex = (suggestIndex + (e.key === "ArrowDown" ? 1 : -1) + suggestItems.length) % suggestItems.length;
    [...suggest.children].slice(0, suggestItems.length).forEach((li, i) => li.setAttribute("aria-selected", String(i === suggestIndex)));
    e.target.setAttribute("aria-activedescendant", `sg${suggestIndex}`);
  } else if (e.key === "Enter") {
    e.preventDefault(); pick(suggestIndex >= 0 ? suggestIndex : 0);
  } else if (e.key === "Escape") { hideSuggestions(); }
}

for (const f of [fromInput, toInput]) {
  f.addEventListener("input", () => onType(f));
  f.addEventListener("focus", () => {
    if (f === fromInput && f.value === "My location") f.select();
    const chosen = f === fromInput ? state.from : state.to;
    if (!chosen || chosen.label !== f.value) onType(f);   // don't re-search a chosen place
  });
  f.addEventListener("keydown", onKey);
  f.addEventListener("blur", () => setTimeout(hideSuggestions, 120));
}

// ------------------------------------------------------------------ full-screen typing
// Searching or chatting on a phone: the panel takes the whole screen above the keyboard.
// One rule decides it, from where the cursor is once focus has settled, so moving from
// one field to another (say "Where to?" to the chat box) never flickers:
//  - tapped into a search field or the chat box: full-screen when the tap is over (its
//    click), so the layout doesn't move under the finger; tabbed in: right away;
//  - the cursor left them (and isn't on a button beside them): back to the tall panel.
const TYPING = "#fromInput, #toInput, #chatInput";
let tapping = null, settle = null;
function setFullscreen(on) {
  if (!isPhone() || document.body.classList.contains("fullscreen") === on) return;
  if (on) { setMin(false, { focus: false }); setTall(true); }
  document.body.classList.toggle("fullscreen", on);
  fitKeyboard();
  window.dispatchEvent(new CustomEvent("dmv:fullscreen", { detail: { on } }));
}
document.addEventListener("pointerdown", (e) => { tapping = e.target.closest?.(TYPING) || null; }, true);
document.addEventListener("click", (e) => {
  const field = e.target.closest?.(TYPING);
  if (field && field === tapping && document.activeElement === field) setFullscreen(true);
  tapping = null;
}, true);
document.addEventListener("focusin", (e) => {
  clearTimeout(settle);
  if (e.target.matches?.(TYPING) && tapping !== e.target) setFullscreen(true);
});
document.addEventListener("focusout", () => {
  clearTimeout(settle);
  settle = setTimeout(() => {
    const el = document.activeElement;
    if (el?.matches?.(TYPING) || el?.closest?.(".where, .composer")) return;
    setFullscreen(false);
  }, 200);
});

// Full-screen fits the space above the keyboard (where the browser reports it), so the
// chat box and the newest messages stay in sight while typing.
function fitKeyboard() {
  const vv = window.visualViewport, root = document.documentElement.style;
  if (!vv || !document.body.classList.contains("fullscreen")) {
    root.removeProperty("--vvh"); root.removeProperty("--vvt");
    return;
  }
  root.setProperty("--vvh", `${Math.round(vv.height)}px`);
  root.setProperty("--vvt", `${Math.round(vv.offsetTop)}px`);
}
window.visualViewport?.addEventListener("resize", fitKeyboard);
window.visualViewport?.addEventListener("scroll", fitKeyboard);
$("whereForm").addEventListener("submit", (e) => e.preventDefault());

$("swapBtn").addEventListener("click", () => {
  const from = state.from, to = state.to;
  if (!to) return;
  state.from = to; fromInput.value = to.label;
  state.to = from || (state.me ? { ...state.me, label: "My location" } : null);
  toInput.value = from ? from.label : (state.me ? "My location" : "");
  track("swap_places");
  if (state.to) planTrip();
});

function locate() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("unsupported"));
    navigator.geolocation.getCurrentPosition(
      (p) => { state.me = { lat: p.coords.latitude, lon: p.coords.longitude, label: "My location", source: "my_location" }; resolve(state.me); },
      (err) => { track("location_denied", { detail: { code: err?.code ?? 0 } }); reject(err); },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 });
  });
}

$("useLoc").addEventListener("click", async () => {
  state.from = null; fromInput.value = "My location";
  try {
    await locate();
    track("location_used");
    if (map) map.flyTo({ center: pt(state.me), zoom: 15, duration: reduceMotion ? 0 : 900 });
    if (state.to) planTrip(); else toInput.focus();
  } catch (_) {
    showNotice("Location is turned off for this site. Type a starting point instead.");
    fromInput.value = ""; fromInput.focus();
  }
});

// A trip the assistant found ("show_trip" from the chat): fill in the ends and plan it.
// Its places are labelled "assistant" (or "saved"), so their names stay out of the log.
function show(evt) {
  const end = (p) => p && { lat: p.lat, lon: p.lon, label: p.label, source: p.saved ? "saved" : "assistant", address: p.address };
  if (!evt.to) return;
  state.from = end(evt.from); state.to = end(evt.to);
  fromInput.value = state.from ? state.from.label : "My location";
  toInput.value = state.to.label;
  track("assistant_trip");
  planTrip();
}

// ------------------------------------------------------------------ saved places
// Quick picks under "Where to?" and pins on the map (the list itself is in account.js).
function goTo(point) {
  state.to = point; toInput.value = point.label;
  hideSuggestions(); toInput.blur();
  planTrip();
}
function startFrom(point) {
  state.from = point; fromInput.value = point.label;
  hideSuggestions();
  if (state.to) planTrip(); else toInput.focus();
}
// "Clear": the trip leaves the screen (its card, the route and pins on the map, the live
// times, the link in the address bar) and the search fields are empty again, ready for a
// new trip. Earlier trips and the conversation stay; "Start over" clears those as well.
function clearTrip({ focus = true } = {}) {
  if (journey.on) endJourney({ restore: false });
  const had = Boolean(state.plan || block);
  stopLive();
  block?.remove();
  block = null; results = null;
  state.plan = null; state.selected = null; state.view = "list"; state.searchId = null;
  state.to = null; state.from = null;
  toInput.value = ""; fromInput.value = "My location";
  if (map?.getSource("trip")) {
    const none = { type: "FeatureCollection", features: [] };
    for (const id of ["others", "trip", "stops"]) map.getSource(id)?.setData(none);
  }
  drawStepPins(null, []);
  destMarker?.remove(); youMarker?.remove();
  syncUrl();
  updatePeek();
  window.dispatchEvent(new CustomEvent("dmv:thread"));
  if (had) { track("trip_cleared"); announce("Trip cleared. Where to next?"); }
  if (focus && !isPhone()) toInput.focus();
}
window.dmvTrip = { goTo, startFrom, show, screen, clear: clearTrip };

function renderQuick() {
  const box = $("quickPlaces");
  const picks = sortPlaces(state.saved).filter(({ s }) => s.active).slice(0, 10);
  box.hidden = !picks.length;
  box.innerHTML = picks.length ? `<span class="quick-label" id="quickLabel">Your places</span>
    <div class="quick-row" role="group" aria-labelledby="quickLabel">${picks.map(({ p, s }) => `
      <button class="quick-chip kind-${esc(p.kind)}" type="button" data-id="${p.id}" aria-label="Go to ${esc(p.name)}${s.badge ? `, ${esc(s.badge)}` : ""}">
        ${KIND[p.kind]?.icon || ""}<span>${esc(p.name)}</span>${s.badge ? `<small>${esc(s.badge)}</small>` : ""}</button>`).join("")}</div>` : "";
  box.querySelectorAll(".quick-chip").forEach((b) => b.addEventListener("click", () => {
    const p = state.saved.find((x) => x.id === +b.dataset.id);
    if (!p) return;
    track("saved_place_used", { detail: { field: "to", kind: p.kind, from: "quick" } });
    goTo(savedPoint(p));
  }));
}

function drawSaved() {
  if (!map) return;
  savedMarkers.forEach((m) => m.remove()); savedMarkers = [];
  savedPopup?.remove();
  for (const { p, s } of sortPlaces(state.saved)) {
    if (s.past) continue;
    const el = document.createElement("button");
    el.type = "button";
    el.className = `saved-pin kind-${p.kind}`;
    el.innerHTML = KIND[p.kind]?.icon || "";
    el.setAttribute("aria-label", `Your place: ${p.name}`);
    el.addEventListener("click", (e) => { e.stopPropagation(); showSavedPopup(p, s); });
    savedMarkers.push(new maplibregl.Marker({ element: el }).setLngLat([p.lon, p.lat]).addTo(map));
  }
}

function showSavedPopup(p, s) {
  track("saved_place_pin", { detail: { kind: p.kind } });
  const box = document.createElement("div");
  box.className = "saved-popup";
  box.innerHTML = `<strong>${esc(p.name)}</strong><small>${esc([KIND[p.kind]?.name, s.badge].filter(Boolean).join(", "))}</small>
    <div class="popup-actions"><button class="btn small primary" type="button" data-act="to">Go here</button>
    <button class="btn small quiet" type="button" data-act="from">Start here</button></div>`;
  box.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    savedPopup?.remove();
    track("saved_place_used", { detail: { field: act, kind: p.kind, from: "pin" } });
    if (act === "to") goTo(savedPoint(p)); else startFrom(savedPoint(p));
  });
  savedPopup?.remove();
  savedPopup = new maplibregl.Popup({ offset: 18, closeButton: true, maxWidth: "260px" })
    .setLngLat([p.lon, p.lat]).setDOMContent(box).addTo(map);
  box.querySelector("button")?.focus();
}

window.addEventListener("dmv:places", (e) => {
  state.saved = e.detail.places || [];
  state.accounts = Boolean(e.detail.enabled || e.detail.signedIn);
  renderQuick(); drawSaved();
  if (state.plan && state.view === "list") render();
});

// ------------------------------------------------------------------ trip cards
// The newest search is a live card in the thread; earlier ones fold into a summary with
// "Check again".
let block = null, results = null;

function newBlock() {
  if (block) freeze(block);
  block = document.createElement("section");
  block.className = "trip-block";
  block.ends = { from: state.from, to: state.to };
  block.at = new Date();
  const from = state.from?.label || "My location";
  block.setAttribute("aria-label", `Trips to ${state.to.label}`);
  block.innerHTML = `<div class="trip-head">${ICONS.route}<div class="trip-ends">
      <strong>${esc(state.to.label)}</strong><small>From ${esc(from)}</small></div>
      <button class="trip-clear" type="button" aria-label="Clear this trip from the map">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>Clear</button></div>
    <div class="trip-body"></div>`;
  block.querySelector(".trip-clear").addEventListener("click", () => clearTrip());
  results = block.querySelector(".trip-body");
  append(block, { top: true });
  if (journey.on) endJourney({ restore: false });
  window.dmvPanel?.show();
  window.dmvPanel?.lower();
}

function freeze(el) {
  el.classList.add("past");
  el.querySelector(".trip-clear")?.remove();
  const n = el.plan?.options?.length || 0;
  const when = el.at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const body = el.querySelector(".trip-body");
  body.innerHTML = `<p class="trip-past">${n ? `${plural(n, "way", "ways")} to go` : "No trips found"} at ${esc(when)}.</p>
    <button class="btn small quiet" type="button">Check again</button>`;
  body.querySelector("button").addEventListener("click", () => {
    state.from = el.ends.from; state.to = el.ends.to;
    fromInput.value = state.from?.label || "My location"; toInput.value = state.to.label;
    el.remove();
    planTrip();
  });
}

// A notice with no trip card yet (e.g. location is off) goes in the thread on its own.
function showNotice(text, extraHtml = "") {
  if (!results) {
    const el = document.createElement("div");
    el.className = "msg notice";
    el.textContent = text;
    append(el);
    return;
  }
  results.innerHTML = `<div class="notice">${esc(text)}${extraHtml}</div>`;
}

// What's on screen, for the assistant: "the trip I'm looking at" means this one.
function describe(opt) {
  const parts = [`leave ${clock(opt.leave_at)}`];
  for (const leg of opt.legs) {
    if (leg.mode === "walk") {
      const m = mins(leg.duration_s);
      if (m) parts.push(`walk ${m} min${leg.to ? ` to ${leg.to.name}` : ""}`);
    } else {
      const live = leg.live ? (leg.delay_s >= 60 ? `live, ${mins(leg.delay_s)} min late` : "live") : "scheduled";
      parts.push(`${leg.agency_name} ${routeLabel(leg)}${leg.headsign ? ` toward ${leg.headsign}` : ""} from ${leg.board.name} at ${clock(leg.board.expected)} to ${leg.alight.name} at ${clock(leg.alight.expected)} (${live})`);
    }
  }
  parts.push(`arrive ${clock(opt.arrive_at)}`);
  if (opt.fare_estimate != null) parts.push(`about $${opt.fare_estimate.toFixed(2)}`);
  return parts.join("; ");
}

function screen() {
  if (!block || !state.plan) return null;
  const opt = state.plan.options?.[state.selected];
  return {
    from: block.ends.from?.label || "the rider's current location",
    to: block.ends.to.label,
    options: state.plan.options?.length || 0,
    selected: opt ? describe(opt) : null,
  };
}

// ------------------------------------------------------------------ planning
async function planTrip() {
  if (!state.to) return;
  newBlock();
  const card = block;
  let from = state.from;
  if (!from) {
    results.innerHTML = `<div class="status-line">Finding where you are…</div>`;
    try { from = await locate(); }
    catch (_) {
      showNotice("Location is turned off for this site. Type a starting point instead.");
      fromInput.value = ""; fromInput.focus(); return;
    }
  }
  if (card !== block) return;   // a newer search started meanwhile
  results.innerHTML = `<div class="status-line">Checking departures near you…</div>`;
  stopLive();
  syncUrl();
  let data;
  try {
    const r = await fetch("/api/trip", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from, to: state.to }),
    });
    data = await r.json();
  } catch (_) {
    if (card === block) showNotice("Couldn't reach the trip planner. Check your connection and try again.");
    return;
  }
  if (card !== block) return;
  state.plan = data; state.selected = data.options?.length ? 0 : null; state.view = "list";
  state.searchId = data.search_id || null;
  block.plan = data;
  pin(block);   // the options just arrived: bring the card into view
  announce(data.options?.length ? `${plural(data.options.length, "way", "ways")} to go to ${state.to.label}.` : (data.notice || "No trips found."));
  render();
  drawPlan();
  startLive();
  loadWalks(data.options?.[0]);
}

function render() {
  const plan = state.plan;
  if (!plan) return;
  freshenWalk(plan);
  if (!plan.options?.length) {
    showNotice(plan.notice || "No trips found.");
    return;
  }
  updatePeek();
  if (state.view === "detail" && state.selected != null) return renderDetail(plan.options[state.selected]);
  const n = plan.options.length;
  const liveNote = plan.live_available === false ? " Live times are paused; showing the timetable." : "";
  results.innerHTML = `<div class="status-line">${plural(n, "way", "ways")} to go in the next ${span(plan.window_min)}. Tap a trip to see it on the map, and again for directions.${liveNote}</div>`
    + saveButton()
    + plan.options.map((opt, i) => optionCard(opt, i)).join("");
  $("savePlace")?.addEventListener("click", () => {
    const t = state.to;
    window.dmvAccount?.savePlace({ name: t.label, lat: t.lat, lon: t.lon, address: t.address || t.label, kind: t.kind });
  });
  results.querySelectorAll(".option").forEach((el) => el.addEventListener("click", () => {
    const i = +el.dataset.i;
    if (state.selected === i) {
      state.view = "detail"; render();
      track("details_opened", { search_id: state.searchId, option_index: i });
    } else {
      state.selected = i; render(); drawPlan(); restartLive(); loadWalks(state.plan.options[i]);
      track("option_selected", { search_id: state.searchId, option_index: i });
    }
  }));
  tick();
}

// "Save this place" for a destination picked from the search (not one already saved).
function saveButton() {
  const t = state.to;
  if (!state.accounts || !t || t.source === "saved" || t.source === "my_location") return "";
  if (state.saved.some((p) => Math.abs(p.lat - t.lat) < 0.0005 && Math.abs(p.lon - t.lon) < 0.0005)) return "";
  return `<button class="save-place" id="savePlace" type="button">${KIND.attraction.icon}Save ${esc(t.label)} to My places</button>`;
}

function countdown(opt) {
  if (opt.walk_only) return { cls: "walk", n: `${mins(opt.walk_s)}`, u: "min walk" };
  const m = Math.round((opt.leave_at - nowS()) / 60);
  if (m <= 0) return { cls: "now", n: "Now", u: "Leave now" };
  return { cls: "", n: `${m}`, u: `min, leave by ${clock(opt.leave_at)}` };
}

function span(minutes) {
  return minutes >= 120 && minutes % 60 === 0 ? `${minutes / 60} hours` : `${minutes} minutes`;
}

function walkLegs(opt) {
  // An airport shuttle ride isn't walking.
  return opt.legs.filter((l) => l.mode === "walk").map((l) => l.duration_s - (l.airport?.shuttle_s || 0));
}

const shuttleLeg = (opt) => opt.legs.find((l) => l.airport?.mode === "shuttle");

// An airport's own directions between the terminal and its rail station.
function airportStep(leg, next) {
  const a = leg.airport, m = mins(leg.duration_s);
  const toStation = Boolean(leg.to);
  const title = a.mode === "shuttle"
    ? (toStation ? `Free ${a.code} shuttle to ${a.station}, about ${m} min` : `Free ${a.code} shuttle to the terminal, about ${m} min`)
    : (toStation ? `Walk about ${m} min to ${leg.to.name}` : `Walk about ${m} min into ${a.name}`);
  const leaves = toStation && next ? ` Your ${isRail(next) ? "train" : "bus"} leaves at ${clock(next.board.expected)}.` : "";
  return { cls: "walk", title, sub: `${a.text}${leaves} (Directions from the airport's website.)` };
}

function optionCard(opt, i) {
  if (opt.walk_only) return walkCard(opt, i);
  const c = countdown(opt);
  const rs = rides(opt);
  const pills = rs.map(pill).join(ICONS.chev);
  const walk = mins(opt.walk_s);
  const longWalk = walkLegs(opt).find((s) => s >= 600);
  const shuttle = shuttleLeg(opt);
  const longWait = opt.legs.find((l) => l.transfer && l.wait_s > (state.plan.short_transfer_min || 10) * 60);
  const changes = opt.transfers ? plural(opt.transfers, "change", "changes") : "No changes";
  const late = rs.find((l) => l.live && l.delay_s >= 120);
  const tag = opt.live ? `<span class="live-tag">Live</span>` : `<span class="sched-tag">Scheduled</span>`;
  return `<button class="option" type="button" data-i="${i}" aria-pressed="${i === state.selected}"
      aria-label="Option ${i + 1}: ${c.n === "Now" ? "leave now" : `leave in ${esc(c.n)} minutes, by ${clock(opt.leave_at)}`}. ${esc(rs.map(routeLabel).join(" then "))}. Arrive ${clock(opt.arrive_at)}.">
    <div class="count ${c.cls}" data-leave="${opt.leave_at}"><span class="n">${esc(c.n)}</span><span class="u">${esc(c.u)}</span></div>
    <div class="pills"><span class="walk-mins" aria-label="${walk} minutes walking">${ICONS.walk}${walk} min</span>${ICONS.chev}${pills}</div>
    <div class="meta">Arrive <strong>${clock(opt.arrive_at)}</strong>, ${mins(opt.duration_s)} min. ${changes}. ${tag}
      ${late ? `<span class="late">${mins(late.delay_s)} min late</span>` : ""}
      ${longWalk ? `<br><span class="walk-note">Includes a ${mins(longWalk)}-minute walk</span>` : ""}
      ${shuttle ? `<br><span class="walk-note">Includes the free ${esc(shuttle.airport.code)} shuttle</span>` : ""}
      ${longWait ? `<br><span class="wait-note">Includes a ${mins(longWait.wait_s)}-minute wait at ${esc(longWait.to.name)}</span>` : ""}
      ${opt.reports?.length ? `<br><span class="report-note">${ICONS.alert}Rider report, ${esc(ago(opt.reports[0].minutes_ago))}: ${esc(opt.reports[0].summary)}</span>` : ""}
      ${opt.fare_estimate != null ? `<br>About $${opt.fare_estimate.toFixed(2)}` : ""}</div>
  </button>`;
}

// Walking the whole way (a short trip): leave whenever, no fare.
function walkCard(opt, i) {
  const c = countdown(opt);
  const far = opt.legs[0].walk?.routed ? ` (${distance(opt.legs[0].walk.distance_m)})` : "";
  return `<button class="option walk-option" type="button" data-i="${i}" aria-pressed="${i === state.selected}"
      aria-label="Option ${i + 1}: walk ${mins(opt.walk_s)} minutes. Arrive ${clock(opt.arrive_at)}.">
    <div class="count ${c.cls}" data-leave="${opt.leave_at}"><span class="n">${esc(c.n)}</span><span class="u">${esc(c.u)}</span></div>
    <div class="pills"><span class="walk-mins">${ICONS.walk}Walk${esc(far)}</span></div>
    <div class="meta">Arrive <strong>${clock(opt.arrive_at)}</strong> if you leave now. No bus or train needed.<br>Free</div>
  </button>`;
}

// A walk-only option has no departure: its times run from now.
function freshenWalk(plan) {
  for (const o of plan?.options || []) {
    if (!o.walk_only) continue;
    o.leave_at = o.depart_at = nowS();
    o.arrive_at = o.leave_at + o.walk_s;
    o.duration_s = o.walk_s;
  }
}

// A trip's steps in order: one per leg (walk, ride, airport shuttle), then "Arrive". The
// trip card's list, the numbered pins on the map and "Follow on the map" all use these.
function tripSteps(opt) {
  const plan = state.plan;
  const steps = [];
  const legs = opt.legs;
  legs.forEach((leg, idx) => {
    if (leg.mode === "walk") {
      const next = legs[idx + 1];
      if (leg.airport) {
        steps.push(airportStep(leg, next));
      } else if (idx === 0 && next) {
        steps.push({ cls: "walk", title: walkTitle(leg, leg.to.name), html: true,
          sub: (next ? `Your ${isRail(next) ? "train" : "bus"} leaves at <span class="time">${clock(next.board.expected)}</span>` : "") + walkDetails(leg) });
      } else if (leg.transfer) {
        const tight = leg.wait_s < 60;
        steps.push({ cls: "walk", title: leg.duration_s > 30 ? walkTitle(leg, leg.to.name) : `Change at ${leg.to.name}`, html: true,
          sub: (tight ? "A tight connection: walk straight there" : `About ${mins(leg.wait_s)} min to make the connection`) + walkDetails(leg) });
      } else {
        steps.push({ cls: "walk", title: walkTitle(leg, plan.destination.label || "your destination"), html: true, sub: walkDetails(leg).replace(/^<br>/, "") });
      }
    } else {
      const delay = leg.live && leg.delay_s ? (leg.delay_s >= 60 ? `, ${mins(leg.delay_s)} min late` : leg.delay_s <= -60 ? `, ${mins(-leg.delay_s)} min early` : ", on time") : "";
      steps.push({ cls: "ride", color: leg.color,
        title: `${routeLabel(leg)}${leg.headsign ? ` toward ${leg.headsign}` : ""}`,
        sub: `${esc(leg.agency_name)}. Leaves <span class="time">${clock(leg.board.expected)}</span> from ${esc(leg.board.name)}${delay} ${leg.live ? '<span class="live-tag">Live</span>' : '<span class="sched-tag">Scheduled</span>'}<br>Ride ${plural(leg.stops, "stop", "stops")}, get off at ${esc(leg.alight.name)} around <span class="time">${clock(leg.alight.expected)}</span>`,
        html: true });
    }
    steps[steps.length - 1].leg = idx;
  });
  steps.push({ cls: "end", title: `Arrive around ${clock(opt.arrive_at)}`, sub: "", leg: null });
  return steps;
}

function renderDetail(opt) {
  const steps = tripSteps(opt);
  results.innerHTML = `<div class="detail">
    <button class="back" id="backBtn" type="button">${ICONS.back}All options</button>
    <div class="detail-head"><span class="big">${clock(opt.leave_at)}</span><span>leave, arrive ${clock(opt.arrive_at)} (${mins(opt.duration_s)} min)</span></div>
    <button class="btn primary follow-btn" id="followTrip" type="button">${ICONS.route}Follow step by step on the map</button>
    <ol class="steps">${steps.map((s, i) => `
      <li class="step ${s.cls}" style="${s.color ? `--c:${esc(s.color)}` : ""}">
        <div class="rail-col"><button class="node" type="button" data-step="${i}" aria-label="Show step ${i + 1} on the map">${s.cls === "end" ? "" : i + 1}</button>${i < steps.length - 1 ? '<span class="stem"></span>' : ""}</div>
        <div class="body"><div class="title">${esc(s.title)}</div>${s.sub ? `<div class="sub">${s.html ? s.sub : esc(s.sub)}</div>` : ""}</div>
      </li>`).join("")}</ol>
    ${opt.reports?.length ? `<section class="trip-reports" aria-label="Rider reports">
      <h3>${ICONS.alert}What other riders reported</h3>
      <ul class="recent-list">${opt.reports.map(reportItem).join("")}</ul>
      <p class="report-fine">Shared by riders on this site, not checked by any transit agency.</p>
    </section>` : ""}
    <div class="detail-actions">
      ${rides(opt).length ? '<button class="btn quiet" id="askTrip" type="button">Ask about this trip</button>' : ""}
      <button class="btn quiet" id="reportTrip" type="button">Report a problem</button>
    </div>
  </div>`;
  $("backBtn").addEventListener("click", () => { state.view = "list"; render(); });
  $("followTrip").addEventListener("click", () => startJourney(0));
  results.querySelectorAll(".step .node").forEach((b) => b.addEventListener("click", () => startJourney(+b.dataset.step)));
  $("reportTrip").addEventListener("click", () => {
    track("report_from_trip", { search_id: state.searchId, option_index: state.selected });
    window.dmvReports?.open();
  });
  $("askTrip")?.addEventListener("click", () => {
    track("ask_about_trip", { search_id: state.searchId, option_index: state.selected });
    const desc = rides(opt).map((l) => `${l.agency_name} ${routeLabel(l)} from ${l.board.name} to ${l.alight.name}`).join(", then ");
    window.dmvChat?.ask(`I'm about to take ${desc}. How do I pay, and is there anything I should know?`);
  });
  $("backBtn").focus();
}

// ------------------------------------------------------------------ live updates
function tick() {
  if (!state.plan?.options?.length || !results) return;
  results.querySelectorAll(".count[data-leave]").forEach((el, i) => {
    const opt = state.plan.options[i];
    if (!opt) return;
    const c = countdown(opt);
    el.className = `count ${c.cls}`;
    el.querySelector(".n").textContent = c.n;
    el.querySelector(".u").textContent = c.u;
  });
}

async function refreshLive() {
  const opt = state.plan?.options?.[state.selected];
  if (!opt || document.hidden) return;
  const legs = rides(opt);
  if (!legs.length) return;                 // walking: nothing to update
  try {
    const r = await fetch("/api/trip/live", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rides: legs.map((l) => ({ agency: l.agency, stop_id: l.board.stop_id, trip_id: l.trip_id, scheduled: l.board.scheduled })) }),
    });
    const data = await r.json();
    let changed = false;
    for (const u of data.rides || []) {
      const leg = legs.find((l) => l.trip_id === u.trip_id);
      if (!leg || u.expected == null) continue;
      const shift = u.expected - leg.board.expected;
      if (Math.abs(shift) < 30 && leg.live === u.live) continue;
      leg.board.expected += shift; leg.alight.expected += shift;
      leg.live = u.live; leg.delay_s = u.delay_s; changed = true;
      if (leg === legs[0]) { opt.leave_at += shift; opt.depart_at += shift; }
      if (leg === legs[legs.length - 1]) opt.arrive_at += shift;
    }
    if (changed) { opt.live = legs.some((l) => l.live); render(); }
  } catch (_) { /* keep the last known times */ }
}

function startLive() {
  stopLive();
  liveTimer = { tick: setInterval(tick, 1000), live: setInterval(refreshLive, 30000) };
}
function stopLive() {
  if (liveTimer) { clearInterval(liveTimer.tick); clearInterval(liveTimer.live); liveTimer = null; }
}
function restartLive() { startLive(); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); refreshLive(); } });

$("aboutLink").addEventListener("click", (e) => { e.preventDefault(); window.dmvChat?.about(); });

// Shareable trips: ?to=lat,lon&tolabel=...&from=lat,lon&fromlabel=...
function placeParam(params, key) {
  const v = params.get(key);
  if (!v) return null;
  const [lat, lon] = v.split(",").map(Number);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { lat, lon, label: params.get(`${key}label`) || `${lat.toFixed(4)}, ${lon.toFixed(4)}`, source: "link" };
}

function syncUrl() {
  const p = new URLSearchParams();
  // Saved places (a hotel, a home) never go into the address bar or shared links.
  if (state.to?.source === "saved" || state.from?.source === "saved") {
    history.replaceState(null, "", location.pathname);
    return;
  }
  if (state.to) { p.set("to", `${state.to.lat.toFixed(5)},${state.to.lon.toFixed(5)}`); p.set("tolabel", state.to.label); }
  if (state.from) { p.set("from", `${state.from.lat.toFixed(5)},${state.from.lon.toFixed(5)}`); p.set("fromlabel", state.from.label); }
  history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
}

const startParams = new URLSearchParams(location.search);
state.from = placeParam(startParams, "from");
state.to = placeParam(startParams, "to");
if (state.from) fromInput.value = state.from.label;
if (state.to) toInput.value = state.to.label;

// A visit: where it came from (the other site's name only) and the screen size.
let referrer = "";
try { const r = document.referrer && new URL(document.referrer); referrer = r && r.host !== location.host ? r.host : ""; } catch (_) {}
const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
track("page_view", { detail: { referrer, trip_link: Boolean(state.to), app: installed, screen: innerWidth < 720 ? "phone" : innerWidth < 1100 ? "tablet" : "large" } });

// ------------------------------------------------------------------ install as an app
// The service worker makes the site installable and gives it an offline page (sw.js).
if ("serviceWorker" in navigator) {
  addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
// Browsers that offer installing (Chrome, Edge) show "Install the app" in the fine print.
let installPrompt = null;
addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  $("installWrap").hidden = false;
});
$("installLink").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!installPrompt) return;
  track("app_install_clicked");
  installPrompt.prompt();
  await installPrompt.userChoice.catch(() => null);
  installPrompt = null;
  $("installWrap").hidden = true;
});
addEventListener("appinstalled", () => { $("installWrap").hidden = true; track("app_installed"); });

initMap();
if (state.to) planTrip(); else if (!isPhone()) toInput.focus();

// ------------------------------------------------------------------ follow on the map
// A trip one step at a time: the step card at the bottom (Back / Next), the map zoomed to
// that part of the journey with the rest faded, numbered pins matching the trip card, and
// the rider's own position when they've already shared it.
const TRIP_LINES = ["trip-casing", "trip-ride", "trip-walk", "trip-shuttle-casing", "trip-shuttle"];

function stepColor(s) {
  if (s.cls === "ride") return s.color || "#2b6f77";
  return s.cls === "end" ? "#DE6A3A" : "#4A5651";
}

function drawStepPins(opt, feats) {
  stepPins.forEach((m) => m.remove()); stepPins = [];
  if (!opt) return;
  tripSteps(opt).forEach((s, i) => {
    if (s.leg == null) return;                        // "Arrive": the destination pin
    const f = feats.find((x) => x.properties.step === s.leg);
    if (!f) return;
    const el = document.createElement("button");
    el.type = "button";
    el.className = "step-pin";
    el.style.setProperty("--c", stepColor(s));
    el.style.color = textOn(stepColor(s));
    el.textContent = String(i + 1);
    el.dataset.step = String(i);
    el.setAttribute("aria-label", `Step ${i + 1}: ${s.title}`);
    el.addEventListener("click", (e) => { e.stopPropagation(); startJourney(i); });
    // A ride starts at its stop; a walk where its path starts.
    const leg = opt.legs[s.leg];
    const at = leg.mode === "ride" ? pt(leg.board) : f.geometry.coordinates[0];
    stepPins.push(new maplibregl.Marker({ element: el, anchor: "bottom-left", offset: [2, -2] })
      .setLngLat(at).addTo(map));
  });
  // Which station door: a tag where the walk meets the station ("Entrance B"), shown
  // while following that step.
  opt.legs.forEach((leg, legIdx) => {
    const w = leg.walk, path = w?.routed ? w.path : null;
    if (!path?.length) return;
    for (const [name, at] of [[w.entrance, path[path.length - 1]], [w.exit, path[0]]]) {
      if (!name) continue;
      const tag = document.createElement("div");
      tag.className = "door-tag";
      tag.dataset.leg = String(legIdx);
      tag.textContent = doorName(name);
      tag.title = name;
      stepPins.push(new maplibregl.Marker({ element: tag, anchor: "top", offset: [0, 6] }).setLngLat(at).addTo(map));
    }
  });
  markCurrentPin();
}

// "Entrance B - 9st & G St, Gallery Place" -> "Entrance B"; "Dupont Circle - Connecticut
// Ave. & Q St. NW" -> "Connecticut Ave. & Q St. NW".
function doorName(name) {
  const m = name.match(/^(Entrance [A-Z0-9]+)\b/i);
  if (m) return m[1];
  const parts = name.split(" - ");
  return (parts.length > 1 ? parts.slice(1).join(" - ") : name).replace(/\s*\(Elevator\)/i, "").slice(0, 40);
}

function markCurrentPin() {
  const leg = journey.on ? journey.steps[journey.i]?.leg : undefined;
  for (const m of stepPins) {
    const el = m.getElement();
    if (el.dataset.step) el.classList.toggle("current", journey.on && +el.dataset.step === journey.i);
    if (el.dataset.leg) el.classList.toggle("show", leg != null && +el.dataset.leg === leg);
  }
}

function emphasize(leg) {
  if (!map?.getLayer("trip-ride")) return;
  const opacity = leg === undefined ? 1 : ["case", ["==", ["get", "step"], leg], 1, 0.28];
  for (const id of TRIP_LINES) map.setPaintProperty(id, "line-opacity", id === "trip-shuttle-casing" || id === "trip-casing" ? (leg === undefined ? 1 : ["case", ["==", ["get", "step"], leg], 1, 0]) : opacity);
}

function startJourney(i = 0) {
  const opt = state.plan?.options?.[state.selected];
  if (!opt || !map) return;
  journey.on = true; journey.opt = opt;
  journey.steps = tripSteps(opt); journey.feats = optionFeatures(opt).feats;
  $("journey").hidden = false;
  document.body.classList.add("following");
  setMin(true, { focus: false });                    // the step card takes the panel's place
  watchMe();
  track("journey_started", { search_id: state.searchId, option_index: state.selected, detail: { step: i } });
  showStep(i);
  $("journeyNext").focus({ preventScroll: true });
}

function refreshJourney() {
  journey.steps = tripSteps(journey.opt); journey.feats = optionFeatures(journey.opt).feats;
  showStep(journey.i, { fit: false });
}

function showStep(i, { fit = true } = {}) {
  const steps = journey.steps;
  i = Math.max(0, Math.min(i, steps.length - 1));
  journey.i = i;
  const s = steps[i], last = i === steps.length - 1;
  $("journeyCount").textContent = `Step ${i + 1} of ${steps.length}`;
  const sub = s.sub ? (s.html ? s.sub : esc(s.sub)).replace(/^<br>/, "").replace('<details class="walk-turns">', '<details class="walk-turns" open>') : "";
  $("journeyBody").innerHTML = `<div class="jn-row">
      <span class="jn-num" style="--c:${esc(stepColor(s))};color:${textOn(stepColor(s))}">${last ? ICONS.flag : i + 1}</span>
      <div><h2 class="jn-title" id="journeyTitle">${esc(s.title)}</h2>${sub ? `<div class="jn-sub">${sub}</div>` : ""}</div>
    </div>`;
  $("journeyPrev").disabled = i === 0;
  $("journeyNext").firstChild.textContent = last ? "Done " : "Next ";
  emphasize(s.leg ?? journey.opt.legs.length - 1);   // arriving: the last walk
  markCurrentPin();
  if (!fit) return;
  sizeSheet();                                        // the card's new height, before zooming
  const coords = journey.feats.filter((f) => f.properties.step === s.leg).flatMap((f) => f.geometry.coordinates);
  if (s.leg == null) {                                // arriving: the end of the last walk
    const d = state.plan.destination;
    map.easeTo({ center: [d.lon, d.lat], zoom: Math.max(map.getZoom(), 16), duration: reduceMotion ? 0 : 700 });
  } else if (coords.length) {
    const b = new maplibregl.LngLatBounds(coords[0], coords[0]);
    coords.forEach((c) => b.extend(c));
    map.fitBounds(b, { duration: reduceMotion ? 0 : 700, maxZoom: 17 });
  }
  if (i) track("journey_step", { search_id: state.searchId, detail: { step: i } });
}

function endJourney({ restore = true } = {}) {
  if (!journey.on) return;
  journey.on = false;
  $("journey").hidden = true;
  document.body.classList.remove("following");
  emphasize(undefined);
  markCurrentPin();
  stopWatch();
  track("journey_done", { search_id: state.searchId, detail: { step: journey.i } });
  if (restore) {
    state.view = "detail";
    setMin(false);
    render();
  } else {
    sizeSheet();
  }
}

$("journeyPrev").addEventListener("click", () => showStep(journey.i - 1));
$("journeyNext").addEventListener("click", () => (journey.i >= journey.steps.length - 1 ? endJourney() : showStep(journey.i + 1)));
$("journeyClose").addEventListener("click", () => endJourney());
document.addEventListener("keydown", (e) => {
  if (!journey.on || e.target.closest("input, textarea")) return;
  if (e.key === "Escape") endJourney();
  else if (e.key === "ArrowRight") showStep(journey.i + 1);
  else if (e.key === "ArrowLeft") showStep(journey.i - 1);
});

// The rider's own dot while following, only if they've already let the site use their
// location (never asks again here).
async function watchMe() {
  if (!navigator.geolocation || journey.watch != null) return;
  try {
    const p = await navigator.permissions?.query({ name: "geolocation" });
    if (p?.state !== "granted" || !journey.on) return;
  } catch (_) { return; }
  journey.watch = navigator.geolocation.watchPosition((pos) => {
    if (!journey.me) {
      const el = document.createElement("div"); el.className = "you-dot";
      el.setAttribute("aria-label", "You are here");
      journey.me = new maplibregl.Marker({ element: el });
    }
    journey.me.setLngLat([pos.coords.longitude, pos.coords.latitude]).addTo(map);
  }, () => {}, { enableHighAccuracy: true, maximumAge: 10000, timeout: 20000 });
}
function stopWatch() {
  if (journey.watch != null) navigator.geolocation.clearWatch(journey.watch);
  journey.watch = null;
  journey.me?.remove(); journey.me = null;
}

// ------------------------------------------------------------------ phone menu
// On phones one labeled Menu holds what the top bar's icons do on larger screens.
const menuBtn = $("menuBtn"), menuList = $("menuList");
function setMenu(open) {
  menuList.hidden = !open;
  menuBtn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  track("menu_opened");
  for (const b of menuList.querySelectorAll("button[data-for]")) {
    const target = $(b.dataset.for);
    b.hidden = !target || Boolean(target.closest("[hidden]"));
  }
  const account = menuList.querySelector('[data-for="accountBtn"] span');
  if (account) account.textContent = $("accountBtnText")?.textContent || "Sign in";
  const explore = menuList.querySelector('[data-for="exploreBtn"] span');
  if (explore) explore.textContent = $("exploreBtn").getAttribute("aria-pressed") === "true" ? "Close Explore" : "Explore every stop and route";
  menuList.querySelector("button:not([hidden])")?.focus();
}
menuBtn.addEventListener("click", (e) => { e.stopPropagation(); setMenu(menuList.hidden); });
menuList.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-for]");
  if (!b) return;
  setMenu(false);
  $(b.dataset.for)?.click();
});
document.addEventListener("click", (e) => {
  if (!menuList.hidden && !menuList.contains(e.target) && !menuBtn.contains(e.target)) setMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !menuList.hidden) { setMenu(false); menuBtn.focus(); }
});
