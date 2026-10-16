// Legend colors for biomass chart + field map (Total, Arhar, Sugarcane, Mustard, Maize, Wheat, Rice).
const BIOMASS_TOTAL_COLOR = "#176b66";
const CROP_COLORS = {
  Rice: "#23bd67",
  Sugarcane: "#f04c46",
  "Arhar (Tur)": "#fad036",
  Mustard: "#4f78e9",
  Maize: "#a849f3",
  Wheat: "#f78229",
  Cotton: "#be123c",
  Soybean: "#a16207",
  "Linseed (Tisi)": "#0d9488",
  "Gram (Chana)": "#65a30d",
  "Lentil (Masoor)": "#0891b2",
  "Fallow/Uncertain": "#9ca3af",
};

function cropBaseName(label) {
  const text = String(label || "").trim();
  if (!text) return "";
  if (CROP_COLORS[text]) return text;
  const first = text.split(" + ")[0].trim();
  return CROP_COLORS[first] ? first : text;
}

const ALL_CROPS_CARD_LIMIT = 4;

const HIDDEN_CROPS = new Set(["subpixel", "tree", "uncertain", "fallow/uncertain", "unclassified"]);

// Chhattisgarh crop calendar — must match backend CALENDAR_SEASON in app/biomass.py
const KHARIF_CROPS = new Set([
  "Rice", "Maize", "Soybean", "Cotton", "Arhar (Tur)", "Sugarcane",
]);
const RABI_CROPS = new Set([
  "Wheat", "Mustard", "Linseed (Tisi)", "Gram (Chana)", "Lentil (Masoor)",
]);

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const ALL_VILLAGES = "__all__";
const ALL_CROPS = "__all_crops__";
const DISTRICT_TREND = "__district__";
const SUBDISTRICT_TREND = "__subdistrict__";

const INDEX_SERIES = [
  ["ndvi", "NDVI", "#2d6a4f", "index"],
  ["evi", "EVI", "#40916c", "index"],
  ["evi2", "EVI2", "#74c69d", "index"],
  ["gndvi", "GNDVI", "#1b4332", "index"],
  ["ndre", "NDRE", "#95d5b2", "index"],
  ["savi", "SAVI", "#52796f", "index"],
  ["osavi", "OSAVI", "#84a98c", "index"],
  ["rvi_optical", "RVI optical", "#344e41", "index"],
  ["ndwi_mcfeeters", "NDWI McFeeters", "#1d4e89", "index"],
  ["ndwi_gao", "NDWI Gao", "#468faf", "index"],
  ["ndmi", "NDMI", "#0077b6", "index"],
  ["ndbi", "NDBI", "#9c6644", "index"],
  ["bsi", "BSI", "#c4a15a", "index"],
  ["rvi_radar", "RVI radar", "#6d597a", "index"],
  ["vh_vv_ratio", "VH/VV", "#b56576", "index"],
  ["lst", "LST °C", "#bc4749", "climate"],
  ["temperature", "Temperature °C", "#e09f3e", "climate"],
  ["precipitation_daily", "Precipitation daily mm", "#1d4e89", "climate"],
  ["precipitation_sum", "Precipitation sum mm", "#457b9d", "climate"],
  ["soil_water_fraction_surface", "Soil water surface", "#2a9d8f", "climate"],
  ["soil_water_fraction_subsurface", "Soil water subsurface", "#264653", "climate"],
];

const INDEX_DEFAULTS = new Set(["ndvi", "evi", "gndvi", "ndmi", "savi", "ndwi_mcfeeters"]);

const state = {
  hierarchy: null,
  open: new Set(["state", "district"]),
  selectedKey: "",
  search: "",
  plotsOnly: false,
  features: [],
  plotPage: 0,
  plotPageSize: 12,
  selectedPlotId: null,
  context: null,
  cropSummary: null,
  cropPlaces: [],
  cropDistrict: "",
  cropSubdistrict: "",
  cropVillage: "",
  cropYear: "",
  cropMonth: "",
  cropSeason: "",
  cropPeriods: [],
  cropPlace: null,
  cropFieldCollection: null,
  mapMode: "",
  placeRequest: 0,
  outlineKey: "",
  outlineBody: null,
  fieldKey: "",
  showVillages: true,
  selectedCrop: "",
  analysisPlotId: null,
  analysisKind: "",
  analysisClassKey: "",
  analysisClasses: [],
  analysisLive: null,
  analysisTileLoading: false,
  analysisSeries: [],
  analysisScope: "",
  analysisRequest: 0,
  analysisJobTimer: null,
  biomassByCropId: null,
  biomassRequest: 0,
  biomassPlaces: null,
  biomassYears: ["2023", "2024", "2025", "2026"],
  biomassCompare: null,
  biomassCompareVillage: "",
  biomassCompareSeason: "",
  cropPage: 0,
  cropPageSize: 40,
  cropTotal: 0,
  charts: [],
  indexPlaces: [],
  indexSeries: [],
  indexFallback: null,
  indexDone: -1,
  indexCronDone: -1,
  indexFingerprint: null,
  indexPinned: false,
  indexPoll: null,
  indexCharts: [],
};

const $ = (id) => document.getElementById(id);

let cadastralMap;
let plotLayer;
let boundaryLayer;
let cropMap;
let villageLayer;
let fieldLayer;
let selectedFieldLayer = null;
let analysisPieChart = null;
let biomassChart = null;
let biomassBarChart = null;
let analysisTrendChart = null;
let analysisTile = null;
let analysisTileUrl = "";
let analysisTileGeometry = null;
let analysisClipGeometry = null;
let analysisClipBound = false;
let cropMapReady = false;

function colorFor(label) {
  if (label === "Total") return BIOMASS_TOTAL_COLOR;
  const base = cropBaseName(label);
  if (CROP_COLORS[base]) return CROP_COLORS[base];
  const text = String(label || "");
  if (!text) return "#6c757d";
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  return `hsl(${hash % 360}, 65%, 50%)`;
}

function darkenHex(hex, amount = 0.28) {
  const value = String(hex || "").replace("#", "");
  if (value.length !== 6) return hex || "#374151";
  const channel = (start) => {
    const part = parseInt(value.slice(start, start + 2), 16);
    return Math.max(0, Math.min(255, Math.round(part * (1 - amount))));
  };
  const toHex = (n) => n.toString(16).padStart(2, "0");
  return `#${toHex(channel(0))}${toHex(channel(2))}${toHex(channel(4))}`;
}

function topCropNames(entries) {
  return entries
    .filter(([label]) => !isHiddenCrop(label) && labelInSeason(label))
    .sort((left, right) => right[1] - left[1])
    .slice(0, ALL_CROPS_CARD_LIMIT)
    .map(([label]) => label);
}

function isHiddenCrop(label) {
  return HIDDEN_CROPS.has(String(label || "").trim().toLowerCase());
}

function showsAllCrops() {
  return state.selectedCrop === ALL_CROPS;
}

function seasonCropSet(season = state.cropSeason) {
  if (season === "kharif") return KHARIF_CROPS;
  if (season === "rabi") return RABI_CROPS;
  return null;
}

function labelCropParts(label) {
  return String(label || "")
    .split(" + ")
    .map((part) => part.trim())
    .filter(Boolean);
}

function labelHasCrop(label, crop) {
  if (!crop) return false;
  if (samePlaceName(label, crop)) return true;
  return labelCropParts(label).some((part) => samePlaceName(part, crop));
}

function labelInSeason(label, season = state.cropSeason) {
  const allowed = seasonCropSet(season);
  if (!allowed) return !isHiddenCrop(label);
  return labelCropParts(label).some((part) => {
    if (isHiddenCrop(part)) return false;
    return [...allowed].some((crop) => samePlaceName(part, crop));
  });
}

function cropInSeason(crop, season = state.cropSeason) {
  const allowed = seasonCropSet(season);
  if (!allowed) return !isHiddenCrop(crop);
  if (isHiddenCrop(crop)) return false;
  if ([...allowed].some((name) => samePlaceName(name, crop))) return true;
  // Double-crop labels (e.g. "Rice + Wheat") count if any part is in season.
  return labelCropParts(crop).some((part) =>
    [...allowed].some((name) => samePlaceName(part, name))
  );
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return number.toLocaleString("en-IN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function showBanner(message) {
  const banner = $("banner");
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
}

const responseCache = new Map();
const inflight = new Map();

async function readJson(url) {
  if (responseCache.has(url)) return responseCache.get(url);
  if (inflight.has(url)) return inflight.get(url);
  const job = (async () => {
    const response = await fetch(url);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (_error) {
        detail = response.statusText;
      }
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    const data = await response.json();
    responseCache.set(url, data);
    return data;
  })();
  inflight.set(url, job);
  try {
    return await job;
  } finally {
    inflight.delete(url);
  }
}

function primaryName(node) {
  return node.name_eng || node.name_en || node.name || "Unnamed";
}

function secondaryName(node) {
  const primary = primaryName(node);
  return node.name && node.name !== primary ? node.name : "";
}

function matchesQuery(text) {
  if (!state.search) return true;
  return (text || "").toLowerCase().includes(state.search);
}

function villageVisible(village, ri, taluka) {
  if (state.plotsOnly && !village.stored_plots) return false;
  if (!state.search) return true;
  const haystack = [
    village.name,
    village.name_en,
    village.name_eng,
    village.giscode,
    village.code,
    ri.name,
    ri.name_en,
    taluka.name,
    taluka.name_en,
  ].join(" ");
  return matchesQuery(haystack);
}

function buttonRow(label, count, options) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tree-row";
  if (options.open) button.classList.add("is-open");
  if (options.selected) button.classList.add("is-selected");
  const chevron = document.createElement("span");
  chevron.className = "chev";
  chevron.textContent = options.leaf ? "" : options.open ? "▾" : "▸";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = label;
  if (options.sub) {
    const small = document.createElement("small");
    small.textContent = options.sub;
    name.appendChild(small);
  }
  const tally = document.createElement("span");
  tally.className = "count";
  tally.textContent = count;
  button.append(chevron, name, tally);
  button.addEventListener("click", options.onClick);
  return button;
}

function renderTree() {
  const root = $("tree");
  root.replaceChildren();
  const data = state.hierarchy;
  if (!data) return;
  const totals = data.totals;
  $("hierarchy-totals").textContent = `${totals.talukas} tehsils · ${totals.ris} RIs · ${formatNumber(totals.villages)} villages · ${formatNumber(totals.plots)} plots`;

  const stateNode = document.createElement("div");
  stateNode.className = "tree-node";
  const stateOpen = state.open.has("state") || Boolean(state.search);
  stateNode.append(buttonRow(data.state.name, data.state.code, {
    open: stateOpen,
    selected: state.selectedKey === "state",
    onClick: () => toggleNode("state", { level: "state", node: data.state }),
  }));
  const stateChildren = document.createElement("div");
  stateChildren.className = `children${stateOpen ? " is-open" : ""}`;

  const districtOpen = state.open.has("district") || Boolean(state.search);
  const districtWrap = document.createElement("div");
  districtWrap.className = "tree-node";
  districtWrap.append(buttonRow(primaryName(data.district), data.district.code, {
    open: districtOpen,
    selected: state.selectedKey === "district",
    sub: secondaryName(data.district),
    onClick: () => toggleNode("district", { level: "district", node: data.district }),
  }));
  const districtChildren = document.createElement("div");
  districtChildren.className = `children${districtOpen ? " is-open" : ""}`;

  let shown = 0;
  data.talukas.forEach((taluka) => {
    const talukaKey = `taluka-${taluka.id}`;
    const riNodes = [];
    taluka.ris.forEach((ri) => {
      const villages = ri.villages.filter((village) => villageVisible(village, ri, taluka));
      if (!villages.length) return;
      const riKey = `ri-${ri.id}`;
      const riOpen = state.open.has(riKey) || Boolean(state.search);
      const riWrap = document.createElement("div");
      riWrap.className = "tree-node";
      riWrap.append(buttonRow(primaryName(ri), `${villages.length}`, {
        open: riOpen,
        selected: state.selectedKey === riKey,
        sub: secondaryName(ri),
        onClick: () => toggleNode(riKey, { level: "ri", node: ri, taluka }),
      }));
      const villageList = document.createElement("div");
      villageList.className = `children${riOpen ? " is-open" : ""}`;
      villages.forEach((village) => {
        shown += 1;
        const key = `village-${village.id}`;
        const row = buttonRow(primaryName(village), village.stored_plots ? `${formatNumber(village.stored_plots)}` : "0", {
          leaf: true,
          selected: state.selectedKey === key,
          sub: secondaryName(village),
          onClick: () => selectVillage(village, ri, taluka),
        });
        villageList.append(row);
      });
      riWrap.append(villageList);
      riNodes.push(riWrap);
    });
    if (!riNodes.length) return;
    const talukaOpen = state.open.has(talukaKey) || Boolean(state.search);
    const talukaWrap = document.createElement("div");
    talukaWrap.className = "tree-node";
    talukaWrap.append(buttonRow(primaryName(taluka), `${formatNumber(taluka.plot_count)} plots`, {
      open: talukaOpen,
      selected: state.selectedKey === talukaKey,
      sub: secondaryName(taluka),
      onClick: () => toggleNode(talukaKey, { level: "taluka", node: taluka }),
    }));
    const talukaChildren = document.createElement("div");
    talukaChildren.className = `children${talukaOpen ? " is-open" : ""}`;
    riNodes.forEach((node) => talukaChildren.append(node));
    talukaWrap.append(talukaChildren);
    districtChildren.append(talukaWrap);
  });
  districtWrap.append(districtChildren);
  stateChildren.append(districtWrap);
  stateNode.append(stateChildren);
  root.append(stateNode);
  if (!shown) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No villages match this search.";
    root.append(empty);
  }
}

function clearVillagePlots() {
  state.features = [];
  state.selectedPlotId = null;
  state.plotPage = 0;
  if (plotLayer) plotLayer.clearLayers();
  $("map-note").hidden = true;
  $("plot-count").textContent = "No village selected";
  $("plot-pager").replaceChildren();
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.className = "empty";
  cell.textContent = "Select a village to list its parcels.";
  row.append(cell);
  $("plot-rows").replaceChildren(row);
}

function toggleNode(key, context) {
  if (state.open.has(key)) state.open.delete(key);
  else state.open.add(key);
  state.selectedKey = key;
  state.context = context;
  clearVillagePlots();
  renderTree();
  renderContext();
  if (context.level === "taluka") loadBoundary(primaryName(context.node), null);
  if (context.level === "district" || context.level === "state") loadBoundary(null, null);
}

