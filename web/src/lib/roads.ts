// Road data: fetching, km lookup (km -> point), reverse lookup (point -> km), search parsing.
// The km maths mirrors szelveny.locate()/measure() in the pipeline: km is linear within each
// calibrated segment, measured along the segment's length.

export const DATA_URL: string = (import.meta.env.PUBLIC_DATA_URL ?? "/data").replace(/\/$/, "");

export type Quality = "official" | "calibrated" | "interpolated" | "estimated";
export type LonLat = [number, number];
export type Segment = [number, number, Quality, LonLat[]]; // km_from, km_to, quality, coords
export type Event = [number, number, string, string]; // km_from, km_to, value, source

export interface Road {
  key: string;
  ref: string;
  county: string | null;
  category: string;
  route: string | null;
  km_from: number | null;
  km_to: number | null;
  length_official: number | null;
  road_class?: string | null;      // NAP's category name: 'autópálya', 'II. rendű főút', …
  administrator?: string | null;   // kezelő (NAP)
  lrs: { status: string | null; path_km: number | null; control_points: number; controls?: number[] };
  bbox: [number, number, number, number] | null;
  segments: Segment[];
  attributes: Record<string, Event[]>;
  through?: string[]; // counties passed through (ISO 3166-2:HU)
  // [km, type, name, ref, details: bridge capacity and limits, rest-area side and places, …]
  objects?: [number, string, string | null, string | null, Record<string, unknown> | null][];
}

// [key, ref, county, category, route, bbox, status, counties passed through]
export type IndexRow = [string, string, string | null, string, string,
  [number, number, number, number] | null, string, string[]];

export interface Source {
  id: string; name: string; url: string | null; licence: string;
  attribution: string | null; retrieved: string; version: string;
}

// M gyorsforgalmi út, F főút, OK összekötő út, BK bekötő út, ramp csomóponti ág, rest pihenőút,
// cycle kerékpárút (NAP kutka); local: roads without a national number.
export const CATEGORY_COLOR: Record<string, string> = {
  M: "#1f5aa6", F: "#c8102e", OK: "#d9822b", BK: "#7a8f2e",
  ramp: "#6d8fc7", rest: "#9aa5b1", cycle: "#2a9d8f", local: "#7c7c7c",
};
export const CATEGORY_ORDER = ["M", "F", "OK", "BK", "ramp", "rest", "cycle", "local"];

export const QUALITY_COLOR: Record<Quality, string> = {
  official: "#00563b", calibrated: "#1a9850", interpolated: "#e6a100", estimated: "#d73027",
};

export const SURFACE_COLOR: Record<string, string> = {
  paved: "#3b4252", gravel: "#c9893a", earth: "#8b5a2b", unpaved: "#d9a86c", other: "#a3a3a3",
};

const PAVED = new Set(["asphalt", "concrete", "concrete:plates", "concrete:lanes", "paved",
  "paving_stones", "sett", "cobblestone", "unhewn_cobblestone", "chipseal"]);
const GRAVEL = new Set(["gravel", "fine_gravel", "compacted", "pebblestone", "rock"]);
const EARTH = new Set(["dirt", "ground", "earth", "grass", "mud", "sand", "soil"]);

/** OSM surface tag or NAP pavement type -> map class (as tiles.py SURFACE_CLASS_SQL). */
export function surfaceClass(value: string): string {
  const v = value.toLowerCase();
  if (v.includes("aszfalt") || v === "beton" || v === "kő, keramit") return "paved";
  if (v.startsWith("makadám-")) return "gravel";
  if (v.endsWith("földút")) return "earth";
  if (PAVED.has(value)) return "paved";
  if (GRAVEL.has(value)) return "gravel";
  if (EARTH.has(value)) return "earth";
  if (value === "unpaved") return "unpaved";
  return "other";
}

export function speedColor(v: string): string {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return "#a3a3a3";
  if (n <= 30) return "#b2182b";
  if (n <= 50) return "#ef8a62";
  if (n <= 70) return "#f4c95d";
  if (n <= 90) return "#7fbf7b";
  if (n <= 110) return "#2c9c5a";
  return "#00563b";
}

export function aadtColor(v: string): string {
  const n = parseInt(v, 10);
  if (n >= 40000) return "#3f007d";
  if (n >= 20000) return "#6a51a3";
  if (n >= 10000) return "#9e9ac8";
  return "#cbc9e2";
}

// ---- formatting ----------------------------------------------------------------------

/** 27.4 -> "27+400" (szelvény) */
export function formatKm(km: number | null | undefined): string {
  if (km == null || !Number.isFinite(km)) return "–";
  const sign = km < 0 ? "-" : "";
  const m = Math.round(Math.abs(km) * 1000);
  return `${sign}${Math.floor(m / 1000)}+${String(m % 1000).padStart(3, "0")}`;
}

export function formatLength(km: number | null | undefined, lang: string): string {
  if (km == null) return "–";
  return `${km.toLocaleString(lang === "hu" ? "hu-HU" : "en-GB", { maximumFractionDigits: 1 })} km`;
}

export function fold(s: string): string {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase();
}

// ---- data ----------------------------------------------------------------------------

const cache = new Map<string, Promise<unknown>>();
function getJSON<T>(path: string): Promise<T> {
  if (!cache.has(path)) {
    cache.set(path, fetch(`${DATA_URL}/${path}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${path}`);
      return r.json();
    }));
  }
  return cache.get(path) as Promise<T>;
}

export const fetchIndex = () => getJSON<IndexRow[]>("index.json");
export const fetchSources = () => getJSON<Source[]>("sources.json");
export const fetchRoad = (key: string) => getJSON<Road>(`roads/${encodeURIComponent(key)}.json`);

