// Strip diagram: the road as a horizontal km ruler with one band per property.
import type { Strings } from "./i18n";
import {
  type Road, type Event, CURVE_COLOR, QUALITY_COLOR, parseCurve, SURFACE_COLOR, aadtColor, formatKm, speedColor,
  surfaceClass,
} from "./roads";

interface Band {
  id: string; // data type: surface, lanes, maxspeed, aadt, structures, curve, quality
  label: string;
  items: { f: number; t: number; color: string; text: string }[];
  ticks?: boolean; // narrow marks (bridges, tunnels)
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);

function niceStep(span: number, target: number): number {
  const raw = span / target;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw) ?? raw;
}

export function bands(road: Road, s: Strings): Band[] {
  const a = road.attributes;
  const ev = (type: string): Event[] => a[type] ?? [];
  const out: Band[] = [];
  const surfaceLabel = (c: string) => (s as Record<string, string>)[`s_${c}`] ?? c;
  out.push({
    id: "surface", label: s.surface,
    items: ev("surface").map(([f, t, v]) => ({
      f, t, color: SURFACE_COLOR[surfaceClass(v)], text: `${surfaceLabel(surfaceClass(v))} (${v})`,
    })),
  });
  out.push({
    id: "lanes", label: s.lanes,
    items: ev("lanes").map(([f, t, v]) => {
      const n = parseInt(v, 10);
      const shade = ["#d8d3c8", "#b8b0a0", "#8c826e", "#5f5646", "#3b342a"][Math.min(Math.max(n, 1), 5) - 1];
      return { f, t, color: shade ?? "#a3a3a3", text: `${v} ${s.lanes.toLowerCase()}` };
    }),
  });
  out.push({
    id: "maxspeed", label: s.maxspeed,
    items: ev("maxspeed").map(([f, t, v]) => ({ f, t, color: speedColor(v), text: `${v} km/h` })),
  });
  if (ev("aadt").length) {
    out.push({
      id: "aadt", label: s.aadt,
      items: ev("aadt").map(([f, t, v]) => ({
        f, t, color: aadtColor(v), text: `${Number(v).toLocaleString()} ${s.vehicles_day}`,
      })),
    });
  }
  const structures = [
    ...ev("bridge").map(([f, t]) => ({ f, t, color: "#1d5fbf", text: s.bridge })),
    ...ev("tunnel").map(([f, t]) => ({ f, t, color: "#2b2b2b", text: s.tunnel })),
  ];
  if (structures.length) out.push({ id: "structures", label: `${s.bridge} / ${s.tunnel}`, items: structures, ticks: true });
  if (ev("curve").length) {
    const T = s as Record<string, string>;
    out.push({
      id: "curve", label: s.curves, ticks: true,
      items: ev("curve").map(([f, t, v]) => {
        const c = parseCurve(v);
        return { f, t, color: CURVE_COLOR[c.cls] ?? "#999", text: `${T[`curve_${c.side}`]}, R ≈ ${c.r} m (${T[`c_${c.cls}`]})` };
      }),
    });
  }
  if (ev("built_up").length) {
    out.push({
      id: "built_up", label: s.built_up,
      items: ev("built_up").filter(([, , v]) => v === "átkelési")
        .map(([f, t]) => ({ f, t, color: "#8c826e", text: s.built_up })),
    });
  }
  out.push({
    id: "quality", label: s.km_quality,
    items: road.segments.map(([f, t, q]) => ({
      f, t, color: QUALITY_COLOR[q], text: (s as Record<string, string>)[`q_${q}`] ?? q,
    })),
  });
  return out;
}

/** Render the diagram into `el`. `compact` drops the label column and shows fewer bands. */
export interface StripOpts { compact?: boolean; only?: string[]; hide?: string[] }

/** Bands shown: all, or (compact) the given data types, else surface, lanes and speed. */
export function stripBands(road: Road, s: Strings, opts: StripOpts = {}): Band[] {
  const list = bands(road, s).filter((b) => !(opts.hide ?? []).includes(b.id));
  if (!opts.compact) return list;
  const only = opts.only ?? ["surface", "lanes", "maxspeed"];
  return list.filter((b) => only.includes(b.id));
}

export function renderStrip(el: HTMLElement, road: Road, s: Strings, opts: StripOpts = {}) {
  const from = road.segments.length ? Math.min(...road.segments.map((x) => x[0])) : road.km_from ?? 0;
  const to = road.segments.length ? Math.max(...road.segments.map((x) => x[1])) : road.km_to ?? 1;
  const span = Math.max(to - from, 0.001);
  const list = stripBands(road, s, opts);

  const labelW = opts.compact ? 0 : 128;
  const width = Math.max(el.clientWidth || 600, 280);
  const plot = width - labelW - 8;
  const rowH = opts.compact ? 14 : 22;
  const gap = opts.compact ? 10 : 12;
  const top = 26;
  const height = top + list.length * (rowH + gap) + 4;
  const x = (km: number) => labelW + ((km - from) / span) * plot;

  const step = niceStep(span, opts.compact ? 4 : Math.max(4, Math.round(plot / 90)));
  let ruler = "";
  for (let k = Math.ceil(from / step) * step; k <= to + 1e-9; k += step) {
    ruler += `<line x1="${x(k)}" x2="${x(k)}" y1="14" y2="${height}" class="grid"/>`
      + `<text x="${x(k)}" y="10" class="tick">${step < 1 ? formatKm(k) : Math.round(k)}</text>`;
  }

  let rows = "";
  list.forEach((b, i) => {
    const y = top + i * (rowH + gap);
    if (!opts.compact) rows += `<text x="0" y="${y + rowH / 2 + 4}" class="label">${esc(b.label)}</text>`;
    rows += `<rect x="${labelW}" y="${y}" width="${plot}" height="${rowH}" class="track"/>`;
    for (const it of b.items) {
      const x0 = x(Math.max(it.f, from));
      const w = Math.max(x(Math.min(it.t, to)) - x0, b.ticks ? 2 : 0.6);
      const tip = `${it.text} · km ${formatKm(it.f)} – ${formatKm(it.t)}`;
      rows += `<rect x="${x0}" y="${y}" width="${w}" height="${rowH}" fill="${it.color}"`
        + ` data-tip="${esc(tip)}"><title>${esc(tip)}</title></rect>`;
    }
  });

  el.innerHTML = `<svg class="strip" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"
    role="img" aria-label="${esc(s.strip_title)}">${ruler}${rows}</svg>`;
}

/** Re-render on resize; returns a cleanup function. */
export function liveStrip(el: HTMLElement, road: Road, s: Strings, opts: StripOpts = {}) {
  renderStrip(el, road, s, opts);
  const ro = new ResizeObserver(() => renderStrip(el, road, s, opts));
  ro.observe(el);
  return () => ro.disconnect();
}
