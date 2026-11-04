// Apex Location Director — desktop frontend.
// Talks to the Rust backend (which talks to the iPhone over usbmuxd) via Tauri invoke.

const invoke = window.__TAURI__.core.invoke;

const state = {
  udid: null,
  pin: null,          // {lat, lng}
  pinTitle: null,
  simulated: null,    // {lat, lng}
  mode: "drive",
  bookmarks: loadBookmarks(),
};

// ---------- backend helpers ----------

async function call(cmd, args = {}) {
  const res = await invoke(cmd, args);
  if (!res || res.ok !== true) {
    throw new Error((res && res.error) || "Unknown error talking to the iPhone.");
  }
  return res;
}

// ---------- map ----------

const map = L.map("map", { zoomControl: true, attributionControl: true }).setView([39.5, -98.35], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

let pinMarker = null;
let simMarker = null;
let routeLine = null;

const pinIcon = L.divIcon({ className: "", html: '<div class="pin-marker">📍</div>', iconSize: [26, 26], iconAnchor: [13, 24] });
const simIcon = L.divIcon({ className: "", html: '<div class="sim-dot"></div>', iconSize: [16, 16], iconAnchor: [8, 8] });

function setPin(lat, lng, title) {
  state.pin = { lat, lng };
  state.pinTitle = title || null;
  if (!pinMarker) {
    pinMarker = L.marker([lat, lng], { icon: pinIcon, draggable: true }).addTo(map);
    pinMarker.on("dragend", () => {
      const p = pinMarker.getLatLng();
      state.pin = { lat: p.lat, lng: p.lng };
      state.pinTitle = null;
      setStatus(`Pin: ${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}`);
    });
  } else {
    pinMarker.setLatLng([lat, lng]);
  }
  setStatus(title ? `Pin: ${title}` : `Pin: ${lat.toFixed(5)}, ${lng.toFixed(5)}`);
}

function setSimulated(lat, lng) {
  state.simulated = lat == null ? null : { lat, lng };
  if (lat == null) { if (simMarker) { map.removeLayer(simMarker); simMarker = null; } return; }
  if (!simMarker) simMarker = L.marker([lat, lng], { icon: simIcon, interactive: false, zIndexOffset: -100 }).addTo(map);
  else simMarker.setLatLng([lat, lng]);
}

function drawRoute(latlngs) {
  if (routeLine) map.removeLayer(routeLine);
  routeLine = latlngs ? L.polyline(latlngs, { color: "#3b82f6", weight: 5, opacity: 0.7 }).addTo(map) : null;
}

map.on("click", (e) => { setPin(e.latlng.lat, e.latlng.lng); hideResults(); });

// ---------- status ----------

const statusEl = document.getElementById("status");
function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = kind;
}

// ---------- coordinate parsing + search ----------

function parseCoordinate(text) {
  const cleaned = text.toUpperCase().replace(/°/g, " ").replace(/,/g, " ");
  const tokens = cleaned.split(/\s+/).filter(Boolean);
  const vals = [];
  for (const tok of tokens) {
    const hemi = tok[tok.length - 1];
    if ("NSEW".includes(hemi)) {
      const num = tok.slice(0, -1);
      if (num) { const v = parseFloat(num); if (isNaN(v)) return null; vals.push(v); }
      if (!vals.length) return null;
      if (hemi === "S" || hemi === "W") vals[vals.length - 1] = -Math.abs(vals[vals.length - 1]);
    } else {
      const v = parseFloat(tok);
      if (isNaN(v)) return null;
      vals.push(v);
    }
  }
  if (vals.length !== 2) return null;
  const [lat, lng] = vals;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
  return { lat, lng };
}

const searchInput = document.getElementById("search-input");
const searchResults = document.getElementById("search-results");
let searchTimer = null;

function hideResults() { searchResults.classList.add("hidden"); searchResults.innerHTML = ""; }

