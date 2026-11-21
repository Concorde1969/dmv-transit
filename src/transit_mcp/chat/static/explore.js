// Explore: every stop and route in the region on the map (/api/explore,
// chat/explore_api.py), with a legend to choose agencies and layers. Tap a stop for the
// routes that serve it and to plan a trip there; tap a route to ask the assistant about it.
import * as ML from "/static/map/vendor/maplibre-gl.mjs";
import { track } from "/static/track.js";

const maplibregl = ML.default || ML;
const $ = (id) => document.getElementById(id);
const btn = $("exploreBtn"), card = $("exploreCard"), list = $("exploreAgencies");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const isPhone = () => matchMedia("(max-width: 720px)").matches;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = { on: false, data: null, loading: null, hidden: new Set(), show: { routes: true, stops: true, hubs: false, parking: true, events: true } };
const PARKING_COLOR = "#1565c0", EVENT_COLOR = "#C25B32";
let popup = null;

// Google's encoded polyline -> [[lon, lat], ...]
function decode(text) {
  const out = []; let i = 0, lat = 0, lon = 0;
  while (i < text.length) {
    for (const which of [0, 1]) {
      let shift = 0, result = 0, b;
      do { b = text.charCodeAt(i++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      const d = result & 1 ? ~(result >> 1) : result >> 1;
      if (which === 0) lat += d; else lon += d;
    }
    out.push([lon / 1e5, lat / 1e5]);
  }
  return out;
}

function textOn(hex) {
  const n = parseInt((hex || "#000000").slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  return (0.299 * r + 0.587 * g + 0.114 * b) > 160 ? "#1B2621" : "#ffffff";
}

// Metro-style letters ("R", "OR") read better as the line's name: "Red Line".
function label(short, long, kind) {
  if (kind === "rail" && long && (!short || short.length <= 2)) return /line$/i.test(long) ? long : `${long} Line`;
  return short || long || "Route";
}

async function load() {
  if (state.data) return state.data;
  state.loading ||= fetch("/api/explore").then((r) => {
    if (!r.ok) throw new Error("explore");
    return r.json();
  });
  const [raw, events] = await Promise.all([state.loading, holidayEvents()]);
  const agencies = raw.agencies;
  const routes = { type: "FeatureCollection", features: [] };
  raw.routes.forEach(([a, id, short, long, color, kind, lines]) => {
    const ag = agencies[a];
    routes.features.push({ type: "Feature", properties: { a: ag.id, id, short, long, kind,
      c: color || ag.color, name: label(short, long, kind) },
      geometry: { type: "MultiLineString", coordinates: lines.map(decode) } });
  });
  const stops = { type: "FeatureCollection", features: raw.stops.map(([a, id, name, lat, lon]) => ({
    type: "Feature", properties: { a: agencies[a].id, id, name, c: agencies[a].color, rail: agencies[a].rail },
    geometry: { type: "Point", coordinates: [lon, lat] } })) };
  const hubs = { type: "FeatureCollection", features: raw.hubs.map(([lat, lon, n, feeds]) => ({
    type: "Feature", properties: { n, feeds }, geometry: { type: "Point", coordinates: [lon, lat] } })) };
  const parking = { type: "FeatureCollection", features: (raw.parking || []).map((lot, i) => ({
    type: "Feature", properties: { i, name: lot.name, airport: lot.kind === "airport" },
    geometry: { type: "Point", coordinates: [lot.lon, lot.lat] } })) };
  const counts = {};
  raw.routes.forEach(([a]) => { counts[agencies[a].id] = (counts[agencies[a].id] || 0) + 1; });
  const happenings = { type: "FeatureCollection", features: events.map((ev, i) => ({
    type: "Feature", properties: { i, name: ev.name, now: Boolean(ev.open_now) },
    geometry: { type: "Point", coordinates: [ev.lon, ev.lat] } })) };
  state.data = { agencies, routes, stops, hubs, parking, lots: raw.parking || [], counts, happenings, events };
  $("exploreEventsToggle").hidden = !events.length;
  return state.data;
}

// Holiday markets and events (/api/events): shared with the welcome's seasonal suggestion.
let eventsLoading = null;
function holidayEvents() {
  eventsLoading ||= fetch("/api/events").then((r) => (r.ok ? r.json() : { events: [] }))
    .then((d) => d.events || []).catch(() => []);
  return eventsLoading;
}

// ------------------------------------------------------------------ map layers
function addLayers(map) {
  if (map.getSource("explore-routes")) return;
  const d = state.data;
  map.addSource("explore-routes", { type: "geojson", data: d.routes });
  map.addSource("explore-stops", { type: "geojson", data: d.stops });
  map.addSource("explore-hubs", { type: "geojson", data: d.hubs });
  map.addSource("explore-parking", { type: "geojson", data: d.parking });
  map.addSource("explore-events", { type: "geojson", data: d.happenings });
  // Under the trip layers, so a planned trip stays on top.
  const before = map.getLayer("others-line") ? "others-line" : undefined;
  map.addLayer({ id: "explore-hubs", type: "circle", source: "explore-hubs", minzoom: 11,
    paint: { "circle-radius": ["interpolate", ["exponential", 2], ["zoom"], 11, 3, 18, 320],
             "circle-color": "#4A5651", "circle-opacity": 0.08,
             "circle-stroke-color": "#4A5651", "circle-stroke-width": 1, "circle-stroke-opacity": 0.45 } }, before);
  map.addLayer({ id: "explore-bus", type: "line", source: "explore-routes", filter: ["!=", ["get", "kind"], "rail"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "c"], "line-opacity": 0.6,
             "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.6, 12, 1.6, 16, 3.5] } }, before);
  map.addLayer({ id: "explore-rail", type: "line", source: "explore-routes", filter: ["==", ["get", "kind"], "rail"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "c"], "line-opacity": 0.9,
             "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2, 12, 3.5, 16, 7] } }, before);
  map.addLayer({ id: "explore-route-hi", type: "line", source: "explore-routes", filter: ["==", ["get", "id"], ""],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "c"], "line-width": ["interpolate", ["linear"], ["zoom"], 8, 4, 16, 10] } }, before);
  map.addLayer({ id: "explore-stops", type: "circle", source: "explore-stops", minzoom: 12,
    filter: ["!", ["get", "rail"]],
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 2, 16, 5],
             "circle-color": ["get", "c"], "circle-stroke-color": "#ffffff", "circle-stroke-width": 1 } }, before);
  map.addLayer({ id: "explore-rail-stops", type: "circle", source: "explore-stops", filter: ["get", "rail"],
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 12, 4.5, 16, 8],
             "circle-color": "#ffffff", "circle-stroke-color": ["get", "c"],
             "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 8, 1.5, 14, 3] } }, before);
  // Parking: a blue "P", bigger at the airports.
  map.addLayer({ id: "explore-parking", type: "circle", source: "explore-parking",
    paint: { "circle-color": PARKING_COLOR, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, ["case", ["get", "airport"], 7, 5],
                               14, ["case", ["get", "airport"], 13, 10]] } }, before);
  map.addLayer({ id: "explore-parking-p", type: "symbol", source: "explore-parking", minzoom: 9,
    layout: { "text-field": "P", "text-font": ["Noto Sans Medium"], "text-allow-overlap": true,
              "text-size": ["interpolate", ["linear"], ["zoom"], 9, 9, 14, 14] },
    paint: { "text-color": "#ffffff" } }, before);
  // Holiday markets: a terracotta dot with a white ring (a dark ring while it's open now),
  // named from zoom 11.
  map.addLayer({ id: "explore-events", type: "circle", source: "explore-events",
    paint: { "circle-color": EVENT_COLOR, "circle-stroke-color": ["case", ["get", "now"], "#7A2E12", "#ffffff"],
             "circle-stroke-width": ["case", ["get", "now"], 3, 2],
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 6, 14, 11] } }, before);
  map.addLayer({ id: "explore-events-label", type: "symbol", source: "explore-events", minzoom: 11,
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": 12.5,
              "text-offset": [0, 1.3], "text-anchor": "top", "text-max-width": 12, "text-optional": true },
    paint: { "text-color": "#7A2E12", "text-halo-color": "#ffffff", "text-halo-width": 1.8 } }, before);
  map.addLayer({ id: "explore-labels", type: "symbol", source: "explore-stops",
    filter: ["any", ["all", ["get", "rail"], [">=", ["zoom"], 11]], [">=", ["zoom"], 16]],
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": 12,
              "text-offset": [0, 1.1], "text-anchor": "top", "text-max-width": 10, "text-optional": true },
    paint: { "text-color": "#1B2621", "text-halo-color": "#ffffff", "text-halo-width": 1.6 } }, before);
  wireMap(map);
}

