import { defineConfig } from "astro/config";

// Static site. Hungarian at /, English at /en/. Road data (PMTiles + JSON) is read from
// PUBLIC_DATA_URL: /data in development (web/public/data, built by `make tiles`), R2 in production.
export default defineConfig({
  // The public address: szelvenykm.hu, or PUBLIC_SITE_URL (set in Cloudflare Pages to
  // https://szelveny.pages.dev until the domain is live, so link previews and canonical URLs work).
  site: process.env.PUBLIC_SITE_URL || "https://szelvenykm.hu",
  output: "static",
  trailingSlash: "always",
  vite: {
    worker: { format: "es" }, // MapLibre's worker is an ES module
  },
});
