"""Hardware upgrade catalog (see data/hardware_catalog.yaml for the stated assumptions)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from . import config


@dataclass(frozen=True)
class ComponentSpec:
    upgradeable: bool
    options_gb: tuple[int, ...] = ()


@dataclass(frozen=True)
class ModelEntry:
    model_identifier: str
    name: str
    ram: ComponentSpec
    storage: ComponentSpec

    def component(self, component: str) -> ComponentSpec:
        return {"ram": self.ram, "storage": self.storage}[component]


@lru_cache(maxsize=1)
def _load(path: str) -> dict[str, ModelEntry]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["models"]

    def spec(d: dict) -> ComponentSpec:
        return ComponentSpec(bool(d["upgradeable"]), tuple(d.get("options_gb", ())))

    return {k: ModelEntry(k, v["name"], spec(v["ram"]), spec(v["storage"])) for k, v in raw.items()}


def entry(model_identifier: str | None, path: Path = config.CATALOG_PATH) -> ModelEntry | None:
    return _load(str(path)).get(model_identifier or "")


def parse_size_gb(spec: str) -> int | None:
    """'32GB' -> 32, '2TB' -> 2048 (binary, as in the telemetry). None if not a size."""
    m = re.fullmatch(r"\s*(\d+)\s*(GB|TB)\s*", spec, flags=re.I)
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)


def fmt_gb(gb: int) -> str:
    return f"{gb // 1024}TB" if gb >= 1024 and gb % 1024 == 0 else f"{gb}GB"
