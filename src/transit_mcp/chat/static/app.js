// DMV Transit - map-first, leave-now trip planner.
// Data: /api/geocode, /api/trip, /api/trip/live (chat/trip_api.py). Map: MapLibre GL with a
// self-hosted Protomaps basemap (/tiles/dmv.pmtiles); all files are served by this site.
import * as ML from "/static/map/vendor/maplibre-gl.mjs";

const maplibregl = ML.default || ML;
const $ = (id) => document.getElementById(id);
const ORIGIN = location.origin;
const DMV_CENTER = [-77.0369, 38.9072];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const isPhone = () => matchMedia("(max-width: 720px)").matches;

const state = {
  from: null,            // {lat, lon, label} or null = "My location"
  to: null,
  me: null,              // last known position
  plan: null,
  selected: null,        // index into plan.options
  view: "list",          // list | detail
};
let map, youMarker, destMarker, liveTimer;

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
  walk: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="M10 22l2-7-3-3 1-5 4 3 3 1M9 12l-3 3"/></svg>',
  chev: '<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
  back: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>',
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
  return (1.05 / (L + 0.05)) >= ((L + 0.05) / (luminance("#20242A") + 0.05)) ? "#ffffff" : "#20242A";
}

function pill(leg) {
  const style = `background:${esc(leg.color)};color:${textOn(leg.color)}`;
  if (isRail(leg)) {
    const bullet = esc((leg.route_short_name || routeLabel(leg)).slice(0, 2));
    return `<span class="pill rail" style="${style}"><span class="bullet">${bullet}</span>${esc(routeLabel(leg))}</span>`;
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
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#ECE8E1" } }],
  };
  if (tiles && window.pmtiles && window.basemaps) {
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    // Protomaps "light", warmed toward the site's concrete-and-bronze palette.
    const flavor = {
      ...basemaps.namedFlavor("light"),
      background: "#ECE8E1", earth: "#EFEBE4", water: "#BCD2D8",
      park_a: "#DCE5D1", park_b: "#D3DFC6", wood_a: "#DCE5D1", wood_b: "#D3DFC6",
      buildings: "#E1DBD0", pedestrian: "#E9E4DB",
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
  applyPadding();
  addEventListener("resize", applyPadding);
}

function applyPadding() {
  if (!map) return;
  map.setPadding(isPhone()
    ? { top: 70, bottom: Math.round(innerHeight * 0.55), left: 20, right: 20 }
    : { top: 80, bottom: 40, left: 480, right: 80 });
}

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
    paint: { "line-color": "#4F5560", "line-width": 4, "line-dasharray": [0.1, 1.8] } });
  map.addLayer({ id: "stops-dot", type: "circle", source: "stops",
    paint: { "circle-radius": 7, "circle-color": "#ffffff", "circle-stroke-width": 4,
             "circle-stroke-color": ["get", "color"] } });
  map.addLayer({ id: "stops-label", type: "symbol", source: "stops",
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": 14,
              "text-offset": [0, 1.3], "text-anchor": "top", "text-max-width": 12 },
    paint: { "text-color": "#20242A", "text-halo-color": "#ffffff", "text-halo-width": 2 } });
  if (state.plan) drawPlan();
}

const line = (coords, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: coords } });
const pt = (p) => [p.lon, p.lat];

