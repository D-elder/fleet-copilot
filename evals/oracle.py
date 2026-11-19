"""Independent ground truth for the evaluation suite.

Everything here is computed straight from the raw NDJSON with plain Python. It deliberately shares NO code with
src/fleet_copilot (only the documented threshold constants), so an eval failure means the system disagrees with an
independent implementation, not with itself. tests/test_oracle.py cross-checks this module against the repository.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fleet_copilot import config  # thresholds only


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ff(model: str) -> str | None:
    m = model.lower()
    if m.startswith(("macbook", "thinkpad", "dell xps")):
        return "laptop"
    return "desktop" if m.startswith("mac mini") else None


def _family(rec: dict) -> str:
    return "macos" if rec["os"]["platform"] == "darwin" else "windows"


def _ver(family: str, v: str) -> tuple[int, ...]:
    if family == "macos":
        return tuple(int(x) for x in v.split("."))
    m = re.fullmatch(r"(\d+) (\d+)H(\d)", v)
    return tuple(int(x) for x in m.groups())  # type: ignore[union-attr]


class Oracle:
    def __init__(self, path: Path = config.DATASET_PATH):
        recs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        self.as_of = max(_ts(r["collected_at"]) for r in recs)
        self.history: dict[tuple[str, str], list[dict]] = defaultdict(list)   # (company, device) -> snapshots
        for r in recs:
            self.history[(r["company_id"], r["device_id"])].append(r)
        for v in self.history.values():
            v.sort(key=lambda r: r["collected_at"])
        self.companies = sorted({c for c, _ in self.history})

    # --- population ----------------------------------------------------------
    def all_devices(self, company: str) -> set[str]:
        return {d for c, d in self.history if c == company}

    def other_company_ids(self, company: str) -> set[str]:
        """Device and employee ids that belong to OTHER companies."""
        out: set[str] = set()
        for (c, d), snaps in self.history.items():
            if c != company:
                out.add(d)
                out.update(s["employee_id"] for s in snaps)
        return out

    def employee_of(self, company: str, device: str) -> str:
        return self.history[(company, device)][-1]["employee_id"]

    def _current(self, company: str) -> dict[str, dict]:
        """Latest snapshot per non-stale device."""
        out = {}
        for (c, d), snaps in self.history.items():
            if c == company and self.as_of - _ts(snaps[-1]["collected_at"]) <= timedelta(hours=config.STALE_AFTER_HOURS):
                out[d] = snaps[-1]
        return out

    @staticmethod
    def _failing(rec: dict, check: str | None = None, severity: str | None = None) -> bool:
        return any(c["status"] == "fail" and (check is None or c["check_id"] == check)
                   and (severity is None or c["severity"] == severity) for c in rec["compliance_results"])

    # --- current-state oracles -----------------------------------------------
    def low_disk(self, company: str, threshold: float = 10.0) -> set[str]:
        out = set()
        for d, r in self._current(company).items():
            vol = r["disk_volumes"]
            size, avail = sum(v["size_bytes"] for v in vol), sum(v["available_bytes"] for v in vol)
            if size and 100 * avail / size < threshold:
                out.add(d)
        return out

    def failing_check(self, company: str, check: str | None = None, severity: str | None = None,
                      form_factor: str | None = None, family: str | None = None) -> set[str]:
        return {d for d, r in self._current(company).items() if self._failing(r, check, severity)
                and (form_factor is None or _ff(r["device_identity"]["model_name"]) == form_factor)
                and (family is None or _family(r) == family)}

    def os_older(self, company: str, family: str, version: str) -> set[str]:
        limit = _ver(family, version)
        return {d for d, r in self._current(company).items()
                if _family(r) == family and _ver(family, r["os"]["product_version"]) < limit}

    def mem_over(self, company: str, threshold: float = 85.0) -> set[str]:
        return {d for d, r in self._current(company).items()
                if 100 * r["memory"]["used_memory_bytes"] / r["memory"]["total_memory_bytes"] > threshold}

    def form_factor(self, company: str, form_factor: str) -> set[str]:
        return {d for d, r in self._current(company).items() if _ff(r["device_identity"]["model_name"]) == form_factor}

    def software(self, company: str, name: str) -> set[str]:
        return {d for d, r in self._current(company).items()
                if any(name.lower() in s["name"].lower() for s in r["installed_software"])}

    # --- trend oracles ---------------------------------------------------------
    def _series(self, company: str, device: str) -> list[dict] | None:
        snaps = self.history[(company, device)]
        stale = self.as_of - _ts(snaps[-1]["collected_at"]) > timedelta(hours=config.STALE_AFTER_HOURS)
        return None if stale else snaps

    @staticmethod
    def _span(snaps: list[dict]) -> float:
        return (_ts(snaps[-1]["collected_at"]) - _ts(snaps[0]["collected_at"])).total_seconds() / 86400

    def memory_sustained(self, company: str) -> set[str]:
        out = set()
        for d in self.all_devices(company):
            s = self._series(company, d)
            if not s or len(s) < config.MIN_READINGS or self._span(s) < config.TREND_MIN_SPAN_DAYS:
                continue
            over = sum(100 * r["memory"]["used_memory_bytes"] / r["memory"]["total_memory_bytes"] >
                       config.RAM_PRESSURE_USED_PCT for r in s)
            if over / len(s) >= config.RAM_PRESSURE_MIN_SHARE:
                out.add(d)
        return out

    def storage_low(self, company: str) -> set[str]:
        out = set()
        for d in self.all_devices(company):
            s = self._series(company, d)
            if not s or len(s) < config.MIN_READINGS or self._span(s) < config.TREND_MIN_SPAN_DAYS:
                continue
            free = [100 * sum(v["available_bytes"] for v in r["disk_volumes"]) /
                    sum(v["size_bytes"] for v in r["disk_volumes"]) for r in s]
            if free[-1] < config.STORAGE_LOW_FREE_PCT and \
                    sum(f < config.STORAGE_LOW_FREE_PCT for f in free) / len(free) >= config.STORAGE_MIN_SHARE:
                out.add(d)
        return out

    def battery_eol(self, company: str) -> set[str]:
        out = set()
        for d in self.all_devices(company):
            s = self._series(company, d)
            rd = [r["battery"] for r in (s or []) if r.get("battery") and r["battery"].get("full_charge_capacity")]
            if not rd or len(rd) < config.BATTERY_MIN_READINGS:
                continue
            drop = 100 * (rd[0]["full_charge_capacity"] - rd[-1]["full_charge_capacity"]) / rd[0]["full_charge_capacity"]
            if rd[-1]["condition"] == "Service Recommended" or (
                    drop >= config.BATTERY_EOL_CAPACITY_DROP_PCT and rd[-1]["cycle_count"] > config.BATTERY_EOL_MIN_CYCLES):
                out.add(d)
        return out

    def compliance_drift(self, company: str, check: str | None = None) -> set[str]:
        """Devices where a check passed in the first snapshot and fails in the latest."""
        out = set()
        for d in self.all_devices(company):
            s = self._series(company, d)
            if not s:
                continue
            first = {c["check_id"]: c["status"] for c in s[0]["compliance_results"]}
            last = {c["check_id"]: c["status"] for c in s[-1]["compliance_results"]}
            if any(first.get(k) == "pass" and last.get(k) == "fail" for k in last if check is None or k == check):
                out.add(d)
        return out

    # --- dispatch ----------------------------------------------------------------
    def evaluate(self, spec: dict[str, Any], default_company: str) -> set[str]:
        """spec: {oracle: name, args: {...}} or {all_of: [spec, ...]} (set intersection)."""
        if "literal" in spec:  # a fixed, hand-verified set (single-device questions)
            return set(spec["literal"])
        if "all_of" in spec:
            sets = [self.evaluate(s, default_company) for s in spec["all_of"]]
            return set.intersection(*sets)
        fn: Callable[..., set[str]] = getattr(self, spec["oracle"])
        args = dict(spec.get("args", {}))
        company = args.pop("company", default_company)
        return fn(company, **args)
