"""Session = who is asking and for which company. Created by the application, never by the LLM."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class Session:
    company_id: str
    admin_id: str
    role: str  # 'viewer' | 'approver'

    @classmethod
    def for_admin(cls, conn: sqlite3.Connection, admin_id: str, company_id: str) -> "Session":
        row = conn.execute("SELECT company_id, role FROM admins WHERE admin_id=?", (admin_id,)).fetchone()
        if row is None or row["company_id"] != company_id:
            # Same message for unknown admin and wrong company: don't reveal which admins exist.
            raise AccessDenied(f"admin {admin_id!r} is not authorised for company {company_id!r}")
        return cls(company_id=company_id, admin_id=admin_id, role=row["role"])