async function selectVillage(village, ri, taluka) {
  const key = `village-${village.id}`;
  state.selectedKey = key;
  state.open.add(`taluka-${taluka.id}`);
  state.open.add(`ri-${ri.id}`);
  state.context = { level: "village", node: village, ri, taluka };
  state.plotPage = 0;
  state.selectedPlotId = null;
  renderTree();
  renderContext();
  $("plot-count").textContent = "Loading parcels…";
  $("plot-rows").replaceChildren();
  try {
    const [collection] = await Promise.all([
      readJson(`/kawardha/plots/map?village_id=${village.id}&valid_geo=false`),
      loadBoundary(primaryName(taluka), village.name_eng || village.name_en || ""),
    ]);
    state.features = collection.features || [];
    drawPlots();
    renderPlotTable();
    const status = state.features[0]?.properties?.geo_status;
    const note = $("map-note");
    if (status === "local_cad") {
      note.hidden = false;
      note.textContent = "These parcels use local sheet metres, drawn on a schematic origin near Kabirdham. That placement is for browsing, not a surveyed position.";
    } else if (!state.features.length) {
      note.hidden = false;
      note.textContent = "This village has no stored plot geometry.";
    } else {
      note.hidden = false;
      note.textContent = `${formatNumber(state.features.length)} parcels. Colour follows the primary owner.`;
    }
  } catch (error) {
    showBanner(error.message);
    $("plot-count").textContent = "Could not load plots.";
  }
}

function renderContext() {
  const context = state.context;
  const detail = $("detail");
  detail.replaceChildren();
  if (!context) {
    $("detail-title").textContent = "Selection";
    $("detail-crumb").textContent = "Choose a village in the hierarchy.";
    return;
  }
  const hierarchy = state.hierarchy;
  const crumb = [hierarchy.state.name, primaryName(hierarchy.district)];
  if (context.taluka) crumb.push(primaryName(context.taluka));
  if (context.level === "taluka") crumb.push(primaryName(context.node));
  if (context.ri) crumb.push(primaryName(context.ri));
  if (context.level === "village") crumb.push(primaryName(context.node));
  $("detail-crumb").textContent = crumb.join("  /  ");
  $("detail-title").textContent = context.level === "village" ? primaryName(context.node) : primaryName(context.node);
  if (context.level === "village") {
    detail.append(statGrid([
      ["Stored plots", formatNumber(context.node.stored_plots)],
      ["Portal plots", formatNumber(context.node.plot_count)],
      ["Status", context.node.status || "—"],
      ["GIS code", context.node.giscode || "—"],
    ]));
    detail.append(definitionList([
      ["Hindi name", context.node.name],
      ["English name", context.node.name_eng || context.node.name_en || "—"],
      ["Village code", context.node.code || "—"],
      ["Tehsil", primaryName(context.taluka)],
      ["RI", primaryName(context.ri)],
    ]));
    return;
  }
  if (context.level === "taluka" || context.level === "ri") {
    detail.append(statGrid([
      ["Villages", formatNumber(context.node.village_count)],
      ["Plots", formatNumber(context.node.plot_count)],
    ]));
  }
  if (context.level === "state" || context.level === "district") {
    const totals = hierarchy.totals;
    detail.append(statGrid([
      ["Tehsils", formatNumber(totals.talukas)],
      ["RIs", formatNumber(totals.ris)],
      ["Villages", formatNumber(totals.villages)],
      ["Plots", formatNumber(totals.plots)],
    ]));
  }
}

function statGrid(pairs) {
  const grid = document.createElement("div");
  grid.className = "stat-grid";
  pairs.forEach(([label, value]) => {
    const cell = document.createElement("div");
    cell.className = "stat";
    const strong = document.createElement("b");
    strong.textContent = value;
    const span = document.createElement("span");
    span.textContent = label;
    cell.append(strong, span);
    grid.append(cell);
  });
  return grid;
}

function definitionList(pairs) {
  const list = document.createElement("dl");
  list.className = "kv";
  pairs.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value || "—";
    list.append(term, description);
  });
  return list;
}

function initCadastralMap() {
  cadastralMap = L.map("map", { zoomControl: true }).setView([22.02, 81.23], 9);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 19,
  }).addTo(cadastralMap);
  boundaryLayer = L.geoJSON(null, {
    style: { color: "#1b4332", weight: 2, fillColor: "#95d5b2", fillOpacity: 0.08 },
  }).addTo(cadastralMap);
  plotLayer = L.geoJSON(null, {
    style: (feature) => ({
      color: "#243028",
      weight: 0.6,
      fillColor: feature.properties.owner_color || "#7d9a62",
      fillOpacity: 0.7,
    }),
    onEachFeature: (feature, layer) => {
      layer.on("click", () => {
        state.selectedPlotId = feature.id;
        const index = state.features.findIndex((item) => item.id === feature.id);
        if (index >= 0) state.plotPage = Math.floor(index / state.plotPageSize);
        renderPlotTable();
        showPlotDetail(feature);
      });
    },
  }).addTo(cadastralMap);
}

async function loadBoundary(tehsil, village) {
  const params = new URLSearchParams({ district: "Kabirdham" });
  if (tehsil) params.set("tehsil", tehsil);
  if (village) params.set("village", village);
  boundaryLayer.clearLayers();
  if (village && !village.trim()) return;
  try {
    const body = await readJson(`/kawardha-data?${params.toString()}`);
    const layer = L.geoJSON(body, {
      style: { color: "#1b4332", weight: 2, fillOpacity: 0.05, dashArray: village ? null : "5 4" },
    });
    layer.eachLayer((child) => boundaryLayer.addLayer(child));
    if (!state.features.length) fitLayer(boundaryLayer);
  } catch (_error) {
    if (!village) return;
  }
}

function drawPlots() {
  plotLayer.clearLayers();
  if (!state.features.length) return;
  plotLayer.addData({ type: "FeatureCollection", features: state.features });
  fitLayer(plotLayer);
}

function fitLayer(layer) {
  const bounds = layer.getBounds();
  if (bounds.isValid()) cadastralMap.fitBounds(bounds.pad(0.12));
}

function renderPlotTable() {
  const rows = $("plot-rows");
  rows.replaceChildren();
  const total = state.features.length;
  $("plot-count").textContent = total
    ? `${formatNumber(total)} parcels in this village`
    : "No stored parcels for this village";
  if (!total) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty";
    cell.textContent = "No plot geometry is stored for this village.";
    row.append(cell);
    rows.append(row);
    $("plot-pager").replaceChildren();
    return;
  }
  const start = state.plotPage * state.plotPageSize;
  state.features.slice(start, start + state.plotPageSize).forEach((feature) => {
    const props = feature.properties || {};
    const row = document.createElement("tr");
    if (feature.id === state.selectedPlotId) row.classList.add("is-selected");
    [props.plot_no, props.khasra, formatNumber(props.area_ha, 3), props.land_use, props.owner_name, props.geo_status]
      .forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value || "—";
        row.append(cell);
      });
    row.addEventListener("click", () => {
      state.selectedPlotId = feature.id;
      renderPlotTable();
      showPlotDetail(feature);
      plotLayer.eachLayer((layer) => {
        if (layer.feature && layer.feature.id === feature.id && layer.getBounds) {
          cadastralMap.fitBounds(layer.getBounds().pad(0.4));
        }
      });
    });
    rows.append(row);
  });
  renderPager($("plot-pager"), state.plotPage, total, state.plotPageSize, (page) => {
    state.plotPage = page;
    renderPlotTable();
  });
}

function showPlotDetail(feature) {
  const props = feature.properties || {};
  $("detail-title").textContent = `Plot ${props.plot_no || feature.id}`;
  const detail = $("detail");
  detail.replaceChildren();
  detail.append(definitionList([
    ["Khasra", props.khasra],
    ["Survey", props.survey_no],
    ["ULPIN", props.ulpin],
    ["Area ha", formatNumber(props.area_ha, 4)],
    ["Land use", props.land_use],
    ["Tenure", props.tenure],
    ["Included khasra", props.included_khasra],
    ["Owner", props.owner_name],
    ["Owner (English)", props.owner_name_en],
    ["Relation", props.owner_relation ? `${props.owner_relation}: ${props.owner_relation_name || ""}` : "—"],
    ["Address", props.owner_address],
    ["Position", props.geo_status],
    ["Village", props.village_name],
  ]));
  const owners = props.owners || [];
  if (owners.length) {
    const list = document.createElement("ul");
    list.className = "owners";
    owners.forEach((owner) => {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = owner.name || "Owner";
      item.append(name);
      const extra = [owner.name_en, owner.relation && owner.relation_name ? `${owner.relation}: ${owner.relation_name}` : "", owner.address]
        .filter(Boolean)
        .join(" · ");
      if (extra) {
        const line = document.createElement("div");
        line.textContent = extra;
        item.append(line);
      }
      list.append(item);
    });
    detail.append(list);
  }
}

function renderPager(host, page, total, size, onPage) {
  host.replaceChildren();
  const pages = Math.max(1, Math.ceil(total / size));
  const prev = document.createElement("button");
  prev.type = "button";
  prev.textContent = "Prev";
  prev.disabled = page <= 0;
  prev.addEventListener("click", () => onPage(page - 1));
  const label = document.createElement("span");
  label.textContent = `${page + 1} / ${pages}`;
  const next = document.createElement("button");
  next.type = "button";
  next.textContent = "Next";
  next.disabled = page + 1 >= pages;
  next.addEventListener("click", () => onPage(page + 1));
  host.append(prev, label, next);
}

function switchView(name) {
  const hashes = { crops: "#crops", indices: "#indices", cadastral: "#cadastral" };
  const nextHash = hashes[name] || "#cadastral";
  if (location.hash !== nextHash) history.replaceState(null, "", nextHash);
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === name);
  });
  $("view-cadastral").hidden = name !== "cadastral";
  $("view-crops").hidden = name !== "crops";
  $("view-indices").hidden = name !== "indices";
  if (name === "cadastral" && cadastralMap) cadastralMap.invalidateSize();
  if (name === "crops") {
    ensureCropMap();
    if (!state.cropSummary) loadCrops().catch((error) => showBanner(error.message));
    else if (cropMap) cropMap.invalidateSize();
  }
  if (name === "indices") ensureIndices().catch((error) => showBanner(error.message));
}

function isGridBlock(name) {
  return /^r\d+c\d+$/i.test(name || "");
}

function indexPlaceLabel(value) {
  if (!value) return "";
  if (/^r\d+c\d+$/i.test(value)) return value;
  return value.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

async function readFresh(url, options) {
  const response = await fetch(url, options);
  let body = null;
  try {
    body = await response.json();
  } catch (_error) {
    body = null;
  }
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : response.statusText;
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return body;
}

function selectedIndexKeys() {
  return [...document.querySelectorAll("#index-toggles input:checked")].map((input) => input.value);
}

function indexQuery() {
  const district = $("index-district").value;
  const subdistrict = $("index-subdistrict").value;
  const village = $("index-village").value;
  const params = new URLSearchParams({
    district,
    frequency: $("index-frequency").value,
  });
  if (subdistrict === DISTRICT_TREND) {
    params.set("subdistrict", "");
    params.set("village", "");
  } else if (village === SUBDISTRICT_TREND) {
    params.set("subdistrict", subdistrict);
    params.set("village", "");
  } else {
    params.set("subdistrict", subdistrict);
    params.set("village", village);
  }
  return params;
}

function indexScopeLabel() {
  const district = indexPlaceLabel($("index-district").value);
  const subdistrict = $("index-subdistrict").value;
  const village = $("index-village").value;
  if (subdistrict === DISTRICT_TREND) return `District trend · ${district}`;
  if (village === SUBDISTRICT_TREND) return `Subdistrict trend · ${district} · ${indexPlaceLabel(subdistrict)}`;
  return `Village trend · ${district} · ${indexPlaceLabel(subdistrict)} · ${indexPlaceLabel(village)}`;
}

function fillIndexSubdistricts() {
  const district = $("index-district").value;
  const subdistricts = [...new Set(
    state.indexPlaces
      .filter((place) => place.district === district && place.subdistrict && !place.village && !isGridBlock(place.subdistrict))
      .map((place) => place.subdistrict),
  )].sort();
  const select = $("index-subdistrict");
  const previous = select.value;
  select.replaceChildren();
  const districtOption = document.createElement("option");
  districtOption.value = DISTRICT_TREND;
  districtOption.textContent = "District trend";
  select.append(districtOption);
  subdistricts.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = indexPlaceLabel(name);
    select.append(option);
  });
  if (previous && !isGridBlock(previous) && [...select.options].some((option) => option.value === previous)) {
    select.value = previous;
  } else if ([...select.options].some((option) => option.value === "kawardha")) {
    select.value = "kawardha";
  }
  fillIndexVillages();
}

function fillIndexVillages() {
  const district = $("index-district").value;
  const subdistrict = $("index-subdistrict").value;
  const select = $("index-village");
  const previous = select.value;
  select.replaceChildren();
  const subOption = document.createElement("option");
  subOption.value = SUBDISTRICT_TREND;
  subOption.textContent = subdistrict === DISTRICT_TREND ? "District trend" : "Subdistrict trend";
  select.append(subOption);
  if (subdistrict !== DISTRICT_TREND) {
    const villages = [...new Set(
      state.indexPlaces
        .filter((place) => place.district === district && place.subdistrict === subdistrict && place.village && !isGridBlock(place.village))
        .map((place) => place.village),
    )].sort();
    villages.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = indexPlaceLabel(name);
      select.append(option);
    });
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  else select.value = SUBDISTRICT_TREND;
}

function renderIndexToggles() {
  const host = $("index-toggles");
  if (!host || host.childElementCount) return;
  INDEX_SERIES.forEach(([key, label]) => {
    const wrap = document.createElement("label");
    wrap.className = "check";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = key;
    input.checked = INDEX_DEFAULTS.has(key);
    input.addEventListener("change", () => drawIndexTrend());
    wrap.append(input, document.createTextNode(label));
    host.append(wrap);
  });
}

function placeText(parts) {
  return (parts || []).filter(Boolean).map(indexPlaceLabel).join(" / ");
}

function renderIndexStatus(storeStatus, cronStatus) {
  const node = $("index-status");
  const button = $("index-store");
  if (!node) return;
  const storeRunning = Boolean(storeStatus && storeStatus.running);
  const cronRunning = Boolean(cronStatus && cronStatus.running);
  if (button) button.disabled = storeRunning || cronRunning;
  if (cronRunning) {
    const done = cronStatus.locations_done_this_cycle || 0;
    const total = cronStatus.locations_total_this_cycle || 0;
    const place = placeText(cronStatus.current_location);
    node.textContent = place
      ? `Cron is storing all villages, subdistricts, and districts: ${done} of ${total}. Now ${place}.`
      : `Cron is ${cronStatus.status || "running"}: ${done} of ${total} places.`;
    return;
  }
  if (!storeStatus) {
    node.textContent = "Storage has not started.";
    return;
  }
  const done = storeStatus.locations_done || 0;
  const total = storeStatus.locations_total || 0;
  const rows = storeStatus.rows_written || 0;
  if (storeRunning) {
    node.textContent = `Storing villages, subdistricts, and districts: ${done} of ${total} places, ${rows} scenes written.`;
    return;
  }
  if (storeStatus.status === "done" && total) {
    node.textContent = `Storage finished. ${done} of ${total} places, ${rows} scenes.`;
  } else if (storeStatus.status === "stopped") {
    node.textContent = `Storage stopped. ${done} of ${total} places, ${rows} scenes.`;
  } else if (storeStatus.last_error) {
    node.textContent = storeStatus.last_error;
  } else if (cronStatus && cronStatus.last_error) {
    node.textContent = cronStatus.last_error;
  } else {
    node.textContent = "Storage has not started. District, subdistrict, and village scenes are stored together.";
  }
}

