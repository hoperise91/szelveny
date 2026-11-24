// Build-time only: the road index used to prerender road pages and the sitemap.
// Locally it reads web/public/data/index.json (`make tiles`); on Cloudflare Pages, where
// public/data is absent (git-ignored), it fetches the live index from R2.
import type { IndexRow } from "./roads";

const REMOTE = (() => {
  const u = (import.meta.env.PUBLIC_DATA_URL ?? "").replace(/\/$/, "");
  return u.startsWith("http") ? u : "https://data.bornakm.ro";
})();

let cached: Promise<IndexRow[]> | null = null;

export function loadIndex(): Promise<IndexRow[]> {
  cached ??= (async () => {
    // Matches nothing (no error) when the file is absent.
    const local = Object.values(import.meta.glob<IndexRow[]>("/public/data/index.json", { import: "default" }))[0];
    if (local) return local();
    try {
      const r = await fetch(`${REMOTE}/index.json`);
      if (!r.ok) throw new Error(String(r.status));
      return (await r.json()) as IndexRow[];
    } catch (e) {
      // Don't break the deploy, but say loudly that road pages are missing.
      console.warn(`[borna] WARNING: road index not found (${REMOTE}/index.json: ${e}). No road pages built.`);
      return [];
    }
  })();
  return cached;
}
