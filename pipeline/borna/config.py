"""Shared constants. Change conventions here, not in individual steps."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = ROOT / "data" / "raw"
DATA_INTERIM = ROOT / "data" / "interim"
DATA_OUT = ROOT / "data" / "out"

PG_DSN = os.environ.get("PG_DSN", "postgresql://borna:borna@localhost:5432/borna")

CRS_STORAGE = 4326  # all stored geometry
CRS_METRIC = 3844   # Stereo 70: every length and chainage computation

# Romania bounding box (lon/lat), used for Overture and other bbox downloads.
RO_BBOX = (20.2, 43.6, 29.8, 48.3)