function applyFilters(map) {
  if (!map.getSource("explore-routes")) return;
  const shown = state.data.agencies.map((a) => a.id).filter((id) => !state.hidden.has(id));
  const byAgency = ["in", ["get", "a"], ["literal", shown]];
  map.setFilter("explore-bus", ["all", ["!=", ["get", "kind"], "rail"], byAgency]);
  map.setFilter("explore-rail", ["all", ["==", ["get", "kind"], "rail"], byAgency]);
  map.setFilter("explore-stops", ["all", ["!", ["get", "rail"]], byAgency]);
  map.setFilter("explore-rail-stops", ["all", ["get", "rail"], byAgency]);
  map.setFilter("explore-labels", ["all", byAgency,
    ["any", ["all", ["get", "rail"], [">=", ["zoom"], 11]], [">=", ["zoom"], 16]]]);
  const vis = (on) => (state.on && on ? "visible" : "none");
  for (const id of ["explore-bus", "explore-rail", "explore-route-hi"]) map.setLayoutProperty(id, "visibility", vis(state.show.routes));
  for (const id of ["explore-stops", "explore-rail-stops", "explore-labels"]) map.setLayoutProperty(id, "visibility", vis(state.show.stops));
  map.setLayoutProperty("explore-hubs", "visibility", vis(state.show.hubs));
  for (const id of ["explore-parking", "explore-parking-p"]) map.setLayoutProperty(id, "visibility", vis(state.show.parking));
  for (const id of ["explore-events", "explore-events-label"]) map.setLayoutProperty(id, "visibility", vis(state.show.events));
}

