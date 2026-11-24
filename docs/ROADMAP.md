# Szelvény — Hungary's open road database
### Roadmap (HU / EN), sister site to Szelvény

> **Working name:** **Szelvény** (*chainage*). Its core feature is finding any position on the national road network as "M1 12+300", just as Szelvény does with "DN1 km 27+400".
> **Alternative:** *Kilométerkő* (kilometre stone).
> **Domain candidates (check availability):** szelveny.hu, szelvenyterkep.hu, kilometerko.hu, utszelveny.hu.
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

**Decision gate:** open the national network package and look for **chainage fields** (road number + start/end szelvény per section).
- **Present →** the reference network is official from the start; Phase 3 shrinks to loading and QA. Estimate: **~2 weeks** to public beta at 6–10 h/week.
- **Absent →** reuse Szelvény's calibration (control points: section ends, KIRA-style km posts in OSM, counting-station sections). Estimate: **~4 weeks**.

**Result (2026-11-24): Present.** *Országos közúthálózat* (GeoPackage, 22,341 sections, native EPSG:23700) has `kszam` (road number), `pkod` (carriageway 0/1/2), `kutka` (category), `kszelv`/`vszelv` (start/end szelvény as `"km + m"`, e.g. `"2 + 548"`) and `szho` (registered length, m) on every row. Geometry check: 98% of sections are within 2% of `szho` and per-category totals agree within ~1 km (36,250 km incl. both carriageways of dual roads, cycle paths and junction ramps). The attribute packages use the same `kszam`/`pkod`/`kszelv`/`vszelv` keys, so they attach as linear events without map matching.

## Phase 1: Fork and strip Szelvény (one session)
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

## Phase 3: Reference network
1. Load the NAP national network as `szelveny.road` / `road_segment` with official chainage (`km_quality = official`).
2. OSM roads not in NAP (local and municipal) are added as `km_quality = none`, identified by name + settlement.
3. Attach the NAP attribute packages as linear events (lanes, speed limits, built-up sections, one-way) and point objects (bridges, rest areas, bus stops).
4. **QA report** (as in Szelvény): NAP length vs KSH per county, NAP vs OSM geometry offsets, NAP vs EuroRegionalMap road numbers, and a list of differences that could also help the Hungarian OSM community. That community already compares OSM with NAP; contact them via the talk-hu list or the Matrix room before publishing a diff.
5. Checkpoints: `tests/lrs_checkpoints.csv` with ~30 known positions (km posts on Mapillary, bridge ends). Target ±20 m on official sections.

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
