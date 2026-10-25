/* Surkha Mine Monitor -- Leaflet client for the FastAPI/PostGIS backend.
 * API base defaults to the same origin; override with ?api=http://host:8000 */
const API = new URLSearchParams(location.search).get("api") || "";

// Survey layers from the GMDC plan
const STYLES = {
  lease_boundary:     { color: "#c0392b", weight: 2.5, fill: false, dashArray: "7 5" },
  lignite_boundary:   { color: "#222222", weight: 2, fill: false },
  ultimate_pit_limit: { color: "#8e44ad", weight: 2, fill: false },
  contours:           { color: "#e2b45a", weight: 1, opacity: 0.85 },
  lakes:              { color: "#2c6fb7", weight: 1, fillColor: "#5b9bd5", fillOpacity: 0.6 },
  canals:             { color: "#2c9fb7", weight: 2 },
};
const DEFAULT_ON = new Set(["lease_boundary", "lignite_boundary", "ultimate_pit_limit"]);
const LINE_LAYERS = new Set(["lease_boundary", "lignite_boundary", "ultimate_pit_limit", "contours", "canals"]);

// Sentinel-2 land-cover classes (LULC) -- one colour per class, used on map, legend and chart
const LULC = [
  { key: "bare",       label: "Bare ground / mining", color: "#b5835a", series: "lease_bare_ha" },
  { key: "vegetation", label: "Vegetation",           color: "#4f8f3a", series: "lease_veg_ha" },
  { key: "water",      label: "Water",                color: "#2f6fbf", series: "lease_water_ha" },
];
const CHANGE = [
  { key: "new_bare",       label: "Newly bare (dug, dumped or fallow)", color: "#c0392b" },
  { key: "no_longer_bare", label: "No longer bare (reclaimed, water, crops)", color: "#2e8b57" },
];

const fmt = (n, d = 0) =>
  n == null ? "–" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d });
const niceDate = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const getJSON = async (path) => {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json();
};
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- base map
const map = L.map("map", { zoomControl: true }).setView([21.65, 72.22], 13);
const imagery = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" });
const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
imagery.addTo(map);
L.control.layers({ "Satellite": imagery, "Street map": osm }, {}, { collapsed: true, position: "topright" }).addTo(map);
L.control.scale({ metric: true, imperial: false, position: "bottomleft" }).addTo(map);

function popupHtml(title, props) {
  const rows = Object.entries(props)
    .filter(([k, v]) => v !== null && v !== "" && k !== "id")
    .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  return `<div class="popup"><h3>${title}</h3><table>${rows}</table></div>`;
}
const swatch = (color, line = false) =>
  line ? `<span class="sw line" style="border-color:${color}"></span>`
       : `<span class="sw" style="border-color:${color};background:${color}"></span>`;

// ---------------------------------------------------------------- map legend (bottom right)
const layerObjs = {}, layerTitles = {};
let overlay = null;            // { title, items:[{label,color}] } for whatever analysis is on the map
const legend = L.control({ position: "bottomright" });
legend.onAdd = () => L.DomUtil.create("div", "map-legend");
legend.addTo(map);

function updateLegend() {
  const on = Object.keys(layerObjs).filter((n) => map.hasLayer(layerObjs[n]));
  let html = "";
  if (overlay) {
    html += `<b>${overlay.title}</b>` + overlay.items.map((i) => `<div>${swatch(i.color)} ${i.label}</div>`).join("");
  }
  if (on.length) {
    html += "<b>Survey layers</b>" + on.map((n) =>
      `<div>${swatch(STYLES[n].color, LINE_LAYERS.has(n))} ${layerTitles[n]}</div>`).join("");
  }
  const el = legend.getContainer();
  el.innerHTML = html;
  el.style.display = html ? "" : "none";
}

let analysisLayer = null;
function showAnalysis(layer, legendSpec) {
  if (analysisLayer) map.removeLayer(analysisLayer);
  analysisLayer = layer ? layer.addTo(map) : null;
  overlay = legendSpec;
  ["ultimate_pit_limit", "lignite_boundary", "lease_boundary"].forEach((n) =>
    layerObjs[n] && map.hasLayer(layerObjs[n]) && layerObjs[n].bringToFront());
  updateLegend();
}