function optionFeatures(opt) {
  const feats = [], stops = [];
  const o = state.plan.origin, d = state.plan.destination;
  let prev = pt(o);
  for (const leg of opt.legs) {
    if (leg.mode === "walk") {
      const target = leg.to ? pt(leg.to) : pt(d);
      const from = leg.from ? pt(leg.from) : prev;
      feats.push(line([from, target], { kind: "walk" }));
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

function drawPlan() {
  if (!map || !map.getSource("trip") || !state.plan) return;
  const opts = state.plan.options;
  const others = [];
  opts.forEach((opt, i) => {
    if (i === state.selected) return;
    for (const leg of rides(opt)) {
      if (leg.geometry?.length > 1) others.push(line(leg.geometry, { color: leg.color }));
    }
  });
  map.getSource("others").setData({ type: "FeatureCollection", features: others });
  const sel = opts[state.selected];
  const { feats, stops } = sel ? optionFeatures(sel) : { feats: [], stops: [] };
  map.getSource("trip").setData({ type: "FeatureCollection", features: feats });
  map.getSource("stops").setData({ type: "FeatureCollection", features: stops });

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
  map.fitBounds(b, { duration: reduceMotion ? 0 : 900, maxZoom: 16 });
}

// ------------------------------------------------------------------ search
const fromInput = $("fromInput"), toInput = $("toInput"), suggest = $("suggest");
let activeField = null, suggestItems = [], suggestIndex = -1, searchTimer;

function showSuggestions(field, items) {
  activeField = field; suggestItems = items; suggestIndex = -1;
  suggest.innerHTML = items.map((it, i) =>
    `<li role="option" id="sg${i}" aria-selected="false">${esc(it.label)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</li>`).join("");
  suggest.hidden = !items.length;
  field.setAttribute("aria-expanded", String(!!items.length));
  [...suggest.children].forEach((li, i) => li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); }));
}

function hideSuggestions() {
  suggest.hidden = true; suggestItems = [];
  fromInput.setAttribute("aria-expanded", "false"); toInput.setAttribute("aria-expanded", "false");
}

function onType(field) {
  clearTimeout(searchTimer);
  const q = field.value.trim();
  if (field === fromInput) state.from = q.toLowerCase() === "my location" ? null : state.from;
  if (q.length < 3) {
    showSuggestions(field, field === fromInput ? [{ label: "My location", me: true }] : []);
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
      const data = await r.json();
      const items = (data.results || []).map((x) => ({ ...x, sub: x.full_label && x.full_label !== x.label ? x.full_label.split(",").slice(-4, -2).join(",").trim() : "" }));
      if (field === fromInput) items.unshift({ label: "My location", me: true });
      if (!items.length && data.message) items.push({ label: data.message, disabled: true });
      if (document.activeElement === field) showSuggestions(field, items);
    } catch (_) { /* keep typing; nothing to show */ }
  }, 380);
}

function pick(i) {
  const it = suggestItems[i];
  if (!it || it.disabled) return;
  const field = activeField;
  hideSuggestions();
  if (field === fromInput) {
    if (it.me) { state.from = null; fromInput.value = "My location"; }
    else { state.from = { lat: it.lat, lon: it.lon, label: it.label }; fromInput.value = it.label; }
    if (state.to) planTrip(); else toInput.focus();
  } else {
    state.to = { lat: it.lat, lon: it.lon, label: it.label };
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
    [...suggest.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === suggestIndex)));
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
$("whereForm").addEventListener("submit", (e) => e.preventDefault());

$("swapBtn").addEventListener("click", () => {
  const from = state.from, to = state.to;
  if (!to) return;
  state.from = to; fromInput.value = to.label;
  state.to = from || (state.me ? { ...state.me, label: "My location" } : null);
  toInput.value = from ? from.label : (state.me ? "My location" : "");
  if (state.to) planTrip();
});

function locate() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("unsupported"));
    navigator.geolocation.getCurrentPosition(
      (p) => { state.me = { lat: p.coords.latitude, lon: p.coords.longitude, label: "My location" }; resolve(state.me); },
      reject, { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 });
  });
}

$("useLoc").addEventListener("click", async () => {
  state.from = null; fromInput.value = "My location";
  try {
    await locate();
    if (map) map.flyTo({ center: pt(state.me), zoom: 15, duration: reduceMotion ? 0 : 900 });
    if (state.to) planTrip(); else toInput.focus();
  } catch (_) {
    showNotice("Location is turned off for this site. Type a starting point instead.");
    fromInput.value = ""; fromInput.focus();
  }
});

// ------------------------------------------------------------------ planning
const results = $("results");

function showNotice(text, extraHtml = "") {
  results.innerHTML = `<div class="notice">${esc(text)}${extraHtml}</div>`;
}

