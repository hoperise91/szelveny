"""Shared constants. Change conventions here, not in individual steps."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = ROOT / "data" / "raw"
DATA_INTERIM = ROOT / "data" / "interim"
DATA_OUT = ROOT / "data" / "out"

# Port 5433 so Szelvény's PostGIS can run next to Bornă's (5432) on the same machine.
PG_DSN = os.environ.get("PG_DSN", "postgresql://szelveny:szelveny@localhost:5433/szelveny")

CRS_STORAGE = 4326  # all stored geometry
CRS_METRIC = 23700  # HD72 / EOV: every length, distance and chainage computation

# Hungary bounding box (lon/lat), used for Overture and other bbox downloads.
HU_BBOX = (16.1, 45.7, 22.95, 48.6)
