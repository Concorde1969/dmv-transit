// "My places": sign in with an emailed code, then save a hotel, home and places to visit
// (/api/account, chat/account_api.py). app.js shows them as quick picks and map pins.
import { KIND, esc, guessKind, isoDay, placeStatus, savedPoint, sortPlaces } from "/static/places.js";
import { track } from "/static/track.js";

const $ = (id) => document.getElementById(id);
const drawer = $("accountDrawer"), body = $("accountBody"), btn = $("accountBtn"), btnText = $("accountBtnText");

const acct = { enabled: false, signed_in: false, email: "", places: [], max_places: 50 };
let view = "email";          // email | code | list | form
let pendingEmail = "";
let editing = null;          // the place being edited, or a draft {name, lat, lon, ...}
let afterSignIn = null;      // a place to save once signed in
let confirmDelete = null;    // place id waiting for "Delete" to be pressed again

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts, credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || "Something went wrong. Please try again.");
  return data;
}

function publish() {
  window.dispatchEvent(new CustomEvent("dmv:places", { detail: { places: acct.signed_in ? acct.places : [], enabled: acct.enabled, signedIn: acct.signed_in } }));
  btnText.textContent = acct.signed_in ? "My places" : "Sign in";
  btn.hidden = !acct.enabled && !acct.signed_in;
}

function setAccount(data) {
  Object.assign(acct, { enabled: data.enabled ?? acct.enabled, signed_in: !!data.signed_in,
    email: data.email || "", places: data.places || [], max_places: data.max_places || acct.max_places });
  publish();
}

// ------------------------------------------------------------------ views
function status(msg, ok = false) {
  const el = $("acctStatus");
  if (el) { el.textContent = msg; el.className = `acct-status ${ok ? "ok" : "err"}`; }
}

function render() {
  if (view === "email") return renderEmail();
  if (view === "code") return renderCode();
  if (view === "form") return renderForm();
  return renderList();
}

function renderEmail() {
  body.innerHTML = `
    <p class="acct-intro">Save your hotel and the places you want to see, then plan a trip to any of them with one tap.</p>
    <form class="acct-form" id="emailForm" novalidate>
      <label for="acctEmail">Your email</label>
      <input id="acctEmail" type="email" autocomplete="email" inputmode="email" maxlength="254" required value="${esc(pendingEmail)}" />
      <p class="acct-status" id="acctStatus" role="status" aria-live="polite"></p>
      <button class="btn primary" type="submit" id="emailSend">Email me a sign-in code</button>
      <p class="acct-fine">No password needed: we email you a 6-digit code each time you sign in. Your email is used only for signing in.</p>
    </form>`;
  $("emailForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("acctEmail").value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return status("Please type your email address, like name@example.com.");
    $("emailSend").disabled = true; status("Sending…", true);
    try {
      const data = await api("/api/account/code", { method: "POST", body: JSON.stringify({ email }) });
      pendingEmail = email; view = "code"; render(); status(data.message, true);
    } catch (err) { status(err.message); $("emailSend").disabled = false; }
  });
  setTimeout(() => $("acctEmail")?.focus(), 50);
}

function renderCode() {
  body.innerHTML = `
    <form class="acct-form" id="codeForm" novalidate>
      <label for="acctCode">Sign-in code</label>
      <input id="acctCode" class="code-input" inputmode="numeric" autocomplete="one-time-code" maxlength="7" pattern="[0-9 ]*" required />
      <p class="acct-status" id="acctStatus" role="status" aria-live="polite"></p>
      <button class="btn primary" type="submit" id="codeSend">Sign in</button>
      <div class="acct-links">
        <button class="link-btn" type="button" id="resend">Send a new code</button>
        <button class="link-btn" type="button" id="otherEmail">Use a different email</button>
      </div>
    </form>`;
  $("codeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("acctCode").value.replace(/\D/g, "");
    if (code.length !== 6) return status("The code has 6 digits. Please check the email.");
    $("codeSend").disabled = true; status("Checking…", true);
    try {
      setAccount(await api("/api/account/verify", { method: "POST", body: JSON.stringify({ email: pendingEmail, code }) }));
      if (afterSignIn) { editing = afterSignIn; afterSignIn = null; view = "form"; }
      else view = "list";
      render();
    } catch (err) { status(err.message); $("codeSend").disabled = false; }
  });
  $("resend").addEventListener("click", async () => {
    try { status((await api("/api/account/code", { method: "POST", body: JSON.stringify({ email: pendingEmail }) })).message, true); }
    catch (err) { status(err.message); }
  });
  $("otherEmail").addEventListener("click", () => { view = "email"; render(); });
  setTimeout(() => $("acctCode")?.focus(), 50);
}

