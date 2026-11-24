## Bornă — pipeline entry points. Run `make help`.

SHELL := /bin/bash
-include .env
export

POSTGRES_DB       ?= borna
POSTGRES_USER     ?= borna
POSTGRES_PASSWORD ?= borna
POSTGRES_PORT     ?= 5432
PG_DSN := postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_PORT)/$(POSTGRES_DB)
export PG_DSN

VENV := .venv
PY   := $(VENV)/bin/python
RUN  := cd pipeline && ../$(PY) -m
SOURCES ?=   # e.g. make download SOURCES=osm (default: all)

.DEFAULT_GOAL := help
.PHONY: help setup check-tools db-up db-down db-reset psql download import register lrs attributes objects qa tiles web-dev web-build refresh deploy-data snapshot-raw restore-raw fetch-install fetch-uninstall test

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

## --- environment ---------------------------------------------------------

setup: $(VENV) ## Create the Python 3.12 venv and install pipeline deps
$(VENV): pipeline/pyproject.toml
	python3.12 -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -e "pipeline[dev]"
	@touch $(VENV)

check-tools: ## Report which required tools are installed
	@for t in docker python3.12 ogr2ogr osm2pgsql tippecanoe duckdb node; do \
	  if command -v $$t >/dev/null && bash -c "$$t --version >/dev/null 2>&1; exit \$$?" 2>/dev/null; then printf "  ok       %s\n" $$t; \
	  else printf "  MISSING  %s\n" $$t; fi; done

db-up: ## Start PostGIS (docker compose)
	docker compose up -d --wait db

db-down: ## Stop PostGIS (keeps data)
	docker compose down

db-reset: ## Destroy the DB volume and recreate it from pipeline/sql/00_schema.sql
	docker compose down -v
	docker compose up -d --wait db

psql: ## Open a psql shell in the DB container
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

## --- pipeline (ROADMAP §4) -----------------------------------------------

download: setup ## Phase 1: fetch public sources into data/raw/ (SOURCES=osm overture)
	$(RUN) borna.download $(SOURCES)

import: setup db-up ## Phase 1: load data/raw/ into PostGIS (raw schema)
	$(RUN) borna.importer $(SOURCES)

register: setup db-up ## Phase 1.3: parse HG 540/2000 annexes into borna.road
	$(RUN) borna.register

lrs: setup db-up ## Phase 2: match register to geometry, calibrate km
	$(RUN) borna.lrs $(ARGS)

attributes: setup db-up ## Phase 2: surface, lanes, speed, bridges, AADT onto road km ranges
	$(RUN) borna.attributes

objects: setup db-up ## Phase 4.7: level crossings, interchanges, borders, tolls placed on road km (after lrs)
	$(RUN) borna.objects

qa: setup db-up ## Phase 2: QA report vs INS + gap report (data/out/qa/)
	$(RUN) borna.qa

tiles: setup db-up ## Phase 3: export PMTiles, road files, search index to web/public/data/
	$(RUN) borna.tiles

web-dev: ## Phase 3: run the site locally at http://localhost:4321 (after make tiles)
	cd web && npm install && npm run dev

web-build: ## Phase 3: build the static site into web/dist/
	cd web && npm install && npm run build

refresh: setup ## Phase 4.1: compare the new export with the live one, run the checks, write the weekly note
	$(RUN) borna.refresh $(ARGS)

R2_BUCKET ?= borna-data
R2_RAW_BUCKET ?= borna-raw
SLOW_SOURCES := hg540 gisco ins_trn139a aadt_end
SAVED_SOURCES := osm $(SLOW_SOURCES)   # copies in the private bucket, for when a server refuses CI
# rclone reads its R2 remote from the environment; keys stay out of the echoed command.
R2_ENV = RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
	RCLONE_CONFIG_R2_ACCESS_KEY_ID="$$R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$$R2_SECRET_ACCESS_KEY" \
	RCLONE_CONFIG_R2_ENDPOINT=https://$$R2_ACCOUNT_ID.r2.cloudflarestorage.com RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

r2-keys:
	@test -n "$(R2_ACCOUNT_ID)" -a -n "$(R2_ACCESS_KEY_ID)" -a -n "$(R2_SECRET_ACCESS_KEY)" || { echo "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY in .env"; exit 1; }

deploy-data: r2-keys ## Phase 3.5: sync web/public/data/ to Cloudflare R2 (needs R2_* in .env, rclone)
	@test -f web/public/data/index.json || { echo "Run make tiles first"; exit 1; }
	@$(R2_ENV) rclone sync web/public/data/ r2:$(R2_BUCKET)/ --checksum --transfers 32 --fast-list --stats-one-line -v

snapshot-raw: r2-keys ## Phase 4.1: save OSM + the slow-changing sources to the private R2 bucket (run where their servers answer)
	@$(R2_ENV) rclone sync data/raw/ r2:$(R2_RAW_BUCKET)/ $(foreach s,$(SAVED_SOURCES),--include "$(s)/**") --checksum --stats-one-line -v

restore-raw: r2-keys ## Phase 4.1: fetch one saved source when its server does not answer (S=hg540)
	@test -n "$(S)" || { echo "usage: make restore-raw S=<source>"; exit 1; }
	@$(R2_ENV) rclone copy r2:$(R2_RAW_BUCKET)/$(S)/ data/raw/$(S)/ --stats-one-line -v
	@test -f data/raw/$(S)/manifest.json && echo "$(S): restored saved copy"

FETCH_AGENT := $(HOME)/Library/LaunchAgents/ro.bornakm.fetch.plist
fetch-install: ## Phase 4.1: install the Mac helper that saves fresh OSM + HG 540 to the private bucket weekly
	@mkdir -p data/out/fetch $(dir $(FETCH_AGENT))
	@sed "s|REPO|$(CURDIR)|g" deploy/ro.bornakm.fetch.plist > $(FETCH_AGENT)
	@launchctl bootout gui/$$(id -u) $(FETCH_AGENT) 2>/dev/null || true
	@launchctl bootstrap gui/$$(id -u) $(FETCH_AGENT) && echo "installed: runs every 3 h while awake, works once a week (log: data/out/fetch/fetch.log)"

fetch-uninstall: ## Phase 4.1: remove the Mac helper
	@launchctl bootout gui/$$(id -u) $(FETCH_AGENT) 2>/dev/null || true
	@rm -f $(FETCH_AGENT) && echo "removed"

test: setup ## Run pipeline unit tests and lint
	cd pipeline && ../$(VENV)/bin/pytest -q && ../$(VENV)/bin/ruff check borna tests