function applyIndexPlace(place) {
  const district = $("index-district");
  if (![...district.options].some((option) => option.value === place.district)) return false;
  district.value = place.district;
  fillIndexSubdistricts();
  const subdistrict = $("index-subdistrict");
  if (!place.subdistrict) {
    subdistrict.value = DISTRICT_TREND;
  } else if ([...subdistrict.options].some((option) => option.value === place.subdistrict)) {
    subdistrict.value = place.subdistrict;
  }
  fillIndexVillages();
  const village = $("index-village");
  if (!place.village) {
    village.value = SUBDISTRICT_TREND;
  } else if ([...village.options].some((option) => option.value === place.village)) {
    village.value = place.village;
  }
  return true;
}

async function preferStoredPlace() {
  if (state.indexPinned || !$("index-district").value) return;
  const stored = await readFresh("/indices/stored-locations?limit=50");
  const places = (stored.locations || [])
    .filter((item) => !isGridBlock(item.subdistrict) && !isGridBlock(item.village))
    .sort((left, right) => (right.rows_count || 0) - (left.rows_count || 0));
  const current = await readFresh(`/indices/retrieve-aggregated?${indexQuery().toString()}`);
  if ((current.series || []).length && !isGridBlock($("index-subdistrict").value)) {
    state.indexPinned = true;
    return;
  }
  const place = places.find((item) => item.village) || places[0];
  if (place && applyIndexPlace(place)) {
    state.indexPinned = true;
    return;
  }
  const subdistrict = $("index-subdistrict");
  if ([...subdistrict.options].some((option) => option.value === "kawardha")) {
    subdistrict.value = "kawardha";
    fillIndexVillages();
  }
  state.indexPinned = true;
}

function drawIndexChart(canvasId, series, group) {
  const selected = new Set(selectedIndexKeys());
  const lines = INDEX_SERIES.filter(([key, , , kind]) => kind === group && selected.has(key));
  const canvas = $(canvasId);
  const existing = state.indexCharts.find((chart) => chart.canvas === canvas);
  if (existing) existing.destroy();
  state.indexCharts = state.indexCharts.filter((chart) => chart.canvas !== canvas);
  const usable = lines.filter(([key]) => series.some((row) => row[key] !== null && row[key] !== undefined));
  if (!usable.length || !series.length) return false;
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: series.map((row) => row.period),
      datasets: usable.map(([key, label, color]) => ({
        label,
        data: series.map((row) => row[key]),
        borderColor: color,
        backgroundColor: color,
        spanGaps: true,
        tension: 0.15,
        pointRadius: series.length > 48 ? 0 : 2,
        borderWidth: 2,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      font: { size: 24 },
      plugins: { legend: { position: "bottom", labels: { font: { size: 24 } } } },
      scales: {
        x: { ticks: { maxTicksLimit: 10, font: { size: 24 } } },
        y: { ticks: { font: { size: 24 } } },
      },
    },
  });
  state.indexCharts.push(chart);
  return true;
}

const INDEX_CARDS = [
  ["ndvi", "Crop Health (NDVI)", "#3b82f6"],
  ["evi", "Plantation thickness (EVI)", "#22c55e"],
  ["evi2", "Crop Yield Potential (EVI2)", "#84cc16"],
  ["gndvi", "Fertilizer status (GNDVI)", "#06b6d4"],
  ["ndre", "Crop Stress (NDRE)", "#14b8a6"],
  ["ndmi", "Soil Moisture (NDMI)", "#ec4899"],
  ["ndbi", "Non Farm Area (NDBI)", "#8b5cf6"],
  ["bsi", "Bare Soil Index (BSI)", "#f59e0b"],
  ["savi", "Soil-Adjusted VI (SAVI)", "#059669"],
  ["osavi", "Optimized SAVI (OSAVI)", "#10b981"],
  ["rvi_optical", "RVI (Optical)", "#6366f1"],
  ["ndwi_mcfeeters", "NDWI (McFeeters)", "#0ea5e9"],
  ["ndwi_gao", "NDWI (Gao)", "#0284c7"],
  ["lst", "Heat Stress (LST)", "#ef4444"],
  ["temperature", "Air temperature (°C)", "#f97316"],
  ["precipitation_daily", "Precipitation (daily)", "#38bdf8"],
  ["precipitation_sum", "Precipitation (sum)", "#0c4a6e"],
  ["rvi_radar", "RVI radar", "#6d597a"],
  ["vh_vv_ratio", "VH/VV", "#b56576"],
  ["soil_water_fraction_surface", "Soil water surface", "#2a9d8f"],
  ["soil_water_fraction_subsurface", "Soil water subsurface", "#264653"],
];

const INDEX_YEAR_COLORS = ["#2563eb", "#16a34a", "#f97316", "#9333ea", "#0891b2", "#dc2626", "#ca8a04", "#4f46e5"];

function indexCardChart(canvas, series, key, color) {
  const points = series
    .map((row) => ({ period: row.period, value: row[key] }))
    .filter((point) => point.period && point.value !== null && point.value !== undefined && Number.isFinite(Number(point.value)));
  const frequency = $("index-frequency").value;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let labels = [];
  let datasets = [];
  if (!points.length) return null;
  if (frequency === "yearly") {
    labels = points.map((point) => String(point.period).slice(0, 4));
    datasets = [{ label: "Value", data: points.map((point) => Number(point.value)), borderColor: color, backgroundColor: color, tension: 0.15, pointRadius: 3 }];
  } else if (frequency === "weekly") {
    labels = points.map((point) => point.period);
    datasets = [{ label: "Value", data: points.map((point) => Number(point.value)), borderColor: color, backgroundColor: color, tension: 0.15, pointRadius: 0 }];
  } else {
    const totals = new Map();
    points.forEach((point) => {
      const date = new Date(point.period);
      if (Number.isNaN(date.getTime())) return;
      const year = String(date.getFullYear());
      const slot = `${year}:${date.getMonth()}`;
      const current = totals.get(slot) || { sum: 0, count: 0 };
      current.sum += Number(point.value);
      current.count += 1;
      totals.set(slot, current);
    });
    const years = [...new Set(points.map((point) => {
      const date = new Date(point.period);
      return Number.isNaN(date.getTime()) ? "" : String(date.getFullYear());
    }).filter(Boolean))].sort();
    labels = months;
    datasets = years.map((year, index) => ({
      label: year,
      data: months.map((_month, monthIndex) => {
        const bucket = totals.get(`${year}:${monthIndex}`);
        return bucket ? bucket.sum / bucket.count : null;
      }),
      borderColor: INDEX_YEAR_COLORS[index % INDEX_YEAR_COLORS.length],
      backgroundColor: INDEX_YEAR_COLORS[index % INDEX_YEAR_COLORS.length],
      spanGaps: true,
      tension: 0.15,
      pointRadius: 2,
    }));
  }
  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { ticks: { maxTicksLimit: 12, font: { size: 11 } } },
        y: { ticks: { font: { size: 11 } } },
      },
    },
  });
}

function drawIndexTrend() {
  const series = state.indexSeries;
  const board = $("index-board");
  if ($("index-range")) {
    const fallback = state.indexFallback;
    if (fallback?.source?.village) {
      $("index-range").textContent = series.length
        ? `${indexScopeLabel()} · showing nearest stored ${indexPlaceLabel(fallback.source.village)} until this village is stored · ${$("index-frequency").value} · ${series[0].period} to ${series[series.length - 1].period}`
        : `${indexScopeLabel()} · no stored scenes yet`;
    } else {
      $("index-range").textContent = series.length
        ? `${indexScopeLabel()} · ${$("index-frequency").value} · ${series[0].period} to ${series[series.length - 1].period}`
        : `${indexScopeLabel()} · no stored scenes yet`;
    }
  }
  state.indexCharts.forEach((chart) => chart.destroy());
  state.indexCharts = [];
  if (!board) return;
  board.replaceChildren();
  if (!series.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No stored index series for this place.";
    board.append(empty);
    return;
  }
  INDEX_CARDS.forEach(([key, label, color]) => {
    const card = document.createElement("article");
    card.className = "index-metric";
    const title = document.createElement("h3");
    title.textContent = label;
    title.style.color = color;
    const chartWrap = document.createElement("div");
    chartWrap.className = "index-chart";
    const canvas = document.createElement("canvas");
    chartWrap.append(canvas);
    card.append(title, chartWrap);
    const chart = indexCardChart(canvas, series, key, color);
    if (!chart) {
      chartWrap.remove();
      const empty = document.createElement("p");
      empty.textContent = "No values for this index.";
      card.append(empty);
    } else {
      state.indexCharts.push(chart);
    }
    board.append(card);
  });
}

async function loadIndexTrend() {
  if (!$("index-district").value) return;
  const payload = await readFresh(`/indices/retrieve-aggregated?${indexQuery().toString()}`);
  const series = payload.series || [];
  const fallbackKey = payload.fallback?.source?.village || "";
  const fingerprint = `${fallbackKey}|` + series.map((row) => `${row.period}:${row.ndvi}:${row.evi}:${row.ndmi}`).join("|");
  if (fingerprint === state.indexFingerprint) return;
  state.indexFingerprint = fingerprint;
  state.indexSeries = series;
  state.indexFallback = payload.fallback || null;
  drawIndexTrend();
}

async function refreshIndexStatus() {
  const [storeStatus, cronStatus] = await Promise.all([
    readFresh("/indices/store-status"),
    readFresh("/indices-cron/status"),
  ]);
  renderIndexStatus(storeStatus, cronStatus);
  const storeDone = storeStatus.locations_done || 0;
  const cronDone = cronStatus.locations_done_this_cycle || 0;
  const active = Boolean(storeStatus.running || cronStatus.running);
  if (active) {
    state.indexDone = storeDone;
    state.indexCronDone = cronDone;
    await loadIndexTrend();
  }
  return storeStatus;
}

async function ensureIndices() {
  renderIndexToggles();
  if (!state.indexPoll) {
    state.indexPoll = setInterval(() => {
      if ($("view-indices").hidden) return;
      refreshIndexStatus().catch(() => {});
    }, 4000);
  }
  if (!state.indexPlaces.length) {
    const payload = await readFresh("/indices/places");
    state.indexPlaces = (payload.locations || []).filter(
      (place) => !isGridBlock(place.subdistrict) && !isGridBlock(place.village),
    );
    const districts = [...new Set(state.indexPlaces.map((place) => place.district))].sort();
    const select = $("index-district");
    select.replaceChildren();
    districts.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = indexPlaceLabel(name);
      select.append(option);
    });
    const kabirdham = districts.find((name) => name === "kabirdham");
    if (kabirdham) select.value = kabirdham;
    fillIndexSubdistricts();
  }
  await preferStoredPlace();
  await refreshIndexStatus();
  await loadIndexTrend();
}

function indexStoreQuery() {
  const source = indexQuery();
  const params = new URLSearchParams({
    district: source.get("district") || "",
    years: "10",
    step_days: "16",
    sync: "false",
  });
  const subdistrict = source.get("subdistrict");
  const village = source.get("village");
  if (subdistrict) params.set("subdistrict", subdistrict);
  if (village) params.set("village", village);
  return params;
}

