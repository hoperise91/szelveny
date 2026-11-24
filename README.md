# Szelvény

*Magyarország nyílt közúti adatbázisa* / *Hungary's open road database*

Look up any Hungarian national road by number and szelvény (chainage), `M1 12+300`, `8 145+020`, `4512 3+400`, and see its category, administrator, surface, lanes, speed limit, traffic and structures. Each value comes with its source, date and licence. The official network comes from the NAP portal (Magyar Közút); local roads come from OpenStreetMap.

**Status:** early scaffolding. See [docs/ROADMAP.md](docs/ROADMAP.md) and the conventions in [CLAUDE.md](CLAUDE.md).

Szelvény is a sister site of [Bornă](https://bornakm.ro) (Romania's open road database) and started from its code ([hoperise91/borna](https://github.com/hoperise91/borna), MIT).

## Quick start

```sh
brew install gdal osm2pgsql tippecanoe node python@3.12 duckdb   # plus Docker Desktop
cp .env.example .env
make check-tools
make setup     # Python venv
make db-up     # PostGIS 16 on localhost:5433 (5432 is left for Bornă)
make test      # pipeline unit tests + lint
make help      # all targets
```

## Licences
- Code: MIT ([LICENSE](LICENSE))
- OSM/Overture-derived data: ODbL, in separate files from NAP layers
- NAP data: NAP user terms (ÁSZF 2024-03-01) §32. *Forrás: Magyar Közút Nonprofit Zrt. – Nemzeti Adathozzáférési Pont (NAP)*. Informational, not an official source.
- Other layers: their original terms, credited per file
