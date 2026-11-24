# Szelvény: conventions

Hungary's open road database: an NVDB-style portal, sister site to **Bornă** (https://bornakm.ro, repo `hoperise91/borna`). It's a separate website with its own domain, forked from Bornă's code. The plan lives in `docs/ROADMAP.md`. Work one roadmap step at a time and commit after each one.

*Working name "Szelvény" (Hungarian for road chainage, as in "M1 12+300"). The owner may rename it; update this file, the README and the site title together.*

The owner prefers a simple, calm, easy UI and checks results against reality: verify geometry, not just totals.

## Data access (the owner's rule; it replaces Bornă's "public data only / no logins")

**Allowed:**
- Openly published data (no account needed).
- Data behind a **free, self-service registration** whose terms **permit storage and republication**. The main case is the **NAP portal** (napportal.kozut.hu): its user terms (ÁSZF, 1 March 2024, §32) explicitly permit storing the data in a database, passing it on, publishing it, making it downloadable and commercial distribution.
- Data under an open licence (CC BY, ODbL, CC0, Licence Ouverte, EU reuse policy, etc.), respecting each licence's conditions.

**Rules for any account-based source:**
- Credentials live in `.env` locally and in GitHub Actions secrets for the weekly refresh. Never commit, print or log them.
- One account, the owner's (NAP ÁSZF §17). Don't create extra accounts.
- Use only the interface the provider designates for data access (NAP ÁSZF §11: only NAP's documented interface). **Don't scrape KIRA (kira.gov.hu) or Útszámkereső (utszamkereso.kozut.hu)** for data; link to them instead.
- Never bypass access controls: no circumventing 403s, rate limits, paywalls, referer locks, captchas or token restrictions, and no reusing someone else's credentials.
- Fetch politely: cache, use conditional requests, back off on errors, and fetch at most once a week per static package (NAP ÁSZF §37 bans activity that affects the site's availability).

**Stop and ask the owner before:**
- adding a source whose terms are unclear, missing, non-commercial-only, or don't mention republication;
- anything that costs money or needs a signed agreement or a formal data request.

Every new source gets a manifest (`data/raw/<source>/manifest.json`) with licence, terms URL, terms date, attribution and retrieval date. Save a copy of its terms in `docs/legal/` with the date in the filename.

## NAP specifics (ÁSZF 2024-03-01, `docs/legal/NAP_ASZF_felhasznalo_2024-03-01.pdf`)
- **§32:** reuse and republication allowed, including commercial use. Attribution isn't required, but always credit: **"Forrás: Magyar Közút Nonprofit Zrt. – Nemzeti Adathozzáférési Pont (NAP)"**.
- **Per-package check:** NAP aggregates several data providers. Before using a package, read its metadata. If it states its own licence or conditions, record them in the manifest. If they conflict with §32, stop and ask.
- **§23, §26:** no warranty on accuracy or currency. The site shows a clear "tájékoztató jellegű, nem hivatalos forrás" / "informational, not an official source" note on `/rolunk` and in downloads.
- **§30–31:** NAP's website (graphics, software, trademarks, names) is protected. Use the data only: no NAP or Magyar Közút logos, and nothing implying official endorsement.
- **§33:** terms can change at any time; registered users are emailed. When that happens, the owner saves the new PDF in `docs/legal/`, and we re-check §32-equivalent permissions before the next refresh.
- **§38:** the account can be suspended without explanation. Always keep the last good NAP snapshot published; the site must keep working, with OSM as the fallback, if NAP access stops.
- **Coverage:** NAP holds the **national (state) road network** (Magyar Közút, ~32,000 km). Municipal and local roads come from OSM.

## Non-negotiables
- **Source, date and licence on every value.** Every row in `road`, `road_segment`, `attribute_event` and `point_object` references `szelveny.source`; attribute events and point objects also carry `source_date` and `licence`. Never merge two sources into one unlabelled value.
- **CRS:** store geometry in **EPSG:4326**. Compute every length, distance and chainage in **EPSG:23700 (EOV)**. Use constants from `pipeline/szelveny/config.py`; don't hard-code SRIDs.
- **Linear referencing is the primary key.** Display positions the Hungarian way: road number + szelvény, `M1 12+300`, `8 145+020`, `4512 3+400`. Store them as numeric km (`12.300`, 3 decimals).
- **Road numbering** (national network): motorways and expressways `M` + number; main roads 1–3 digits; secondary roads (*mellékutak*) 4 digits (*összekötő út*) or 5 digits (*bekötő út*). Local roads have no national number: identify them by name + settlement.
- **Counties:** ISO 3166-2:HU codes (19 counties + Budapest). In user-facing Hungarian text say **megye** (the owner's choice), not *vármegye*.
- **Bilingual.** User-facing text is **Hungarian by default** (`/`), English at `/en/`. Use correct Hungarian diacritics (ő ű with double acute, not õ û or ô).

## Licensing
- Code: MIT.
- OSM/Overture-derived layers: ODbL (share-alike). **Keep them in separate files from NAP and other non-ODbL layers**, so neither licence is forced onto the other.
- NAP layers: ÁSZF §32, credited as above.
- Magyar Közút annual road tables on kozadatportal.hu: **CC Non-Commercial**. Keep them separate and label them non-commercial in downloads.
- EuroRegionalMap (Open Maps for Europe): open licence (attribution), token in `.env` as `ERM_TOKEN`. Checked 2026-11-24: **Hungary is not in the open 2026 release** (only border segments), so it can't cross-check Hungarian roads; re-check with the next annual release.

## Layout (inherited from Bornă; changes listed)
- `pipeline/szelveny/`: Python 3.12 package, one module per Makefile target.
  - `config.py`: `CRS_METRIC = 23700`, `HU_BBOX = (16.1, 45.7, 22.95, 48.6)`.
  - `hungary.py` replaces `romania.py` (county codes, name folding).
  - `nap.py` replaces Bornă's `register.py` (HG 540 parser): it downloads the subscribed NAP static packages through the documented interface and loads them into `raw.nap_*`.
  - `lrs.py`: if NAP's network package carries official chainage, the reference network is loaded from it and calibration code is used only for OSM-only roads. Otherwise reuse Bornă's calibration.
- `pipeline/sql/`: schemas `raw` and `szelveny`.
- `web/`: Astro + MapLibre + PMTiles. Routes: `/` map, `/ut/<szam>` road page, `/kereses?q=M1 12+300`, `/adatok` downloads, `/rolunk` sources, licences and disclaimer. English versions under `/en/`.
- Hosting: Cloudflare Pages (site) + R2 (tiles and downloads, own subdomain), same as Bornă. Pages caps files at 25 MiB, so big files go on R2.