async function startIndexStorePlace() {
  const button = $("index-store-place");
  button.disabled = true;
  try {
    const body = await readFresh(`/indices/store?${indexStoreQuery().toString()}`, { method: "POST" });
    const place = body.location || {};
    const label = [place.district, place.subdistrict, place.village].filter(Boolean).join(" / ") || "this place";
    showBanner(`Storing indices for ${label}. Charts keep reading the stored series.`);
    state.indexFingerprint = null;
    await refreshIndexStatus();
    await loadIndexTrend();
  } catch (error) {
    showBanner(error.message);
    $("index-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function startIndexStore() {
  const button = $("index-store");
  button.disabled = true;
  try {
    const body = await readFresh(
      "/indices/store-all?years=10&step_days=16&sync=false&pending_only=true",
      { method: "POST" },
    );
    showBanner(`Storing indices for ${body.locations} pending villages.`);
    await refreshIndexStatus();
  } catch (error) {
    showBanner(error.message);
    $("index-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function cropPeriodParams() {
  const params = new URLSearchParams();
  if (state.cropYear) params.set("year", state.cropYear);
  if (state.cropSeason) params.set("season", state.cropSeason);
  return params;
}

function periodLabel() {
  const year = state.cropYear;
  if (!year) return "";
  if (state.cropSeason === "kharif") return `Kharif ${year}`;
  if (state.cropSeason === "rabi") return `Rabi ${year}-${String(Number(year) + 1).slice(-2)}`;
  return String(year);
}

function biomassSeasonQuery() {
  return state.cropSeason ? `season=${encodeURIComponent(state.cropSeason)}` : "";
}

function fillMonthSelect() {
  const select = $("place-month");
  const months = state.cropPeriods
    .filter((period) => String(period.year) === String(state.cropYear))
    .map((period) => Number(period.month));
  const previous = state.cropMonth;
  select.replaceChildren(new Option("All months", ""));
  months.forEach((month) => select.append(new Option(MONTH_NAMES[month - 1], String(month))));
  if (previous && months.includes(Number(previous))) {
    state.cropMonth = String(previous);
    select.value = state.cropMonth;
  } else {
    state.cropMonth = "";
    select.value = "";
  }
}

function fillPeriodSelects(requestedYear, requestedMonth) {
  const years = [...new Set(state.cropPeriods.map((period) => String(period.year)))].sort((left, right) => Number(right) - Number(left));
  const yearSelect = $("place-year");
  yearSelect.replaceChildren();
  years.forEach((year) => yearSelect.append(new Option(year, year)));
  const year = years.includes(String(requestedYear)) ? String(requestedYear) : (years[0] || "");
  state.cropYear = year;
  if (year) yearSelect.value = year;
  state.cropMonth = requestedMonth ? String(requestedMonth) : "";
  fillMonthSelect();
}

async function loadCropStats() {
  const params = cropPeriodParams();
  const [summary, places] = await Promise.all([
    readJson(`/crops/summary?${params.toString()}`),
    readJson(`/crops/places?${params.toString()}`),
  ]);
  state.cropSummary = summary;
  state.cropPlaces = places.places;
}

async function loadCrops() {
  const periods = await readJson("/crops/periods");
  state.cropPeriods = periods.periods || [];
  const requested = new URLSearchParams(location.search);
  fillPeriodSelects(requested.get("year"), requested.get("month"));
  await loadCropStats();
  renderCropSummary();
  await initCropPlace();
}

async function changeCropPeriod() {
  state.cropYear = $("place-year").value;
  state.cropMonth = $("place-month") ? $("place-month").value : "";
  state.cropSeason = $("place-season") ? $("place-season").value : "";
  // Drop crop chip if it is not part of the newly selected season.
  if (state.selectedCrop && !showsAllCrops() && !cropInSeason(state.selectedCrop)) {
    state.selectedCrop = ALL_CROPS;
  }
  state.fieldKey = "";
  state.cropFieldCollection = null;
  state.biomassPlaces = null;
  state.biomassCompare = null;
  state.biomassCompareVillage = "";
  state.biomassCompareSeason = "";
  await loadCropStats();
  const villages = kawardhaPlaces().map((place) => place.village_name);
  if (state.cropVillage !== ALL_VILLAGES && !villages.some((name) => samePlaceName(name, state.cropVillage))) {
    state.cropVillage = ALL_VILLAGES;
  }
  syncHierarchySelects();
  renderCropSummary();
  await applyPlaceSelection();
  if (state.analysisKind === "biomass") {
    loadVillageBiomass().catch((error) => showBanner(error.message));
  }
}

function cropLabelStats(labels, cropName) {
  if (!cropName) return null;
  if (Array.isArray(labels)) {
    const row = labels.find((item) => samePlaceName(item.label, cropName));
    return row
      ? { fields: Number(row.fields) || 0, area_ha: Number(row.area_ha) || 0 }
      : { fields: 0, area_ha: 0 };
  }
  const entry = Object.entries(labels || {}).find(([label]) => samePlaceName(label, cropName));
  if (!entry) return { fields: 0, area_ha: 0 };
  return {
    fields: Number(entry[1].fields) || 0,
    area_ha: Number(entry[1].area_ha) || 0,
  };
}

function placesCropTotals(places, cropName) {
  return places.reduce((total, place) => {
    const stats = cropLabelStats(place.labels, cropName);
    total.fields += stats.fields;
    total.area_ha += stats.area_ha;
    total.allFields += Number(place.fields) || 0;
    total.allArea += Number(place.area_ha) || 0;
    return total;
  }, { fields: 0, area_ha: 0, allFields: 0, allArea: 0 });
}

function kawardhaPlaces() {
  return state.cropPlaces.filter((place) =>
    samePlaceName(place.district_name, state.cropDistrict)
    && samePlaceName(place.subdistrict_name, state.cropSubdistrict)
  );
}

function labelBars(rows) {
  const wrap = document.createElement("div");
  wrap.className = "mix-list";
  const visible = rows.filter(([label]) => !isHiddenCrop(label) && (showsAllCrops() ? labelInSeason(label) : cropInSeason(label) || labelInSeason(label)));
  const max = Math.max(...visible.map(([, count]) => count), 1);
  visible.forEach(([label, count, areaHa]) => {
    const row = document.createElement("div");
    row.className = "mix-row";
    const name = document.createElement("span");
    name.textContent = label;
    if (showsAllCrops() || samePlaceName(label, state.selectedCrop)) row.classList.add("is-active");
    const track = document.createElement("span");
    track.className = "mix-track";
    const fill = document.createElement("i");
    fill.style.width = `${(count / max) * 100}%`;
    fill.style.background = colorFor(label);
    track.append(fill);
    const tally = document.createElement("b");
    tally.textContent = `${formatNumber(count)}\n${formatNumber(areaHa, 1)} ha`;
    row.append(name, track, tally);
    wrap.append(row);
  });
  return wrap;
}

function renderCropDetail() {
  const places = kawardhaPlaces();
  const selected = state.cropVillage && state.cropVillage !== ALL_VILLAGES
    ? places.find((place) => samePlaceName(place.village_name, state.cropVillage))
    : null;
  const detail = $("crop-detail");
  detail.replaceChildren();
  if (!selected) {
    const summary = state.cropSummary;
    $("crop-detail-title").textContent = state.cropSubdistrict || "Subdistrict";
    $("crop-detail-crumb").textContent = [state.cropDistrict, state.cropSubdistrict, "all villages", periodLabel()].filter(Boolean).join(" / ");
    if (!summary) return;
    const cropName = showsAllCrops() ? "" : state.selectedCrop;
    const totals = placesCropTotals(places, cropName || "Rice");
    const cards = [
      ["Villages", formatNumber(places.length || summary.villages)],
      ["Fields", formatNumber(state.selectedCrop ? totals.allFields : summary.fields)],
    ];
    if (showsAllCrops()) {
      topCropNames(summary.labels.map((item) => [item.label, item.fields])).forEach((name) => {
        const stats = placesCropTotals(places, name);
        cards.push([`${name} · ${formatNumber(stats.area_ha, 1)} ha`, formatNumber(stats.fields)]);
      });
    } else if (cropName) {
      const share = totals.allFields ? (100 * totals.fields) / totals.allFields : 0;
      cards.push(
        [`${cropName} · ${formatNumber(totals.area_ha, 1)} ha`, formatNumber(totals.fields)],
        [`${cropName} share`, totals.allFields ? `${formatNumber(share, 1)}%` : "—"],
      );
    } else {
      cards.push(["Area ha", formatNumber(summary.area_ha, 1)]);
    }
    detail.append(statGrid(cards));
    detail.append(labelBars(summary.labels.map((item) => [item.label, item.fields, item.area_ha])));
    return;
  }
  const cropName = showsAllCrops() ? "" : state.selectedCrop;
  const crop = cropName ? cropLabelStats(selected.labels, cropName) : null;
  $("crop-detail-title").textContent = selected.village_name;
  $("crop-detail-crumb").textContent = [selected.district_name, selected.subdistrict_name, periodLabel()].filter(Boolean).join(" / ");
  const cards = [["Fields", formatNumber(selected.fields)]];
  if (showsAllCrops()) {
    topCropNames(Object.entries(selected.labels || {}).map(([label, value]) => [label, value.fields])).forEach((name) => {
      const stats = cropLabelStats(selected.labels, name);
      cards.push([`${name} · ${formatNumber(stats.area_ha, 1)} ha`, formatNumber(stats.fields)]);
    });
  } else if (crop) {
    cards.push(
      [`${cropName} · ${formatNumber(crop.area_ha, 1)} ha`, formatNumber(crop.fields)],
      [`${cropName} share`, selected.fields ? `${formatNumber((100 * crop.fields) / selected.fields, 1)}%` : "—"],
    );
  } else {
    cards.push(["Area ha", formatNumber(selected.area_ha, 1)]);
  }
  detail.append(statGrid(cards));
  const rows = Object.entries(selected.labels).sort((left, right) => right[1].fields - left[1].fields);
  detail.append(labelBars(rows.map(([label, value]) => [label, value.fields, value.area_ha])));
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((left, right) => left.localeCompare(right));
}

function subdistrictsFor(district) {
  return uniqueSorted(
    state.cropPlaces
      .filter((place) => samePlaceName(place.district_name, district))
      .map((place) => place.subdistrict_name),
  );
}

function syncHierarchySelects() {
  const districtSelect = $("place-district");
  const subSelect = $("place-subdistrict");
  const villageSelect = $("place-village");
  const districts = uniqueSorted(state.cropPlaces.map((place) => place.district_name));
  districtSelect.replaceChildren();
  districts.forEach((name) => districtSelect.append(new Option(name, name)));
  if (districts.includes(state.cropDistrict)) districtSelect.value = state.cropDistrict;
  const subdistricts = subdistrictsFor(state.cropDistrict);
  subSelect.replaceChildren();
  subdistricts.forEach((name) => subSelect.append(new Option(name, name)));
  if (subdistricts.includes(state.cropSubdistrict)) subSelect.value = state.cropSubdistrict;
  const villages = kawardhaPlaces()
    .map((place) => place.village_name)
    .sort((left, right) => left.localeCompare(right));
  villageSelect.replaceChildren(new Option("All villages", ALL_VILLAGES));
  villages.forEach((name) => villageSelect.append(new Option(name, name)));
  villageSelect.value = [...villageSelect.options].some((option) => option.value === state.cropVillage)
    ? state.cropVillage
    : ALL_VILLAGES;
  $("place-kicker").textContent = state.cropDistrict ? `${state.cropDistrict} district` : "District";
  $("place-heading").textContent = state.cropSubdistrict || "Subdistrict";
}

function selectVillagePlace(village) {
  const next = village || ALL_VILLAGES;
  const changed = !samePlaceName(next, state.cropVillage);
  if (changed) clearAnalysisField();
  state.cropVillage = next;
  const analysisLabel = $("analysis-plot");
  if (analysisLabel) {
    analysisLabel.textContent = next && next !== ALL_VILLAGES ? next : "Choose a layer on the left.";
  }
  state.cropPage = 0;
  const villageSelect = $("place-village");
  if (villageSelect && villageSelect.value !== state.cropVillage) villageSelect.value = state.cropVillage;
  renderCropDetail();
  applyPlaceSelection().catch((error) => showBanner(error.message));
  if (changed && state.analysisKind) {
    loadStoredAnalysis(state.analysisKind).catch((error) => showBanner(error.message));
  }
}

function renderCropSummary() {
  renderCropLegend();
  renderCropDetail();
}

function renderCropLegend() {
  const legend = $("crop-legend");
  const summary = state.cropSummary;
  legend.replaceChildren();
  if (!summary) return;
  const allButton = document.createElement("button");
  allButton.type = "button";
  allButton.className = showsAllCrops() ? "is-active" : "";
  allButton.textContent = "All crops";
  allButton.addEventListener("click", () => selectCrop(ALL_CROPS));
  legend.append(allButton);
  // Legend chips are single crops from the calendar for the selected season.
  const seasonCrops = seasonCropSet();
  const legendLabels = seasonCrops
    ? [...seasonCrops].filter((name) => summary.labels.some((item) => labelHasCrop(item.label, name)))
    : summary.labels
      .map((item) => item.label)
      .filter((label) => !isHiddenCrop(label) && !String(label).includes(" + "));
  legendLabels.forEach((label) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = samePlaceName(state.selectedCrop, label) ? "is-active" : "";
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    swatch.style.background = colorFor(label);
    button.append(swatch, document.createTextNode(label));
    button.addEventListener("click", () => selectCrop(label));
    legend.append(button);
  });
}

function selectCrop(label) {
  if (isHiddenCrop(label)) return;
  state.selectedCrop = samePlaceName(state.selectedCrop, label) ? "" : label;
  if (state.selectedCrop) hideAnalysisTile();
  else if (analysisTileUrl) showAnalysisTile(analysisTileUrl, analysisTileGeometry);
  renderCropLegend();
  renderCropSummary();
  const place = selectedPlace();
  const fieldKey = `${place.district}|${place.subdistrict}|${place.village}|${state.cropYear}|${state.cropSeason || "all"}`;
  const needFields = Boolean(state.selectedCrop)
    && place.village
    && place.village !== ALL_VILLAGES
    && state.fieldKey !== fieldKey;
  if (needFields) applyPlaceSelection().catch((error) => showBanner(error.message));
  else drawMap(false);
}

function selectedPlace() {
  return {
    district: state.cropDistrict,
    subdistrict: state.cropSubdistrict,
    village: state.cropVillage,
  };
}

async function initCropPlace() {
  const districts = [...new Set(state.cropPlaces.map((place) => place.district_name).filter(Boolean))].sort();
  const requested = new URLSearchParams(location.search);
  const district = districts.find((name) => samePlaceName(name, requested.get("district"))) || districts[0] || "";
  state.cropDistrict = district;
  const subdistricts = [...new Set(
    state.cropPlaces.filter((place) => place.district_name === district).map((place) => place.subdistrict_name).filter(Boolean),
  )];
  state.cropSubdistrict = subdistricts.find((name) => samePlaceName(name, requested.get("subdistrict")))
    || subdistricts.find((name) => samePlaceName(name, "Kawardha"))
    || subdistricts[0]
    || "";
  const match = kawardhaPlaces().find((place) => samePlaceName(place.village_name, requested.get("village")));
  state.cropVillage = match ? match.village_name : ALL_VILLAGES;
  syncHierarchySelects();
  const requestedCrop = requested.get("crop");
  if (requestedCrop && !isHiddenCrop(requestedCrop)) state.selectedCrop = requestedCrop;
  syncLayerFlags();
  renderCropDetail();
  await applyPlaceSelection();
}

function syncLayerFlags() {
  state.showVillages = $("layer-villages").checked;
}

function samePlaceName(left, right) {
  return String(left || "").trim().toLowerCase() === String(right || "").trim().toLowerCase();
}

function villageFeaturesFromOutline() {
  const body = state.outlineBody;
  if (!body) return [];
  if (body.villages && body.villages.features) return body.villages.features;
  return [];
}

function visibleFieldFeatures() {
  const features = (state.cropFieldCollection && state.cropFieldCollection.features) || [];
  if (!state.selectedCrop || isHiddenCrop(state.selectedCrop)) return [];
  return features.filter((feature) => {
    const label = (feature.properties || {}).final_label;
    if (!label || isHiddenCrop(label) || !labelInSeason(label)) return false;
    if (showsAllCrops()) return true;
    return labelHasCrop(label, state.selectedCrop) && cropInSeason(state.selectedCrop);
  });
}

function villageCropPlace(name) {
  return kawardhaPlaces().find((place) => samePlaceName(place.village_name, name)) || null;
}

function villageBoundaryStyle(feature, selectedName) {
  const name = (feature.properties || {}).village_name;
  if (selectedName && samePlaceName(name, selectedName)) {
    return { color: "#eab308", weight: 3.5, fill: false, fillOpacity: 0 };
  }
  return { color: "#ffffff", weight: 1.4, fill: false, fillOpacity: 0 };
}

function bindVillageBoundary(feature, polygon, selectedName, highlighted) {
  const props = feature.properties || {};
  const name = props.village_name || "Village";
  const crop = villageCropPlace(name);
  const area = crop ? crop.area_ha : props.area_ha;
  const selected = selectedName && samePlaceName(name, selectedName);
  const label = `${name} · ${formatNumber(area, 1)} ha`;
  polygon.bindTooltip(label, {
    permanent: selected,
    sticky: !selected,
    direction: "center",
    className: "village-label",
  });
  if (selected) highlighted.push(polygon);
  polygon.on("mouseover", () => {
    if (!samePlaceName(name, state.cropVillage)) {
      polygon.setStyle({ color: "#fde68a", weight: 2.6, fill: false, fillOpacity: 0 });
    }
    polygon.bringToFront();
  });
  polygon.on("mouseout", () => {
    polygon.setStyle(villageBoundaryStyle(feature, state.cropVillage === ALL_VILLAGES ? "" : state.cropVillage));
  });
  polygon.on("click", (event) => {
    L.DomEvent.stopPropagation(event);
    if (!samePlaceName(name, state.cropVillage)) selectVillagePlace(name);
  });
}

function fieldStyle(feature) {
  const label = (feature.properties || {}).final_label;
  if (!label || isHiddenCrop(label) || samePlaceName(label, "Fallow/Uncertain")) {
    return { color: "#9ca3af", weight: 1.2, opacity: 0.9, fill: true, fillColor: "#d1d5db", fillOpacity: 0.35 };
  }
  const fill = colorFor(label);
  return {
    color: darkenHex(fill, 0.32),
    weight: 1.4,
    opacity: 1,
    fill: true,
    fillColor: fill,
    fillOpacity: 0.72,
  };
}

function drawMap(fit) {
  const note = $("crop-map-note");
  ensureCropMap();
  villageLayer.clearLayers();
  fieldLayer.clearLayers();
  selectedFieldLayer = null;
  const place = selectedPlace();
  let bounds = null;

  const showFieldPolygons = place.village && place.village !== ALL_VILLAGES && state.selectedCrop;
  if (showFieldPolygons) {
    const features = visibleFieldFeatures();
    if (features.length) {
      const layer = L.geoJSON(
        { type: "FeatureCollection", features },
        {
          pane: "fieldPane",
          style: fieldStyle,
          onEachFeature: (feature, polygon) => {
            const props = feature.properties || {};
            const crop = props.final_label ? `${props.final_label} · ` : "";
            polygon.bindTooltip(`${crop}${formatNumber(props.area_ha, 3)} ha`, { sticky: true });
            polygon.on("click", (event) => {
              L.DomEvent.stopPropagation(event);
              showFieldBiomass(feature, polygon).catch((error) => showBanner(error.message));
            });
          },
        },
      );
      layer.addTo(fieldLayer);
      if (layer.getBounds().isValid()) bounds = layer.getBounds();
    }
  }

  if (state.showVillages) {
    const villages = villageFeaturesFromOutline();
    const selectedName = place.village && place.village !== ALL_VILLAGES ? place.village : "";
    if (!selectedName && place.subdistrict && state.outlineBody && state.outlineBody.subdistrict) {
      const layer = L.geoJSON(state.outlineBody.subdistrict, {
        style: () => ({ color: "#eab308", weight: 3.5, fill: false, fillOpacity: 0 }),
        onEachFeature: (feature, polygon) => {
          const props = feature.properties || {};
          polygon.bindTooltip(props.subdistrict_name || place.subdistrict, {
            permanent: true,
            direction: "center",
            className: "village-label",
          });
        },
      });
      layer.addTo(villageLayer);
      if (layer.getBounds().isValid()) bounds = layer.getBounds();
    } else if (villages.length) {
      const highlighted = [];
      const layer = L.geoJSON(
        { type: "FeatureCollection", features: villages },
        {
          style: (feature) => villageBoundaryStyle(feature, selectedName),
          interactive: !showFieldPolygons,
          onEachFeature: (feature, polygon) => bindVillageBoundary(feature, polygon, selectedName, highlighted),
        },
      );
      layer.addTo(villageLayer);
      highlighted.forEach((polygon) => polygon.bringToFront());
      if (!(showFieldPolygons && bounds && bounds.isValid())) {
        const selectedFeature = selectedName
          ? villages.find((feature) => samePlaceName((feature.properties || {}).village_name, selectedName))
          : null;
        const focus = selectedFeature ? L.geoJSON(selectedFeature) : layer;
        if (focus.getBounds().isValid()) bounds = focus.getBounds();
        else if (layer.getBounds().isValid()) bounds = layer.getBounds();
      }
    } else if (!place.village && place.subdistrict && state.outlineBody && state.outlineBody.subdistrict) {
      const layer = L.geoJSON(state.outlineBody.subdistrict, {
        style: () => ({ color: "#f7f3ea", weight: 2.5, fillColor: "#2d6a4f", fillOpacity: 0.12 }),
        onEachFeature: (feature, polygon) => {
          const props = feature.properties || {};
          polygon.bindTooltip(props.subdistrict_name || place.subdistrict, {
            permanent: true,
            direction: "center",
            className: "village-label",
          });
        },
      });
      layer.addTo(villageLayer);
      if (layer.getBounds().isValid()) bounds = layer.getBounds();
    } else if (!place.subdistrict && state.outlineBody && state.outlineBody.type === "FeatureCollection") {
      const layer = L.geoJSON(state.outlineBody, {
        style: () => ({ color: "#f7f3ea", weight: 2.5, fillColor: "#2d6a4f", fillOpacity: 0.18 }),
        onEachFeature: (feature, polygon) => {
          const props = feature.properties || {};
          polygon.bindTooltip(props.district_name || place.district, {
            permanent: true,
            direction: "center",
            className: "village-label",
          });
        },
      });
      layer.addTo(villageLayer);
      if (layer.getBounds().isValid()) bounds = layer.getBounds();
    }
  }

  if (showFieldPolygons && fieldLayer) {
    fieldLayer.eachLayer((layer) => {
      if (typeof layer.bringToFront === "function") layer.bringToFront();
    });
  }
  if (analysisTile && typeof analysisTile.bringToFront === "function") analysisTile.bringToFront();
  if (state.analysisKind === "biomass") {
    loadVillageBiomass().catch((error) => setStoreStatus(error.message));
  }
  cropMap.invalidateSize();
  if (fit && bounds && bounds.isValid()) focusMap(bounds);
  note.hidden = false;
  note.textContent = mapNote(place);
}

function focusMap(bounds) {
  const close = Boolean(state.selectedCrop);
  const padded = bounds.pad(close ? 0.04 : 0.12);
  requestAnimationFrame(() => {
    cropMap.invalidateSize();
    cropMap.flyToBounds(padded, { duration: 0.7, maxZoom: close ? 18 : 16 });
  });
}

function mapNote(place) {
  if (place.village === ALL_VILLAGES) {
    return `${place.subdistrict} subdistrict boundary.`;
  }
  if (place.village && place.village !== ALL_VILLAGES && state.selectedCrop) {
    const shown = visibleFieldFeatures().length;
    const crop = villageCropPlace(place.village);
    const area = crop ? `${formatNumber(crop.area_ha, 1)} ha · ` : "";
    if (!state.cropFieldCollection) return `Loading ${place.village}…`;
    if (showsAllCrops()) {
      return shown
        ? `${place.village} · all crops · ${periodLabel()} · ${formatNumber(shown)} fields`
        : `${place.village} · no crop fields in ${periodLabel()}`;
    }
    const period = periodLabel();
    return shown
      ? `${place.village} · ${area}${state.selectedCrop} · ${period} · ${formatNumber(shown)} fields`
      : `${place.village} · ${area}no ${state.selectedCrop} fields in ${period}`;
  }
  if (place.village && place.village !== ALL_VILLAGES) {
    return `${place.village} · choose a crop or All crops to draw those fields.`;
  }
  if (place.subdistrict) return `${place.subdistrict} subdistrict boundary.`;
  if (place.district) return `${place.district} district boundary.`;
  return "Select a district to draw its boundary.";
}

async function applyPlaceSelection() {
  const requestId = ++state.placeRequest;
  syncLayerFlags();
  const place = selectedPlace();
  state.cropDistrict = place.district;
  state.cropSubdistrict = place.subdistrict;
  state.cropVillage = place.village;
  state.cropPlace = place;
  renderCropDetail();
  const note = $("crop-map-note");
  note.hidden = false;
  try {
    const outlineKey = `${place.district}|${place.subdistrict}`;
    if (place.district && place.subdistrict) {
      if (state.outlineKey !== outlineKey) {
        note.textContent = "Loading village boundaries…";
        const params = new URLSearchParams({
          district_name: place.district,
          subdistrict_name: place.subdistrict,
        });
        const body = await readJson(`/boundaries?${params.toString()}`);
        if (requestId !== state.placeRequest) return;
        state.outlineKey = outlineKey;
        state.outlineBody = body;
        state.cropFieldCollection = null;
        state.fieldKey = "";
      }
      if (requestId !== state.placeRequest) return;
      if (state.outlineKey === outlineKey) drawMap(true);
      const fieldKey = `${outlineKey}|${place.village}|${state.cropYear}|${state.cropSeason || "all"}`;
      if (place.village && place.village !== ALL_VILLAGES && state.selectedCrop && state.fieldKey !== fieldKey) {
        note.textContent = "Loading field boundaries…";
        const params = new URLSearchParams({
          district_name: place.district,
          subdistrict_name: place.subdistrict,
          village_name: place.village,
        });
        cropPeriodParams().forEach((value, key) => params.set(key, value));
        const body = await readJson(`/boundaries?${params.toString()}`);
        if (requestId !== state.placeRequest) return;
        state.cropFieldCollection = body;
        state.fieldKey = fieldKey;
        state.mapMode = "fields";
      }
      if (!place.village || place.village === ALL_VILLAGES) {
        state.cropFieldCollection = null;
        state.fieldKey = "";
        state.mapMode = "outline";
      }
      if (state.outlineKey === outlineKey) drawMap(true);
    } else if (place.district) {
      if (state.outlineKey !== outlineKey) {
        note.textContent = "Loading district boundary…";
        state.cropFieldCollection = null;
        state.fieldKey = "";
        state.mapMode = "outline";
        const body = await readJson(`/boundaries?district_name=${encodeURIComponent(place.district)}`);
        if (requestId !== state.placeRequest) return;
        state.outlineKey = outlineKey;
        state.outlineBody = body;
        drawMap(true);
      } else {
        state.mapMode = "outline";
        drawMap(false);
      }
    } else {
      state.mapMode = "";
      state.outlineKey = "";
      state.outlineBody = null;
      state.cropFieldCollection = null;
      state.fieldKey = "";
      if (villageLayer) villageLayer.clearLayers();
      if (fieldLayer) fieldLayer.clearLayers();
      note.textContent = "Select a district to draw its boundary.";
    }
  } catch (error) {
    if (requestId !== state.placeRequest) return;
    note.textContent = error.message;
    showBanner(error.message);
  }
}

function ensureCropMap() {
  if (cropMapReady) {
    cropMap.invalidateSize();
    return;
  }
  cropMap = L.map("crop-map", { preferCanvas: true }).setView([22.02, 81.23], 11);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles &copy; Esri",
    maxZoom: 22,
    maxNativeZoom: 17,
  }).addTo(cropMap);
  cropMap.createPane("fieldPane");
  cropMap.getPane("fieldPane").style.zIndex = 650;
  villageLayer = L.layerGroup().addTo(cropMap);
  fieldLayer = L.layerGroup().addTo(cropMap);
  cropMapReady = true;
}

const ANALYSES = {
  growth: {
    title: "Growth",
    path: "/analyze_Growthclasswise",
    storedPath: "/api-stored/growth",
    classes: [
      ["weak", "Weak", "#bc1e29"],
      ["stress", "Stress", "#58cf54"],
      ["moderate", "Moderate", "#28ae31"],
      ["healthy", "Healthy", "#056c3e"],
    ],
  },
  water: {
    title: "Water uptake",
    path: "/wateruptakeclasswise",
    storedPath: "/api-stored/water-uptake",
    classes: [
      ["deficient", "Deficient", "#EBFF34"],
      ["less", "Less", "#CC8213"],
      ["adequat", "Adequate", "#1348E8"],
      ["excellent", "Excellent", "#2E199A"],
      ["excess", "Excess", "#060217"],
    ],
  },
  soil: {
    title: "Soil moisture",
    path: "/SoilMoistureclasswise",
    storedPath: "/api-stored/soil-moisture",
    classes: [
      ["less", "Less", "#2FC0D3"],
      ["adequate", "Adequate", "#4365D4"],
      ["excellent", "Excellent", "#473CDF"],
      ["excess", "Excess", "#2116BF"],
      ["shallow_water", "Shallow water", "#000475"],
    ],
  },
  pest: {
    title: "Pest detection",
    path: "/pest-detectionclasswise",
    storedPath: "/api-stored/pest-detection",
    classes: [
      ["healthy", "Healthy", "#22c55e"],
      ["chewing", "Chewing", "#FF0000"],
      ["fungi", "Fungi", "#00DD88"],
      ["sucking", "Sucking", "#2600FF"],
      ["wilt", "Wilt", "#D60BFF"],
      ["soilborne", "Soil borne", "#FFE608"],
    ],
  },
};

function analysisPlaceLabel() {
  const place = selectedPlace();
  if (place.village && place.village !== ALL_VILLAGES) return place.village;
  return "";
}

function clearAnalysisField() {
  state.analysisPlotId = null;
  const node = $("analysis-plot");
  if (node) node.textContent = analysisPlaceLabel() || "Choose a layer on the left.";
  analysisTileUrl = "";
  analysisTileGeometry = null;
  state.analysisClassKey = "";
  state.analysisClasses = [];
  state.analysisLive = null;
  hideAnalysisTile();
}

function hideAnalysisTile() {
  if (analysisTile && cropMap) cropMap.removeLayer(analysisTile);
  analysisTile = null;
  analysisClipGeometry = null;
  applyAnalysisClip();
}

function classwiseRow(classwise, key) {
  return (classwise || []).find((row) => samePlaceName(String(row.class_name || "").replace(/\s+/g, "_"), key));
}

function analysisPercent(summary, key, classwise) {
  const row = classwiseRow(classwise, key);
  if (row && row.percentage != null) return Number(row.percentage) || 0;
  if (key === "healthy" && summary.healthy_pixel_percentage == null && summary.total_pixel_count) {
    return (100 * Number(summary.healthy_pixel_count || 0)) / Number(summary.total_pixel_count);
  }
  const value = summary[`${key}_pixel_percentage`]
    ?? summary[`${key}_affected_pixel_percentage`];
  return Number(value) || 0;
}

function analysisClassArea(summary, key, classwise, totalHa, percent) {
  const row = classwiseRow(classwise, key);
  if (row && row.area_hectares != null) return Number(row.area_hectares) || 0;
  return totalHa ? (totalHa * percent) / 100 : 0;
}

function analysisInk(hex) {
  const raw = String(hex || "").replace("#", "");
  const full = raw.length === 3 ? raw.split("").map((char) => char + char).join("") : raw;
  const red = parseInt(full.slice(0, 2), 16);
  const green = parseInt(full.slice(2, 4), 16);
  const blue = parseInt(full.slice(4, 6), 16);
  if ([red, green, blue].some((part) => Number.isNaN(part))) return "#ffffff";
  return (0.299 * red + 0.587 * green + 0.114 * blue) / 255 > 0.62 ? "#111827" : "#ffffff";
}

function selectedVillageFeature() {
  const place = selectedPlace();
  if (!place.village || place.village === ALL_VILLAGES) return null;
  return villageFeaturesFromOutline().find((feature) => samePlaceName((feature.properties || {}).village_name, place.village)) || null;
}

function villageAreaHa(summary) {
  const fromSummary = Number(summary && summary.area_hectares);
  if (fromSummary) return fromSummary;
  const feature = selectedVillageFeature();
  const crop = villageCropPlace(selectedPlace().village);
  return Number((crop && crop.area_ha) || (feature && (feature.properties || {}).area_ha)) || 0;
}

function geometryRings(geometry) {
  if (!geometry || !geometry.coordinates) return [];
  if (geometry.type === "Polygon") return [geometry.coordinates[0]];
  if (geometry.type === "MultiPolygon") return geometry.coordinates.map((polygon) => polygon[0]).filter(Boolean);
  return [];
}

function ensureAnalysisPane() {
  if (!cropMap || cropMap.getPane("analysisPane")) return;
  const pane = cropMap.createPane("analysisPane");
  pane.style.zIndex = "620";
  pane.style.pointerEvents = "none";
}

function bindAnalysisClip() {
  if (!cropMap || analysisClipBound) return;
  analysisClipBound = true;
  const apply = () => applyAnalysisClip();
  cropMap.on("move zoom viewreset zoomend moveend", apply);
}

function applyAnalysisClip() {
  const pane = cropMap && cropMap.getPane("analysisPane");
  if (!pane) return;
  const rings = geometryRings(analysisClipGeometry);
  if (rings.length < 1) {
    pane.style.clipPath = "none";
    pane.style.webkitClipPath = "none";
    return;
  }
  const path = rings.map((ring) => ring.map((coord, index) => {
    const point = cropMap.latLngToLayerPoint([coord[1], coord[0]]);
    return `${index ? "L" : "M"}${Math.round(point.x)} ${Math.round(point.y)}`;
  }).join(" ") + " Z").join(" ");
  const value = `path(evenodd, '${path}')`;
  pane.style.clipPath = value;
  pane.style.webkitClipPath = value;
}

function showAnalysisTile(tileUrl, geometry, options) {
  const settings = options || {};
  if (tileUrl) analysisTileUrl = tileUrl;
  if (geometry && geometry.coordinates) analysisTileGeometry = geometry;
  if (state.selectedCrop && !settings.force) {
    hideAnalysisTile();
    return;
  }
  ensureCropMap();
  ensureAnalysisPane();
  bindAnalysisClip();
  if (analysisTile) cropMap.removeLayer(analysisTile);
  analysisTile = null;
  const village = selectedVillageFeature();
  analysisClipGeometry = (analysisTileGeometry && analysisTileGeometry.coordinates)
    ? analysisTileGeometry
    : (village && village.geometry) || null;
  if (analysisTileUrl && String(analysisTileUrl).includes("{z}")) {
    analysisTile = L.tileLayer(analysisTileUrl, {
      pane: "analysisPane",
      opacity: 0.92,
      maxZoom: 18,
      maxNativeZoom: 18,
      updateWhenIdle: true,
    }).addTo(cropMap);
  }
  applyAnalysisClip();
  if (settings.refit === false) return;
  if (fieldLayer) {
    fieldLayer.eachLayer((group) => {
      if (typeof group.eachLayer !== "function") return;
      group.eachLayer((layer) => {
        if (typeof layer.setStyle === "function") layer.setStyle({ fillOpacity: 0.08 });
      });
    });
  }
  const focus = analysisClipGeometry || (village && village.geometry);
  if (!focus) return;
  const layer = L.geoJSON(focus);
  if (layer.getBounds().isValid()) {
    cropMap.fitBounds(layer.getBounds().pad(0.12), { maxZoom: 16 });
  }
}

function pestCircleStats(hierarchy) {
  if (!hierarchy) return null;
  const classIds = { chewing: 1, fungi: 2, sucking: 3, wilt: 4, soilborne: 5 };
  return ANALYSES.pest.classes.map(([key, label, color]) => {
    const node = hierarchy[key] || {};
    return {
      key,
      label,
      color,
      classId: classIds[key],
      tileUrl: node.tile_url || "",
      percent: Number(node.percentage) || 0,
      hectares: Number(node.total_area_ha) || 0,
    };
  });
}

function classTileFromCombined(combinedUrl, classId) {
  const url = String(combinedUrl || "");
  const marker = "/classwise-tiles/";
  const start = url.indexOf(marker);
  if (start < 0 || classId == null) return "";
  const rest = url.slice(start + marker.length);
  const key = rest.split("/")[0];
  if (!key) return "";
  return `/classwise-tiles/${key}/class/${classId}/{z}/{x}/{y}.png`;
}

function analysisClassStats(spec, current) {
  const summary = current.pixel_summary || {};
  const classwise = current.classwise || [];
  const feature = current.feature || ((current.features || [])[0]) || {};
  const combined = (feature.properties || {}).tile_url || current.tile_url || "";
  const totalHa = villageAreaHa(summary);
  if (spec.path.includes("pest") && current.hierarchy) {
    return pestCircleStats(current.hierarchy).map((item) => ({
      ...item,
      tileUrl: item.tileUrl || classTileFromCombined(combined, item.classId),
      combined,
    }));
  }
  return spec.classes.map(([key, label, color], index) => {
    const row = classwiseRow(classwise, key);
    const classId = row && row.class_id != null ? Number(row.class_id) : index + 1;
    const percent = analysisPercent(summary, key, classwise);
    return {
      key,
      label,
      color,
      classId,
      tileUrl: (row && row.tile_url) || classTileFromCombined(combined, classId),
      combined,
      percent,
      hectares: analysisClassArea(summary, key, classwise, totalHa, percent),
    };
  });
}

function paintClassSelection() {
  document.querySelectorAll("#analysis-legend button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.key === state.analysisClassKey);
  });
  if (!analysisPieChart) return;
  analysisPieChart.data.datasets[0].borderColor = state.analysisClasses.map((item) => item.key === state.analysisClassKey ? "#111827" : "#ffffff");
  analysisPieChart.data.datasets[0].borderWidth = state.analysisClasses.map((item) => item.key === state.analysisClassKey ? 3 : 1);
  analysisPieChart.update();
}

