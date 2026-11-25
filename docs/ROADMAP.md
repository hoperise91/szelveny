# Szelvény — Hungary's open road database
### Roadmap (HU / EN), sister site to Szelvény

> **Working name:** **Szelvény** (*chainage*). Its core feature is finding any position on the national road network as "M1 12+300", just as Szelvény does with "DN1 km 27+400".
> **Alternative:** *Kilométerkő* (kilometre stone).
> **Domain:** **szelvenykm.hu** (chosen 2026-11-24, like bornakm.ro; szelveny.hu is taken). Data on `data.szelvenykm.hu`, contact `contact@szelvenykm.hu`.
> **Tagline:** *Magyarország nyílt közúti adatbázisa* / *Hungary's open road database*

**Key difference from Szelvény:** Hungary's official road data (Magyar Közút's National Road Databank, OKA) is published through the **NAP portal** under terms that allow republication (ÁSZF 2024-03-01 §32). So the official network is the *starting point* here, not the gap. Local roads come from OSM.

---

## Phase 0: Setup (an evening)
1. **Register on napportal.kozut.hu.** Accept the ÁSZF; it's already saved in `docs/legal/`. Put the credentials in `.env` (`NAP_USER`, `NAP_PASSWORD`, or whatever the API uses).
2. **Subscribe to the static packages** (Adminisztráció → adatfelhasználói lista → Új feliratkozás):
   - Országos közúthálózat (national road network)
   - Lakott területi szakaszok (built-up sections)
   - Egyirányú útpályák (one-way carriageways)
   - Összes gépjármű forgalmi sávok száma (lane counts)
   - Sebességkorlátozás (speed limits)
   - Hídhasználati feltételek (bridge restrictions)
   - Pihenőparkolók (rest areas)
   - Autóbusz megállóhelyek (bus stops)
   - Any traffic-count / counting-station package on offer
   **Catalogue as checked on 2026-11-21 (23 static packages, public metadata):**
   - *Must:* Országos közúthálózat · **Szelvényjelek** (official chainage markers) · Közúti azonosítópontok · Közút neve, kategóriája és kezelője · Összes gépjármű forgalmi sávok száma · Sebességkorlátozás · **Burkolat típusa, szélessége** (pavement type and width) · **Éves keresztmetszeti forgalmi adatok** (annual AADT, whole national network) · Lakott területi szakaszok
   - *Should:* Egyirányú útpályák · Hídhasználati feltételek · Tehergépjárművekre vonatkozó korlátozások · Pihenőparkolók
   - *Nice:* Autóbusz megállóhelyek · Elsődleges úthálózat
   - *Skip:* the four E-TÉR network packages (likely duplicates of the national network) and the BPK (Budapest Közút) packages (2019, some list no data provider).
   - Delivery: WFS / GML 3, with Shape ZIP, GeoJSON and GeoPackage downloads offered. The main road package says it's updated weekly. Providers: Magyar Közút plus the motorway concession companies (AKA, MKIF, M6 Duna, M6 Tolna, Mecsek), so do the per-package licence check for each.
   - **Account status:** registered 2026-11-21, **approved by 2026-11-24**. Data access is open through NAP's documented interface; credentials go in `.env` only.
   - **Subscribed 2026-11-24:** all 9 *Must* + 4 *Should* packages (HTTP, Pull). Metadata, providers, cadence (all weekly) and the per-package licence check are in `data/raw/nap/manifest.json`: no package states its own licence, so ÁSZF §32 applies; *Országos közúthálózat* is flagged an EU high-value dataset. Field documentation: `docs/nap/NAP_statikus_adatcsomagok_leirasa_2026-02-04.pdf`.
