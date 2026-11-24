// sitemap.xml: every indexable page, with its other-language version (hreflang).
import type { APIRoute } from "astro";
import { routes, type Lang } from "../lib/i18n";
import { hasStaticPage } from "../lib/roads";
import { loadIndex } from "../lib/site-data";
import { SITE } from "../lib/seo";

const abs = (p: string) => new URL(p, SITE).href;
const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");

function entry(paths: Partial<Record<Lang, string>>, priority: number, lastmod?: string): string {
  const langs = (["ro", "en"] as Lang[]).filter((l) => paths[l]);
  const alts = langs.length > 1
    ? [...langs.map((l) => `<xhtml:link rel="alternate" hreflang="${l}" href="${esc(abs(paths[l]!))}"/>`),
       `<xhtml:link rel="alternate" hreflang="x-default" href="${esc(abs(paths.ro!))}"/>`].join("")
    : "";
  return langs.map((l) =>
    `<url><loc>${esc(abs(paths[l]!))}</loc>${lastmod ? `<lastmod>${lastmod}</lastmod>` : ""}<priority>${priority}</priority>${alts}</url>`,
  ).join("\n");
}

const PRIORITY: Record<string, number> = { A: 0.8, DX: 0.8, DN: 0.8, V: 0.6, DJ: 0.6, DC: 0.4, DP: 0.3 };

export const GET: APIRoute = async () => {
  const today = new Date().toISOString().slice(0, 10);
  const pages = (["map", "data", "about"] as const).map((p) =>
    entry({ ro: routes.ro[p], en: routes.en[p] }, p === "map" ? 1 : 0.5, today));
  const roads = (await loadIndex()).map(([key, , , cat]) => {
    const path = (l: Lang) => (hasStaticPage(l, cat) ? `${routes[l].road}${encodeURIComponent(key)}/` : undefined);
    return entry({ ro: path("ro"), en: path("en") }, PRIORITY[cat] ?? 0.4);
  });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
${[...pages, ...roads].join("\n")}
</urlset>`;
  return new Response(xml, { headers: { "Content-Type": "application/xml; charset=utf-8" } });
};
