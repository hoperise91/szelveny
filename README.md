# Bornă

*Baza de date deschisă a drumurilor din România* / *Romania's open road database*

Look up any Romanian road by number and kilometre position (`DN1 km 27+400`) and see its class, administrator, surface, lanes, speed limit, traffic and structures. Each value comes with its source, date and licence. Built only from public data.

**Status:** early scaffolding. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start

```sh
brew install gdal osm2pgsql tippecanoe node python@3.12 duckdb   # plus Docker Desktop
cp .env.example .env
make check-tools
make setup     # Python venv
make db-up     # PostGIS 16 on localhost:5432
make help      # all targets
```

## Licences
- Code: MIT ([LICENSE](LICENSE))
- OSM/Overture-derived data: ODbL
- Other layers: their original terms, credited per file