// ------------------------------------------------------------------ taps and popups
function routePill(r) {
  const bg = r.color || "#4A5651";
  return `<span class="xp-pill${r.kind === "rail" ? " rail" : ""}" style="background:${esc(bg)};color:${esc(r.text || textOn(bg))}">${esc(label(r.short, r.long, r.kind))}</span>`;
}

function openPopup(map, lngLat, html, onClose) {
  popup?.remove();
  const box = document.createElement("div");
  box.className = "xp-popup";
  box.innerHTML = html;
  popup = new maplibregl.Popup({ offset: 10, maxWidth: "300px", closeButton: true })
    .setLngLat(lngLat).setDOMContent(box).addTo(map);
  if (onClose) popup.on("close", onClose);
  return box;
}

async function showStop(map, f) {
  const p = f.properties, [lon, lat] = f.geometry.coordinates;
  const agency = state.data.agencies.find((a) => a.id === p.a);
  track("explore_stop", { detail: { agency: p.a } });
  const box = openPopup(map, [lon, lat], `<strong>${esc(p.name)}</strong>
    <small><span class="xp-swatch" style="background:${esc(p.c)}"></span>${esc(agency?.name || p.a)}</small>
    <div class="xp-routes" aria-live="polite">Loading routes…</div>
    <div class="popup-actions">
      <button class="btn small primary" type="button" data-act="to">Go here</button>
      <button class="btn small quiet" type="button" data-act="from">Start here</button>
    </div>
    <button class="link-btn" type="button" data-act="ask">Ask about this stop</button>`);
  const point = { lat, lon, label: p.name, source: "explore" };
  box.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    popup?.remove();
    track("explore_go", { detail: { act } });
    if (act === "to") window.dmvTrip?.goTo(point);
    else if (act === "from") window.dmvTrip?.startFrom(point);
    else window.dmvChat?.ask(`Tell me about the ${agency?.name || ""} stop "${p.name}": which routes stop there, when they run, and how to pay.`);
  });
  try {
    const r = await fetch(`/api/explore/stop?feed=${encodeURIComponent(p.a)}&id=${encodeURIComponent(p.id)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.message);
    const routes = d.routes.length ? d.routes.map(routePill).join("") : "No scheduled routes found.";
    const access = d.wheelchair === true ? "<p class=\"xp-note\">Step-free access listed by the agency.</p>" : "";
    const hub = d.hub && d.hub.agencies.length ? `<p class="xp-note">Transfer hub: ${d.hub.stops} stops within a short walk, also served by ${esc(d.hub.agencies.join(", "))}.</p>` : "";
    box.querySelector(".xp-routes").innerHTML = `<div class="xp-pills">${routes}</div>${access}${hub}`;
  } catch (_) {
    box.querySelector(".xp-routes").textContent = "Couldn't load the routes for this stop.";
  }
}

function showParking(map, f) {
  const lot = state.data.lots[f.properties.i];
  track("explore_parking", { detail: { kind: lot.kind } });
  const facts = [];
  if (lot.spaces) facts.push(`${Number(lot.spaces).toLocaleString()} spaces`);
  if (lot.short_term_spaces) facts.push(`${Number(lot.short_term_spaces).toLocaleString()} short-term`);
  const options = (lot.options || []).map((o) => `<li><strong>${esc(o.name)}</strong>${o.cost ? `: ${esc(o.cost)}` : ""}${o.spaces ? ` <small>(${Number(o.spaces).toLocaleString()} spaces)</small>` : ""}</li>`).join("");
  const also = (lot.also_serves || []).length ? ` Also used by ${esc(lot.also_serves.join(", "))} riders.` : "";
  const source = (() => { try { return new URL(lot.source).hostname.replace(/^www\./, ""); } catch (_) { return "the operator"; } })();
  const box = openPopup(map, f.geometry.coordinates, `<strong>${esc(lot.name)}</strong>
    <small><span class="xp-swatch" style="background:${PARKING_COLOR}"></span>${esc(lot.operator)}${facts.length ? `, ${esc(facts.join(", "))}` : ""}</small>
    ${lot.cost ? `<p class="xp-cost">${esc(lot.cost)}</p>` : ""}
    ${options ? `<ul class="xp-options">${options}</ul>` : ""}
    ${lot.notes || also ? `<details class="xp-more"><summary>More about parking here</summary><p class="xp-note">${esc(lot.notes || "")}${also}</p></details>` : ""}
    <p class="xp-note">From <a href="${esc(lot.source)}" target="_blank" rel="noopener">${esc(source)}</a>${lot.checked ? `, checked ${esc(lot.checked)}` : ""}. Confirm before you park.</p>
    <div class="popup-actions"><button class="btn small primary" type="button" data-act="to">Go here</button>
      <button class="btn small quiet" type="button" data-act="from">Start here</button></div>`);
  const [lon, lat] = f.geometry.coordinates;
  const point = { lat, lon, label: lot.name, source: "explore" };
  box.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    popup?.remove();
    track("explore_go", { detail: { act, parking: true } });
    if (act === "to") window.dmvTrip?.goTo(point); else window.dmvTrip?.startFrom(point);
  });
}

function showEvent(map, f) {
  const ev = state.data.events[f.properties.i];
  track("explore_event");
  const next = ev.next_open;
  const when = ev.open_now ? `Open now, until ${esc(next.hours.split(" to ")[1])}`
    : `Next open ${esc(next.day)}, ${esc(next.hours)}`;
  const source = (() => { try { return new URL(ev.source).hostname.replace(/^www\./, ""); } catch (_) { return "the organizer"; } })();
  const more = [ev.summary, ev.getting_there, ev.notes].filter(Boolean).map((t) => `<p class="xp-note">${esc(t)}</p>`).join("");
  const box = openPopup(map, f.geometry.coordinates, `<strong>${esc(ev.name)}</strong>
    <small><span class="xp-swatch" style="background:${EVENT_COLOR}"></span>${esc(ev.venue || ev.address || "")}</small>
    <p class="xp-cost">${when}</p>
    <ul class="xp-options"><li>${esc(ev.dates)}</li>${(ev.usual_hours || []).map((h) => `<li>${esc(h)}</li>`).join("")}${ev.cost ? `<li>${esc(ev.cost)}</li>` : ""}</ul>
    ${more ? `<details class="xp-more"><summary>More about this event</summary>${more}</details>` : ""}
    <p class="xp-note">From <a href="${esc(ev.source)}" target="_blank" rel="noopener">${esc(source)}</a>${ev.checked ? `, checked ${esc(ev.checked)}` : ""}. Confirm before you go.</p>
    <div class="popup-actions"><button class="btn small primary" type="button" data-act="to">Go here</button>
      <button class="btn small quiet" type="button" data-act="ask">Ask about it</button></div>`);
  const [lon, lat] = f.geometry.coordinates;
  const point = { lat, lon, label: ev.name, source: "explore" };
  box.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    popup?.remove();
    track("explore_go", { detail: { act, event: true } });
    if (act === "to") window.dmvTrip?.goTo(point);
    else window.dmvChat?.ask(`Tell me about the ${ev.name}: when it's open, what it costs, and the easiest way to get there by transit.`);
  });
}

