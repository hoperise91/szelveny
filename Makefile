## Szelvény: pipeline entry points. Run `make help`.

SHELL := /bin/bash
-include .env
export

POSTGRES_DB       ?= szelveny
POSTGRES_USER     ?= szelveny
POSTGRES_PASSWORD ?= szelveny
POSTGRES_PORT     ?= 5433
PG_DSN := postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_PORT)/$(POSTGRES_DB)
export PG_DSN

VENV := .venv
PY   := $(VENV)/bin/python
RUN  := cd pipeline && ../$(PY) -m
SOURCES ?=   # e.g. make download SOURCES=osm (default: all)

.DEFAULT_GOAL := help
.PHONY: help setup check-tools db-up db-down db-reset psql download import lrs attributes objects qa tiles web-dev web-build refresh deploy-data snapshot-raw restore-raw fetch-install fetch-uninstall test

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

## --- pipeline ------------------------------------------------------------

download: setup ## Fetch open sources into data/raw/ (SOURCES=osm overture gisco)
	$(RUN) szelveny.download $(SOURCES)

import: setup db-up ## Load data/raw/ into PostGIS (raw schema)
	$(RUN) szelveny.importer $(SOURCES)

lrs: setup db-up ## Build roads from OSM numbers and calibrate km (OSM-only roads, fallback)
	$(RUN) szelveny.lrs $(ARGS)

attributes: setup db-up ## Surface, lanes, speed, bridges, curves onto road km ranges
	$(RUN) szelveny.attributes

objects: setup db-up ## Level crossings, interchanges, borders, tolls, ferries placed on road km (after lrs)
	$(RUN) szelveny.objects

qa: setup db-up ## Gap and coverage report (data/out/qa/)
	$(RUN) szelveny.qa

tiles: setup db-up ## Export PMTiles, road files, search index to web/public/data/
	$(RUN) szelveny.tiles

web-dev: ## Run the site locally at http://localhost:4321 (after make tiles)
	cd web && npm install && npm run dev

web-build: ## Build the static site into web/dist/
	cd web && npm install && npm run build

refresh: setup ## Compare the new export with the live one, run the checks, write the weekly note
	$(RUN) szelveny.refresh $(ARGS)

R2_BUCKET ?= szelveny-data
R2_RAW_BUCKET ?= szelveny-raw
SLOW_SOURCES := gisco
SAVED_SOURCES := osm $(SLOW_SOURCES)   # copies in the private bucket, for when a server refuses CI
# rclone reads its R2 remote from the environment; keys stay out of the echoed command.
R2_ENV = RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
	RCLONE_CONFIG_R2_ACCESS_KEY_ID="$$R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$$R2_SECRET_ACCESS_KEY" \
	RCLONE_CONFIG_R2_ENDPOINT=https://$$R2_ACCOUNT_ID.r2.cloudflarestorage.com RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

r2-keys:
	@test -n "$(R2_ACCOUNT_ID)" -a -n "$(R2_ACCESS_KEY_ID)" -a -n "$(R2_SECRET_ACCESS_KEY)" || { echo "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY in .env"; exit 1; }

deploy-data: r2-keys ## Sync web/public/data/ to Cloudflare R2 (needs R2_* in .env, rclone)
	@test -f web/public/data/index.json || { echo "Run make tiles first"; exit 1; }
	@$(R2_ENV) rclone sync web/public/data/ r2:$(R2_BUCKET)/ --checksum --transfers 32 --fast-list --stats-one-line -v

snapshot-raw: r2-keys ## Save OSM + the slow-changing sources to the private R2 bucket (run where their servers answer)
	@$(R2_ENV) rclone sync data/raw/ r2:$(R2_RAW_BUCKET)/ $(foreach s,$(SAVED_SOURCES),--include "$(s)/**") --checksum --stats-one-line -v

restore-raw: r2-keys ## Fetch one saved source when its server does not answer (S=osm)
	@test -n "$(S)" || { echo "usage: make restore-raw S=<source>"; exit 1; }
	@$(R2_ENV) rclone copy r2:$(R2_RAW_BUCKET)/$(S)/ data/raw/$(S)/ --stats-one-line -v
	@test -f data/raw/$(S)/manifest.json && echo "$(S): restored saved copy"

FETCH_AGENT := $(HOME)/Library/LaunchAgents/hu.szelveny.fetch.plist
fetch-install: ## Install the Mac helper that saves fresh OSM to the private bucket weekly
	@mkdir -p data/out/fetch $(dir $(FETCH_AGENT))
	@sed "s|REPO|$(CURDIR)|g" deploy/hu.szelveny.fetch.plist > $(FETCH_AGENT)
	@launchctl bootout gui/$$(id -u) $(FETCH_AGENT) 2>/dev/null || true
	@launchctl bootstrap gui/$$(id -u) $(FETCH_AGENT) && echo "installed: runs every 3 h while awake, works once a week (log: data/out/fetch/fetch.log)"

fetch-uninstall: ## Remove the Mac helper
	@launchctl bootout gui/$$(id -u) $(FETCH_AGENT) 2>/dev/null || true
	@rm -f $(FETCH_AGENT) && echo "removed"

test: setup ## Run pipeline unit tests and lint
	cd pipeline && ../$(VENV)/bin/pytest -q && ../$(VENV)/bin/ruff check szelveny tests
