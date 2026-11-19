"""Plan coverage: every evidence item the plan declared must be backed by a ToolResult from this turn."""
from __future__ import annotations

from typing import Any

from .models import ToolResult

TREND_TOOL_KIND = {"trend_battery": "battery_health", "trend_memory": "memory_pressure",
                   "trend_storage": "storage", "trend_compliance": "compliance_drift"}


def covered(need: dict[str, Any], results: list[ToolResult]) -> bool:
    kind, device_id = need["kind"], need.get("device_id")
    for r in results:
        if kind == "devices_state" and r.tool in ("query_devices", "fleet_overview"):
            return True
        if kind == "fleet_overview" and r.tool == "fleet_overview":
            return True
        if kind == "device_detail" and r.tool == "get_device" and (device_id is None
                                                                    or r.filters.get("device_id") == device_id):
            return True
        if kind in TREND_TOOL_KIND and r.tool == "analyze_trends" and r.filters.get("kind") == TREND_TOOL_KIND[kind]:
            # a fleet-wide trend covers any device; a device-scoped one covers only that device
            if device_id is None or r.filters.get("device_id") in (None, device_id):
                return True
    return False


def uncovered(evidence_needed: list[dict[str, Any]], results: dict[str, ToolResult]) -> list[str]:
    rs = list(results.values())
    return [f"{n['kind']}" + (f"({n['device_id']})" if n.get("device_id") else "")
            for n in evidence_needed if not covered(n, rs)]