function drawAnalysisPie(stats) {
  const canvas = $("analysis-pie");
  if (!canvas) return;
  canvas.parentElement.classList.add("is-ready");
  if (analysisPieChart) analysisPieChart.destroy();
  analysisPieChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: stats.map((item) => item.label),
      datasets: [{
        data: stats.map((item) => Math.max(Number(item.percent) || 0, 0)),
        backgroundColor: stats.map((item) => item.color),
        borderColor: stats.map((item) => item.key === state.analysisClassKey ? "#111827" : "#ffffff"),
        borderWidth: stats.map((item) => item.key === state.analysisClassKey ? 3 : 1),
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "58%",
      plugins: { legend: { display: false } },
      onClick: (_event, elements) => {
        const hit = elements && elements[0];
        if (!hit || !stats[hit.index]) return;
        showClassTile(stats[hit.index]).catch((error) => showBanner(error.message));
      },
    },
  });
}

function drawAnalysisLegend(stats) {
  const host = $("analysis-legend");
  if (!host) return;
  host.replaceChildren();
  stats.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.key = item.key;
    button.className = state.analysisClassKey === item.key ? "is-active" : "";
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    swatch.style.background = item.color;
    const name = document.createElement("span");
    name.textContent = item.label;
    const percent = document.createElement("b");
    percent.textContent = `${item.percent > 0 && item.percent < 1 ? formatNumber(item.percent, 2) : formatNumber(item.percent, 0)}%`;
    const area = document.createElement("span");
    area.textContent = `${formatNumber(item.hectares, 2)} ha`;
    button.append(swatch, name, percent, area);
    button.addEventListener("click", () => {
      showClassTile(item).catch((error) => showBanner(error.message));
    });
    host.append(button);
  });
}

