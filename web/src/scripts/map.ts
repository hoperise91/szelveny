// The map page: MapLibre + PMTiles, a sidebar to search and browse by county, and a road panel.
import * as maplibregl from "maplibre-gl";
import type { ExpressionSpecification, GeoJSONSource, LngLatLike } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import type { FeatureCollection } from "geojson";
import { Protocol } from "pmtiles";
import { countiesSorted, countyName } from "../lib/counties";
import { MAPLIBRE_RO, type Strings } from "../lib/i18n";
import {
  CATEGORY_COLOR, CATEGORY_ORDER, DATA_URL, QUALITY_COLOR, SURFACE_COLOR, type IndexRow,
  type LonLat, type Quality, type Road, fetchIndex, fetchRoad, fold, formatKm, formatLength,
  locate, measure, parseQuery, qualityShares, fetchSources, surfaceClass, parseCurve, type Event,
  roadHref,
} from "../lib/roads";
import { liveStrip, stripBands } from "../lib/strip";
import {
  COLOUR_MODES, DATA_GROUPS, DATA_TYPES, DEFAULT_DATA, OBJECT_COLOR, type DataId, type Mode,
} from "../lib/datatypes";

const OFM = "https://tiles.openfreemap.org/styles/";
const GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"; // for road labels on raster maps
const ROAD_TYPES = ["A", "DX", "DN", "V", "DJ", "DC", "DP"];

// Background maps: free, no keys. Raster ones get a minimal style.
const MAPTILER_KEY: string | undefined = import.meta.env.PUBLIC_MAPTILER_KEY;
const BASEMAPS: { id: string; maptiler?: boolean;
  style: string | (() => string | maplibregl.StyleSpecification) }[] = [
  { id: "light", style: `${OFM}positron` },
  { id: "dark", style: `${OFM}dark` },
  { id: "detail", style: `${OFM}liberty` },
  // Terrain and satellite come from MapTiler when a key is configured (sharp and fast), else
  // from free fallbacks (OpenTopoMap is slow; Sentinel-2 imagery is only 10 m per pixel).
  { id: "topo", maptiler: true, style: () => MAPTILER_KEY
    ? `https://api.maptiler.com/maps/topo-v2/style.json?key=${MAPTILER_KEY}`
    : raster("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      "Map data © OpenStreetMap contributors, SRTM · Map style © OpenTopoMap (CC-BY-SA)", 17, ["a", "b", "c"]) },
  { id: "satellite", maptiler: true, style: () => MAPTILER_KEY
    ? raster(`https://api.maptiler.com/tiles/satellite-v2/{z}/{x}/{y}.jpg?key=${MAPTILER_KEY}`,
      "© MapTiler © OpenStreetMap contributors", 20, undefined, 512)
    : raster("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
      "Sentinel-2 cloudless – s2maps.eu by EOX IT Services GmbH (contains modified Copernicus Sentinel data 2016), CC BY 4.0", 14) },
  { id: "none", style: () => ({
    version: 8, glyphs: GLYPHS, sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color":
      getComputedStyle(document.documentElement).getPropertyValue("--paper").trim() || "#f6f3ec" } }],
  }) },
];

function raster(url: string, attribution: string, maxzoom: number, subdomains?: string[],
  tileSize = 256): maplibregl.StyleSpecification {
  const tiles = subdomains ? subdomains.map((d) => url.replace("{s}", d)) : [url];
  return {
    version: 8,
    glyphs: GLYPHS,
    sources: { bg: { type: "raster", tiles, tileSize, maxzoom, attribution } },
    layers: [{ id: "bg", type: "raster", source: "bg" }],
  };
}

function basemapStyle(id: string): string | maplibregl.StyleSpecification {
  const b = BASEMAPS.find((x) => x.id === id) ?? BASEMAPS[0];
  return typeof b.style === "string" ? b.style : b.style();
}
const RO_BOUNDS: [[number, number], [number, number]] = [[20.2, 43.6], [29.75, 48.3]];

// Administrator by road category (OG 43/1997): national roads -> the state (CNAIR), county
// roads -> county council, communal roads -> local council, DP -> private owner.
export const OWNER_OF: Record<string, string> = { A: "state", DX: "state", DN: "state", V: "state", DJ: "county", DC: "commune", DP: "private" };
export const OWNER_COLOR: Record<string, string> = { state: "#7b2cbf", county: "#f28c28", commune: "#2a9d8f", private: "#6b6b6b" };
// Carriageway width classes (m), NVDB-style; estimated width = lanes x a standard lane width.
const WIDTH_STEPS: [number, string][] = [[0, "#d7191c"], [5, "#fdae61"], [6, "#fee08b"], [7, "#a6d96a"], [9, "#2c7bb6"], [12, "#253494"]];
const LANE_WIDTH: ExpressionSpecification = ["match", ["get", "t"], ["A", "DX", "DN", "V"], 3.5, 3.0];

const esc = (v: unknown) => String(v ?? "").replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
const shield = (ref: string, cat: string, lg = false) =>
  `<span class="shield${lg ? " lg" : ""}" data-cat="${esc(cat)}">${esc(ref)}</span>`;