async function geocode(q) {
  const url = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&addressdetails=0&q=${encodeURIComponent(q)}`;
  const r = await fetch(url, { headers: { "Accept-Language": "en" } });
  if (!r.ok) throw new Error("search failed");
  return await r.json();
}

searchInput.addEventListener("input", () => {
  const q = searchInput.value.trim();
  clearTimeout(searchTimer);
  if (q.length < 3 || parseCoordinate(q)) { hideResults(); return; }
  searchTimer = setTimeout(async () => {
    try {
      const rows = await geocode(q);
      if (!rows.length) { hideResults(); return; }
      searchResults.innerHTML = "";
      for (const row of rows) {
        const div = document.createElement("div");
        div.className = "result";
        const name = row.display_name.split(",")[0];
        div.innerHTML = `<div class="title"></div><div class="subtitle"></div>`;
        div.querySelector(".title").textContent = name;
        div.querySelector(".subtitle").textContent = row.display_name;
        div.onclick = () => {
          const lat = parseFloat(row.lat), lng = parseFloat(row.lon);
          setPin(lat, lng, name);
          map.flyTo([lat, lng], 15);
          searchInput.value = row.display_name;
          hideResults();
        };
        searchResults.appendChild(div);
      }
      searchResults.classList.remove("hidden");
    } catch { hideResults(); }
  }, 350);
});

searchInput.addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  const q = searchInput.value.trim();
  const coord = parseCoordinate(q);
  if (coord) { setPin(coord.lat, coord.lng, `${coord.lat.toFixed(5)}, ${coord.lng.toFixed(5)}`); map.flyTo([coord.lat, coord.lng], 15); hideResults(); return; }
  try {
    const rows = await geocode(q);
    if (rows.length) {
      const lat = parseFloat(rows[0].lat), lng = parseFloat(rows[0].lon);
      setPin(lat, lng, rows[0].display_name.split(",")[0]);
      map.flyTo([lat, lng], 15);
    } else setStatus(`No results for "${q}".`, "error");
  } catch { setStatus("Search failed. Check your internet.", "error"); }
  hideResults();
});

document.getElementById("search-clear").onclick = () => { searchInput.value = ""; hideResults(); };

// ---------- routing ----------

const SPEEDS = { walk: 1.4, run: 3.0, cycle: 5.5, drive: null };

async function roadRoute(from, to) {
  const url = `https://router.project-osrm.org/route/v1/driving/${from.lng},${from.lat};${to.lng},${to.lat}?overview=full&geometries=geojson`;
  const r = await fetch(url);
  if (!r.ok) throw new Error("route http");
  const data = await r.json();
  const route = data.routes && data.routes[0];
  if (!route) throw new Error("no route");
  const pts = route.geometry.coordinates.map(([lng, lat]) => [lat, lng]); // -> [lat,lng]
  return { points: pts, distance: route.distance, duration: route.duration };
}

function thin(points, minMeters) {
  if (points.length <= 2) return points;
  const out = [points[0]];
  for (let i = 1; i < points.length - 1; i++) {
    if (haversine(out[out.length - 1], points[i]) >= minMeters) out.push(points[i]);
  }
  out.push(points[points.length - 1]);
  return out;
}

function haversine(a, b) {
  const R = 6371000, toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]), dLng = toRad(b[1] - a[1]);
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

// ---------- actions ----------

function requireDevice() {
  if (!state.udid) { setStatus("Connect an iPhone first (gear icon).", "error"); return false; }
  return true;
}

document.getElementById("btn-teleport").onclick = async () => {
  if (!requireDevice()) return;
  if (!state.pin) { setStatus("Drop a pin or search for a place first.", "error"); return; }
  const { lat, lng } = state.pin;
  setStatus("Teleporting…");
  try {
    await call("set_location", { udid: state.udid, lat, lng });
    drawRoute(null);
    setSimulated(lat, lng);
    setStatus(`You're at ${lat.toFixed(5)}, ${lng.toFixed(5)}. It stays after you unplug.`, "ok");
  } catch (e) { setStatus(e.message, "error"); }
};

