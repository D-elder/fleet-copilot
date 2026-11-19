"""Typed contracts shared by tools, repository, validation and the graph."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

OsFamily = Literal["macos", "windows"]
FormFactor = Literal["laptop", "desktop"]
Severity = Literal["low", "medium", "high"]


class DeviceFilters(BaseModel):
    """Typed filters for query_devices. All filters are ANDed and apply to each device's latest snapshot."""

    model_config = {"extra": "forbid"}

    form_factor: FormFactor | None = None
    os_family: OsFamily | None = None
    os_version_lt: str | None = Field(
        default=None, description="Return devices whose OS version is older than this. Requires os_family."
    )
    disk_free_pct_lt: float | None = Field(default=None, ge=0, le=100)
    mem_used_pct_gt: float | None = Field(default=None, ge=0, le=100)
    failing_check: str | None = Field(default=None, description="check_id that must be failing, e.g. screen_lock")
    check_severity: Severity | None = Field(
        default=None, description="Severity of a FAILING check. Matched on the same check as failing_check."
    )
    has_software: str | None = None
    employee_id: str | None = None
    model_name_contains: str | None = None

    @model_validator(mode="after")
    def _os_version_needs_family(self) -> "DeviceFilters":
        if self.os_version_lt and not self.os_family:
            raise ValueError("os_version_lt requires os_family: versions are not comparable across OS families")
        return self


class Window(BaseModel):
    start: str | None = None
    end: str | None = None


class ResultRow(BaseModel):
    device_id: str
    snapshot_id: str
    collected_at: str
    values: dict[str, Any] = Field(default_factory=dict)


class Excluded(BaseModel):
    device_id: str
    reason: str


class ToolResult(BaseModel):
    """Every read tool returns this. Claim validation compares the agent's answer against it."""

    result_id: str
    tool: str
    company_id: str
    filters: dict[str, Any]
    window: Window
    as_of: str
    population_checked: list[str]  # device_ids considered, so zero-result answers can show what was checked
    count: int
    rows: list[ResultRow]
    excluded: list[Excluded] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def device_ids(self) -> list[str]:
        return [r.device_id for r in self.rows]

    @property
    def snapshot_ids(self) -> list[str]:
        return [r.snapshot_id for r in self.rows]


def make_result_id(tool: str, company_id: str, filters: dict[str, Any], as_of: str) -> str:
    """Deterministic id: identical question over identical data gives the identical id."""
    blob = json.dumps([tool, company_id, filters, as_of], sort_keys=True, default=str)
    return "R-" + hashlib.sha1(blob.encode()).hexdigest()[:10]


def make_snapshot_id(device_id: str, collected_at: str) -> str:
    return f"{device_id}@{collected_at}"
