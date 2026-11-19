from __future__ import annotations

import pytest

from fleet_copilot.models import Answer, Claim, DeviceFilters
from fleet_copilot.validation import render_answer, validate_answer

from .conftest import ACME, GLOBEX


@pytest.fixture
def low_disk(repo_for):
    repo = repo_for(ACME)
    res = repo.query_devices(DeviceFilters(disk_free_pct_lt=10))
    return repo, res


def claim(res, **over):
    base = dict(result_id=res.result_id, device_ids=res.device_ids, count=res.count, snapshot_ids=res.snapshot_ids)
    base.update(over)
    return Claim(**base)


def check(repo, res, answer):
    return validate_answer(answer, {res.result_id: res}, ACME, repo.device_ids())


def test_correct_claim_passes(low_disk):
    repo, res = low_disk
    assert check(repo, res, Answer(summary="Two devices are low on disk.", claims=[claim(res)])).ok


def test_wrong_count_is_caught(low_disk):
    repo, res = low_disk
    rep = check(repo, res, Answer(summary="x", claims=[claim(res, count=5)]))
    assert not rep.ok and any("count=5" in e for e in rep.errors)


def test_extra_and_missing_devices_are_caught(low_disk):
    repo, res = low_disk
    extra = check(repo, res, Answer(summary="x", claims=[claim(res, device_ids=res.device_ids + ["7VP16KHV88LM"],
                                                               count=3)]))
    assert not extra.ok and any("extra=['7VP16KHV88LM']" in e for e in extra.errors)
    missing = check(repo, res, Answer(summary="x", claims=[claim(res, device_ids=res.device_ids[:1], count=1,
                                                                 snapshot_ids=res.snapshot_ids[:1])]))
    assert not missing.ok and any("missing=" in e for e in missing.errors)


def test_subset_claim_is_allowed_only_when_declared(low_disk):
    repo, res = low_disk
    one = dict(device_ids=res.device_ids[:1], count=1, snapshot_ids=res.snapshot_ids[:1])
    assert check(repo, res, Answer(summary="x", claims=[claim(res, subset=True, **one)])).ok
    assert not check(repo, res, Answer(summary="x", claims=[claim(res, **one)])).ok


def test_invented_snapshot_id_is_caught(low_disk):
    repo, res = low_disk
    rep = check(repo, res, Answer(summary="x", claims=[claim(res, snapshot_ids=["8NM23J95R5I6@2030-01-01T00:00:00Z",
                                                                                res.snapshot_ids[1]])]))
    assert not rep.ok and any("not in the result" in e for e in rep.errors)


def test_missing_citation_is_caught(low_disk):
    repo, res = low_disk
    rep = check(repo, res, Answer(summary="x", claims=[claim(res, snapshot_ids=[])]))
    assert not rep.ok and any("no snapshot_id cited" in e for e in rep.errors)


def test_unknown_result_id_is_caught(low_disk):
    repo, res = low_disk
    rep = check(repo, res, Answer(summary="x", claims=[claim(res, result_id="R-doesnotexist")]))
    assert not rep.ok and any("not returned by any tool call" in e for e in rep.errors)


def test_summary_may_not_mention_unclaimed_device(low_disk):
    repo, res = low_disk
    rep = check(repo, res, Answer(summary="Also 7VP16KHV88LM looks bad.", claims=[claim(res)]))
    assert not rep.ok and any("7VP16KHV88LM" in e for e in rep.errors)


def test_result_from_another_company_is_rejected(repo_for, low_disk):
    repo, _ = low_disk
    foreign = repo_for(GLOBEX).query_devices(DeviceFilters(disk_free_pct_lt=10))
    rep = validate_answer(Answer(summary="x", claims=[claim(foreign)]), {foreign.result_id: foreign},
                          ACME, repo.device_ids())
    assert not rep.ok and any("different company" in e for e in rep.errors)


def test_zero_result_answer_validates_and_shows_what_was_checked(repo_for):
    repo = repo_for(ACME)
    res = repo.query_devices(DeviceFilters(form_factor="laptop", check_severity="high"))
    ans = Answer(summary="No laptops are failing high-severity checks.",
                 claims=[Claim(result_id=res.result_id, device_ids=[], count=0, snapshot_ids=[])])
    assert check(repo, res, ans).ok
    text = render_answer(ans, {res.result_id: res})
    assert "matched 0 of 10 devices checked" in text
    assert '"check_severity": "high"' in text and "acme-001" in text and res.as_of in text