// ---------------------------------------------------------------- survey layers
async function loadLayers() {
  const catalogue = await getJSON("/api/layers");
  let bounds = null;
  for (const meta of catalogue) {
    const fc = await getJSON(`/api/layers/${meta.name}`);
    const style = STYLES[meta.name] || { color: "#888" };
    const lyr = L.geoJSON(fc, {
      style: () => style,
      onEachFeature: (f, l) => {
        if (meta.name === "contours" && f.properties.Value) {
          l.bindTooltip(`${f.properties.Value} m`, { sticky: true, className: "contour-label" });
        }
        l.bindPopup(popupHtml(meta.title, f.properties));
      },
    });
    layerObjs[meta.name] = lyr;
    layerTitles[meta.name] = meta.title;
    if (DEFAULT_ON.has(meta.name)) lyr.addTo(map);
    if (meta.name === "lease_boundary") bounds = lyr.getBounds();

    const li = document.createElement("li");
    li.innerHTML = `<label><input type="checkbox" ${DEFAULT_ON.has(meta.name) ? "checked" : ""}>
      ${swatch(style.color, LINE_LAYERS.has(meta.name))} ${meta.title}</label>`;
    li.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? lyr.addTo(map) : map.removeLayer(lyr);
      updateLegend();
    });
    $("layer-list").appendChild(li);
  }
  ["ultimate_pit_limit", "lignite_boundary", "lease_boundary"].forEach((n) => layerObjs[n]?.bringToFront());
  if (bounds) {
    map.invalidateSize();
    map.fitBounds(bounds, { padding: [20, 20], maxZoom: 15 });
  }
  updateLegend();
}

// ---------------------------------------------------------------- site at a glance
async function loadStats() {
  const s = await getJSON("/api/stats/summary");
  const rows = [
    ["Mining lease", `${fmt(s.lease_area_ha)} ha`],
    ["Ultimate pit", `${fmt(s.pit_area_ha)} ha`],
    ["Lignite deposit", `${fmt(s.lignite_area_ha)} ha`],
    ["Pit share of lease", `${fmt(s.pit_pct_of_lease, 1)} %`],
    ["Ground elevation", `${fmt(s.min_contour_m)}–${fmt(s.max_contour_m)} m`],
  ];
  $("stats").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}

// ---------------------------------------------------------------- Sentinel-2 land cover
let observations = [];

