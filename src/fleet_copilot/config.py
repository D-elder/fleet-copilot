"""Central configuration. Every threshold lives here so the README can document it once."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DATASET_PATH = DATA_DIR / "device-telemetry-dataset.ndjson"
ADMINS_PATH = DATA_DIR / "admins.yaml"
DB_PATH = Path(os.getenv("FLEET_DB", ROOT / "var" / "fleet.db"))

# --- Data rules -------------------------------------------------------------
# "Now" is the dataset's last timestamp (stored in the meta table), never the wall clock.
STALE_AFTER_HOURS = 48          # latest snapshot older than this (vs as-of) => device is stale
EVIDENCE_MAX_AGE_DAYS = 7       # evidence older than this (vs as-of) cannot support an action

# --- Trend / threshold rules ------------------------------------------------
LOW_DISK_FREE_PCT = 10.0
RAM_PRESSURE_USED_PCT = 85.0
RAM_PRESSURE_MIN_SHARE = 0.5    # share of snapshots above the threshold ...
RAM_PRESSURE_MIN_DAYS = 14      # ... over a window at least this long
BATTERY_MIN_READINGS = 10
BATTERY_MIN_SPAN_DAYS = 14
BATTERY_EOL_CAPACITY_DROP_PCT = 3.0
BATTERY_EOL_MIN_CYCLES = 900

# --- Form factor (derived from model name; the dataset has no explicit field) ---
FORM_FACTOR_BY_MODEL_PREFIX = {
    "macbook": "laptop",
    "thinkpad": "laptop",
    "dell xps": "laptop",
    "mac mini": "desktop",
}

# --- LLM (OpenRouter) --------------------------------------------------------
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("FLEET_MODEL", "anthropic/claude-sonnet-5.5")
PROVIDER = os.getenv("FLEET_PROVIDER", "")
TEMPERATURE = float(os.getenv("FLEET_TEMPERATURE", "0"))
SEED = int(os.getenv("FLEET_SEED", "7"))
MAX_AGENT_ITERATIONS = 8