function showRoute(map, f, lngLat) {
  const p = f.properties;
  const agency = state.data.agencies.find((a) => a.id === p.a);
  track("explore_route", { detail: { agency: p.a, kind: p.kind } });
  map.setFilter("explore-route-hi", ["all", ["==", ["get", "a"], p.a], ["==", ["get", "id"], p.id]]);
  const name = label(p.short, p.long, p.kind);
  const long = name !== p.long && p.long && p.long !== p.short ? p.long : "";
  const box = openPopup(map, lngLat, `${routePill({ short: p.short, long: p.long, color: p.c, kind: p.kind })}
    ${long ? `<strong class="xp-long">${esc(long)}</strong>` : ""}
    <small><span class="xp-swatch" style="background:${esc(agency?.color)}"></span>${esc(agency?.name || p.a)}</small>
    <button class="btn small quiet" type="button" data-act="ask">Ask about this route</button>`,
  () => map.setFilter("explore-route-hi", ["==", ["get", "id"], ""]));
  box.querySelector("[data-act=ask]").addEventListener("click", () => {
    popup?.remove();
    window.dmvChat?.ask(`Tell me about ${agency?.name || ""} route ${name}${long ? ` (${long})` : ""}: where it goes, when it runs, and the fare.`);
  });
}

let wired = false;
function wireMap(map) {
  if (wired) return;
  wired = true;
  const stopLayers = ["explore-rail-stops", "explore-stops"], routeLayers = ["explore-rail", "explore-bus"];
  map.on("click", (e) => {
    if (!state.on) return;
    const pad = isPhone() ? 12 : 6;
    const box = [[e.point.x - pad, e.point.y - pad], [e.point.x + pad, e.point.y + pad]];
    const live = (ids) => ids.filter((id) => map.getLayoutProperty(id, "visibility") !== "none");
    const ev = map.queryRenderedFeatures(box, { layers: live(["explore-events"]) })[0];
    if (ev) return showEvent(map, ev);
    const lot = map.queryRenderedFeatures(box, { layers: live(["explore-parking"]) })[0];
    if (lot) return showParking(map, lot);
    const stop = map.queryRenderedFeatures(box, { layers: live(stopLayers) })[0];
    if (stop) return showStop(map, stop);
    const route = map.queryRenderedFeatures(box, { layers: live(routeLayers) })[0];
    if (route) showRoute(map, route, e.lngLat);
  });
  for (const id of ["explore-events", "explore-parking", ...stopLayers, ...routeLayers]) {
    map.on("mouseenter", id, () => { if (state.on) map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  }
}

// ------------------------------------------------------------------ legend card
function renderAgencies() {
  const d = state.data;
  const rows = d.agencies.filter((a) => d.counts[a.id] || d.stops.features.some((s) => s.properties.a === a.id));
  rows.sort((a, b) => (b.rail - a.rail) || a.name.localeCompare(b.name));
  list.innerHTML = rows.map((a) => `<label class="xp-agency">
      <input type="checkbox" value="${esc(a.id)}"${state.hidden.has(a.id) ? "" : " checked"} />
      <span class="xp-swatch" style="background:${esc(a.color)}"></span>
      <span class="xp-name">${esc(a.name)}</span>
      <small>${d.counts[a.id] ? `${d.counts[a.id]} route${d.counts[a.id] === 1 ? "" : "s"}` : ""}</small>
    </label>`).join("");
}

function setOn(on) {
  const map = window.dmvMap;
  state.on = on;
  btn.setAttribute("aria-pressed", String(on));
  card.hidden = !on;
  document.body.classList.toggle("exploring", on);
  if (!on) popup?.remove();
  if (map?.getSource("explore-routes")) applyFilters(map);
}

let opened = false;
async function open() {
  const map = window.dmvMap;
  if (!map) return;
  if (!opened) $("exploreList").open = !isPhone();   // the agency list starts folded on phones
  opened = true;
  setOn(true);
  track("explore_opened");
  $("exploreStatus").textContent = "Loading every stop and route…";
  try {
    await load();
    // The map is set up once its trip layers exist (app.js adds them on "load").
    if (!map.getLayer("others-line")) await new Promise((res) => map.once("load", res));
    addLayers(map);
    renderAgencies();
    applyFilters(map);
    $("exploreStatus").textContent = `${state.data.routes.features.length.toLocaleString()} routes, ${state.data.stops.features.length.toLocaleString()} stops. Zoom in for bus stops; tap a stop or route for details.`;
    if (map.getZoom() > 11.5 && !window.dmvTrip?.screen()) map.easeTo({ zoom: 11, duration: reduceMotion ? 0 : 700 });
    window.dmvPanel?.lower();
  } catch (_) {
    $("exploreStatus").textContent = "Couldn't load the network. Please try again.";
  }
}

btn.addEventListener("click", () => (state.on ? setOn(false) : open()));
$("exploreClose").addEventListener("click", () => { setOn(false); btn.focus(); });
card.addEventListener("change", (e) => {
  const t = e.target;
  const map = window.dmvMap;
  if (t.matches(".xp-agency input")) t.checked ? state.hidden.delete(t.value) : state.hidden.add(t.value);
  else if (t.dataset.show) state.show[t.dataset.show] = t.checked;
  if (map) applyFilters(map);
});
$("exploreAll").addEventListener("click", () => { state.hidden.clear(); renderAgencies(); applyFilters(window.dmvMap); });
$("exploreNone").addEventListener("click", () => {
  state.data.agencies.forEach((a) => state.hidden.add(a.id)); renderAgencies(); applyFilters(window.dmvMap);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.on && !popup?.isOpen()) setOn(false); });

window.dmvExplore = { open, close: () => setOn(false), events: holidayEvents };

// The installed app's "Explore the map" shortcut opens /?explore=1.
if (new URLSearchParams(location.search).get("explore") === "1") {
  const started = Date.now();
  const wait = setInterval(() => {
    if (window.dmvMap) { clearInterval(wait); open(); }
    else if (Date.now() - started > 15000) clearInterval(wait);
  }, 200);
}