document.getElementById("btn-go").onclick = async () => {
  if (!requireDevice()) return;
  if (!state.pin) { setStatus("Drop a pin for the destination first.", "error"); return; }
  if (!state.simulated) { setStatus("Teleport somewhere first; the trip starts from there.", "error"); return; }
  const from = state.simulated, to = state.pin;
  setStatus(`Finding a ${state.mode} route…`);
  try {
    let points, distance, speed;
    try {
      const r = await roadRoute(from, to);
      points = thin(r.points, 15);
      distance = r.distance;
      speed = SPEEDS[state.mode] ?? Math.max(5, r.distance / Math.max(r.duration, 1));
    } catch {
      points = [[from.lat, from.lng], [to.lat, to.lng]]; // straight-line fallback
      distance = haversine(points[0], points[1]);
      speed = SPEEDS[state.mode] ?? 13.4;
    }
    if (haversine([from.lat, from.lng], points[0]) > 1) points.unshift([from.lat, from.lng]);
    drawRoute(points);
    await call("play_route", { udid: state.udid, points, speed });
    setSimulated(to.lat, to.lng);
    const mins = Math.round(distance / speed / 60);
    setStatus(`${cap(state.mode)} started: ${(distance / 1609.34).toFixed(1)} mi, ~${mins} min. Runs on the phone on its own.`, "ok");
  } catch (e) { setStatus(e.message, "error"); }
};

document.getElementById("btn-reset").onclick = async () => {
  if (!requireDevice()) return;
  setStatus("Returning to real GPS…");
  try {
    await call("clear", { udid: state.udid });
    setSimulated(null);
    drawRoute(null);
    setStatus("Back on real GPS.", "ok");
  } catch (e) { setStatus(e.message, "error"); }
};

document.querySelectorAll('input[name="mode"]').forEach((el) =>
  el.addEventListener("change", () => { if (el.checked) state.mode = el.value; })
);

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// ---------- bookmarks ----------

function loadBookmarks() {
  try {
    const raw = JSON.parse(localStorage.getItem("bookmarks") || "null");
    if (raw) return raw;
  } catch {}
  return [
    { name: "Times Square, NYC", lat: 40.758, lng: -73.9855 },
    { name: "London Eye", lat: 51.5033, lng: -0.1195 },
    { name: "Shibuya Crossing, Tokyo", lat: 35.6595, lng: 139.7004 },
  ];
}
function saveBookmarks() { localStorage.setItem("bookmarks", JSON.stringify(state.bookmarks)); }

const bookmarkMenu = document.getElementById("bookmark-menu");
function refreshBookmarks() {
  bookmarkMenu.innerHTML = '<option value="">Saved places…</option>';
  state.bookmarks.forEach((b, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = b.name;
    bookmarkMenu.appendChild(opt);
  });
}
bookmarkMenu.onchange = () => {
  const b = state.bookmarks[parseInt(bookmarkMenu.value, 10)];
  if (b) { setPin(b.lat, b.lng, b.name); map.flyTo([b.lat, b.lng], 14); }
  bookmarkMenu.value = "";
};
document.getElementById("bookmark-save").onclick = () => {
  if (!state.pin) { setStatus("Drop a pin to bookmark first.", "error"); return; }
  const name = prompt("Name this place:", state.pinTitle || `${state.pin.lat.toFixed(4)}, ${state.pin.lng.toFixed(4)}`);
  if (!name) return;
  state.bookmarks.push({ name, lat: state.pin.lat, lng: state.pin.lng });
  saveBookmarks(); refreshBookmarks();
  setStatus(`Saved "${name}".`, "ok");
};

// ---------- setup / devices ----------

const overlay = document.getElementById("setup-overlay");
const deviceSelect = document.getElementById("device-select");
const deviceNote = document.getElementById("device-note");
let phones = [];
let pollTimer = null;

function majorVersion(v) { return parseInt((v || "").split(".")[0], 10) || 0; }

async function refreshDevices() {
  try {
    const res = await call("devices");
    phones = res.devices || [];
  } catch { phones = []; }
  // device dropdown in setup
  deviceSelect.innerHTML = "";
  if (!phones.length) {
    deviceSelect.innerHTML = "<option>No iPhone found — plug one in and unlock it</option>";
    deviceNote.textContent = "";
  } else {
    for (const p of phones) {
      const opt = document.createElement("option");
      opt.value = p.udid;
      opt.textContent = `${p.name} · iOS ${p.version} · ${p.usb ? "USB" : "Wi-Fi"}`;
      deviceSelect.appendChild(opt);
    }
    if (!state.udid || !phones.find((p) => p.udid === state.udid)) state.udid = phones[0].udid;
    deviceSelect.value = state.udid;
    const p = phones.find((x) => x.udid === state.udid);
    deviceNote.textContent = p && majorVersion(p.version) < 27 ? `Tested on iOS 27. iOS ${p.version} may not work.` : "";
  }
  updateSetupSteps();
  updateMainStatus();
}