async function planTrip() {
  if (!state.to) return;
  let from = state.from;
  if (!from) {
    results.innerHTML = `<div class="status-line">Finding where you are…</div>`;
    try { from = await locate(); }
    catch (_) {
      showNotice("Location is turned off for this site. Type a starting point instead.");
      fromInput.value = ""; fromInput.focus(); return;
    }
  }
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
    showNotice("Couldn't reach the trip planner. Check your connection and try again.");
    return;
  }
  state.plan = data; state.selected = data.options?.length ? 0 : null; state.view = "list";
  render();
  drawPlan();
  startLive();
}

function render() {
  const plan = state.plan;
  if (!plan) return;
  if (!plan.options?.length) {
    const later = plan.later_options || [];
    const nextTxt = later.length ? ` The next trip leaves at ${clock(later[0].leave_at)}.` : "";
    const btn = later.length ? `<div><button class="btn quiet" id="showLater" type="button">Show later trips</button></div>` : "";
    showNotice((plan.notice || "No trips found.") + nextTxt, btn);
    if (later.length) $("showLater").addEventListener("click", () => {
      plan.options = later; plan.later_options = []; state.selected = 0; render(); drawPlan(); startLive();
    });
    return;
  }
  if (state.view === "detail" && state.selected != null) return renderDetail(plan.options[state.selected]);
  const n = plan.options.length;
  const liveNote = plan.live_available === false ? " Live times are paused; showing the timetable." : "";
  results.innerHTML = `<div class="status-line">${plural(n, "way", "ways")} to go, leaving within ${plan.max_wait_min} minutes.${liveNote}</div>`
    + plan.options.map((opt, i) => optionCard(opt, i)).join("");
  results.querySelectorAll(".option").forEach((el) => el.addEventListener("click", () => {
    const i = +el.dataset.i;
    if (state.selected === i) { state.view = "detail"; render(); }
    else { state.selected = i; render(); drawPlan(); restartLive(); }
  }));
  tick();
}

function countdown(opt) {
  const m = Math.round((opt.leave_at - nowS()) / 60);
  if (m <= 0) return { cls: "now", n: "Now", u: "Leave now" };
  return { cls: "", n: `${m}`, u: `min, leave by ${clock(opt.leave_at)}` };
}

function optionCard(opt, i) {
  const c = countdown(opt);
  const rs = rides(opt);
  const pills = rs.map(pill).join(ICONS.chev);
  const walk = mins(opt.walk_s);
  const changes = opt.transfers ? plural(opt.transfers, "change", "changes") : "No changes";
  const late = rs.find((l) => l.live && l.delay_s >= 120);
  const tag = opt.live ? `<span class="live-tag">Live</span>` : `<span class="sched-tag">Scheduled</span>`;
  return `<button class="option" type="button" data-i="${i}" aria-pressed="${i === state.selected}"
      aria-label="Option ${i + 1}: ${c.n === "Now" ? "leave now" : `leave in ${esc(c.n)} minutes, by ${clock(opt.leave_at)}`}. ${esc(rs.map(routeLabel).join(" then "))}. Arrive ${clock(opt.arrive_at)}.">
    <div class="count ${c.cls}" data-leave="${opt.leave_at}"><span class="n">${esc(c.n)}</span><span class="u">${esc(c.u)}</span></div>
    <div class="pills"><span class="walk-mins">${ICONS.walk}${walk}</span>${ICONS.chev}${pills}</div>
    <div class="meta">Arrive <strong>${clock(opt.arrive_at)}</strong>, ${mins(opt.duration_s)} min. ${changes}. ${tag}
      ${late ? `<span class="late">${mins(late.delay_s)} min late</span>` : ""}
      ${opt.fare_estimate != null ? `<br>About $${opt.fare_estimate.toFixed(2)}` : ""}</div>
  </button>`;
}

