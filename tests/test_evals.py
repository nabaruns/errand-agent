"""Runs the eval suite under pytest, writes the results table, and proves the evals can fail."""

from __future__ import annotations

import pytest

from errand_agent import approval, executor
from evals.run import run_all, run_one, write_reports
from evals.scenarios import SCENARIOS


def test_eval_suite_passes_and_writes_table():
    results = run_all()
    path = write_reports(results)
    failed = [f"{r.id} {r.name}: {r.notes}" for r in results if not r.passed]
    assert len(results) >= 20
    assert not failed, failed
    assert path.read_text().count("| pass |") == len(results)


def spec(name: str):
    return next(s for s in SCENARIOS if s.fn.__name__ == name)


def test_eval_catches_side_effect_without_approval(monkeypatch):
    """Negative control: an executor that skips the approval check must fail the invariants."""

    def unchecked(self, tr, aid, action, name, **args):
        return self.call_tool(tr, name, **args)

    monkeypatch.setattr(executor.Executor, "_effect", unchecked)
    result = run_one(spec("happy_path"))
    assert not result.passed
    assert any("without an approval check" in v for v in result.invariant_violations)


def test_eval_catches_loose_approval_parsing(monkeypatch):
    """Negative control: treating 'yes, and also...' as approval must fail the skip-confirmation scenario."""
    monkeypatch.setattr(approval, "classify_reply", lambda text: "affirm" if text.lower().startswith("yes")
                        else "other")
    monkeypatch.setattr("errand_agent.agent.classify_reply", approval.classify_reply)
    result = run_one(spec("skip_confirmation"))
    assert not result.passed


def test_eval_catches_success_claimed_before_verification(monkeypatch):
    """Negative control: skipping the status check after a timeout must fail."""

    def trusting_status(self, tr, key, hold_id):
        return {"ok": True, "status": "not_booked", "booking": None}

    monkeypatch.setattr(executor.Executor, "_book_status", trusting_status)
    result = run_one(spec("timeout_after_success"))
    assert not result.passed