// ---- geometry ------------------------------------------------------------------------

const R = 6371008.8;
const rad = (d: number) => (d * Math.PI) / 180;

function dist(a: LonLat, b: LonLat): number {
  const dLat = rad(b[1] - a[1]);
  const dLon = rad(b[0] - a[0]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function lineLength(coords: LonLat[]): number {
  let s = 0;
  for (let i = 1; i < coords.length; i++) s += dist(coords[i - 1], coords[i]);
  return s;
}

/** Point at `km` on the road, with the segment's quality; null if the km is not on the map. */
export function locate(road: Road, km: number): { point: LonLat; quality: Quality } | null {
  const seg = road.segments.find(([f, t]) => km >= f - 1e-6 && km <= t + 1e-6);
  if (!seg) return null;
  const [f, t, quality, coords] = seg;
  const target = (t > f ? (km - f) / (t - f) : 0) * lineLength(coords);
  let run = 0;
  for (let i = 1; i < coords.length; i++) {
    const d = dist(coords[i - 1], coords[i]);
    if (run + d >= target) {
      const r = d ? (target - run) / d : 0;
      const [a, b] = [coords[i - 1], coords[i]];
      return { point: [a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r], quality };
    }
    run += d;
  }
  return { point: coords[coords.length - 1], quality };
}

/** km of the point on the road nearest to `p`, and how far `p` is from the road (m). */
export function measure(road: Road, p: LonLat): { km: number; offset: number; point: LonLat; quality: Quality } | null {
  let best: { km: number; offset: number; point: LonLat; quality: Quality } | null = null;
  const kx = Math.cos(rad(p[1])); // local equirectangular projection around p
  for (const [f, t, quality, coords] of road.segments) {
    const total = lineLength(coords);
    let run = 0;
    for (let i = 1; i < coords.length; i++) {
      const a = coords[i - 1], b = coords[i];
      const ax = (a[0] - p[0]) * kx, ay = a[1] - p[1];
      const bx = (b[0] - p[0]) * kx, by = b[1] - p[1];
      const dx = bx - ax, dy = by - ay;
      const len2 = dx * dx + dy * dy;
      const u = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
      const q: LonLat = [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
      const off = dist(p, q);
      const segLen = dist(a, b);
      if (!best || off < best.offset) {
        const along = total ? (run + segLen * u) / total : 0;
        best = { km: f + (t - f) * along, offset: off, point: q, quality };
      }
      run += segLen;
    }
  }
  return best;
}

/** Share of the road's mapped km by quality. */
export function qualityShares(road: Road): Record<Quality, number> {
  const out: Record<Quality, number> = { official: 0, calibrated: 0, interpolated: 0, estimated: 0 };
  let total = 0;
  for (const [f, t, q] of road.segments) {
    out[q] += t - f;
    total += t - f;
  }
  if (total > 0) for (const k of Object.keys(out) as Quality[]) out[k] /= total;
  return out;
}

// ---- search --------------------------------------------------------------------------

export interface ParsedQuery { ref: string | null; km: number | null; text: string }

/** 'M1 12+300', 'm 1 km 12+300', '8 145,02', '4512' -> {ref: 'M1', km: 12.3}.
 * The metre part may exceed 999 (a km section longer than 1 km): '0+1064' -> 1.064. */
export function parseQuery(q: string): ParsedQuery {
  const text = q.trim();
  const m = fold(text).replace(/\s+/g, " ")
    .match(/^(?:(M) ?(\d{1,2})|([1-9]\d{0,4}))\.?(?: ?(?:KM)? ?(\d+(?: ?\+ ?\d+|[.,]\d{1,3})?))?$/);
  if (!m) return { ref: null, km: null, text };
  let km: number | null = null;
  if (m[4]) {
    const plus = m[4].includes("+");
    const [i, d = ""] = m[4].replace(/ /g, "").split(/[+.,]/);
    km = parseInt(i, 10) + (d ? parseInt(plus ? d : d.padEnd(3, "0").slice(0, 3), 10) / 1000 : 0);
  }
  return { ref: m[1] ? `M${parseInt(m[2], 10)}` : m[3], km, text };
}

// ---- static road pages (SEO) -----------------------------------------------------------
// Every road has a prerendered page at /ut/<KEY>/. English pages exist only for these
// categories: Cloudflare Pages caps a deploy at 20k files, and bekötő/local road searches are
// Hungarian.
export const EN_PAGE_CATEGORIES = new Set(["M", "F", "OK"]);

export function hasStaticPage(lang: string, category: string): boolean {
  return lang === "hu" || EN_PAGE_CATEGORIES.has(category);
}

/** Link to a road page: the static page when it exists, else the client page (?r=). */
export function roadHref(roadBase: string, lang: string, key: string, category?: string): string {
  return category && hasStaticPage(lang, category)
    ? `${roadBase}${encodeURIComponent(key)}/`
    : `${roadBase}?r=${encodeURIComponent(key)}`;
}

// Horizontal curves (Phase 4.8): event value '<class>:<R m>:<L|R>:<turn deg>', tighter = darker.
export const CURVE_CLASSES = ["hairpin", "very_sharp", "sharp", "moderate"] as const;
export const CURVE_COLOR: Record<string, string> = {
  hairpin: "#7a1f12", very_sharp: "#c0392b", sharp: "#e67e22", moderate: "#f2c48d",
};
export function parseCurve(v: string): { cls: string; r: number; side: "L" | "R"; turn: number } {
  const [cls, r, side, turn] = v.split(":");
  return { cls, r: Number(r), side: side === "L" ? "L" : "R", turn: Number(turn) };
}