export function initMap(root: HTMLElement) {
  const s: Strings = JSON.parse(root.dataset.strings!);
  const lang = root.dataset.lang!;
  const roadUrl = root.dataset.roadUrl!;
  const view = root.querySelector<HTMLElement>("#view")!;
  const sidebar = view;
  const homeHTML = view.innerHTML;
  const input = root.querySelector<HTMLInputElement>("#q")!;
  const clearBtn = root.querySelector<HTMLButtonElement>("#q-clear")!;
  const legend = root.querySelector<HTMLElement>("#legend")!;
  const cat = (c: string) => (s as Record<string, string>)[`cat_${c}`] ?? c;
  const qual = (q: string) => (s as Record<string, string>)[`q_${q}`] ?? q;

  let index: IndexRow[] = [];
  let mode: Mode = "category";
  let selected: string | null = null;
  let stopStrip: (() => void) | null = null;
  // Sidebar views and their history for "Back".
  type View = { kind: "home" | "county" | "results" | "road"; render: () => void; county?: string };
  const homeView: View = { kind: "home", render: renderHome };
  const countyView = (code: string, filter: string): View =>
    ({ kind: "county", county: code, render: () => renderCounty(code, filter) });
  const stack: View[] = [];
  let current: View = homeView;
  let roadCounty: string | null = null; // county of the open road (at the chosen km), for "Back"
  let keepView = false; // set while deselecting by clicking the map: lists don't move the map

  // ---- map --------------------------------------------------------------------------
  maplibregl.setWorkerUrl(workerUrl); // bundled by Vite (an ES-module worker)
  const protocol = new Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  const stored = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };
  const store = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };

  let basemap = BASEMAPS.find((b) => b.id === stored("borna.basemap"))?.id ?? "light";
  // v2: expressways (DX) added; older saved choices didn't know them
  let visibleCats = new Set<string>(JSON.parse(stored("borna.cats.v2") ?? "null") ?? ROAD_TYPES);
  type FC = FeatureCollection;
  let selData: FC = { type: "FeatureCollection", features: [] };

  const map = new maplibregl.Map({
    locale: lang === "ro" ? MAPLIBRE_RO : undefined,
    container: root.querySelector<HTMLElement>("#map")!,
    style: basemapStyle(basemap),
    bounds: RO_BOUNDS,
    fitBoundsOptions: { padding: 24 },
    attributionControl: {
      compact: true,
      customAttribution: "© OpenStreetMap contributors (ODbL) · HG 540/2000 · INS · data.gov.ro · Bornă",
    },
  });
  if (import.meta.env.DEV) (window as unknown as { bornaMap: unknown }).bornaMap = map; // for testing
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");

  // ---- phones: the sidebar is a bottom sheet; the legend folds -------------------------------
  const phone = window.matchMedia("(max-width: 860px)");
  const side = root.querySelector<HTMLElement>(".side")!;
  const sheetHandle = root.querySelector<HTMLButtonElement>("#sheet-handle")!;
  let sheetOpen = false;
  function setSheet(open: boolean) {
    sheetOpen = open;
    root.classList.toggle("sheet-open", open);
    sheetHandle.setAttribute("aria-expanded", String(open));
    const peek = parseFloat(getComputedStyle(root).getPropertyValue("--sheet-peek")) || 0;
    const covered = phone.matches ? (open ? side.offsetHeight : peek) : 0;
    root.style.setProperty("--sheet-visible", `${covered}px`);
  }
  // Room around a fitted road; on phones the open sheet covers the bottom, so keep the road above
  // it (showing a road always opens the sheet). Camera padding would interrupt running animations.
  const fitPad = (p: number) => ({ top: p, left: p, right: p, bottom: p + (phone.matches ? side.offsetHeight : 0) });
  sheetHandle.addEventListener("click", () => setSheet(!sheetOpen));
  input.addEventListener("focus", () => { if (phone.matches) setSheet(true); });
  // A list or a road opened (from the map, search or a list) slides the sheet up; home doesn't.
  new MutationObserver(() => {
    if (phone.matches && !view.querySelector('[data-view="home"]')) setSheet(true);
  }).observe(view, { childList: true });
  phone.addEventListener("change", () => setSheet(sheetOpen));
  setSheet(false);

  const legendBox = root.querySelector<HTMLElement>("#legend-box")!;
  const legendHead = root.querySelector<HTMLButtonElement>("#legend-head")!;
  function setLegend(open: boolean) {
    legendBox.classList.toggle("folded", !open);
    legendHead.setAttribute("aria-expanded", String(open));
  }
  // Folded by default on phones, open on larger screens; the visitor's choice is remembered.
  setLegend((stored("borna.legend") ?? (phone.matches ? "folded" : "open")) === "open");
  legendHead.addEventListener("click", () => {
    const open = legendBox.classList.contains("folded");
    setLegend(open);
    store("borna.legend", open ? "open" : "folded");
  });
  map.addControl(layersControl(), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

  const pin = new maplibregl.Marker({ element: Object.assign(document.createElement("div"), { className: "km-pin" }) });
  const pinPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 16 });
  const hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12, className: "hover-pop" });

  // Line width by category and zoom, times k plus `add` px (zoom must stay the top-level input).
  const width = (k = 1, add = 0): ExpressionSpecification => [
    "interpolate", ["exponential", 1.6], ["zoom"],
    5, ["+", add, ["*", k, ["match", ["get", "t"], ["A", "DX"], 2.2, "DN", 1.6, "V", 1.1, "DJ", 0.8, 0.6]]],
    10, ["+", add, ["*", k, ["match", ["get", "t"], ["A", "DX"], 4, "DN", 3.2, "V", 2.6, "DJ", 2.2, 1.4]]],
    14, ["+", add, ["*", k, ["match", ["get", "t"], ["A", "DX"], 8, "DN", 6.5, "V", 5, "DJ", 4.5, 3]]],
  ];
  const catColor: ExpressionSpecification = [
    "match", ["get", "t"], ...Object.entries(CATEGORY_COLOR).flat(), "#7c7c7c",
  ] as unknown as ExpressionSpecification;
  const qualityColor: ExpressionSpecification = [
    "match", ["get", "q"], ...Object.entries(QUALITY_COLOR).flat(), "#999",
  ] as unknown as ExpressionSpecification;
  const typeFilter = (): ExpressionSpecification => ["in", ["get", "t"], ["literal", [...visibleCats]]];
  const ownerColor = ["match", ["get", "t"],
    ["A", "DX", "DN", "V"], OWNER_COLOR.state, "DJ", OWNER_COLOR.county, "DC", OWNER_COLOR.commune,
    OWNER_COLOR.private] as unknown as ExpressionSpecification;
  const widthColor = (metres: ExpressionSpecification): ExpressionSpecification =>
    ["step", metres, WIDTH_STEPS[0][1], ...WIDTH_STEPS.slice(1).flat()] as unknown as ExpressionSpecification;
  // Labels by zoom: motorways first, then national, county, communal roads.
  const labelFilter = (): ExpressionSpecification => {
    const tf = typeFilter();
    const only = (cats: string[]): ExpressionSpecification => ["all", tf, ["in", ["get", "t"], ["literal", cats]]];
    return ["step", ["zoom"], only(["A", "DX"]), 6, only(["A", "DX", "DN"]), 8,
      only(["A", "DX", "DN", "V", "DJ"]), 11, tf] as unknown as ExpressionSpecification;
  };
  let showLabels = stored("borna.labels") !== "0";
  let showConstruction = stored("borna.construction") !== "0";
  // Precision details (km quality, reference points, mapped length) are for the maintainer only:
  // ?detalii=1 turns them on in this browser (remembered), ?detalii=0 off.
  const detailsParam = new URLSearchParams(location.search).get("detalii");
  if (detailsParam != null) store("borna.details", detailsParam === "1" ? "1" : "0");
  const details = stored("borna.details") === "1";
  // Data types shown in the road panel (ticked in the data list).
  // v2: E-roads and point objects added; older saved choices didn't know them
  let shownData = new Set<DataId>(JSON.parse(stored("borna.data.v2") ?? "null") ?? DEFAULT_DATA);
  // Source versions, for "source · date" next to each value.
  const sourceVersion: Record<string, string> = {};
  fetchSources().then((list) => list.forEach((x) => { sourceVersion[x.id] = x.version; })).catch(() => {});
  let hoverKey = "";
  const ROAD_LAYERS = ["roads-casing", "roads", "surface", "maxspeed", "aadt", "lanes", "width", "construction"];

  // Bornă's sources and layers. Runs on every style load: switching the basemap replaces the
  // style, so the overlays (and the current mode, road types and selection) are added again.
  function addOverlays() {
    if (map.getSource("borna")) return;
    // Below the basemap's labels but above all its lines and fills: the first symbol layer
    // after the last non-symbol layer (some styles put a few labels early).
    const layers = map.getStyle().layers;
    const lastShape = layers.map((l) => l.type !== "symbol").lastIndexOf(true);
    const before = layers.slice(lastShape + 1).find((l) => l.type === "symbol")?.id;
    map.addSource("borna", {
      type: "vector",
      url: `pmtiles://${new URL(`${DATA_URL}/borna.pmtiles`, location.href).href}`,
    });
    map.addSource("sel", { type: "geojson", data: selData });
    const line = { "line-join": "round", "line-cap": "round" } as const;
    const dark = darkBackground();

    map.addLayer({ id: "roads-casing", type: "line", source: "borna", "source-layer": "roads", layout: line,
      paint: { "line-color": dark ? "#111111" : "#ffffff", "line-width": width(1, 2), "line-opacity": dark ? 0.6 : 0.85 } }, before);
    map.addLayer({ id: "road-hover", type: "line", source: "borna", "source-layer": "roads", layout: line,
      filter: ["==", ["get", "k"], ""],
      paint: { "line-color": dark ? "#ffffff" : "#1d1c1a", "line-opacity": 0.3, "line-width": width(1, 7) } }, before);
    map.addLayer({ id: "sel-halo", type: "line", source: "sel", layout: line,
      paint: { "line-color": "#ffcf33", "line-width": ["interpolate", ["linear"], ["zoom"], 5, 9, 14, 18],
        "line-opacity": 0.9, "line-blur": 1 } }, before);
    map.addLayer({ id: "roads", type: "line", source: "borna", "source-layer": "roads", layout: line,
      paint: { "line-color": catColor, "line-width": width() } }, before);
    // Motorways, expressways and national roads under construction: dashed, in their colour.
    map.addLayer({ id: "construction", type: "line", source: "borna", "source-layer": "construction",
      layout: { "line-join": "round", visibility: showConstruction ? "visible" : "none" },
      paint: { "line-color": catColor, "line-width": width(0.85), "line-opacity": 0.7,
        "line-dasharray": [2, 2] } }, before);
    map.addLayer({ id: "surface", type: "line", source: "borna", "source-layer": "surface", layout: { ...line, visibility: "none" },
      paint: { "line-width": width(1.15), "line-color": [
        "match", ["get", "v"], ...Object.entries(SURFACE_COLOR).flat(), "#a3a3a3"] as unknown as ExpressionSpecification } }, before);
    map.addLayer({ id: "maxspeed", type: "line", source: "borna", "source-layer": "maxspeed", layout: { ...line, visibility: "none" },
      paint: { "line-width": width(1.15), "line-color": [
        "case", [">", ["to-number", ["get", "v"], 0], 0],
        ["step", ["to-number", ["get", "v"], 0], "#b2182b", 31, "#ef8a62", 51, "#f4c95d", 71, "#7fbf7b", 91, "#2c9c5a", 111, "#00563b"],
        "#a3a3a3"] } }, before);
    map.addLayer({ id: "aadt", type: "line", source: "borna", "source-layer": "aadt", layout: { ...line, visibility: "none" },
      paint: {
        "line-color": ["step", ["get", "v"], "#cbc9e2", 10000, "#9e9ac8", 20000, "#6a51a3", 40000, "#3f007d"],
        "line-width": ["interpolate", ["linear"], ["zoom"],
          5, ["interpolate", ["linear"], ["get", "v"], 3000, 1.5, 20000, 3.5, 75000, 8],
          12, ["interpolate", ["linear"], ["get", "v"], 3000, 4, 20000, 8, 75000, 16]],
      } }, before);
    map.addLayer({ id: "lanes", type: "line", source: "borna", "source-layer": "lanes", layout: { ...line, visibility: "none" },
      paint: { "line-width": width(1.1), "line-opacity": 0.75,
        "line-color": widthColor(["*", ["to-number", ["get", "v"], 0], LANE_WIDTH]) } }, before);
    map.addLayer({ id: "width", type: "line", source: "borna", "source-layer": "width", layout: { ...line, visibility: "none" },
      paint: { "line-width": width(1.5, 1), "line-color": widthColor(["to-number", ["get", "v"], 0]) } }, before);
    map.addLayer({ id: "sel-line", type: "line", source: "sel", layout: line,
      paint: { "line-color": ["get", "color"], "line-width": ["interpolate", ["linear"], ["zoom"], 5, 3, 14, 8] } }, before);
    for (const c of Object.keys(CATEGORY_COLOR)) {
      if (!map.hasImage(`shield-${c}`)) map.addImage(`shield-${c}`, shieldImage(CATEGORY_COLOR[c]),
        { pixelRatio: 2, stretchX: [[10, 38]], stretchY: [[10, 30]], content: [6, 5, 42, 35] });
    }
    map.addLayer({ id: "road-labels", type: "symbol", source: "borna", "source-layer": "roads",
      filter: labelFilter(),
      layout: {
        visibility: showLabels ? "visible" : "none",
        "symbol-placement": "line", "symbol-spacing": 320,
        "symbol-sort-key": ["match", ["get", "t"], ["A", "DX"], 0, "DN", 1, "V", 2, "DJ", 3, "DC", 4, 5],
        "text-field": ["get", "r"], "text-font": ["Noto Sans Bold"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 6, 10, 12, 12],
        "text-rotation-alignment": "viewport", "icon-rotation-alignment": "viewport",
        "icon-image": ["concat", "shield-", ["get", "t"]],
        "icon-text-fit": "both", "icon-text-fit-padding": [1, 4, 1, 4], "text-padding": 6,
      },
      paint: { "text-color": "#ffffff" } });
    // Ferry crossings: dashed lines over water, shown with the ferry terminals.
    if (!map.getLayer("ferries")) map.addLayer({ id: "ferries", type: "line", source: "borna", "source-layer": "ferries",
      layout: { "line-join": "round", visibility: shownData.has("obj_ferry") ? "visible" : "none" },
      paint: { "line-color": OBJECT_COLOR.ferry, "line-width": 2, "line-dasharray": [2, 1.5] } });
    // Point objects (level crossings, interchanges, borders, tolls): dots, ticked in the data list.
    map.addLayer({ id: "objects", type: "circle", source: "borna", "source-layer": "objects",
      paint: {
        "circle-color": ["match", ["get", "y"], ...Object.entries(OBJECT_COLOR).flat(), "#666"] as unknown as ExpressionSpecification,
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 3, 12, 5, 15, 7],
        "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
      } });
    applyMode();
    applyRoadTypes();
    applyObjects();
  }

  function applyObjects() {
    if (!map.getLayer("objects")) return;
    const types = DATA_TYPES.filter((d) => d.object && shownData.has(d.id)).map((d) => d.object!);
    map.setFilter("objects", ["in", ["get", "y"], ["literal", types]]);
    if (map.getLayer("ferries")) map.setLayoutProperty("ferries", "visibility", types.includes("ferry") ? "visible" : "none");
  }

  // "Nod rutier 5 · Bărcănești", "Punct de trecere a frontierei · Borș"
  function objectLabel(type: string, name?: string | null, ref?: string | null): string {
    return [T[`one_${type}`] ?? type, [ref, name].filter(Boolean).join(" ")].filter(Boolean).join(" · ");
  }
  map.on("style.load", addOverlays);

  // Rounded route-shield background for labels, stretchable around the text.
  function shieldImage(color: string): ImageData {
    const c = document.createElement("canvas");
    c.width = 48; c.height = 40;
    const g = c.getContext("2d")!;
    g.fillStyle = color;
    g.strokeStyle = "rgba(255,255,255,.95)";
    g.lineWidth = 3;
    g.beginPath();
    g.roundRect(2, 2, 44, 36, 8);
    g.fill();
    g.stroke();
    return g.getImageData(0, 0, 48, 40);
  }

  // Satellite and dark maps (and "no map" on a dark page) need dark road casings.
  function darkBackground() {
    return basemap === "satellite" || basemap === "dark"
      || (basemap === "none" && matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function setSel(fc: FC) {
    selData = fc;
    (map.getSource("sel") as GeoJSONSource | undefined)?.setData(fc);
  }

  // hover (layer-scoped listeners survive style changes)
  // Snapping cursor: near a road (SNAP_PX) the cursor becomes a ring on the nearest point of the
  // road, and hover value, highlight and click all use that point: easier than aiming a hand.
  const coarse = matchMedia("(pointer: coarse)").matches;
  const SNAP_PX = coarse ? 18 : 12;
  type Snap = { f: maplibregl.MapGeoJSONFeature; pt: maplibregl.Point; lngLat: maplibregl.LngLat; d: number };
  const around = (p: maplibregl.Point, r: number): [maplibregl.PointLike, maplibregl.PointLike] =>
    [[p.x - r, p.y - r], [p.x + r, p.y + r]];
  function snapTo(p: maplibregl.Point, r = SNAP_PX): Snap | null {
    if (!map.getLayer("roads")) return null;
    let best: Snap | null = null;
    const rank = (f: maplibregl.MapGeoJSONFeature) => CATEGORY_ORDER.indexOf(f.properties.t);
    for (const f of map.queryRenderedFeatures(around(p, r), { layers: ["roads"] })) {
      const g = f.geometry;
      const lines = g.type === "LineString" ? [g.coordinates] : g.type === "MultiLineString" ? g.coordinates : [];
      for (const line of lines) {
        let a = map.project(line[0] as LngLatLike);
        for (let i = 1; i < line.length; i++) {
          const b = map.project(line[i] as LngLatLike);
          const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy;
          const t = len2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) : 0;
          const q = new maplibregl.Point(a.x + t * dx, a.y + t * dy);
          const d = q.dist(p);
          // nearest wins; within 1.5 px of each other the more important road wins (junctions)
          if (!best || d < best.d - 1.5 || (d <= best.d + 1.5 && rank(f) < rank(best.f))) {
            best = { f, pt: q, lngLat: map.unproject(q), d };
          }
          a = b;
        }
      }
    }
    return best && best.d <= r ? best : null;
  }
  const ringEl = document.createElement("div");
  ringEl.className = "snap-ring";
  const snapRing = new maplibregl.Marker({ element: ringEl });
  function clearHover() {
    hoverKey = "";
    if (map.getLayer("road-hover")) map.setFilter("road-hover", ["==", ["get", "k"], ""]);
    hoverPopup.remove();
    snapRing.remove();
    map.getCanvas().style.cursor = "";
  }
  function onHover(p: maplibregl.Point) {
    if (!map.getLayer("roads")) return;
    const obj = map.getLayer("objects") ? map.queryRenderedFeatures(around(p, 6), { layers: ["objects"] })[0] : undefined;
    if (obj) { // an object dot under the cursor comes first
      clearHover();
      map.getCanvas().style.cursor = "pointer";
      hoverPopup.setLngLat(map.unproject(p)).setHTML(`<span class="hv">${esc(objectLabel(
        obj.properties.y, obj.properties.n, obj.properties.x))}</span>`).addTo(map);
      return;
    }
    const sn = snapTo(p);
    if (sn) {
      map.getCanvas().style.cursor = "none";
      snapRing.setLngLat(sn.lngLat).addTo(map);
      hoverKey = sn.f.properties.k as string;
      map.setFilter("road-hover", ["==", ["get", "k"], hoverKey]);
      const v = hoverValue(sn.pt, sn.f.properties.t as string);
      hoverPopup.setLngLat(sn.lngLat)
        .setHTML(v ? `<span class="hv">${esc(v)}</span>` : `<span class="hv muted">${esc(s.unknown)}</span>`)
        .addTo(map);
      return;
    }
    const con = map.getLayer("construction") ? map.queryRenderedFeatures(around(p, 4), { layers: ["construction"] })[0] : undefined;
    if (con) {
      clearHover();
      const o = con.properties.o ? ` · ${esc(s.opening)} ${esc(con.properties.o)}` : "";
      hoverPopup.setLngLat(map.unproject(p))
        .setHTML(`<span class="hv">${esc(s.under_construction)}${o}</span>`).addTo(map);
      return;
    }
    clearHover();
  }
  let hoverFrame = 0;
  let hoverAt: maplibregl.Point | null = null;
  map.on("mousemove", (e) => {
    if ((e.originalEvent as MouseEvent).buttons) { clearHover(); return; } // panning
    hoverAt = e.point;
    if (!hoverFrame) hoverFrame = requestAnimationFrame(() => { hoverFrame = 0; if (hoverAt) onHover(hoverAt); });
  });
  map.getCanvasContainer().addEventListener("mouseleave", () => { hoverAt = null; clearHover(); });
  map.on("movestart", () => snapRing.remove());
  // Hover shows only the value the map is coloured by at that spot, not the road's name: its label
  // is on the map anyway (owner's choice). Values come from the tiles' attribute layers.
  function hoverValue(pt: maplibregl.Point, t: string): string | null {
    const T = s as Record<string, string>;
    const locale = lang === "ro" ? "ro-RO" : "en-GB";
    const num = (x: number, d = 1) => x.toLocaleString(locale, { maximumFractionDigits: d });
    if (mode === "category") return cat(t);
    if (mode === "owner") return T[`owner_${["A", "DX", "DN", "V"].includes(t) ? "state"
      : t === "DJ" ? "county" : t === "DC" ? "commune" : "private"}`];
    const box: [maplibregl.PointLike, maplibregl.PointLike] = [[pt.x - 4, pt.y - 4], [pt.x + 4, pt.y + 4]];
    const at = (layer: string) => map.getLayer(layer)
      ? map.queryRenderedFeatures(box, { layers: [layer] })[0]?.properties.v : undefined;
    if (mode === "surface") { const v = at("surface"); return v != null ? T[`s_${v}`] ?? String(v) : null; }
    if (mode === "maxspeed") { const v = at("maxspeed"); return v == null ? null : /^\d+$/.test(String(v)) ? `${v} km/h` : String(v); }
    if (mode === "aadt") { const v = at("aadt"); return v != null ? `${num(Number(v), 0)} ${s.vehicles_day}` : null; }
    if (mode === "width") {
      const w = at("width");
      if (w != null) return `${num(Number(w))} m`;
      const l = at("lanes");  // estimate: lanes × 3.5 m (A, DX, DN, V) or 3.0 m (DJ, DC)
      return l != null ? `≈ ${num(Number(l) * (["A", "DX", "DN", "V"].includes(t) ? 3.5 : 3.0))} m · ${l} ${s.lanes.toLowerCase()}` : null;
    }
    return null;
  }
  // click: the road the cursor snaps to (an object dot first), at the snapped point
  map.on("click", (e) => {
    if (!map.getLayer("roads")) return;
    const obj = map.getLayer("objects") ? map.queryRenderedFeatures(around(e.point, 6), { layers: ["objects"] })[0] : undefined;
    if (obj) { // an object opens its road at its km
      hoverPopup.remove();
      selectRoad(obj.properties.k as string, { km: Number(obj.properties.m), push: true });
      return;
    }
    const sn = snapTo(e.point);
    if (!sn) { // empty map: deselect
      if (current.kind === "road") back(true); else if (selected) clearSelection();
      return;
    }
    hoverPopup.remove();
    selectRoad(sn.f.properties.k as string, { at: [sn.lngLat.lng, sn.lngLat.lat], push: true });
  });
  map.once("load", () => {
    root.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
    restoreFromUrl();
  });

  // ---- layers panel: background map + road types ----------------------------------------
  const panel = root.querySelector<HTMLElement>("#layers-panel")!;
  const T = s as Record<string, string>;
  function renderPanel() {
    panel.innerHTML = `
      <h3>${esc(s.data_list)}</h3>
      <p class="small muted" style="margin:-4px 0 6px">${esc(s.data_hint)}</p>
      <div class="data-list">${DATA_GROUPS.map((g) => `
        <div class="data-grp">${esc(T[g.label])}</div>${g.items.map((d) => `
        <div class="data-row">
          <label><input type="checkbox" data-show="${d.id}" ${shownData.has(d.id) ? "checked" : ""}/>${d.object
            ? `<span class="dot" style="background:${OBJECT_COLOR[d.object]}"></span>` : ""}${esc(T[d.label])}</label>
          ${d.colour ? `<input type="radio" name="colour" class="colour-pick" data-colour="${d.colour}"
            aria-label="${esc(s.colour_by_long)}: ${esc(T[d.label])}" ${mode === d.colour ? "checked" : ""}/>` : ""}
        </div>`).join("")}`).join("")}
      </div>
      <h3>${esc(s.basemap)}</h3>
      <div class="bm-grid">${BASEMAPS.map((b) => `
        <button type="button" class="bm" data-bm="${b.id}" aria-pressed="${b.id === basemap}">
          <span class="bm-thumb" data-thumb="${b.id}"></span><span>${esc(T[`bm_${b.id}`])}</span></button>`).join("")}
      </div>
      <h3>${esc(s.road_types)}</h3>
      <div class="rt-list">${ROAD_TYPES.map((c) => `
        <label class="rt"><input type="checkbox" data-cat="${c}" ${visibleCats.has(c) ? "checked" : ""}/>
          <span class="rt-line" style="background:${CATEGORY_COLOR[c]}"></span>
          <span class="shield" data-cat="${c}">${c}</span><span>${esc(cat(c))}</span></label>`).join("")}
      </div>
      <div class="rt-quick">
        <button type="button" class="chip" data-quick="all">${esc(s.show_all)}</button>
        <button type="button" class="chip" data-quick="main">${esc(s.only_main)}</button>
      </div>
      <label class="rt labels-opt"><input type="checkbox" data-construction ${showConstruction ? "checked" : ""}/>
        <span class="rt-line rt-dash"></span><span>${esc(s.show_construction)}</span></label>
      <label class="rt"><input type="checkbox" data-labels ${showLabels ? "checked" : ""}/>
        <span class="shield" data-cat="DN">DN1</span><span>${esc(s.show_labels)}</span></label>`;
  }
  renderPanel();
  panel.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const bm = t.closest<HTMLElement>("[data-bm]");
    if (bm) return setBasemap(bm.dataset.bm!);
    const q = t.closest<HTMLElement>("[data-quick]");
    if (q) {
      visibleCats = new Set(q.dataset.quick === "main" ? ["A", "DX", "DN", "V"] : ROAD_TYPES);
      renderPanel();
      applyRoadTypes();
    }
  });
  panel.addEventListener("change", (e) => {
    const box = e.target as HTMLInputElement;
    if (box.dataset.show) {
      const id = box.dataset.show as DataId;
      if (box.checked) shownData.add(id); else shownData.delete(id);
      store("borna.data.v2", JSON.stringify([...shownData]));
      applyObjects();
      if (current.kind === "road") { stopStrip?.(); stopStrip = null; current.render(); } // open road follows
      return;
    }
    if (box.dataset.colour) { setMode(box.dataset.colour as Mode); return; }
    if (box.hasAttribute("data-construction")) {
      showConstruction = box.checked;
      store("borna.construction", showConstruction ? "1" : "0");
      if (map.getLayer("construction")) map.setLayoutProperty("construction", "visibility", showConstruction ? "visible" : "none");
      applyMode();
      return;
    }
    if (box.hasAttribute("data-labels")) {
      showLabels = box.checked;
      store("borna.labels", showLabels ? "1" : "0");
      if (map.getLayer("road-labels")) map.setLayoutProperty("road-labels", "visibility", showLabels ? "visible" : "none");
      return;
    }
    if (!box.dataset.cat) return;
    if (box.checked) visibleCats.add(box.dataset.cat); else visibleCats.delete(box.dataset.cat);
    applyRoadTypes();
  });
  document.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    if (!panel.hidden && !panel.contains(t) && !t.closest(".layers-btn")) togglePanel(false);
  });

  function togglePanel(open = panel.hidden) {
    panel.hidden = !open;
    root.querySelector(".layers-btn")?.setAttribute("aria-expanded", String(open));
  }

  function setBasemap(id: string) {
    if (id === basemap) return;
    basemap = id;
    store("borna.basemap", id);
    renderPanel();
    loading(true);
    map.setStyle(basemapStyle(id)); // style.load -> addOverlays()
    showMaptilerLogo();
  }

  // MapTiler's free plan requires its logo while one of its maps is shown.
  const mtLogo = root.querySelector<HTMLElement>("#maptiler-logo")!;
  function showMaptilerLogo() {
    mtLogo.hidden = !(MAPTILER_KEY && BASEMAPS.find((b) => b.id === basemap)?.maptiler);
  }
  showMaptilerLogo();

  // "Loading map…" until the map has drawn everything (some basemap servers are slow).
  const loadingEl = root.querySelector<HTMLElement>("#map-loading")!;
  function loading(on: boolean) {
    loadingEl.classList.toggle("done", !on);
    if (on) map.once("idle", () => loading(false));
  }
  loading(true);

  function applyRoadTypes() {
    store("borna.cats.v2", JSON.stringify([...visibleCats]));
    if (!map.getLayer("roads")) return;
    for (const id of ROAD_LAYERS) map.setFilter(id, typeFilter());
    map.setFilter("road-labels", labelFilter());
    applyMode();
  }

  // ---- layer modes & legend -----------------------------------------------------------
  // One compact "Colour: …" choice instead of a row of tabs (the data list also sets it).
  const colourBtn = root.querySelector<HTMLButtonElement>("#colour-btn")!;
  const colourMenu = root.querySelector<HTMLElement>("#colour-menu")!;
  const showColourMenu = (open: boolean) => {
    colourMenu.hidden = !open;
    colourBtn.setAttribute("aria-expanded", String(open));
  };
  colourBtn.addEventListener("click", (e) => { e.stopPropagation(); showColourMenu(colourMenu.hasAttribute("hidden")); });
  document.addEventListener("click", (e) => {
    if (!colourMenu.hidden && !colourMenu.contains(e.target as Node)) showColourMenu(false);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") showColourMenu(false); });
  colourMenu.querySelectorAll<HTMLButtonElement>("button").forEach((b) =>
    b.addEventListener("click", () => { setMode(b.dataset.layer as Mode); showColourMenu(false); }));
  function setMode(m: Mode) {
    mode = m;
    colourMenu.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x.getAttribute("data-layer") === m)));
    const label = COLOUR_MODES.find((d) => d.colour === m)!.label;
    root.querySelector("#colour-current")!.textContent = T[label];
    applyMode();
    if (!panel.hidden) renderPanel();
  }

  legend.addEventListener("click", (e) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>("[data-toggle]");
    if (!row) return;
    const c = row.dataset.toggle!;
    if (visibleCats.has(c)) visibleCats.delete(c); else visibleCats.add(c);
    renderPanel();
    applyRoadTypes();
  });

  function applyMode() {
    if (!map.getLayer("roads")) return;
    const neutral = darkBackground() ? "#9a958a" : "#cdc6b8";
    map.setPaintProperty("roads", "line-color", mode === "category" ? catColor : mode === "owner" ? ownerColor : neutral);
    const shown: Record<string, Mode> = { surface: "surface", maxspeed: "maxspeed", aadt: "aadt", lanes: "width", width: "width" };
    for (const [id, m] of Object.entries(shown)) map.setLayoutProperty(id, "visibility", m === mode ? "visible" : "none");
    const row = (color: string, label: string, h = 5) =>
      `<div class="row"><span class="sw" style="background:${color};height:${h}px"></span>${esc(label)}</div>`;
    let html = "";
    if (mode === "category") html = ROAD_TYPES.map((c) =>
      `<button type="button" class="row toggle" data-toggle="${c}" aria-pressed="${visibleCats.has(c)}">
        <span class="sw" style="background:${CATEGORY_COLOR[c]}"></span>${esc(cat(c))}</button>`).join("")
      + (showConstruction ? `<div class="row"><span class="sw sw-dash"></span>${esc(s.under_construction)}</div>` : "");
    if (mode === "surface") html = Object.entries(SURFACE_COLOR).map(([k, c]) => row(c, T[`s_${k}`])).join("");
    if (mode === "maxspeed") html = [["#b2182b", "≤ 30"], ["#ef8a62", "50"], ["#f4c95d", "70"], ["#7fbf7b", "90"], ["#2c9c5a", "100–110"], ["#00563b", "130"]]
      .map(([c, l]) => row(c, `${l} km/h`)).join("");
    if (mode === "aadt") html = [["#cbc9e2", "< 10 000"], ["#9e9ac8", "10–20 000"], ["#6a51a3", "20–40 000"], ["#3f007d", "> 40 000"]]
      .map(([c, l], i) => row(c, l, 3 + i * 2)).join("") + `<div class="small muted" style="max-width:190px;margin-top:4px">${esc(s.aadt_note)}</div>`;
    if (mode === "owner") html = Object.entries(OWNER_COLOR).map(([k, c]) => row(c, T[`owner_${k}`])).join("")
      + `<div class="small muted" style="max-width:210px;margin-top:4px">${esc(s.owner_note)}</div>`;
    if (mode === "width") html = WIDTH_STEPS.map(([m, c], i) => row(c,
      i === 0 ? `< ${WIDTH_STEPS[1][0]} m` : i === WIDTH_STEPS.length - 1 ? `≥ ${m} m` : `${m}–${WIDTH_STEPS[i + 1][0]} m`)).join("")
      + `<div class="small muted" style="max-width:220px;margin-top:4px">${esc(s.width_note)}</div>`;
    legend.innerHTML = html;
  }

  // MapLibre control: the layers button, stacked with the zoom buttons.
  function layersControl(): maplibregl.IControl {
    return {
      onAdd() {
        const div = document.createElement("div");
        div.className = "maplibregl-ctrl maplibregl-ctrl-group";
        div.innerHTML = `<button type="button" class="layers-btn" aria-expanded="false" aria-controls="layers-panel"
          title="${esc(s.layers_button)}" aria-label="${esc(s.layers_button)}">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M10 2.5 2 6.6l8 4.1 8-4.1z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m2 10 8 4.1 8-4.1M2 13.4l8 4.1 8-4.1" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg></button>`;
        div.querySelector("button")!.addEventListener("click", (e) => { e.stopPropagation(); togglePanel(); });
        return div;
      },
      onRemove() { /* never removed */ },
    };
  }

  // ---- sidebar views --------------------------------------------------------------------
  function show(view: View, push: boolean) {
    if (push) stack.push(current);
    current = view;
    stopStrip?.();
    stopStrip = null;
    view.render();
    sidebar.scrollTop = 0;
  }

  // Back from a road goes to a road list, never to an earlier road: the list it was chosen
  // from (search results, or this county's list with its filter), else its county's list.
  function back(stayHere = false) {
    keepView = stayHere;
    try { goBack(); } finally { keepView = false; }
  }
  function goBack() {
    if (current.kind === "road") {
      clearSelection();
      const prev = stack.at(-1);
      if (prev && (prev.kind === "results" || (prev.kind === "county" && (!roadCounty || prev.county === roadCounty)))) {
        stack.pop();
        return show(prev, false);
      }
      if (roadCounty) {
        stack.length = 0;
        stack.push(homeView);
        return show(countyView(roadCounty, "all"), false);
      }
    }
    show(stack.pop() ?? homeView, false);
  }
  const backBtn = () => `<button type="button" class="back" data-back>← ${esc(s.back)}</button>`;
  view.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    if (t.closest("[data-back]")) return back();
    const ex = t.closest<HTMLElement>("[data-example]");
    if (ex) { input.value = ex.dataset.example!; onSearch(true); return; }
    const county = t.closest<HTMLElement>("[data-county]");
    if (county) return show(countyView(county.dataset.county!, "all"), true);
    const road = t.closest<HTMLElement>("[data-road]");
    if (road) {
      const km = road.dataset.km ? Number(road.dataset.km) : undefined;
      return selectRoad(road.dataset.road!, { fit: km == null, km, push: true });
    }
  });

  function renderHome() {
    view.innerHTML = homeHTML;
    const counts = new Map<string, number>();
    for (const r of index) {
      if (r[2]) counts.set(r[2], (counts.get(r[2]) ?? 0) + 1);
      for (const c of r[7]) counts.set(c, (counts.get(c) ?? 0) + 1);
    }
    view.querySelector("#county-list")!.innerHTML = countiesSorted().map(([code, name]) =>
      `<li><button type="button" data-county="${code}"><span>${esc(name)}</span><span class="n">${counts.get(code) ?? ""}</span></button></li>`).join("");
  }

  const roadItem = (r: IndexRow, km?: number) => {
    const missing = r[6] === "no_geometry";
    const where = r[2] ? countyName(r[2]) : s.national_road;
    return `<li><button type="button" class="road-item${missing ? " missing" : ""}" data-road="${esc(r[0])}"${km != null ? ` data-km="${km}"` : ""}>
      ${shield(r[1], r[3])}<span class="route">${esc(r[4] || cat(r[3]))}</span>
      <span class="meta">${esc(where)}${missing ? ` · ${esc(s.not_on_map)}` : ""}</span></button></li>`;
  };

  function renderCounty(code: string, filter: string) {
    const rows = index.filter((r) => r[2] === code || r[7].includes(code));
    const cats = CATEGORY_ORDER.filter((c) => rows.some((r) => r[3] === c));
    const shown = filter === "all" ? rows : rows.filter((r) => r[3] === filter);
    view.innerHTML = `<section>${backBtn()}
      <h2 class="side-h" style="font-size:24px">${esc(countyName(code))}</h2>
      <div class="filters">
        <button type="button" class="chip" data-filter="all" aria-pressed="${filter === "all"}">${esc(s.all)} · ${rows.length}</button>
        ${cats.map((c) => `<button type="button" class="chip" data-filter="${c}" aria-pressed="${filter === c}">${c} · ${rows.filter((r) => r[3] === c).length}</button>`).join("")}
      </div>
      <ul class="road-list">${shown.map((r) => roadItem(r)).join("")}</ul></section>`;
    view.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) =>
      b.addEventListener("click", () => { current = countyView(code, b.dataset.filter!); current.render(); }));
    const own = rows.filter((r) => r[2] === code && r[5]);
    const b = (own.length ? own : rows).map((r) => r[5]).filter(Boolean) as [number, number, number, number][];
    if (b.length && !keepView) map.fitBounds([[Math.min(...b.map((x) => x[0])), Math.min(...b.map((x) => x[1]))],
      [Math.max(...b.map((x) => x[2])), Math.max(...b.map((x) => x[3]))]], { padding: fitPad(40), duration: 700 });
  }

  // ---- search ---------------------------------------------------------------------------
  let t: number | undefined;
  input.addEventListener("input", () => {
    clearBtn.hidden = !input.value;
    window.clearTimeout(t);
    t = window.setTimeout(() => onSearch(false), 140);
  });
  root.querySelector("#search-form")!.addEventListener("submit", (e) => { e.preventDefault(); onSearch(true); });
  clearBtn.addEventListener("click", () => { input.value = ""; clearBtn.hidden = true; stack.length = 0; show(homeView, false); input.focus(); });

  function onSearch(enter: boolean) {
    const q = input.value.trim();
    if (!q) { stack.length = 0; show(homeView, false); return; }
    const p = parseQuery(q);
    let rows: IndexRow[] = [];
    if (p.ref) {
      rows = index.filter((r) => r[1] === p.ref);
      if (!rows.length) rows = index.filter((r) => r[1].startsWith(p.ref!)).slice(0, 60);
      if (enter && rows.length === 1 && rows[0][1] === p.ref) {
        return selectRoad(rows[0][0], { km: p.km ?? undefined, fit: p.km == null, push: true });
      }
    }
    const fq = fold(q).replace(/\s+/g, " ");
    const counties = p.ref ? [] : countiesSorted().filter(([, n]) => fold(n).includes(fq));
    if (!p.ref) {
      const compact = fq.replace(/\s/g, "");
      rows = index.filter((r) => r[1].startsWith(compact) || fold(r[4]).includes(fq)).slice(0, 60);
    }
    const km = p.km ?? undefined;
    const render = () => {
      view.innerHTML = `<section>
        ${counties.map(([c, n]) => `<button type="button" class="road-item" data-county="${c}"><span class="shield" style="background:var(--ink)">${c}</span><span class="route"><b>${esc(n)}</b> · ${esc(s.browse_counties)}</span></button>`).join("")}
        ${rows.length ? `<ul class="road-list">${rows.map((r) => roadItem(r, km)).join("")}</ul>`
          : counties.length ? "" : `<p class="msg">${esc(s.no_results)}</p>`}
      </section>`;
    };
    const results: View = { kind: "results", render };
    if (current.kind === "results") { current = results; render(); } else { stack.length = 0; show(results, false); }
  }

  // ---- road selection -------------------------------------------------------------------
  async function selectRoad(key: string, opts: { at?: LonLat; km?: number; fit?: boolean; push?: boolean } = {}) {
    let road: Road;
    try { road = await fetchRoad(key); } catch { view.innerHTML = `<p class="msg">${esc(s.road_not_found)}</p>`; return; }
    selected = key;
    const color = CATEGORY_COLOR[road.category] ?? "#7c7c7c";
    setSel({
      type: "FeatureCollection",
      features: road.segments.map(([, , , coords]) => ({
        type: "Feature", properties: { color }, geometry: { type: "LineString", coordinates: coords },
      })),
    });

    let here: { km: number; quality: Quality } | null = null;
    let outOfRange = false;
    if (opts.at) {
      const m = measure(road, opts.at);
      if (m) { here = m; placePin(m.point, road, m.km); }
    } else if (opts.km != null) {
      const loc = locate(road, opts.km);
      if (loc) {
        here = { km: opts.km, quality: loc.quality };
        placePin(loc.point, road, opts.km);
        map.flyTo({ center: loc.point as LngLatLike, zoom: Math.max(map.getZoom(), 14), duration: 900,
          offset: [0, -(fitPad(0).bottom / 2)] }); // above the sheet on phones
      } else { outOfRange = true; pin.remove(); pinPopup.remove(); }
    } else { pin.remove(); pinPopup.remove(); }
    if (opts.fit && road.bbox) {
      map.fitBounds([[road.bbox[0], road.bbox[1]], [road.bbox[2], road.bbox[3]]], { padding: fitPad(50), maxZoom: 14, duration: 800 });
    }
    const url = new URL(location.href);
    url.searchParams.set("r", key);
    if (here) url.searchParams.set("km", here.km.toFixed(3)); else url.searchParams.delete("km");
    url.searchParams.delete("q");
    history.replaceState(null, "", url);
    roadCounty = road.county ?? countyAtKm(road, here?.km);
    // another road replaces the open one instead of stacking up
    show({ kind: "road", render: () => renderRoad(road, here, outOfRange) },
      (opts.push ?? false) && current.kind !== "road");
  }

  function placePin(p: LonLat, road: Road, km: number) {
    pin.setLngLat(p as LngLatLike).addTo(map);
    pinPopup.setLngLat(p as LngLatLike).setOffset(16)
      .setHTML(`${shield(road.ref, road.category)} <span class="km">km ${formatKm(km)}</span>`).addTo(map);
  }

  function renderRoad(road: Road, here: { km: number; quality: Quality } | null, outOfRange: boolean) {
    const onMap = road.segments.length > 0;
    const shares = qualityShares(road);
    const aadt = (road.attributes.aadt ?? []).map((e) => Number(e[2]));
    const statusText = (s as Record<string, string>)[`status_${road.lrs.status ?? "no_geometry"}`] ?? "";
    const through = road.counties ? [...new Set(road.counties.map((c) => c[0]))] : road.through ?? [];
    const srcIds = new Set(Object.values(road.attributes).flat().map((e) => e[3]));
    // The strip shows the ticked data types that are drawn along the road.
    const stripOnly = ["surface", "lanes", "maxspeed", "aadt", "structures", "curve"].filter((id) => shownData.has(id as DataId));
    const stripLabels = onMap ? stripBands(road, s, { compact: true, only: stripOnly }).map((b) => b.label) : [];
    const sources = [road.legal_source?.split(" (")[0] ?? (road.lrs.status === "osm_only" ? "" : "HG 540/2000"),
      onMap ? "© OpenStreetMap (ODbL)" : "", srcIds.has("aadt_end") ? "MMAP, data.gov.ro (CC BY 4.0)" : ""].filter(Boolean);

    view.innerHTML = `<section>${backBtn()}
      <div class="detail-head">${shield(road.ref, road.category, true)}
        <div><h2>${esc(cat(road.category))}</h2>
        <div class="small muted">${esc(road.county ? countyName(road.county) : s.national_road)}</div></div></div>
      <p class="detail-route">${esc(road.route ?? "")}</p>
      ${road.lrs.status === "osm_only" ? `<p class="note small">${esc(s.not_in_register_note)}</p>` : ""}
      ${road.now_national ? `<p class="note small">${esc(s.now_national_note.replace("{ref}", road.now_national))}</p>` : ""}
      ${here ? `<div class="here"><span class="small muted">${esc(s.here)}</span>
        <span class="km">km ${formatKm(here.km)}</span>
        ${details ? `<span class="q" data-q="${here.quality}" title="${esc((s as Record<string, string>)[`q_${here.quality}_long`])}">${esc(qual(here.quality))}</span>
        <span class="ref-dist small muted" title="${esc(s.ref_hint)}">${esc(refDistance(road, here.km))}</span>` : ""}</div>` : ""}
      ${outOfRange ? `<p class="msg">${esc(s.km_out_of_range)}</p>` : ""}
      ${here && shownData.size ? atPoint(road, here.km) : ""}
      <dl class="facts">
        <dt>${esc(s.official_length)}</dt><dd>${formatLength(road.length_official, lang)}</dd>
        <dt>${esc(s.official_km)}</dt><dd class="mono">${formatKm(road.km_from)} – ${formatKm(road.km_to)}</dd>
        ${details ? `<dt>${esc(s.on_map)}</dt><dd>${onMap ? `${formatLength(road.lrs.path_km, lang)} · <span class="muted">${esc(statusText)}</span>` : esc(s.not_on_map)}</dd>` : ""}
        ${onMap && details ? `<dt>${esc(s.km_quality)}</dt><dd>${Math.round(shares.calibrated * 100)}% ${esc(qual("calibrated"))}
          <div class="qbar">${(["calibrated", "interpolated", "estimated"] as Quality[]).map((q) =>
            `<span style="width:${shares[q] * 100}%;background:${QUALITY_COLOR[q]}"></span>`).join("")}</div></dd>` : ""}
        <dt>${esc(s.administrator)}</dt><dd>${esc((s as Record<string, string>)[`owner_${OWNER_OF[road.category] ?? "private"}`])}${road.owner ? ` · ${esc(road.owner)}` : ""}</dd>
        ${through.length ? `<dt>${esc(s.through)}</dt><dd>${through.map((c) => esc(countyName(c))).join(", ")}</dd>` : ""}
        ${aadt.length ? `<dt>${esc(s.aadt)}</dt><dd>${Math.min(...aadt).toLocaleString()} – ${Math.max(...aadt).toLocaleString()} ${esc(s.vehicles_day)}</dd>` : ""}
      </dl>
      ${onMap ? `<form class="goto" id="goto"><input inputmode="decimal" placeholder="${esc(s.go_to_km)}, ${formatKm(((road.km_from ?? 0) + (road.km_to ?? 0)) / 2)}" aria-label="${esc(s.go_to_km)}"/>
        <button class="btn">${esc(s.go)}</button></form>
        ${stripLabels.length ? `<div class="mini-strip" id="mini-strip"></div>
        <div class="mini-legend">${stripLabels.map((l) => `<span>${esc(l)}</span>`).join("<span>·</span>")}</div>` : ""}` : ""}
      <div class="actions">
        <a class="btn primary" href="${roadHref(roadUrl, lang, road.key, road.category)}">${esc(s.road_page)} →</a>
        ${onMap ? `<button type="button" class="btn" id="fit">${esc(s.zoom_to_road)}</button>` : ""}
      </div>
      <p class="src">${esc(s.sources)}: ${sources.map(esc).join(" · ")}</p></section>`;

    if (onMap) {
      view.querySelectorAll<HTMLButtonElement>(".near").forEach((b) =>
        b.addEventListener("click", () => selectRoad(road.key, { km: Number(b.dataset.km) })));
      const stripEl = view.querySelector<HTMLElement>("#mini-strip");
      if (stripEl) stopStrip = liveStrip(stripEl, road, s, { compact: true, only: stripOnly });
      view.querySelector("#fit")!.addEventListener("click", () => road.bbox && map.fitBounds(
        [[road.bbox[0], road.bbox[1]], [road.bbox[2], road.bbox[3]]], { padding: fitPad(50), maxZoom: 14 }));
      view.querySelector("#goto")!.addEventListener("submit", (e) => {
        e.preventDefault();
        const v = (e.currentTarget as HTMLFormElement).querySelector("input")!.value;
        const p = parseQuery(`${road.ref} ${v}`);
        if (p.km != null) selectRoad(road.key, { km: p.km });
      });
    }
  }

  // How far the km is from the nearest control point (county border, km stone, junction): the
  // km is exact there and less certain further away. Shown next to the quality label.
  function refDistance(road: Road, km: number): string {
    const cs = road.lrs.controls ?? [];
    if (!cs.length) return s.no_ref;
    const d = Math.min(...cs.map((c) => Math.abs(c - km)));
    const locale = lang === "ro" ? "ro-RO" : "en-GB";
    const txt = d < 1 ? `${Math.round(d * 1000 / 10) * 10} m` : `${d.toLocaleString(locale, { maximumFractionDigits: d < 10 ? 1 : 0 })} km`;
    return s.nearest_ref.replace("{d}", txt);
  }

  // "Source · date" under each value (dates from sources.json; HG 540 is cited, not dated).
  function srcText(id: string): string {
    const v = sourceVersion[id];
    const date = v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v.split("-").reverse().join(".") : v;
    const name = id === "osm" ? "OpenStreetMap" : id === "aadt_end" ? "MMAP, data.gov.ro" : id === "hg540" ? "HG 540/2000" : id;
    return `${name}${date && id !== "hg540" ? ` · ${date}` : ""}`;
  }

  // The ticked data types at one km of the road: value, source and date (the data list's purpose).
  function atPoint(road: Road, km: number): string {
    const locale = lang === "ro" ? "ro-RO" : "en-GB";
    // Several OSM ways can cover one km (junction pieces, the other carriageway): take the
    // longest stretch, which is the road's own line rather than a short piece of a junction.
    const ev = (type: string): Event | undefined => (road.attributes[type] ?? [])
      .filter(([f, t]) => km >= f - 1e-6 && km <= t + 1e-6)
      .sort((a, b) => (b[1] - b[0]) - (a[1] - a[0]))[0];
    const roadSrc = road.lrs.status === "osm_only" ? "osm" : "hg540";
    const laneW = ["A", "DX", "DN", "V"].includes(road.category) ? 3.5 : 3.0;
    const rows: [string, string, string | undefined][] = [];
    for (const d of DATA_TYPES) {
      if (!shownData.has(d.id)) continue;
      let val: string | null = null;
      let src: string | undefined;
      const take = (e: Event | undefined, text: (v: string) => string) => {
        if (e) { val = text(e[2]); src = e[3]; }
      };
      switch (d.id) {
        case "category": val = cat(road.category); src = roadSrc; break;
        case "eroad": { // routes can share a stretch (E60 and E68): list all
          const es = (road.attributes.eroad ?? []).filter(([f, t]) => km >= f - 1e-6 && km <= t + 1e-6);
          if (es.length) { val = [...new Set(es.map((e) => e[2]))].sort().join(", "); src = es[0][3]; }
          break;
        }
        case "owner":
          val = T[`owner_${OWNER_OF[road.category] ?? "private"}`] + (road.owner ? ` · ${road.owner}` : "");
          src = roadSrc; break;
        case "surface": take(ev("surface"), (v) => `${T[`s_${surfaceClass(v)}`] ?? v} (${v})`); break;
        case "lanes": take(ev("lanes"), (v) => v); break;
        case "width": {
          take(ev("width"), (v) => `${v} m`);
          const l = ev("lanes");
          const n = l ? parseInt(l[2], 10) : NaN;
          if (val == null && l && n) { // estimate from lanes, labelled as such
            val = `≈ ${(n * laneW).toLocaleString(locale, { maximumFractionDigits: 1 })} m (${s.estimated_short})`;
            src = l[3];
          }
          break;
        }
        case "structures": {
          const b = ev("bridge"), t = ev("tunnel");
          val = b ? s.bridge : t ? s.tunnel : null;
          src = (b ?? t)?.[3];
          break;
        }
        case "curve": { // no curve here = straight; no curve data at all (old cached file) = "—"
          const c = ev("curve");
          if (c) {
            const p = parseCurve(c[2]);
            val = `${T[`curve_${p.side}`]}, R ≈ ${p.r} m (${T[`c_${p.cls}`]})`; src = c[3];
          } else if (road.attributes.curve) { val = s.curve_straight; src = "osm"; }
          break;
        }
        case "maxspeed": take(ev("maxspeed"), (v) => (/^\d+$/.test(v) ? `${v} km/h` : v)); break;
        case "aadt": take(ev("aadt"), (v) => `${Number(v).toLocaleString(locale)} ${s.vehicles_day}`); break;
      }
      if (d.object) continue; // listed under "Nearby" below
      rows.push([T[d.label], val ?? s.none_here, val == null ? undefined : src]);
    }
    // Ticked point objects within 2 km of the point, nearest first.
    const objTypes = new Set(DATA_TYPES.filter((d) => d.object && shownData.has(d.id)).map((d) => d.object));
    const near = (road.objects ?? []).filter(([k, t]) => objTypes.has(t as never) && Math.abs(k - km) <= 2)
      .sort((a, b) => Math.abs(a[0] - km) - Math.abs(b[0] - km)).slice(0, 6);
    return `<h3 class="at-point-h">${esc(s.at_point)}</h3>
      <dl class="facts at-point">${rows.map(([l, v, src]) =>
        `<dt>${esc(l)}</dt><dd${src ? ` title="${esc(s.sources)}: ${esc(srcText(src))}"` : ""}>${esc(v)}</dd>`).join("")}
      ${near.length ? `<dt>${esc(s.nearby)}</dt><dd>${near.map(([k, t, n, r]) =>
        `<button type="button" class="near" data-km="${k}"><span class="dot" style="background:${OBJECT_COLOR[t as keyof typeof OBJECT_COLOR]}"></span><span><span class="mono small muted">km ${formatKm(k)}</span> ${esc(objectLabel(t, n, r))}</span></button>`).join("")}</dd>` : ""}</dl>`;
  }

  function countyAtKm(road: Road, km: number | undefined): string | null {
    if (km == null || !road.counties) return null;
    const sec = road.counties.find(([, f, t]) => f != null && t != null && km >= f && km <= t);
    return sec ? sec[0] : null;
  }

  function clearSelection() {
    selected = null;
    setSel({ type: "FeatureCollection", features: [] });
    pin.remove(); pinPopup.remove();
    const url = new URL(location.href);
    url.searchParams.delete("r"); url.searchParams.delete("km");
    history.replaceState(null, "", url);
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && current.kind === "road") back();
  });

  function restoreFromUrl() {
    const p = new URLSearchParams(location.search);
    const r = p.get("r");
    const km = p.get("km");
    if (r) selectRoad(r, { km: km != null ? Number(km) : undefined, fit: km == null });
    else if (p.get("q")) { input.value = p.get("q")!; clearBtn.hidden = false; onSearch(true); }
  }

  // ---- start ------------------------------------------------------------------------------
  fetchIndex().then((rows) => {
    index = rows;
    if (current.kind === "home") renderHome();
  });
  renderHome();
}