function dates(p) {
  if (p.kind === "hotel" && p.start_date) return `${p.start_date}${p.end_date ? ` to ${p.end_date}` : ""}`;
  return p.start_date || "";
}

function placeRow(p, s) {
  const k = KIND[p.kind] || KIND.other;
  const deleting = confirmDelete === p.id;
  return `<li class="place-row kind-${esc(p.kind)}${s.past ? " past" : ""}" data-id="${p.id}">
    <span class="place-icon">${k.icon}</span>
    <div class="place-text">
      <span class="place-name">${esc(p.name)}</span>
      <small>${esc(k.name)}${s.badge ? `, ${esc(s.badge)}` : ""}${p.address && p.address !== p.name ? `<br>${esc(p.address)}` : ""}</small>
    </div>
    <div class="place-actions">
      <button class="btn small primary" type="button" data-act="go" aria-label="Plan a trip to ${esc(p.name)}">Go</button>
      <button class="btn small quiet" type="button" data-act="edit" aria-label="Change ${esc(p.name)}">Edit</button>
      <button class="btn small quiet${deleting ? " danger" : ""}" type="button" data-act="delete" aria-label="${deleting ? "Press again to delete" : "Delete"} ${esc(p.name)}">${deleting ? "Delete?" : "Remove"}</button>
    </div>
  </li>`;
}