3. Download each once by hand. Note its format and update cadence, and **check its metadata for a provider-specific licence** (CLAUDE.md, "Per-package check").
4. Pick the name and register the domain. Create the GitHub repo, a Cloudflare Pages project and an R2 bucket.
   - **Set up 2026-11-24:** name Szelvény, domain **szelvenykm.hu** (registrar HungaryDomain.com / LaniSys Kft., registered 2026-11-24; renews yearly on 24 November, prepaid and non-refundable, cancel at least 14 days before; DNS panel https://host.lanisys.com:8080 manages records only, nameserver changes go through LaniSys support). GitHub `hoperise91/szelveny` (private for now). Cloudflare Pages project `szelveny` → https://szelveny.pages.dev (from GitHub main; root `web`, `npm run build`, output `dist`, `NODE_VERSION=24`; auto-deploys on push). R2 buckets `szelveny-data` (site data, to be served at `data.szelvenykm.hu`) and `szelveny-raw` (private saved copies). Data uploaded to `szelveny-data` (`make deploy-data`); until `data.szelvenykm.hu` exists the site reads it from the bucket's public development URL `https://pub-49b9dd65b5064d15b95fcc282b1c5b10.r2.dev` (Pages env `PUBLIC_DATA_URL`; r2.dev is rate-limited, switch to the custom domain once the domain is on Cloudflare). Bucket CORS = `deploy/r2-cors.json`. Pages env `PUBLIC_SITE_URL=https://szelveny.pages.dev` makes link previews, canonical URLs and the sitemap use the pages.dev address; **delete it once szelvenykm.hu is live** (the default is szelvenykm.hu). Weekly refresh stays disabled until the R2 GitHub secrets are set.

**Decision gate:** open the national network package and look for **chainage fields** (road number + start/end szelvény per section).
- **Present →** the reference network is official from the start; Phase 3 shrinks to loading and QA. Estimate: **~2 weeks** to public beta at 6–10 h/week.
- **Absent →** reuse Szelvény's calibration (control points: section ends, KIRA-style km posts in OSM, counting-station sections). Estimate: **~4 weeks**.

**Result (2026-11-24): Present.** *Országos közúthálózat* (GeoPackage, 22,341 sections, native EPSG:23700) has `kszam` (road number), `pkod` (carriageway 0/1/2), `kutka` (category), `kszelv`/`vszelv` (start/end szelvény as `"km + m"`, e.g. `"2 + 548"`) and `szho` (registered length, m) on every row. Geometry check: 98% of sections are within 2% of `szho` and per-category totals agree within ~1 km (36,250 km incl. both carriageways of dual roads, cycle paths and junction ramps). The attribute packages use the same `kszam`/`pkod`/`kszelv`/`vszelv` keys, so they attach as linear events without map matching.

**Next session (when szelvenykm.hu is the owner's):**
1. Owner adds szelvenykm.hu to Cloudflare as a site and sets Cloudflare's nameservers at the registrar. **2026-11-25:** zone added on Cloudflare (Lanisys parking A/AAAA/www records dropped; null MX and `v=spf1 -all` kept), nameservers `isabel.ns.cloudflare.com` and `langston.ns.cloudflare.com`; delegation changed by LaniSys (ticket #726) the same evening, live at the registry 20:31 EET. DNSSEC is off (no DS at the registry).
2. Pages project `szelveny` → Custom domains: `szelvenykm.hu` and `www.szelvenykm.hu`. **Done 2026-11-25.**
3. R2 `szelveny-data` → Custom domain `data.szelvenykm.hu`; then disable the r2.dev URL; cache rule on `data.szelvenykm.hu` as for Bornă (edge 1 h, browser 10 min). **Done 2026-11-25** (custom domain + cache rule `data`; r2.dev URL disabled after the site was checked on the new domain).
4. Pages env: delete `PUBLIC_SITE_URL`, set `PUBLIC_DATA_URL=https://data.szelvenykm.hu`; redeploy. **Done 2026-11-25** (Production env).
5. Email Routing for `contact@szelvenykm.hu` → the owner's inbox; Web Analytics; Search Console + Bing with the sitemap. **Email Routing and Web Analytics done 2026-11-25** (`contact@` → the owner's Gmail, as for Bornă; the null MX and `v=spf1 -all` placeholders were replaced by Cloudflare's MX, SPF and DKIM records; Web Analytics automatic setup). **Search Console + Bing done 2026-11-25** (owner's Google account, Domain property verified by DNS TXT `google-site-verification=…`; Bing verified by CNAME `990107f5….szelvenykm.hu → verify.bing.com`; keep both DNS records). Sitemap `https://szelvenykm.hu/sitemap.xml` submitted to both; Google read 7,842 URLs.
6. Optional: make the GitHub repo public (like Bornă); add szelvenykm.hu to the MapTiler key's allowed origins. **Done 2026-11-26:** repo public; Pages env `PUBLIC_MAPTILER_KEY` = Bornă's MapTiler key ("Bornă web", shared free quota), allowed origins now also szelvenykm.hu, www, szelveny.pages.dev, *.szelveny.pages.dev.
Open product items: Phase 4 search page (`/kereses` answer panel), per-county downloads (NAP and OSM layers as separate files, the NC tables labelled), local-street search (later, owner's call), contacting the OSM community about `nap_osm_offsets.csv` (not now).

## Phase 1: Fork and strip Bornă (one session)
1. Copy the Szelvény repo into this folder (fresh git history, MIT licence kept, credit Szelvény in the README).
2. Rename the package and schema `szelveny` → `szelveny`. Swap `romania.py` → `hungary.py` (ISO 3166-2:HU) and set `config.py` to EPSG:23700 and the HU bbox.
3. Remove Romania-only parts: the HG 540 register, CESTRIN/END AADT, INS, ANCPI.
4. Web: switch the default language to Hungarian, routes `/ut/`, `/kereses`, `/adatok`, `/rolunk`, new name and colours. Keep Szelvény's calm UI.
5. `make check-tools`, tests green, an empty site deploys.

**Done 2026-11-24** (deploy pending step 4 of Phase 0: domain, Pages project, R2 bucket). Notes:
- Road categories from the number alone are `M`, `F` (1–3 digits), `OK` (4), `BK` (5), `local`. The number can't tell first- from second-class main roads (NAP: 2,902 two-digit sections are *másodrendű*), so that split waits for NAP's `kutka` in Phase 3.
- NAP szelvény strings can carry metre parts above 999 (`"0 + 1064"`, a km section longer than 1 km after a szelvény jump). `hungary.parse_szelveny` accepts them; Phase 3 keeps the original string for display.
- `lrs.py` now builds every numbered road from OSM (the fallback for ÁSZF §38); HG 540 control points (county borders, county-road claims) are gone.

## Phase 2: Harvest data
| Source | Use | Licence |
|---|---|---|
| **NAP static packages** (above) | Official national network, chainage, lanes, speed limits, built-up sections, bridges, rest areas | ÁSZF §32 (republication allowed) |
| **OpenStreetMap** (Geofabrik `hungary-latest.osm.pbf`) | Local and municipal roads; fallback geometry | ODbL |
| **Overture transportation** (HU bbox) | Stable segment IDs, extra attributes | ODbL |
| **EuroRegionalMap** (HU in the open release) | Official cross-check of the main network | Open licence |
| **TENtec** | TEN-T sections, corridors | EC reuse policy |
| **Eurostat GISCO** (NUTS, LAU) | County and settlement tagging | Open |
| **KSH STADAT** (road network length by county, pavement) | QA | Open |
| **kozadatportal.hu**: Magyar Közút annual road tables | Context and statistics | **CC Non-Commercial**, kept separate |

All downloads write manifests (licence, terms URL and date, attribution). NAP fetches are weekly at most, cached, and respect CLAUDE.md's access rules.

**Status 2026-11-24:**
| Source | State |
|---|---|
| NAP (13 packages) | ✅ `make nap` → `raw.nap_*`, weekly in CI, last good copy kept |
| OpenStreetMap (Geofabrik HU) | ✅ `raw.osm_*`, 20 counties by ISO tag |
| Overture transportation | ✅ `raw.overture_*` (ODbL; incl. TomTom data) |
| KSH STADAT 24.1.2.1, 24.1.1.30 | ✅ `raw.ksh_county_roads`, `raw.ksh_network` (CC BY 4.0) |
| kozadatportal: Magyar Közút tables | ✅ `data/raw/kozut_nc/`, latest year of 7 datasets, **CC BY-NC 2.0**, kept separate; static, fetched once |
| Eurostat GISCO (NUTS, LAU) | ✅ `raw.gisco_nuts` (20 counties), `raw.gisco_lau` (3,155 settlements). **Non-commercial** terms; owner approved 2026-11-24 (Szelvény is not used commercially) |
| EuroRegionalMap | ✗ **Hungary is not in the open 2026 release** (downloaded 2026-11-24: no HU road features, only 13 border segments; no HU metadata). Licence fine (attribution). Not usable as a cross-check; the WFS token from Bornă returns 401 |
| TENtec | ⏸ the public page offers PDF maps only, no GIS download or reuse terms found |

**NAP download method (owner, 2026-11-24):** `nap.py` may call each subscribed package's documented WFS link (the "Adatelérés" URLs on its metadata page; layer names in `data/raw/nap/manifest.json`) directly, weekly at most, with caching and backoff. No other GeoServer/WFS queries without asking.

## Phase 3: Reference network
1. Load the NAP national network as `szelveny.road` / `road_segment` with official chainage (`km_quality = official`).
2. OSM roads not in NAP (local and municipal) are added as `km_quality = none`, identified by name + settlement.
3. Attach the NAP attribute packages as linear events (lanes, speed limits, built-up sections, one-way) and point objects (bridges, rest areas, bus stops).
4. **QA report** (as in Szelvény): NAP length vs KSH per county, NAP vs OSM geometry offsets, NAP vs EuroRegionalMap road numbers, and a list of differences that could also help the Hungarian OSM community. That community already compares OSM with NAP; contact them via the talk-hu list or the Matrix room before publishing a diff.
5. Checkpoints: `tests/lrs_checkpoints.csv` with ~30 known positions (km posts on Mapillary, bridge ends). Target ±20 m on official sections.

**Done 2026-11-24** (`make reference lrs local attributes objects qa`):
- 3.1 `reference.py`: 6,430 NAP roads, 22,341 sections with official szelvény, carriageway and NAP's own strings; class and administrator (kezelő) per road. 32,742 km excl. cycle paths vs KSH 2025 32,708 km.
- 3.2 `lrs.py` adds OSM-numbered roads NAP lacks (none ≥ 1 km on this data, 13 to review); `local.py`: 104,280 local roads by name + settlement (km_quality `none`), on the map only for now.
- 3.3 NAP attributes as events (~224k: lanes, speed limits, surface, width, AADT 2025, built-up, one-way, truck limits, bridges) and point objects (km posts, bridges, rest areas). The export shows NAP's value where NAP has one; OSM's stay in the database with their source.
- 3.4 QA (`data/out/qa/README.md`): NAP vs KSH per county within ±0.6% except Szabolcs-Szatmár-Bereg (+2.0%); NAP vs OSM geometry median 0.2 m, 99.9% within 10 m; 38 stretches where OSM is > 30 m off or missing; 26 NAP numbers no OSM way carries. Not published: contact the OSM community first. NAP vs EuroRegionalMap is not possible: Hungary is not in the open release.
- 3.5 `tests/lrs_checkpoints.csv`: 29 NAP km posts (4 categories, 20 counties), all within 20 m (median 0–5 m). NAP km posts overall: median 1.3 m; OSM km posts (independent): median 17.7 m.

## Phase 4: Portal MVP
- `/`: map with toggles (road class, speed limit, lanes, built-up areas, bridges).
- `/ut/M1`: road page with official length, counties crossed, a szelvény strip diagram and attributes.
- `/kereses?q=M1 12+300`: position lookup → pin + section panel (value · source · date · licence).
- `/adatok`: per-county downloads (NAP layers and OSM layers as **separate files**) + changelog.
- `/rolunk`: sources, licences, the ÁSZF credit line, the "informational, not an official source" disclaimer, error reporting.
- English under `/en/`. Deploy to Pages + R2, cookieless analytics.

## Phase 5: Refresh and depth
- Weekly GitHub Actions refresh (NAP credentials as secrets), with a change report like Szelvény's.
- Keep the last good NAP snapshot if a fetch fails or the account is suspended (ÁSZF §38).
- Traffic: station-based AADT if NAP or kozadatportal provide it (check the licence of each).
- Optional: cross-links between Szelvény and Szelvény at the RO–HU border crossings.

## Phase 6: Only if needed
Requests to Magyar Közút (for example, OKA attributes not on NAP, or pavement condition), using the live site as the calling card. Ask the owner first.

## Hosting (free tier, as Szelvény)
Cloudflare Pages (site) + R2 (tiles and downloads; 10 GB and 10M reads/month free, no egress fees) + GitHub Actions. Running cost: the domain only.

## First Claude Code prompt
> Read CLAUDE.md and docs/ROADMAP.md. Do Phase 1: copy ~/Claude/szelveny into this folder as a fresh repo (exclude data/, web/dist, caches, .env), rename szelveny→szelveny, set EPSG:23700 and the HU bbox, replace romania.py with hungary.py, remove the Romania-only sources, and switch the web UI to Hungarian default with English at /en/. Stop after the tests pass and show me the tree and what you removed.
