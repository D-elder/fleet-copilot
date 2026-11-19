"""First-run behaviour found by a fresh-clone test: a missing API key must be a friendly message, not a stack trace."""
from __future__ import annotations

import pytest

from fleet_copilot import cli


def run(tmp_path, *extra):
    return cli.main(["--company", "acme-001", "--admin", "admin-acme-1", "--db", str(tmp_path / "t.db"), *extra])


def test_missing_api_key_gives_a_friendly_error_and_exit_code_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert run(tmp_path) == 2
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is not set" in err and ".env.example" in err and "Traceback" not in err


def test_wrong_company_is_denied_before_anything_else(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert cli.main(["--company", "globex-002", "--admin", "admin-acme-1", "--db", str(tmp_path / "t.db")]) == 2
    assert "Access denied" in capsys.readouterr().err