function combinedTileFrom(current) {
  const feature = (current && (current.feature || ((current.features || [])[0]))) || {};
  const props = feature.properties || {};
  return {
    url: props.tile_url || current.tile_url || "",
    geometry: feature.geometry && feature.geometry.coordinates ? feature.geometry : null,
  };
}

async function showDefaultCombinedTile(requestId) {
  setStoreStatus("Loading combined tile…");
  const current = await ensureClasswiseCurrent();
  if (requestId != null && requestId !== state.analysisRequest) return;
  if (state.analysisClassKey) return;
  const tile = combinedTileFrom(current);
  if (!tile.url || !String(tile.url).includes("{z}")) {
    setStoreStatus("Combined tile is not available for this month.");
    return;
  }
  showAnalysisTile(tile.url, tile.geometry, { force: true });
  setStoreStatus("Combined tile");
}

function renderAnalysis(spec, body) {
  if (!spec) return;
  const current = body.current || body;
  const stats = analysisClassStats(spec, current);
  state.analysisClasses = stats;
  drawAnalysisPie(stats);
  drawAnalysisLegend(stats);
  const label = $("analysis-plot");
  const placeName = analysisScopeLabel(state.analysisScope) || analysisPlaceLabel();
  if (label && placeName) label.textContent = `${placeName} · ${spec.title}`;
  if (state.analysisClassKey) {
    const selected = stats.find((item) => item.key === state.analysisClassKey);
    if (selected && selected.tileUrl) {
      showAnalysisTile(selected.tileUrl, analysisTileGeometry, { force: true, refit: false });
      return;
    }
  }
  showDefaultCombinedTile(state.analysisRequest).catch((error) => {
    setStoreStatus(error.message);
    showBanner(error.message);
  });
}

function analysisEndDate() {
  const row = selectedStoredRow();
  const storedEnd = row && row.request_params && row.request_params.end_date;
  if (storedEnd) return String(storedEnd).slice(0, 10);
  const dateInput = $("analysis-date");
  return dateInput ? dateInput.value : "";
}

let classwiseFlight = null;

async function ensureClasswiseCurrent() {
  const spec = ANALYSES[state.analysisKind];
  const endDate = analysisEndDate();
  if (!spec) throw new Error("Choose growth, water, soil, or pest first.");
  if (!endDate) throw new Error("No stored month is available for this place.");
  const live = state.analysisLive;
  if (live && live.kind === state.analysisKind && live.endDate === endDate && live.current) return live.current;
  const flightKey = `${state.analysisKind}|${endDate}|${selectedPlace().district}|${selectedPlace().subdistrict}|${selectedPlace().village}`;
  if (classwiseFlight && classwiseFlight.key === flightKey) return classwiseFlight.promise;
  const promise = (async () => {
    const params = storeLocationParams(endDate);
    params.set("include_tiles", "true");
    const body = await readFresh(`${spec.path}?${params.toString()}`, { method: "POST" });
    const current = body.current || body;
    state.analysisLive = { kind: state.analysisKind, endDate, current };
    return current;
  })();
  classwiseFlight = { key: flightKey, promise };
  try {
    return await promise;
  } finally {
    if (classwiseFlight && classwiseFlight.promise === promise) classwiseFlight = null;
  }
}

async function showClassTile(item) {
  if (state.analysisClassKey === item.key) {
    state.analysisClassKey = "";
    paintClassSelection();
    const current = state.analysisLive && state.analysisLive.current;
    const tile = combinedTileFrom(current || {});
    showAnalysisTile(tile.url || item.combined, tile.geometry || analysisTileGeometry, { force: true, refit: false });
    setStoreStatus("Combined tile");
    return;
  }
  if (state.analysisTileLoading) return;
  state.analysisTileLoading = true;
  state.analysisClassKey = item.key;
  paintClassSelection();
  setStoreStatus(`Loading ${item.label} class tile…`);
  try {
    const current = await ensureClasswiseCurrent();
    const stats = analysisClassStats(ANALYSES[state.analysisKind], current);
    state.analysisClasses = stats;
    const match = stats.find((row) => row.key === item.key);
    const tileUrl = match && match.tileUrl;
    if (!tileUrl || !String(tileUrl).includes("{z}")) {
      state.analysisClassKey = "";
      paintClassSelection();
      const tile = combinedTileFrom(current);
      if (tile.url) showAnalysisTile(tile.url, tile.geometry, { force: true, refit: false });
      setStoreStatus(`${item.label} has no separate class tile.`);
      return;
    }
    showAnalysisTile(tileUrl, analysisTileGeometry, { force: true, refit: false });
    setStoreStatus(`${item.label} class tile`);
  } finally {
    state.analysisTileLoading = false;
    paintClassSelection();
  }
}

function analysisScopeLabel(scope) {
  const place = selectedPlace();
  if (scope === "village") return place.village;
  if (scope === "subdistrict") return `${place.subdistrict} subdistrict`;
  return `${place.district} district`;
}

function storedAttempts() {
  const place = selectedPlace();
  const attempts = [];
  if (place.village && place.village !== ALL_VILLAGES && place.subdistrict) {
    attempts.push({ subdistrict: place.subdistrict, village: place.village, scope: "village" });
  }
  if (place.subdistrict) {
    attempts.push({ subdistrict: place.subdistrict, village: "", scope: "subdistrict" });
  }
  if (place.district) attempts.push({ subdistrict: "", village: "", scope: "district" });
  return attempts;
}

function storeLocationParams(endDate) {
  const place = selectedPlace();
  const params = new URLSearchParams({ district: place.district });
  if (place.subdistrict) params.set("subdistrict", place.subdistrict);
  if (place.village && place.village !== ALL_VILLAGES) params.set("village", place.village);
  if (endDate) params.set("end_date", endDate);
  return params;
}

function rowMatchesAttempt(row, attempt) {
  const village = String(row.village || "").trim();
  const subdistrict = String(row.subdistrict || "").trim();
  if (attempt.scope === "village") return samePlaceName(village, attempt.village);
  if (attempt.scope === "subdistrict") return !village && samePlaceName(subdistrict, attempt.subdistrict);
  return !village && !subdistrict;
}

async function fetchStoredSeries(kind) {
  const spec = ANALYSES[kind];
  if (!spec) return { series: [], scope: "district" };
  const attempts = storedAttempts();
  for (const attempt of attempts) {
    const params = new URLSearchParams({ district: selectedPlace().district, limit: "500" });
    if (attempt.subdistrict) params.set("subdistrict", attempt.subdistrict);
    if (attempt.village) params.set("village", attempt.village);
    const body = await readFresh(`${spec.storedPath}?${params.toString()}`);
    const series = (body.series || [])
      .filter((row) => rowMatchesAttempt(row, attempt))
      .sort((left, right) => String(right.year_month || "").localeCompare(String(left.year_month || "")));
    if (series.length) return { series, scope: attempt.scope };
  }
  return { series: [], scope: attempts[0] ? attempts[0].scope : "district" };
}

function fillAnalysisMonths(preferred) {
  const select = $("analysis-month");
  if (!select) return;
  const months = [...new Set(state.analysisSeries.map((row) => row.year_month).filter(Boolean))];
  select.replaceChildren();
  if (!months.length) {
    select.append(new Option("No stored months", ""));
    return;
  }
  months.forEach((month) => select.append(new Option(month, month)));
  if (preferred && months.includes(preferred)) select.value = preferred;
}

function selectedStoredRow() {
  return state.analysisSeries[0] || null;
}

function storedPayload(row) {
  const data = row && row.response_data;
  if (!data || typeof data !== "object") return { current: {} };
  if (data.current) return data;
  return { current: data };
}

