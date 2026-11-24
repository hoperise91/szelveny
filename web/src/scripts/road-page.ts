// The road page: official data, the strip diagram, a small map, and every value with its source.
import * as maplibregl from "maplibre-gl";
import type { LngLatLike } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { countyName } from "../lib/counties";
import { MAPLIBRE_RO, type Strings } from "../lib/i18n";
import {
  CATEGORY_COLOR, type Quality, type Road, type Source, fetchRoad, fetchSources,
  formatKm, formatLength, hasStaticPage, parseQuery, qualityShares,
} from "../lib/roads";
import { liveStrip } from "../lib/strip";

const BASEMAP = "https://tiles.openfreemap.org/styles/positron";
const esc = (v: unknown) => String(v ?? "").replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
const ATTR_ORDER = ["surface", "lanes", "maxspeed", "aadt", "width", "bridge", "tunnel"];

export async function initRoadPage(root: HTMLElement) {
  // Precision details are for the maintainer only (?detalii=1 on the map turns them on).
  const details = (() => { try { return localStorage.getItem("borna.details") === "1"; } catch { return false; } })();
  const s: Strings = JSON.parse(root.dataset.strings!);
  const lang = root.dataset.lang!;
  const mapUrl = root.dataset.mapUrl!;
  const S = s as Record<string, string>;
  const roadUrl = root.dataset.roadUrl!;
  const prerendered = !!root.dataset.key; // /drum/KEY/: the basics are already in the HTML
  const key = root.dataset.key || new URLSearchParams(location.search).get("r");
  // Old links (/drum/?r=KEY) move to the road's own page, which search engines index.
  const staticUrl = (k: string) => `${roadUrl}${encodeURIComponent(k)}/`;
  if (!prerendered && key && lang === "ro") { location.replace(staticUrl(key)); return; }

  let road: Road;
  let sources: Source[] = [];
  try {
    if (!key) throw new Error("no road");
    [road, sources] = await Promise.all([fetchRoad(key), fetchSources().catch(() => [])]);
  } catch {
    if (prerendered) { document.getElementById("road-loading")?.remove(); return; }
    root.innerHTML = `<p class="msg">${esc(s.road_not_found)}</p><p><a href="${mapUrl}">← ${esc(s.nav_map)}</a></p>`;
    return;
  }
  if (!prerendered && hasStaticPage(lang, road.category)) { location.replace(staticUrl(road.key)); return; }
  const src = new Map(sources.map((x) => [x.id, x]));
  if (!prerendered) document.title = `${road.ref}${road.county ? ` (${countyName(road.county)})` : ""} · Bornă`;

  const onMap = road.segments.length > 0;
  const shares = qualityShares(road);
  const statusText = S[`status_${road.lrs.status ?? "no_geometry"}`] ?? "";
  const hg = src.get("hg540");

  root.innerHTML = `
    <p class="small"><a href="${mapUrl}?r=${encodeURIComponent(road.key)}">← ${esc(s.open_on_map)}</a></p>
    <div class="rp-head">
      <span class="shield lg" data-cat="${esc(road.category)}">${esc(road.ref)}</span>
      <div><h1><span class="visually-hidden">${esc(road.ref)} · </span>${esc(S[`cat_${road.category}`] ?? road.category)}</h1>
        <div class="muted">${esc(road.county ? countyName(road.county) : s.national_road)}</div></div>
    </div>
    <p class="rp-route">${esc(road.route ?? "")}</p>
    ${road.lrs.status === "osm_only" ? `<p class="rp-note" style="margin:-12px 0 18px">${esc(s.not_in_register_note)}</p>` : ""}
    ${road.now_national ? `<p class="rp-note" style="margin:-12px 0 18px">${esc(s.now_national_note.replace("{ref}", road.now_national))}</p>` : ""}

    <div class="rp-grid">
      <div class="rp-map" id="rp-map">${onMap ? "" : `<p class="msg" style="padding:16px">${esc(s.not_on_map)}</p>`}</div>
      <div class="card">
        <dl class="rp-facts">
          <dt>${esc(s.official_length)}</dt><dd>${formatLength(road.length_official, lang)}</dd>
          <dt>${esc(s.official_km)}</dt><dd class="mono">${formatKm(road.km_from)} – ${formatKm(road.km_to)}</dd>
          ${details ? `<dt>${esc(s.on_map)}</dt><dd>${onMap ? `${formatLength(road.lrs.path_km, lang)}<br><span class="small muted">${esc(statusText)}</span>` : esc(s.not_on_map)}</dd>` : ""}
          ${onMap && details ? `<dt>${esc(s.km_quality)}</dt><dd>${(["calibrated", "interpolated", "estimated"] as Quality[])
            .filter((q) => shares[q] > 0.005)
            .map((q) => `<span class="q" data-q="${q}">${Math.round(shares[q] * 100)}% ${esc(S[`q_${q}`])}</span>`).join("<br>")}</dd>` : ""}
          <dt>${esc(s.legal_source)}</dt><dd class="small">${esc(road.legal_source ?? (road.lrs.status === "osm_only" ? "—" : ""))}${hg?.url ? ` · <a href="${esc(hg.url)}" rel="noopener">legislatie.just.ro</a>` : ""}</dd>
        </dl>
        ${onMap ? `<form class="rp-goto" id="rp-goto"><input inputmode="decimal" aria-label="${esc(s.go_to_km)}"
          placeholder="${esc(s.go_to_km)}, ${formatKm(((road.km_from ?? 0) + (road.km_to ?? 0)) / 2)}"/>
          <button class="btn primary">${esc(s.go)}</button></form>` : ""}
      </div>
    </div>

    ${onMap ? `<h2>${esc(s.strip_title)}</h2>
      <div class="card rp-strip"><div id="rp-strip"></div></div>
      <p class="rp-note">${esc(s.strip_note)}</p>` : ""}

    ${road.counties?.length ? `<h2>${esc(s.by_county)}</h2>
      <div class="table-wrap"><table><thead><tr><th>${esc(s.county)}</th><th class="num">${esc(s.km_from)}</th>
        <th class="num">${esc(s.km_to)}</th><th class="num">${esc(s.length)}</th></tr></thead><tbody>
      ${road.counties.map(([c, f, t, l]) => `<tr><td>${esc(countyName(c))}</td><td class="num">${formatKm(f)}</td>
        <td class="num">${formatKm(t)}</td><td class="num">${l == null ? "–" : l.toFixed(3)}</td></tr>`).join("")}
      </tbody></table></div>` : ""}

    ${Object.keys(road.attributes).length ? `<h2>${esc(s.details_all)}</h2>
      ${ATTR_ORDER.filter((t) => road.attributes[t]?.length).map((t) => {
        const ev = road.attributes[t];
        return `<details class="attr"><summary>${esc(S[t] ?? t)} <span class="n">${ev.length}</span></summary>
          <div class="table-wrap"><table><thead><tr><th class="num">${esc(s.km_from)}</th><th class="num">${esc(s.km_to)}</th>
            <th>${esc(s.value)}</th><th>${esc(s.source)}</th><th>${esc(s.date)}</th><th>${esc(s.licence)}</th></tr></thead><tbody>
          ${ev.map(([f, to, v, sid]) => {
            const so = src.get(sid);
            return `<tr><td class="num">${formatKm(f)}</td><td class="num">${formatKm(to)}</td><td>${esc(v)}</td>
              <td>${esc(so?.name ?? sid)}</td><td class="mono small">${esc(so?.version ?? "")}</td><td class="small">${esc(so?.licence ?? "")}</td></tr>`;
          }).join("")}</tbody></table></div></details>`;
      }).join("")}` : ""}
  `;

  if (!onMap) return;
  liveStrip(root.querySelector<HTMLElement>("#rp-strip")!, road, s, { hide: details ? [] : ["quality"] });
  root.querySelector("#rp-goto")!.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = (e.currentTarget as HTMLFormElement).querySelector("input")!.value;
    const p = parseQuery(`${road.ref} ${v}`);
    if (p.km != null) location.href = `${mapUrl}?r=${encodeURIComponent(road.key)}&km=${p.km}`;
  });

  maplibregl.setWorkerUrl(workerUrl);
  const map = new maplibregl.Map({
    locale: lang === "ro" ? MAPLIBRE_RO : undefined,
    container: root.querySelector<HTMLElement>("#rp-map")!,
    style: BASEMAP,
    bounds: road.bbox ? [[road.bbox[0], road.bbox[1]], [road.bbox[2], road.bbox[3]]] : undefined,
    fitBoundsOptions: { padding: 30 },
    attributionControl: { compact: true, customAttribution: "© OpenStreetMap contributors (ODbL) · Bornă" },
    cooperativeGestures: true,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  map.on("load", () => {
    map.addSource("road", {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: road.segments.map(([f, t, q, coords]) => ({
          type: "Feature", properties: { q, f, t }, geometry: { type: "LineString", coordinates: coords },
        })),
      },
    });
    const before = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
    map.addLayer({ id: "road-halo", type: "line", source: "road", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#ffcf33", "line-width": 11, "line-opacity": 0.85 } }, before);
    map.addLayer({ id: "road-line", type: "line", source: "road", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": CATEGORY_COLOR[road.category] ?? "#7c7c7c", "line-width": 4 } }, before);
    const ends = road.segments.length
      ? [road.segments[0][3][0], road.segments[road.segments.length - 1][3].at(-1)!] : [];
    ends.forEach((p, i) => {
      const el = Object.assign(document.createElement("div"), { className: "km-end" });
      el.textContent = `km ${formatKm(i === 0 ? road.segments[0][0] : road.segments[road.segments.length - 1][1])}`;
      new maplibregl.Marker({ element: el, anchor: "bottom" }).setLngLat(p as LngLatLike).addTo(map);
    });
  });
}
