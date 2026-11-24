// Titles, descriptions and JSON-LD for prerendered pages. Pure functions (no I/O).
import { countyName } from "./counties";
import { AUTHOR, CONTACT_EMAIL, routes, strings, type Lang } from "./i18n";
import type { IndexRow } from "./roads";

export const SITE: string = import.meta.env.PUBLIC_SITE_URL || "https://szelvenykm.hu";
/** Credit line for NAP data (ÁSZF §32; always shown, though attribution isn't required). */
export const NAP_CREDIT = "Forrás: Magyar Közút Nonprofit Zrt. – Nemzeti Adathozzáférési Pont (NAP)";
const abs = (p: string) => new URL(p, SITE).href;

const clean = (s: string | null | undefined) => (s ?? "").replace(/\s+/g, " ").trim();

/** Cut at a word boundary so the result is at most `max` characters, with "…". */
export function clip(s: string, max: number): string {
  if (s.length <= max) return s;
  const cut = s.slice(0, max - 1);
  const at = cut.lastIndexOf(" ");
  return `${(at > max * 0.6 ? cut.slice(0, at) : cut).replace(/[\s,;:–-]+$/, "")}…`;
}

const catName = (lang: Lang, cat: string) =>
  (strings[lang] as Record<string, string>)[`cat_${cat}`] ?? cat;

/** "M1 · Budapest – Hegyeshalom" (Base adds " · Szelvény"; keep it under ~60). */
export function roadTitle(row: IndexRow, lang: Lang): string {
  const [, ref, county, cat, route] = row;
  const head = county ? `${ref} ${countyName(county)}` : ref;
  const tail = clean(route) || catName(lang, cat);
  return `${head} · ${clip(tail, Math.max(18, 50 - head.length))}`;
}

export function roadDescription(row: IndexRow, lang: Lang): string {
  const [, ref, county, cat, route, , , through] = row;
  const s = strings[lang];
  const name = `${ref} (${catName(lang, cat).toLowerCase()})`;
  const where = county ? ` ${s.meta_road_in_county} ${countyName(county)}${lang === "en" ? " county" : ""}` : "";
  const r = clean(route);
  const crosses = !county && through?.length
    ? ` ${s.meta_road_crosses}: ${through.map((c) => countyName(c)).join(", ")}.`
    : "";
  const lead = `${name}${where}${r ? `: ${r}` : ""}.`;
  return clip(`${clip(lead, crosses ? 90 : 110)}${crosses} ${s.meta_road_tail}`, 158);
}

export const organization = {
  "@context": "https://schema.org",
  "@type": "Organization",
  "@id": abs("/#org"),
  name: "Szelvény",
  url: abs("/"),
  logo: abs("/apple-touch-icon.png"),
  email: CONTACT_EMAIL,
  founder: { "@type": "Person", name: AUTHOR.name.en, alternateName: AUTHOR.name.hu, sameAs: [AUTHOR.linkedin] },
};

export function website(lang: Lang) {
  const base = lang === "en" ? "/en/" : "/";
  return {
    "@context": "https://schema.org",
    "@type": "WebSite",
    "@id": abs(`${base}#website`),
    name: "Szelvény",
    alternateName: strings[lang].tagline,
    url: abs(base),
    inLanguage: lang === "hu" ? "hu-HU" : "en",
    publisher: { "@id": abs("/#org") },
    potentialAction: {
      "@type": "SearchAction",
      target: { "@type": "EntryPoint", urlTemplate: abs(`${routes[lang].search}?q={search_term_string}`) },
      "query-input": "required name=search_term_string",
    },
  };
}

export function breadcrumbs(items: { name: string; path: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((it, i) => ({
      "@type": "ListItem", position: i + 1, name: it.name, item: abs(it.path),
    })),
  };
}

/** Google Dataset Search: one Dataset per downloadable file set. */
export function dataset(d: {
  name: string; description: string; pagePath: string; dateModified?: string;
  license: string; files: { url: string; format: string }[];
}) {
  return {
    "@context": "https://schema.org",
    "@type": "Dataset",
    name: d.name,
    description: d.description,
    url: abs(d.pagePath),
    license: d.license,
    isAccessibleForFree: true,
    creator: { "@id": abs("/#org") },
    publisher: organization,
    spatialCoverage: { "@type": "Place", name: "Magyarország" },
    ...(d.dateModified && { dateModified: d.dateModified }),
    keywords: ["közút", "úthálózat", "szelvény", "Magyarország", "Hungary roads", "NAP", "OpenStreetMap", "GIS"],
    distribution: d.files.map((f) => ({ "@type": "DataDownload", encodingFormat: f.format, contentUrl: f.url })),
  };
}