function showStoredMonth() {
  const spec = ANALYSES[state.analysisKind];
  const row = selectedStoredRow();
  if (!spec || !row) return;
  renderAnalysis(spec, storedPayload(row));
  const label = $("analysis-plot");
  if (label) label.textContent = `${analysisScopeLabel(state.analysisScope)} · ${spec.title} · ${row.year_month}`;
}

function biomassCropId(feature) {
  const props = (feature && feature.properties) || {};
  const cropId = Number(props.crop_id || (feature && feature.id));
  return Number.isFinite(cropId) && cropId > 0 ? cropId : 0;
}

function eachFieldPolygon(visit) {
  if (!fieldLayer) return;
  fieldLayer.eachLayer((group) => {
    if (typeof group.eachLayer === "function") group.eachLayer(visit);
    else visit(group);
  });
}

function destroyBiomassChart() {
  if (biomassChart) biomassChart.destroy();
  biomassChart = null;
  if (biomassBarChart) biomassBarChart.destroy();
  biomassBarChart = null;
}

function markBiomassActive() {
  state.analysisKind = "biomass";
  state.analysisClassKey = "";
  state.analysisLive = null;
  hideAnalysisTile();
  const panel = $("analysis-panel");
  if (panel) panel.classList.add("is-biomass");
  document.querySelectorAll("[data-analysis]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.analysis === "biomass");
  });
  const panelTitle = $("analysis-panel-title");
  if (panelTitle) panelTitle.textContent = "Village biomass";
  if (analysisPieChart) analysisPieChart.destroy();
  analysisPieChart = null;
  destroyBiomassChart();
  const pie = $("analysis-pie");
  if (pie) pie.parentElement.classList.remove("is-ready");
}

function activateBiomass() {
  markBiomassActive();
  const label = $("analysis-plot");
  if (label) label.textContent = "Loading field biomass…";
  const legend = $("analysis-legend");
  if (legend) legend.replaceChildren();
  setStoreStatus("");
  loadVillageBiomass().catch((error) => {
    setStoreStatus(error.message);
    showBanner(error.message);
  });
}

function biomassYear(place, year) {
  const bucket = place && place[String(year)];
  return bucket || { fields: 0, tonnes: 0, area_ha: 0, crops: {} };
}

function biomassCropEntries(bucket, limit) {
  const crops = (bucket && bucket.crops) || {};
  const entries = Object.entries(crops).filter(([, amount]) => (Number(amount.tonnes) || 0) > 0.0005);
  entries.sort((a, b) => (Number(b[1].tonnes) || 0) - (Number(a[1].tonnes) || 0));
  return limit ? entries.slice(0, limit) : entries;
}

function biomassRollupFrom(bucket) {
  const area = Number(bucket && bucket.area_ha) || 0;
  const tonnes = Number(bucket && bucket.tonnes) || 0;
  return {
    fields: Number(bucket && bucket.fields) || 0,
    area,
    tonnes,
    rate: area ? tonnes / area : 0,
  };
}

async function loadVillageCompare(villageName) {
  if (!villageName || villageName === ALL_VILLAGES) {
    state.biomassCompare = null;
    state.biomassCompareVillage = "";
    state.biomassCompareSeason = "";
    return null;
  }
  const season = state.cropSeason || "";
  if (
    state.biomassCompare
    && samePlaceName(state.biomassCompareVillage, villageName)
    && state.biomassCompareSeason === season
  ) {
    return state.biomassCompare;
  }
  const seasonQuery = biomassSeasonQuery();
  const url = `/biomass/compare?village=${encodeURIComponent(villageName)}${seasonQuery ? `&${seasonQuery}` : ""}`;
  const body = await readJson(url);
  state.biomassCompare = body;
  state.biomassCompareVillage = villageName;
  state.biomassCompareSeason = season;
  if (Array.isArray(body.years) && body.years.length) {
    state.biomassYears = body.years.map(String);
  }
  return body;
}

async function loadVillageBiomass() {
  const requestId = ++state.biomassRequest;
  const jobs = [];
  state.biomassByCropId = new Map();
  const selectedName = state.cropVillage && state.cropVillage !== ALL_VILLAGES ? state.cropVillage : "";
  const season = state.cropSeason || "";
  if (!samePlaceName(state.biomassCompareVillage, selectedName) || state.biomassCompareSeason !== season) {
    state.biomassCompare = null;
    state.biomassCompareVillage = "";
    state.biomassCompareSeason = "";
  }
  if (state.biomassPlaces && requestId === state.biomassRequest) {
    renderVillageBiomass(null);
  }
  if (!state.biomassPlaces) {
    const seasonQuery = biomassSeasonQuery();
    const placesUrl = `/biomass/places${seasonQuery ? `?${seasonQuery}` : ""}`;
    jobs.push(readJson(placesUrl).then((body) => {
      if (requestId !== state.biomassRequest) return;
      state.biomassPlaces = body.villages || [];
      state.biomassYears = (body.years || ["2023", "2024", "2025", "2026"]).map(String);
      renderVillageBiomass(null);
    }));
  }
  if (selectedName) {
    jobs.push(loadVillageCompare(selectedName).catch(() => null));
  }
  await Promise.all(jobs);
  if (requestId !== state.biomassRequest || state.analysisKind !== "biomass") return;
  renderVillageBiomass(null);
  showBanner("");
}

function biomassSourceText(source) {
  if (source === "scenes") return "Calculated from this village's Sentinel-2 scenes.";
  if (source === "village_average") return "This village had no scenes, so the rate is the average of villages that did.";
  if (source === "mixed") return "One season used this village's scenes. The other used the average of villages with scenes.";
  return "";
}

const BIOMASS_FONT = { family: "ui-sans-serif, system-ui, sans-serif", size: 20 };
const BIOMASS_TITLE_FONT = { family: "ui-sans-serif, system-ui, sans-serif", size: 20, weight: "600" };
const BIOMASS_SPAN = 0.15;

function biomassDigits(value) {
  const size = Math.abs(Number(value) || 0);
  if (size >= 100) return 0;
  if (size >= 10) return 1;
  return 2;
}

function biomassBand(value) {
  const number = Number(value) || 0;
  return {
    min: number * (1 - BIOMASS_SPAN),
    max: number * (1 + BIOMASS_SPAN),
    digits: biomassDigits(number),
  };
}

function biomassRangeText(value) {
  const band = biomassBand(value);
  if (Math.abs(band.max) < 0.0005) return "0 t";
  return `${formatNumber(band.min, band.digits)}–${formatNumber(band.max, band.digits)} t`;
}

function biomassRangeEnds(value) {
  const band = biomassBand(value);
  const wrap = document.createElement("div");
  wrap.className = "biomass-range";
  [["Min", band.min], ["Max", band.max]].forEach(([label, amount]) => {
    const end = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const figure = document.createElement("b");
    figure.textContent = `${formatNumber(amount, band.digits)} t`;
    end.append(caption, figure);
    wrap.append(end);
  });
  return wrap;
}

function biomassVillageList(places, selectedName, years) {
  const wrap = document.createElement("div");
  wrap.className = "biomass-villages";
  const columns = `minmax(0, 0.8fr) repeat(${years.length}, 1.1fr)`;
  const head = document.createElement("div");
  head.className = "biomass-head";
  head.style.gridTemplateColumns = columns;
  ["Village", ...years.map((year) => `${year} range`)].forEach((text) => {
    const cell = document.createElement("span");
    cell.textContent = text;
    head.append(cell);
  });
  wrap.append(head);
  places.forEach((place) => {
    const button = document.createElement("button");
    button.type = "button";
    button.style.gridTemplateColumns = columns;
    if (samePlaceName(place.village, selectedName)) button.className = "is-active";
    const name = document.createElement("span");
    name.className = "biomass-place-name";
    name.textContent = place.village;
    button.append(name);
    years.forEach((year, index) => {
      const tonnes = biomassYear(place, year).tonnes;
      const cell = document.createElement("span");
      cell.textContent = biomassRangeText(tonnes);
      if (index > 0) {
        const before = biomassYear(place, years[index - 1]).tonnes;
        if (tonnes > before) cell.className = "is-up";
        else if (tonnes < before) cell.className = "is-down";
      }
      button.append(cell);
    });
    button.addEventListener("click", () => selectVillagePlace(place.village));
    wrap.append(button);
  });
  return wrap;
}

function biomassStat(label, bucket, deltaText, rateText) {
  const card = document.createElement("div");
  card.className = "biomass-stat";
  const title = document.createElement("span");
  title.textContent = label;
  card.append(title, biomassRangeEnds(biomassRollupFrom(bucket).tonnes));
  if (rateText) {
    const rate = document.createElement("small");
    rate.textContent = rateText;
    card.append(rate);
  }
  biomassCropEntries(bucket, BIOMASS_STAT_CROPS).forEach(([crop, amount]) => {
    const line = document.createElement("small");
    line.textContent = `${crop} ${biomassRangeText(amount.tonnes)}`;
    card.append(line);
  });
  if (deltaText) {
    const delta = document.createElement("small");
    if (deltaText.startsWith("+")) delta.className = "is-up";
    else if (deltaText.startsWith("-")) delta.className = "is-down";
    delta.textContent = deltaText;
    card.append(delta);
  }
  return card;
}

function biomassCompareDelta(amount, previousYear) {
  if (amount == null || amount.change_pct == null || !amount.fields) return "";
  const pct = Number(amount.change_pct);
  const sign = pct > 0 ? "+" : "";
  return `${sign}${formatNumber(pct, 0)}% vs ${previousYear}`;
}

function biomassBucketFromCompare(amount) {
  const crops = {};
  return {
    fields: Number(amount && amount.fields) || 0,
    tonnes: Number(amount && amount.tonnes) || 0,
    area_ha: Number(amount && amount.area_ha) || 0,
    min_biomass: Number(amount && amount.min_biomass) || 0,
    max_biomass: Number(amount && amount.max_biomass) || 0,
    crops,
  };
}

function biomassYearCardsFromCompare(compare) {
  const row = document.createElement("div");
  row.className = "biomass-pair";
  const years = (compare.years || []).map(String);
  const totals = compare.totals || [];
  const byYear = new Map(totals.map((item) => [String(item.year), item]));
  const cropByYear = {};
  (compare.crops || []).forEach((entry) => {
    (entry.years || []).forEach((amount) => {
      const key = String(amount.year);
      if (!cropByYear[key]) cropByYear[key] = {};
      if (Number(amount.tonnes) > 0) cropByYear[key][entry.crop] = { tonnes: amount.tonnes };
    });
  });
  years.forEach((year, index) => {
    const amount = byYear.get(year) || { year, fields: 0, tonnes: 0, area_ha: 0 };
    const bucket = biomassBucketFromCompare(amount);
    bucket.crops = cropByYear[year] || {};
    const previous = index > 0 ? years[index - 1] : null;
    const delta = previous ? biomassCompareDelta(amount, previous) : "";
    const rate = amount.biomass_t_ha != null
      ? `${formatNumber(amount.biomass_t_ha, 1)} t/ha · ${formatNumber(amount.fields)} fields`
      : `${formatNumber(amount.fields)} fields`;
    row.append(biomassStat(year, bucket, delta, rate));
  });
  return row;
}

function biomassChartFonts() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: "bottom",
        labels: { font: BIOMASS_FONT, color: "#111827", boxWidth: 14, padding: 14, usePointStyle: true },
      },
      tooltip: {
        titleFont: BIOMASS_TITLE_FONT,
        bodyFont: BIOMASS_FONT,
        padding: 10,
      },
    },
  };
}

const BIOMASS_STAT_CROPS = 4;
const BIOMASS_CHART_CROPS = 6;
const BIOMASS_LEGEND_ORDER = ["Arhar (Tur)", "Sugarcane", "Mustard", "Maize", "Wheat", "Rice"];

function biomassSeries(place, years) {
  const totals = new Map();
  years.forEach((year) => {
    biomassCropEntries(biomassYear(place, year)).forEach(([crop, amount]) => {
      totals.set(crop, (totals.get(crop) || 0) + (Number(amount.tonnes) || 0));
    });
  });
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
  const preferred = BIOMASS_LEGEND_ORDER
    .filter((crop) => totals.has(crop))
    .map((crop) => [crop, totals.get(crop)]);
  const rest = ranked.filter(([crop]) => !BIOMASS_LEGEND_ORDER.includes(crop));
  const crops = [...preferred, ...rest].slice(0, BIOMASS_CHART_CROPS);
  return [
    ["Total", null, BIOMASS_TOTAL_COLOR],
    ...crops.map(([crop]) => [crop, crop, colorFor(crop)]),
  ];
}

function biomassBucketBand(bucket) {
  const low = Number(bucket && bucket.min_biomass);
  const high = Number(bucket && bucket.max_biomass);
  if (low || high) return [low || 0, high || 0];
  const band = biomassBand(bucket && bucket.tonnes);
  return [band.min, band.max];
}