function drawChart(obs, current) {
  const W = 300, H = 150, L = 36, R = 8, T = 10, B = 20;
  const t = (d) => Date.parse(d);
  const t0 = t(obs[0].obs_date), t1 = t(obs.at(-1).obs_date);
  const ymax = Math.max(...obs.flatMap((o) => LULC.map((c) => o[c.series] || 0))) * 1.1 || 1;
  const X = (d) => L + (t1 === t0 ? 0.5 : (t(d) - t0) / (t1 - t0)) * (W - L - R);
  const Y = (v) => T + (1 - v / ymax) * (H - T - B);
  const step = 10 ** Math.floor(Math.log10(ymax / 2));
  const ticks = [0, Math.round(ymax / 2 / step) * step];
  const years = [...new Set(obs.map((o) => o.obs_date.slice(0, 4)))];
  const every = Math.ceil(years.length / 4);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Land cover inside the lease over time">`;
  svg += ticks.map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/>
    <text x="${L - 5}" y="${Y(v) + 3.5}" text-anchor="end">${fmt(v)}</text>`).join("");
  svg += years.filter((_, i) => i % every === 0)
    .map((y) => `<text x="${X(y + "-07-01")}" y="${H - 5}" text-anchor="middle">${y}</text>`).join("");
  if (current) svg += `<line class="now" x1="${X(current)}" x2="${X(current)}" y1="${T}" y2="${H - B}"/>`;
  for (const c of LULC) {
    const pts = obs.map((o) => `${X(o.obs_date).toFixed(1)},${Y(o[c.series] || 0).toFixed(1)}`).join(" ");
    svg += `<polyline points="${pts}" fill="none" stroke="${c.color}" stroke-width="1.6"/>`;
  }
  svg += obs.map((o) => `<rect class="pt" data-date="${o.obs_date}" x="${X(o.obs_date) - 4}" y="${T}" width="8"
    height="${H - T - B}" fill="transparent"><title>${niceDate(o.obs_date)}</title></rect>`).join("");
  $("lulc-chart").innerHTML = svg + "</svg>";
  $("lulc-chart").querySelectorAll(".pt").forEach((r) =>
    r.addEventListener("click", () => { $("lulc-date").value = r.dataset.date; showLandCover(r.dataset.date); }));
}

async function showLandCover(date) {
  const fc = await getJSON(`/api/monitoring/footprint?date=${date}`);
  const area = Object.fromEntries(fc.features.map((f) => [f.properties.class, f.properties.area_ha]));
  const obs = observations.find((o) => o.obs_date === fc.obs_date);
  const lyr = L.geoJSON(fc, {
    style: (f) => {
      const c = LULC.find((k) => k.key === f.properties.class) || { color: "#999" };
      return { color: c.color, weight: 0.6, fillColor: c.color, fillOpacity: 0.55 };
    },
    onEachFeature: (f, l) => {
      const c = LULC.find((k) => k.key === f.properties.class);
      l.bindPopup(popupHtml(c ? c.label : f.properties.class,
        { date: niceDate(fc.obs_date), "area mapped": `${fmt(f.properties.area_ha)} ha` }));
    },
  });
  showAnalysis(lyr, { title: `Land cover, ${niceDate(fc.obs_date)}`, items: LULC });
  // panel legend: class, colour, and its area inside the lease for that date
  $("lulc-legend").innerHTML = LULC.map((c) =>
    `<li>${swatch(c.color)} ${c.label}<span class="ha">${fmt(obs ? obs[c.series] : area[c.key])} ha</span></li>`
  ).join("") + `<li class="note">Areas are inside the lease; the map also shows a 1 km margin.</li>`;
  drawChart(observations, fc.obs_date);
}

async function loadMonitoring() {
  const ts = await getJSON("/api/monitoring/timeseries");
  if (!ts.count) {
    $("lulc-meta").innerHTML = "No satellite scenes loaded yet. Run <i>python -m monitoring.s2_monitor</i> to add them.";
    return;
  }
  observations = ts.observations;
  const last = observations.at(-1);
  $("lulc-meta").textContent = `${ts.count} cloud-free scenes from ${niceDate(observations[0].obs_date)} ` +
    `to ${niceDate(last.obs_date)}, classified at 10 m.`;
  const opts = observations.slice().reverse()
    .map((o) => `<option value="${o.obs_date}">${niceDate(o.obs_date)}</option>`).join("");
  $("lulc-date").innerHTML = opts;
  $("cmp-from").innerHTML = opts;
  $("cmp-to").innerHTML = opts;
  $("cmp-from").value = observations[Math.max(0, observations.length - 5)].obs_date;
  $("cmp-to").value = last.obs_date;
  $("lulc-tools").hidden = false;
  $("cmp-tools").hidden = false;
  $("cmp-empty").hidden = true;
  $("lulc-date").addEventListener("change", (e) => showLandCover(e.target.value));
  drawChart(observations, null);
  // the legend lists classes even before a date is shown
  $("lulc-legend").innerHTML = LULC.map((c) => `<li>${swatch(c.color)} ${c.label}</li>`).join("");
}

// ---------------------------------------------------------------- compare two dates
$("cmp-run").addEventListener("click", async () => {
  const r = await getJSON(`/api/monitoring/change?from=${$("cmp-from").value}&to=${$("cmp-to").value}`);
  const lyr = L.geoJSON(r, {
    style: (f) => {
      const c = CHANGE.find((k) => k.key === f.properties.kind);
      return { color: c.color, weight: 0.6, fillColor: c.color, fillOpacity: 0.6 };
    },
    onEachFeature: (f, l) => l.bindPopup(popupHtml(CHANGE.find((k) => k.key === f.properties.kind).label,
      { area: `${fmt(f.properties.area_ha, 1)} ha`, between: `${niceDate(r.from)} and ${niceDate(r.to)}` })),
  });
  showAnalysis(lyr, { title: `Change, ${niceDate(r.from)} to ${niceDate(r.to)}`, items: CHANGE });
  $("cmp-result").innerHTML =
    `${swatch(CHANGE[0].color)} <b>${fmt(r.new_bare_ha)} ha</b> became bare, ${fmt(r.new_bare_outside_pit_ha)} ha of it outside the ultimate pit.<br>` +
    `${swatch(CHANGE[1].color)} <b>${fmt(r.no_longer_bare_ha)} ha</b> stopped being bare.`;
});

// ---------------------------------------------------------------- original plan overlay
let planLayer = null;
$("plan-toggle").addEventListener("change", async (e) => {
  if (!e.target.checked) { if (planLayer) map.removeLayer(planLayer); return; }
  if (!planLayer) {
    const meta = await getJSON("/data/plan_overlay.json");
    planLayer = L.imageOverlay(API + "/data/plan_overlay.png", meta.bounds,
      { opacity: $("plan-opacity").value / 100, interactive: false });
  }
  planLayer.addTo(map);
  planLayer.bringToBack();
  imagery.bringToBack();
});
$("plan-opacity").addEventListener("input", (e) => planLayer && planLayer.setOpacity(e.target.value / 100));

// ---------------------------------------------------------------- more analysis
let setbackTimer = null;
async function runSetback(buffer) {
  if (buffer <= 0) { showAnalysis(null, null); $("setback").innerHTML = ""; return; }
  const res = await getJSON(`/api/analysis/water-setback?buffer_m=${buffer}`);
  const lyr = L.geoJSON(res, {
    style: { color: "#c0392b", weight: 3, dashArray: "2 5", fillColor: "#c0392b", fillOpacity: 0.3 },
    onEachFeature: (f, l) => l.bindPopup(popupHtml(f.properties.class || f.properties.layer, {
      "distance to pit": f.properties.inside_pit ? "inside the pit limit" : `${fmt(f.properties.distance_m)} m`,
    })),
  });
  showAnalysis(lyr, { title: `Water within ${buffer} m of the pit`, items: [{ label: "Lake or canal", color: "#c0392b" }] });
  $("setback").innerHTML = res.violations
    ? `${res.violations} lakes or canals lie within ${buffer} m of the ultimate pit.`
    : `No lakes or canals within ${buffer} m.`;
}
$("buffer").addEventListener("input", (e) => {
  $("buffer-val").textContent = e.target.value;
  clearTimeout(setbackTimer);
  setbackTimer = setTimeout(() => runSetback(+e.target.value).catch(showError), 250);
});

$("mon-alerts").addEventListener("click", async () => {
  const r = await getJSON("/api/monitoring/alerts?persist=6");
  if (r.note) { $("alerts-result").textContent = "Needs at least six satellite scenes."; return; }
  const ZONES = [
    { key: "inside_lease_outside_pit", label: "Inside lease, outside pit", color: "#d68910" },
    { key: "outside_lease", label: "Outside the lease", color: "#c0392b" },
  ];
  const lyr = L.geoJSON(r, {
    style: (f) => {
      const z = ZONES.find((k) => k.key === f.properties.zone);
      return { color: z.color, weight: 1, fillColor: z.color, fillOpacity: 0.55 };
    },
    onEachFeature: (f, l) => l.bindPopup(popupHtml("Bare in the last six scenes",
      { where: ZONES.find((k) => k.key === f.properties.zone).label, area: `${fmt(f.properties.area_ha, 1)} ha` })),
  });
  showAnalysis(lyr, { title: "Bare in six scenes in a row", items: ZONES });
  const sum = (z) => r.features.filter((f) => f.properties.zone === z).reduce((a, f) => a + f.properties.area_ha, 0);
  $("alerts-result").innerHTML = `${fmt(sum("inside_lease_outside_pit"))} ha inside the lease but outside the pit, ` +
    `${fmt(sum("outside_lease"))} ha outside the lease.` +
    `<span class="hint">Places worth a closer look: dumps, roads and long-fallow land also stay bare.</span>`;
});

$("outside-toggle").addEventListener("change", async (e) => {
  if (!e.target.checked) { showAnalysis(null, null); $("outside").innerHTML = ""; return; }
  const fc = await getJSON("/api/analysis/lignite-outside-lease");
  const lyr = L.geoJSON(fc, {
    style: { color: "#b7950b", weight: 1, fillColor: "#f1c40f", fillOpacity: 0.6 },
    onEachFeature: (f, l) => l.bindPopup(popupHtml("Lignite beyond the lease", { area: `${fmt(f.properties.area_ha, 1)} ha` })),
  });
  showAnalysis(lyr, { title: "Lignite beyond the lease", items: [{ label: "Deposit outside lease", color: "#f1c40f" }] });
  const total = fc.features.reduce((a, f) => a + f.properties.area_ha, 0);
  $("outside").textContent = `${fmt(total, 1)} ha of the mapped deposit lies beyond the lease line.`;
});

// ---------------------------------------------------------------- click anywhere
map.on("click", async (e) => {
  const { lat, lng } = e.latlng;
  const r = await getJSON(`/api/analysis/point?lon=${lng.toFixed(6)}&lat=${lat.toFixed(6)}`);
  const zone = r.in_pit ? "Inside the ultimate pit" : r.in_lignite ? "Over the lignite deposit"
    : r.in_lease ? "Inside the lease" : "Outside the lease";
  L.popup().setLatLng(e.latlng).setContent(popupHtml(zone, {
    "location": `${lat.toFixed(5)}° N, ${lng.toFixed(5)}° E`,
    "to lease boundary": `${fmt(r.distance_to_lease_boundary_m)} m`,
    "nearest water": r.nearest_water ? `${r.nearest_water.layer}, ${fmt(r.nearest_water.distance_m)} m` : "–",
    "ground height": r.nearest_contour ? `about ${r.nearest_contour.elevation_m} m` : "–",
  })).openOn(map);
});

function showError(err) {
  console.error(err);
  $("stats").innerHTML = `<dt>Couldn't reach the server. Is it running?</dt>`;
}

(async () => {
  try {
    await Promise.all([loadLayers(), loadStats()]);
    loadMonitoring().catch((e) => {
      console.error(e);
      $("lulc-meta").textContent = "Satellite data is unavailable right now.";
    });
  } catch (err) { showError(err); }
})();