function renderDetail(opt) {
  const plan = state.plan;
  const steps = [];
  const legs = opt.legs;
  legs.forEach((leg, idx) => {
    if (leg.mode === "walk") {
      const next = legs[idx + 1];
      if (idx === 0) {
        steps.push({ cls: "walk", title: `Walk ${mins(leg.duration_s)} min to ${leg.to.name}`,
          sub: next ? `Your ${isRail(next) ? "train" : "bus"} leaves at ${clock(next.board.expected)}` : "" });
      } else if (leg.transfer) {
        steps.push({ cls: "walk", title: leg.duration_s > 30 ? `Walk ${mins(leg.duration_s)} min to ${leg.to.name}` : `Change at ${leg.to.name}`,
          sub: `About ${mins(leg.wait_s)} min to make the connection` });
      } else {
        steps.push({ cls: "walk", title: `Walk ${mins(leg.duration_s)} min to ${plan.destination.label || "your destination"}`, sub: "" });
      }
    } else {
      const delay = leg.live && leg.delay_s ? (leg.delay_s >= 60 ? `, ${mins(leg.delay_s)} min late` : leg.delay_s <= -60 ? `, ${mins(-leg.delay_s)} min early` : ", on time") : "";
      steps.push({ cls: "ride", color: leg.color,
        title: `${routeLabel(leg)}${leg.headsign ? ` toward ${leg.headsign}` : ""}`,
        sub: `${esc(leg.agency_name)}. Leaves <span class="time">${clock(leg.board.expected)}</span> from ${esc(leg.board.name)}${delay} ${leg.live ? '<span class="live-tag">Live</span>' : '<span class="sched-tag">Scheduled</span>'}<br>Ride ${plural(leg.stops, "stop", "stops")}, get off at ${esc(leg.alight.name)} around <span class="time">${clock(leg.alight.expected)}</span>`,
        html: true });
    }
  });
  steps.push({ cls: "end", title: `Arrive around ${clock(opt.arrive_at)}`, sub: "" });

  results.innerHTML = `<div class="detail">
    <button class="back" id="backBtn" type="button">${ICONS.back}All options</button>
    <div class="detail-head"><span class="big">${clock(opt.leave_at)}</span><span>leave, arrive ${clock(opt.arrive_at)} (${mins(opt.duration_s)} min)</span></div>
    <ol class="steps">${steps.map((s, i) => `
      <li class="step ${s.cls}" style="${s.color ? `--c:${esc(s.color)}` : ""}">
        <div class="rail-col"><span class="node"></span>${i < steps.length - 1 ? '<span class="stem"></span>' : ""}</div>
        <div class="body"><div class="title">${esc(s.title)}</div>${s.sub ? `<div class="sub">${s.html ? s.sub : esc(s.sub)}</div>` : ""}</div>
      </li>`).join("")}</ol>
    <div class="detail-actions">
      <button class="btn quiet" id="askTrip" type="button">Ask about this trip</button>
    </div>
  </div>`;
  $("backBtn").addEventListener("click", () => { state.view = "list"; render(); });
  $("askTrip").addEventListener("click", () => {
    const desc = rides(opt).map((l) => `${l.agency_name} ${routeLabel(l)} from ${l.board.name} to ${l.alight.name}`).join(", then ");
    window.dmvChat?.ask(`I'm about to take ${desc}. How do I pay, and is there anything I should know?`);
  });
  $("backBtn").focus();
}

// ------------------------------------------------------------------ live updates
function tick() {
  if (!state.plan?.options?.length) return;
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
  return { lat, lon, label: params.get(`${key}label`) || `${lat.toFixed(4)}, ${lon.toFixed(4)}` };
}

function syncUrl() {
  const p = new URLSearchParams();
  if (state.to) { p.set("to", `${state.to.lat.toFixed(5)},${state.to.lon.toFixed(5)}`); p.set("tolabel", state.to.label); }
  if (state.from) { p.set("from", `${state.from.lat.toFixed(5)},${state.from.lon.toFixed(5)}`); p.set("fromlabel", state.from.label); }
  history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
}

const startParams = new URLSearchParams(location.search);
state.from = placeParam(startParams, "from");
state.to = placeParam(startParams, "to");
if (state.from) fromInput.value = state.from.label;
if (state.to) toInput.value = state.to.label;

initMap();
if (state.to) planTrip(); else toInput.focus();