function drawVillageYearly(place, years) {
  destroyBiomassChart();
  if (typeof Chart === "undefined" || !place) return;
  const bar = $("biomass-bars");
  if (!bar) return;
  const datasets = biomassSeries(place, years)
    .map(([label, key, color]) => ({
      label,
      data: years.map((year) => {
        const bucket = biomassYear(place, year);
        return biomassBucketBand(key ? (bucket.crops || {})[key] : bucket);
      }),
      backgroundColor: color,
      borderRadius: 6,
      maxBarThickness: 56,
    }))
    .filter((dataset) => dataset.data.some((range) => range[1] > 0.05));
  if (!datasets.length) return;
  biomassBarChart = new Chart(bar, {
    type: "bar",
    data: { labels: years, datasets },
    options: {
      ...biomassChartFonts(),
      plugins: {
        ...biomassChartFonts().plugins,
        tooltip: {
          ...biomassChartFonts().plugins.tooltip,
          callbacks: {
            label(context) {
              const raw = context.raw;
              if (!Array.isArray(raw)) return context.dataset.label || "";
              const digits = biomassDigits((raw[0] + raw[1]) / 2);
              return `${context.dataset.label}: ${formatNumber(raw[0], digits)}–${formatNumber(raw[1], digits)} t`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { font: BIOMASS_FONT, color: "#111827" }, grid: { display: false } },
        y: {
          beginAtZero: true,
          title: { display: true, text: "Village biomass (t)", font: BIOMASS_TITLE_FONT, color: "#374151" },
          ticks: { font: BIOMASS_FONT, color: "#374151" },
        },
      },
    },
  });
}

function biomassTotals(places, year) {
  return places.reduce((sum, place) => {
    const bucket = biomassYear(place, year);
    sum.fields += bucket.fields || 0;
    sum.area_ha += Number(bucket.area_ha) || 0;
    sum.tonnes += Number(bucket.tonnes) || 0;
    biomassCropEntries(bucket).forEach(([crop, amount]) => {
      const total = sum.crops[crop] || (sum.crops[crop] = { tonnes: 0 });
      total.tonnes += Number(amount.tonnes) || 0;
    });
    return sum;
  }, { fields: 0, area_ha: 0, tonnes: 0, crops: {} });
}

function biomassChangeText(before, after, beforeYear) {
  const diff = (Number(after) || 0) - (Number(before) || 0);
  if (Math.abs(diff) < 0.05) return `Same as ${beforeYear}`;
  if (!before) return `New since ${beforeYear}`;
  const pct = (diff / before) * 100;
  const sign = diff > 0 ? "+" : "";
  return `${sign}${formatNumber(pct, 0)}% vs ${beforeYear}`;
}

function biomassYearCards(yearBuckets) {
  const row = document.createElement("div");
  row.className = "biomass-pair";
  yearBuckets.forEach(([year, bucket], index) => {
    const previous = index > 0 ? yearBuckets[index - 1] : null;
    const delta = previous ? biomassChangeText(previous[1].tonnes, bucket.tonnes, previous[0]) : "";
    const rollup = biomassRollupFrom(bucket);
    const rate = rollup.area
      ? `${formatNumber(rollup.rate, 1)} t/ha · ${formatNumber(rollup.fields)} fields`
      : `${formatNumber(rollup.fields)} fields`;
    row.append(biomassStat(year, bucket, delta, rate));
  });
  return row;
}

function biomassFrame(className, canvasId) {
  const frame = document.createElement("div");
  frame.className = className;
  const canvas = document.createElement("canvas");
  canvas.id = canvasId;
  frame.append(canvas);
  return frame;
}

function renderVillageBiomass(fieldRow) {
  destroyBiomassChart();
  const panelTitle = $("analysis-panel-title");
  if (panelTitle) panelTitle.textContent = "Village biomass";
  const legend = $("analysis-legend");
  if (!legend) return;
  const places = state.biomassPlaces || [];
  const selectedName = state.cropVillage && state.cropVillage !== ALL_VILLAGES ? state.cropVillage : "";
  const selected = places.find((place) => samePlaceName(place.village, selectedName)) || null;
  const years = state.biomassYears || ["2024", "2025"];
  const compare = state.biomassCompare && samePlaceName(state.biomassCompareVillage, selectedName)
    ? state.biomassCompare
    : null;
  const card = document.createElement("div");
  card.className = "biomass-card";
  const bandNote = document.createElement("p");
  bandNote.className = "biomass-note";
  bandNote.textContent = "Compare whole villages year by year. Fields are not matched across years. Min/Max is ±15%.";
  card.append(bandNote);
  if (selected) {
    const heading = document.createElement("p");
    heading.className = "biomass-kicker";
    heading.textContent = `${selected.village} · village total by year`;
    const chartNote = document.createElement("p");
    chartNote.className = "biomass-note";
    chartNote.textContent = "Village biomass each year (all crops summed). Use t/ha when field counts differ.";
    card.append(
      heading,
      compare ? biomassYearCardsFromCompare(compare) : biomassYearCards(years.map((year) => [year, biomassYear(selected, year)])),
      chartNote,
      biomassFrame("biomass-chart biomass-chart-bars", "biomass-bars"),
    );
  }
  if (fieldRow) {
    const field = document.createElement("div");
    field.className = "biomass-field";
    const caption = document.createElement("span");
    caption.textContent = `Selected field only (not used for year compare) · ${fieldRow.crop} · ${fieldRow.year} · ${formatNumber(fieldRow.area_ha, 3)} ha`;
    field.append(caption, biomassRangeEnds(fieldRow.total_tonnes));
    card.append(field);
  }
  const districtTitle = document.createElement("p");
  districtTitle.className = "biomass-kicker";
  districtTitle.textContent = "All villages";
  card.append(districtTitle);
  if (!state.biomassPlaces) {
    const waiting = document.createElement("p");
    waiting.className = "biomass-note";
    waiting.textContent = "Loading village totals…";
    card.append(waiting);
  } else {
    card.append(biomassYearCards(years.map((year) => [year, biomassTotals(places, year)])));
    if (places.length) card.append(biomassVillageList(places, selectedName, years));
  }
  legend.replaceChildren(card);
  const active = card.querySelector(".biomass-villages .is-active");
  if (active) active.scrollIntoView({ block: "nearest" });
  if (selected) drawVillageYearly(selected, years);
  if (fieldRow) setStoreStatus(biomassSourceText(fieldRow.rate_source));
  else if (!selected) setStoreStatus("Choose a village to compare biomass year by year.");
  else setStoreStatus(`${selected.village}: village totals for ${years.join(", ")}.`);
}

function renderFieldBiomass(row) {
  renderVillageBiomass(row);
}

async function showFieldBiomass(feature, polygon) {
  if (state.analysisKind !== "biomass") markBiomassActive();
  if (selectedFieldLayer && selectedFieldLayer !== polygon) {
    selectedFieldLayer.setStyle(fieldStyle(selectedFieldLayer.feature || {}));
  }
  selectedFieldLayer = polygon;
  polygon.setStyle({ weight: 3, color: "#111827" });
  polygon.bringToFront();
  const props = (feature && feature.properties) || {};
  const fieldId = Number(props.field_id);
  let row = null;
  if (Number.isFinite(fieldId)) {
    const params = new URLSearchParams({ field_id: String(fieldId) });
    const village = props.village_name || selectedPlace().village;
    if (village && village !== ALL_VILLAGES) params.set("village", village);
    const year = Number(state.cropYear);
    if ((state.biomassYears || []).includes(String(year))) params.set("year", String(year));
    if (props.final_label) params.set("crop", props.final_label);
    try {
      const body = await readJson(`/biomass/field?${params.toString()}`);
      row = body && Array.isArray(body.fields) ? body.fields[0] : body;
    } catch (_error) {
      row = null;
    }
  }
  if (!state.biomassPlaces) await loadVillageBiomass();
  if (state.analysisKind !== "biomass") return;
  if (!row) {
    setStoreStatus("No biomass for this field. Village year compare still uses village totals.");
    renderVillageBiomass(null);
    return;
  }
  renderFieldBiomass(row);
  const band = biomassBand(row.total_tonnes);
  polygon.bindPopup(
    `<strong>${row.crop}</strong><br>${formatNumber(row.area_ha, 3)} ha<br>Min ${formatNumber(band.min, band.digits)} t<br>Max ${formatNumber(band.max, band.digits)} t<br><em>Year compare uses the whole village, not this field.</em>`,
  ).openPopup();
}

async function loadStoredAnalysis(kind) {
  if (kind === "biomass") {
    activateBiomass();
    return;
  }
  const panel = $("analysis-panel");
  if (panel) panel.classList.remove("is-biomass");
  destroyBiomassChart();
  const spec = ANALYSES[kind];
  if (!spec) return;
  const place = selectedPlace();
  if (!place.district) {
    showBanner("Choose a district first.");
    return;
  }
  if (state.analysisKind !== kind) {
    state.analysisClassKey = "";
    state.analysisLive = null;
  }
  state.analysisKind = kind;
  document.querySelectorAll("[data-analysis]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.analysis === kind);
  });
  const panelTitle = $("analysis-panel-title");
  if (panelTitle) panelTitle.textContent = "Class share";
  const requestId = ++state.analysisRequest;
  setStoreStatus(`Reading stored ${spec.title.toLowerCase()}…`);
  try {
    const loaded = await fetchStoredSeries(kind);
    if (requestId !== state.analysisRequest) return;
    state.analysisSeries = loaded.series;
    state.analysisScope = loaded.scope;
    fillAnalysisMonths();
    const label = $("analysis-plot");
    if (label) label.textContent = analysisScopeLabel(loaded.scope);
    if (!loaded.series.length) {
      if (analysisPieChart) analysisPieChart.destroy();
      analysisPieChart = null;
      const pie = $("analysis-pie");
      if (pie) pie.parentElement.classList.remove("is-ready");
      const legend = $("analysis-legend");
      if (legend) legend.replaceChildren();
      setStoreStatus(`No stored ${spec.title.toLowerCase()} rows for this place.`);
      hideAnalysisTile();
      return;
    }
    showStoredMonth();
  } catch (error) {
    if (requestId !== state.analysisRequest) return;
    setStoreStatus(error.message);
    showBanner(error.message);
  }
}

function setStoreStatus(message) {
  const node = $("analysis-store-status");
  if (node) node.textContent = message || "";
}

async function storeAnalysisMonth() {
  const kind = state.analysisKind;
  const spec = ANALYSES[kind];
  const endDate = $("analysis-date").value;
  const place = selectedPlace();
  if (!spec) {
    showBanner("Choose growth, water, soil, or pest before storing.");
    return;
  }
  if (!endDate) {
    showBanner("Choose an end date.");
    return;
  }
  if (!place.district) {
    showBanner("Choose a district first.");
    return;
  }
  const button = $("analysis-store");
  button.disabled = true;
  setStoreStatus(`Storing ${spec.title.toLowerCase()} for ${endDate}…`);
  try {
    const params = storeLocationParams(endDate);
    params.set("include_tiles", "true");
    const body = await readFresh(`${spec.path}?${params.toString()}`, { method: "POST" });
    state.analysisKind = kind;
    state.analysisLive = { kind, endDate, current: body.current || body };
    setStoreStatus(`Stored ${spec.title.toLowerCase()} for ${endDate.slice(0, 7)}.`);
    await loadStoredAnalysis(kind);
  } catch (error) {
    setStoreStatus(error.message);
    showBanner(error.message);
  } finally {
    button.disabled = false;
  }
}

async function pollBackfillJob(jobId) {
  if (state.analysisJobTimer) clearInterval(state.analysisJobTimer);
  let finished = false;
  const tick = async () => {
    try {
      const job = await readFresh(`/backfill-stored-data/daily/jobs/${jobId}`);
      const done = job.completed || 0;
      const total = job.total || 0;
      setStoreStatus(`Backfill ${job.status || "running"}: ${done} of ${total} places.`);
      if (job.status === "done" || job.status === "stopped" || job.status === "error") {
        finished = true;
        clearInterval(state.analysisJobTimer);
        state.analysisJobTimer = null;
        if (job.errors && job.errors.length) setStoreStatus(job.errors[job.errors.length - 1]);
        if (state.analysisKind) await loadStoredAnalysis(state.analysisKind);
      }
    } catch (error) {
      finished = true;
      clearInterval(state.analysisJobTimer);
      state.analysisJobTimer = null;
      setStoreStatus(error.message);
    }
  };
  await tick();
  if (finished) return;
  state.analysisJobTimer = setInterval(() => {
    tick().catch(() => {});
  }, 4000);
}

async function startAnalysisBackfill() {
  const place = selectedPlace();
  if (!place.district) {
    showBanner("Choose a district first.");
    return;
  }
  const button = $("analysis-backfill");
  button.disabled = true;
  setStoreStatus("Starting backfill…");
  try {
    const params = storeLocationParams();
    params.set("months", "14");
    const body = await readFresh(`/backfill-stored-data/daily?${params.toString()}`, { method: "POST" });
    setStoreStatus(`Backfill ${body.status} for this place.`);
    if (body.job_id) await pollBackfillJob(body.job_id);
  } catch (error) {
    setStoreStatus(error.message);
    showBanner(error.message);
  } finally {
    button.disabled = false;
  }
}

async function boot() {
  initCadastralMap();
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });
  $("tree-search").addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    renderTree();
  });
  $("plots-only").addEventListener("change", (event) => {
    state.plotsOnly = event.target.checked;
    renderTree();
  });
  $("place-district").addEventListener("change", () => {
    state.cropDistrict = $("place-district").value;
    const subdistricts = subdistrictsFor(state.cropDistrict);
    state.cropSubdistrict = subdistricts.find((name) => samePlaceName(name, "Kawardha")) || subdistricts[0] || "";
    state.cropVillage = ALL_VILLAGES;
    state.cropPage = 0;
    state.outlineKey = "";
    state.fieldKey = "";
    syncHierarchySelects();
    renderCropDetail();
    applyPlaceSelection().catch((error) => showBanner(error.message));
    if (state.analysisKind) loadStoredAnalysis(state.analysisKind).catch((error) => showBanner(error.message));
  });
  $("place-subdistrict").addEventListener("change", () => {
    state.cropSubdistrict = $("place-subdistrict").value;
    state.cropVillage = ALL_VILLAGES;
    state.cropPage = 0;
    state.outlineKey = "";
    state.fieldKey = "";
    syncHierarchySelects();
    renderCropDetail();
    applyPlaceSelection().catch((error) => showBanner(error.message));
    if (state.analysisKind) loadStoredAnalysis(state.analysisKind).catch((error) => showBanner(error.message));
  });
  $("place-village").addEventListener("change", () => {
    selectVillagePlace($("place-village").value || ALL_VILLAGES);
  });
  $("place-year").addEventListener("change", () => {
    state.cropYear = $("place-year").value;
    state.cropMonth = "";
    fillMonthSelect();
    changeCropPeriod().catch((error) => showBanner(error.message));
  });
  if ($("place-season")) {
    $("place-season").addEventListener("change", () => {
      changeCropPeriod().catch((error) => showBanner(error.message));
    });
  }
  if ($("place-month")) {
    $("place-month").addEventListener("change", () => {
      changeCropPeriod().catch((error) => showBanner(error.message));
    });
  }
  $("layer-villages").addEventListener("change", () => {
    syncLayerFlags();
    drawMap(false);
  });
  ["index-district", "index-subdistrict", "index-village", "index-frequency"].forEach((id) => {
    $(id).addEventListener("change", () => {
      state.indexPinned = true;
      state.indexFingerprint = null;
      if (id === "index-district") fillIndexSubdistricts();
      if (id === "index-subdistrict") fillIndexVillages();
      loadIndexTrend().catch((error) => showBanner(error.message));
    });
  });
  document.querySelectorAll("[data-analysis]").forEach((button) => {
    button.addEventListener("click", () => {
      state.analysisClassKey = "";
      if (button.dataset.analysis === "biomass") {
        activateBiomass();
        return;
      }
      loadStoredAnalysis(button.dataset.analysis).catch((error) => showBanner(error.message));
    });
  });

  try {
    const hierarchy = await readJson("/kawardha/hierarchy");
    state.hierarchy = hierarchy;
    state.context = { level: "district", node: hierarchy.district };
    state.selectedKey = "district";
    renderTree();
    renderContext();
    loadBoundary(null, null).catch(() => {});
    const hash = location.hash.replace("#", "");
    if (hash.startsWith("village=")) {
      const villageId = Number(hash.slice("village=".length));
      let found = null;
      hierarchy.talukas.some((taluka) => taluka.ris.some((ri) => ri.villages.some((village) => {
        if (village.id === villageId) {
          found = { village, ri, taluka };
          return true;
        }
        return false;
      })));
      if (found) selectVillage(found.village, found.ri, found.taluka);
    } else if (hash === "indices") {
      switchView("indices");
    } else if (hash !== "cadastral") {
      switchView("crops");
    }
  } catch (error) {
    showBanner(error.message);
  }
}

boot();