function renderList() {
  const sorted = sortPlaces(acct.places);
  const current = sorted.filter(({ s }) => !s.past), past = sorted.filter(({ s }) => s.past);
  body.innerHTML = `
    <p class="acct-who">Signed in as <strong>${esc(acct.email)}</strong></p>
    <button class="btn primary add-place" type="button" id="addPlace">Add a place</button>
    ${acct.places.length ? "" : `<p class="acct-intro">Add your hotel (with your check-in and check-out dates) and the attractions you want to visit. They'll appear as buttons under "Where to?" and as pins on the map.</p>`}
    ${current.length ? `<ul class="place-list">${current.map(({ p, s }) => placeRow(p, s)).join("")}</ul>` : ""}
    ${past.length ? `<h3 class="acct-sub">Past stays and visits</h3><ul class="place-list">${past.map(({ p, s }) => placeRow(p, s)).join("")}</ul>` : ""}
    <p class="acct-status" id="acctStatus" role="status" aria-live="polite"></p>
    <div class="acct-foot">
      <button class="btn quiet" type="button" id="signOut">Sign out</button>
      <button class="link-btn danger" type="button" id="deleteAccount">${confirmDelete === "account" ? "Press again to delete your account and all saved places" : "Delete my account"}</button>
    </div>`;
  $("addPlace").addEventListener("click", () => { editing = { kind: "attraction" }; view = "form"; render(); });
  body.querySelectorAll(".place-row").forEach((row) => row.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    const p = acct.places.find((x) => x.id === +row.dataset.id);
    if (!act || !p) return;
    if (act === "go") { close(true); window.dmvTrip?.goTo(savedPoint(p)); track("saved_place_used", { detail: { field: "to", kind: p.kind, from: "list" } }); }
    if (act === "edit") { editing = { ...p }; view = "form"; confirmDelete = null; render(); }
    if (act === "delete") removePlace(p);
  }));
  $("signOut").addEventListener("click", async () => {
    try { setAccount(await api("/api/account/signout", { method: "POST" })); } catch (_) { setAccount({ signed_in: false }); }
    view = "email"; render();
  });
  $("deleteAccount").addEventListener("click", async () => {
    if (confirmDelete !== "account") { confirmDelete = "account"; renderList(); $("deleteAccount").focus(); return; }
    try {
      const data = await api("/api/account", { method: "DELETE" });
      confirmDelete = null; setAccount(data); view = "email"; render(); status(data.message, true);
    } catch (err) { status(err.message); }
  });
}

async function removePlace(p) {
  if (confirmDelete !== p.id) {
    confirmDelete = p.id; renderList();
    body.querySelector(`.place-row[data-id="${p.id}"] [data-act="delete"]`)?.focus();
    return;
  }
  try {
    const data = await api(`/api/account/places/${p.id}`, { method: "DELETE" });
    confirmDelete = null; acct.places = data.places; publish(); renderList(); status(`Removed ${p.name}.`, true);
  } catch (err) { status(err.message); }
}

function kindChoices(current) {
  return Object.entries(KIND).map(([k, v]) => `
    <label class="kind-choice kind-${k}"><input type="radio" name="kind" value="${k}" ${k === current ? "checked" : ""} />
      <span>${v.icon}${esc(v.name)}</span></label>`).join("");
}

function renderForm() {
  const p = editing || { kind: "attraction" };
  const isNew = !p.id;
  body.innerHTML = `
    <form class="acct-form place-form" id="placeForm" novalidate>
      <h3 class="acct-sub">${isNew ? "Add a place" : "Change this place"}</h3>
      <fieldset class="kinds"><legend>What kind of place?</legend>${kindChoices(p.kind)}</fieldset>
      <label for="plSearch">Where is it?</label>
      <div class="pl-search">
        <input id="plSearch" autocomplete="off" placeholder="Search a hotel, attraction or address" value="${esc(p.address || p.name || "")}"
               role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="plSuggest" />
        <ul class="suggest" id="plSuggest" role="listbox" aria-label="Places" hidden></ul>
      </div>
      <p class="pl-picked" id="plPicked">${p.lat != null ? "Location set." : ""}</p>
      <label for="plName">Name to show</label>
      <input id="plName" maxlength="80" required value="${esc(p.name || "")}" placeholder="e.g. My hotel, National Air and Space Museum" />
      <div class="pl-dates" id="plDates"></div>
      <p class="acct-status" id="acctStatus" role="status" aria-live="polite"></p>
      <div class="form-actions">
        <button class="btn primary" type="submit" id="plSave">${isNew ? "Save place" : "Save changes"}</button>
        <button class="btn quiet" type="button" id="plCancel">Cancel</button>
      </div>
    </form>`;

  const datesBox = $("plDates");
  const drawDates = () => {
    const kind = body.querySelector('input[name="kind"]:checked')?.value;
    const today = isoDay();
    if (kind === "hotel") {
      datesBox.innerHTML = `<div class="two">
        <div><label for="plStart">Check-in <span class="opt">(optional)</span></label><input id="plStart" type="date" value="${esc(p.start_date || "")}" /></div>
        <div><label for="plEnd">Check-out <span class="opt">(optional)</span></label><input id="plEnd" type="date" value="${esc(p.end_date || "")}" /></div></div>
        <p class="acct-fine">With dates, your hotel shows as a quick pick only during your stay.</p>`;
    } else if (kind === "attraction") {
      datesBox.innerHTML = `<label for="plStart">Day you plan to visit <span class="opt">(optional)</span></label>
        <input id="plStart" type="date" min="${p.start_date && p.start_date < today ? esc(p.start_date) : today}" value="${esc(p.start_date || "")}" />`;
    } else datesBox.innerHTML = "";
  };
  drawDates();
  body.querySelectorAll('input[name="kind"]').forEach((r) => r.addEventListener("change", () => {
    if ($("plStart")) p.start_date = $("plStart").value || null;
    if ($("plEnd")) p.end_date = $("plEnd").value || null;
    p.kind = r.value; drawDates();
  }));
  wireSearch(p);

  $("plCancel").addEventListener("click", () => { editing = null; view = acct.signed_in ? "list" : "email"; render(); });
  $("placeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const kind = body.querySelector('input[name="kind"]:checked')?.value;
    const name = $("plName").value.trim();
    if (p.lat == null) return status("Please search for the place and pick it from the list.");
    if (!name) return status("Please give the place a name.");
    const start = $("plStart")?.value || null, end = $("plEnd")?.value || null;
    if (start && end && end < start) return status("The check-out date is before the check-in date.");
    const payload = { name, kind, lat: p.lat, lon: p.lon, address: p.address || null, start_date: start, end_date: kind === "hotel" ? end : null };
    $("plSave").disabled = true; status("Saving…", true);
    try {
      const data = await api(p.id ? `/api/account/places/${p.id}` : "/api/account/places",
        { method: p.id ? "PUT" : "POST", body: JSON.stringify(payload) });
      acct.places = data.places; publish();
      editing = null; view = "list"; render(); status(`Saved ${data.place.name}.`, true);
    } catch (err) { status(err.message); $("plSave").disabled = false; }
  });
  setTimeout(() => (p.lat == null ? $("plSearch") : $("plName"))?.focus(), 50);
}

// Location search inside the form (same address search as the trip planner).
function wireSearch(p) {
  const input = $("plSearch"), list = $("plSuggest");
  let timer, items = [], session = null;
  const hide = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); };
  const show = (rows) => {
    items = rows;
    list.innerHTML = rows.map((it, i) => `<li role="option" id="pl${i}" aria-selected="false">${esc(it.label)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</li>`).join("");
    list.hidden = !rows.length; input.setAttribute("aria-expanded", String(!!rows.length));
    [...list.children].forEach((li, i) => li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(i); }));
  };
  async function choose(i) {
    const it = items[i];
    if (!it || it.disabled) return;
    hide();
    if (it.place_id && it.lat == null) {
      try {
        const r = await fetch(`/api/place?id=${encodeURIComponent(it.place_id)}&session=${session || ""}`);
        const data = await r.json();
        if (!r.ok) throw new Error(data.message);
        Object.assign(it, { lat: data.lat, lon: data.lon, kind: it.kind || data.kind });
      } catch (err) { return status(err.message || "Couldn't find that place. Please try another."); }
    }
    session = null;
    Object.assign(p, { lat: it.lat, lon: it.lon, address: it.full_label || [it.label, it.sub].filter(Boolean).join(", ") });
    input.value = it.label;
    const nameInput = $("plName");
    if (!nameInput.value.trim()) nameInput.value = it.label;
    if (!p.id && it.kind) {
      const k = guessKind(it.kind);
      const radio = body.querySelector(`input[name="kind"][value="${k}"]`);
      if (radio && k !== "other") { radio.checked = true; radio.dispatchEvent(new Event("change")); }
    }
    $("plPicked").textContent = "Location set.";
    nameInput.focus();
  }
  input.addEventListener("input", () => {
    clearTimeout(timer);
    p.lat = p.lon = null; $("plPicked").textContent = "";
    const q = input.value.trim();
    if (q.length < 3) return hide();
    timer = setTimeout(async () => {
      try {
        session ||= crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
        const data = await (await fetch(`/api/geocode?q=${encodeURIComponent(q)}&session=${session}`)).json();
        const rows = data.results || [];
        if (!rows.length && data.message) rows.push({ label: data.message, disabled: true });
        if (document.activeElement === input) show(rows);
      } catch (_) { /* keep typing */ }
    }, 380);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    const lis = [...list.children];
    const cur = lis.findIndex((li) => li.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = (cur + (e.key === "ArrowDown" ? 1 : -1) + lis.length) % lis.length;
      lis.forEach((li, i) => li.setAttribute("aria-selected", String(i === next)));
      input.setAttribute("aria-activedescendant", `pl${next}`);
    } else if (e.key === "Enter") { e.preventDefault(); choose(cur >= 0 ? cur : 0); }
    else if (e.key === "Escape") hide();
  });
  input.addEventListener("blur", () => setTimeout(hide, 120));
}

// ------------------------------------------------------------------ drawer
function open(nextView) {
  if (!drawer.classList.contains("open")) track("account_opened", { detail: { signed_in: acct.signed_in } });
  window.dmvChat?.close?.(true);
  window.dmvReports?.close?.(true);
  view = nextView || (acct.signed_in ? (view === "form" ? "form" : "list") : (view === "code" ? "code" : "email"));
  confirmDelete = null;
  render();
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  btn.setAttribute("aria-expanded", "true");
}

function close(quiet = false) {
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  btn.setAttribute("aria-expanded", "false");
  if (!quiet) btn.focus();
}

// "Save this place" from a trip: straight to the form, after signing in if needed.
function savePlace(draft) {
  const p = { ...draft, kind: guessKind(draft.kind) };
  if (acct.signed_in) { editing = p; open("form"); }
  else { afterSignIn = p; open(view === "code" ? "code" : "email"); }
}

btn.addEventListener("click", () => (drawer.classList.contains("open") ? close() : open()));
$("closeAccount").addEventListener("click", () => close());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) close(); });

window.dmvAccount = { open, close, savePlace, status: placeStatus };

api("/api/account").then(setAccount).catch(() => publish());