function currentPhone() { return phones.find((p) => p.udid === state.udid); }

function updateMainStatus() {
  if (!phones.length) { setStatus("Connect an iPhone to begin (gear icon for setup)."); return; }
  const p = currentPhone();
  if (p && !state.simulated) setStatus(`Ready: ${p.name} (iOS ${p.version}). Drop a pin and Teleport.`);
}

deviceSelect.onchange = () => { state.udid = deviceSelect.value; updateSetupSteps(); };

function setStepStatus(id, text, kind = "") {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = "step-status " + kind;
}

function updateSetupSteps() {
  const p = currentPhone();
  const paired = !!(p && p.paired);
  const devmode = !!(p && p.developerMode);
  document.getElementById("btn-pair").disabled = !p || paired;
  document.getElementById("btn-devmode").disabled = !paired;
  document.getElementById("btn-prepare").disabled = !paired;
  setStepStatus("status-pair", paired ? "Paired" : "", paired ? "done" : "");
  setStepStatus("status-devmode", devmode ? "On" : "", devmode ? "done" : "");
}

document.getElementById("setup-open").onclick = () => { overlay.classList.remove("hidden"); startPolling(); };
document.getElementById("setup-done").onclick = () => { overlay.classList.add("hidden"); stopPolling(); updateMainStatus(); };

function startPolling() { refreshDevices(); stopPolling(); pollTimer = setInterval(refreshDevices, 2500); }
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

document.getElementById("btn-pair").onclick = async () => {
  if (!state.udid) return;
  setStepStatus("status-pair", "Waiting for you to tap Trust…");
  for (let i = 0; i < 40; i++) {
    try {
      const res = await call("pair", { udid: state.udid });
      if (res.state === "paired") { setStepStatus("status-pair", "Paired", "done"); refreshDevices(); return; }
      if (res.state === "denied") { setStepStatus("status-pair", "Trust declined — unplug, replug, retry.", "error"); return; }
      setStepStatus("status-pair", res.state === "locked" ? "Unlock the iPhone…" : "Tap Trust on the iPhone…");
    } catch (e) { setStepStatus("status-pair", e.message, "error"); return; }
    await sleep(2000);
  }
  setStepStatus("status-pair", "Timed out waiting for Trust.", "error");
};

document.getElementById("btn-devmode").onclick = async () => {
  if (!state.udid) return;
  setStepStatus("status-devmode", "Asking the iPhone…");
  try {
    await call("reveal_dev_mode", { udid: state.udid });
    setStepStatus("status-devmode", "Now switch on Developer Mode on the iPhone.");
  } catch (e) { setStepStatus("status-devmode", e.message, "error"); }
};

document.getElementById("btn-prepare").onclick = async () => {
  if (!state.udid) return;
  setStepStatus("status-prepare", "Preparing (can take a minute)…");
  try {
    await call("prepare", { udid: state.udid });
    setStepStatus("status-prepare", "Ready. Click Done.", "done");
  } catch (e) { setStepStatus("status-prepare", e.message, "error"); }
};

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---------- license gate ----------

const licenseOverlay = document.getElementById("license-overlay");
const licenseInput = document.getElementById("license-input");
const licenseError = document.getElementById("license-error");

async function verifyKey(key) {
  try { return await invoke("verify_license", { key }); } catch { return false; }
}

async function startApp() {
  licenseOverlay.classList.add("hidden");
  refreshBookmarks();
  await refreshDevices();
  if (!phones.length || !currentPhone()?.paired) { overlay.classList.remove("hidden"); startPolling(); }
}

document.getElementById("license-activate").onclick = async () => {
  const key = licenseInput.value.trim();
  if (!key) return;
  if (await verifyKey(key)) {
    localStorage.setItem("license", key);
    startApp();
  } else {
    licenseError.textContent = "That key isn't valid. Check it and try again.";
    licenseError.className = "error";
  }
};
licenseInput.addEventListener("keydown", (e) => { if (e.key === "Enter") document.getElementById("license-activate").click(); });

// ---------- init ----------

(async () => {
  const saved = localStorage.getItem("license");
  if (saved && (await verifyKey(saved))) startApp();
  else licenseOverlay.classList.remove("hidden");
})();
