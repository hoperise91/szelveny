// getStaticPaths for the prerendered road pages (build time).
import type { Lang } from "./i18n";
import { type IndexRow, hasStaticPage } from "./roads";
import { loadIndex } from "./site-data";

const natural = new Intl.Collator("ro", { numeric: true }).compare;

export async function roadPaths(lang: Lang) {
  const rows = (await loadIndex()).filter((r) => hasStaticPage(lang, r[3]));
  // "Other roads" links: same county and category (county roads) or same category (national).
  const groups = new Map<string, IndexRow[]>();
  for (const r of rows) {
    const g = `${r[2] ?? ""}|${r[3]}`;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(r);
  }
  for (const list of groups.values()) list.sort((a, b) => natural(a[1], b[1]));
  return rows.map((row) => {
    const list = groups.get(`${row[2] ?? ""}|${row[3]}`)!;
    const i = list.indexOf(row);
    // Up to 60 neighbours by number, centred on this road, so every road is linked from others.
    const start = Math.max(0, Math.min(i - 30, list.length - 61));
    const others = list.slice(start, start + 61).filter((r) => r !== row);
    return { params: { key: row[0] }, props: { row, others } };
  });
}
