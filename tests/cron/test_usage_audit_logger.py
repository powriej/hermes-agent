"""Tests for the cron usage_audit.jsonl logger.

Covers:
- successful write produces a single valid JSONL line with full schema
- missing token info still writes a line with null fields
- writer exception is swallowed (json.dumps raises) — call must return cleanly
- file path is created if parent dir is missing
- timestamp format is RFC3339 UTC with millisecond precision and 'Z' suffix
- path resolves through _get_hermes_home() (profile-safe)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from cron import scheduler


@pytest.fixture
def tmp_hermes_home(tmp_path, monkeypatch):
    """Redirect _get_hermes_home() so the audit logger writes under tmp_path."""
    fake_home = tmp_path / "home" / ".hermes"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: fake_home)
    return fake_home


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestUsageAuditPath:
    def test_resolves_through_get_hermes_home(self, tmp_hermes_home):
        p = scheduler._usage_audit_path()
        assert p == tmp_hermes_home / "cron" / "usage_audit.jsonl"



class TestUtcnowIsoMs:
    def test_format_has_millisecond_precision_and_z(self):
        ts = scheduler._utcnow_iso_ms()
        # YYYY-MM-DDTHH:MM:SS.mmmZ
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts), ts


class TestWriteUsageAudit:
    def test_successful_write_produces_valid_jsonl(self, tmp_hermes_home):
        record = {
            "ts": "2026-05-01T04:23:11.123Z",
            "job_id": "bluenode-dispatch-recommend-sweep",
            "fire_id": "deadbeefcafe",
            "prompt_tokens": 11894,
            "completion_tokens": 287,
            "total_tokens": 12181,
            "response_silent": False,
            "deliver_target": None,
            "model": "google/gemma-4-31b-it",
            "duration_ms": 4231,
            "error": None,
        }
        scheduler._write_usage_audit(record)

        path = scheduler._usage_audit_path()
        assert path.exists()
        lines = _read_jsonl(path)
        assert len(lines) == 1
        assert lines[0] == record


    def test_writer_exception_swallowed(self, tmp_hermes_home):
        # Force json.dumps to raise — writer must NOT propagate.
        with patch("cron.scheduler.json.dumps", side_effect=RuntimeError("kaboom")):
            scheduler._write_usage_audit({"job_id": "x"})

        # File never created.
        assert not scheduler._usage_audit_path().exists()

    def test_parent_dir_created_if_missing(self, tmp_hermes_home):
        # Ensure the cron path does not exist yet.
        target = tmp_hermes_home / "cron"
        assert not target.exists()

        scheduler._write_usage_audit({"k": "v"})

        assert target.exists() and target.is_dir()
        assert (target / "usage_audit.jsonl").exists()

    def test_appends_multiple_records(self, tmp_hermes_home):
        scheduler._write_usage_audit({"i": 1})
        scheduler._write_usage_audit({"i": 2})
        scheduler._write_usage_audit({"i": 3})
        lines = _read_jsonl(scheduler._usage_audit_path())
        assert [r["i"] for r in lines] == [1, 2, 3]

    def test_unicode_preserved_not_escaped(self, tmp_hermes_home):
        # ensure_ascii=False so non-ASCII model names / job names round-trip cleanly.
        scheduler._write_usage_audit({"job_id": "한글", "model": "gemma"})
        text = scheduler._usage_audit_path().read_text(encoding="utf-8")
        assert "한글" in text


class TestServedModelFields:
    """The requested-vs-served split.

    Motivated by a real 6-day outage: a cron model went region-dead (HTTP 403), every call silently
    fell back to another provider, and the audit logged the REQUESTED model as "ok" 153 times.
    Nothing recorded that a different model answered, until the fallback hit its own quota.
    """

    def test_fallback_is_visible_when_a_different_model_serves(self):
        fields = scheduler._served_model_fields(
            "deepseek-v4-flash", {"model": "claude-haiku-4.5", "provider": "copilot"})
        assert fields == {
            "served_model": "claude-haiku-4.5", "served_provider": "copilot", "fallback_used": True}

    def test_no_fallback_when_requested_model_served(self):
        fields = scheduler._served_model_fields(
            "qwen3.8-max", {"model": "qwen3.8-max", "provider": "opencode-go"})
        assert fields["served_model"] == "qwen3.8-max"
        assert fields["fallback_used"] is False

    def test_agent_reported_fallback_route_is_used(self):
        """The turn result names its own primary -> active route (agent/served_model.py); that is
        the evidence, even if the agent spells the handed model differently than cron does."""
        fields = scheduler._served_model_fields(
            "vendor/deepseek-v4-flash",
            {"model": "claude-haiku-4.5", "provider": "copilot",
             "requested_model": "deepseek-v4-flash", "served_model": "claude-haiku-4.5"})
        assert fields["served_model"] == "claude-haiku-4.5"
        assert fields["fallback_used"] is True

        same = scheduler._served_model_fields(
            "vendor/qwen3.8-max",
            {"model": "qwen3.8-max", "requested_model": "qwen3.8-max", "served_model": None})
        assert same["served_model"] == "qwen3.8-max"
        assert same["fallback_used"] is False

    def test_proxy_deployment_is_served_model_but_not_a_fallback(self):
        fields = scheduler._served_model_fields(
            "gpt-alias", {"model": "gpt-alias", "requested_model": "gpt-alias",
                          "served_model": "azure-eu-deployment-7"})
        assert fields["served_model"] == "azure-eu-deployment-7"
        assert fields["fallback_used"] is False

    def test_pre_agent_provider_switch_is_a_fallback(self):
        """The agent was already constructed on the fallback, so its own result shows no swap."""
        fields = scheduler._served_model_fields(
            "deepseek-v4-flash",
            {"model": "claude-haiku-4.5", "requested_model": "claude-haiku-4.5", "provider": "copilot"},
            "claude-haiku-4.5")
        assert fields["served_model"] == "claude-haiku-4.5"
        assert fields["fallback_used"] is True

    @pytest.mark.parametrize("result", [None, "not-a-dict", 42, [], {"model": object(), "provider": 7}])
    def test_unusable_result_is_tolerated(self, result):
        """The failure path can reach the audit write before a result exists, and a non-string
        value would make the row unserialisable. An audit write must never be what raises."""
        fields = scheduler._served_model_fields("qwen3.8-max", result)
        assert fields == {"served_model": None, "served_provider": None, "fallback_used": False}

    def test_unknown_served_model_is_not_reported_as_a_fallback(self):
        fields = scheduler._served_model_fields("qwen3.8-max", {"provider": "opencode-go"})
        assert fields["served_model"] is None
        assert fields["fallback_used"] is False

    def test_missing_requested_model_does_not_claim_a_fallback(self):
        fields = scheduler._served_model_fields(None, {"model": "qwen3.8-max"})
        assert fields["served_model"] == "qwen3.8-max"
        assert fields["fallback_used"] is False


class TestFireAuditRecordsServedModel:
    """The row run_job actually writes, on both the success and the failure path."""

    def test_success_row_names_the_model_that_served(self, tmp_hermes_home):
        audit = scheduler._FireAudit({"deliver": "local"}, "job1", "deepseek-v4-flash")
        audit.write({"prompt_tokens": 3, "model": "claude-haiku-4.5", "provider": "copilot",
                     "requested_model": "deepseek-v4-flash", "served_model": "claude-haiku-4.5"}, None)
        row = _read_jsonl(scheduler._usage_audit_path())[0]
        assert row["model"] == "deepseek-v4-flash"
        assert row["requested_model"] == "deepseek-v4-flash"
        assert row["served_model"] == "claude-haiku-4.5"
        assert row["served_provider"] == "copilot"
        assert row["fallback_used"] is True
        assert row["error"] is None

    def test_pre_agent_switch_keeps_the_originally_requested_model(self, tmp_hermes_home):
        audit = scheduler._FireAudit({}, "job1", "claude-haiku-4.5", requested_model="deepseek-v4-flash")
        audit.write({"model": "claude-haiku-4.5", "requested_model": "claude-haiku-4.5",
                     "provider": "copilot"}, None)
        row = _read_jsonl(scheduler._usage_audit_path())[0]
        assert row["requested_model"] == "deepseek-v4-flash"
        assert row["served_model"] == "claude-haiku-4.5"
        assert row["fallback_used"] is True

    def test_failed_fire_records_the_model_it_died_on(self, tmp_hermes_home):
        """Fallback activated and then failed too — the shape of the day the masking ended."""
        class _Agent:
            model, provider = "claude-haiku-4.5", "copilot"
            last_served_model = None
            _fallback_activated = True
            _primary_runtime = {"model": "deepseek-v4-flash"}

        audit = scheduler._FireAudit({}, "job1", "deepseek-v4-flash")
        audit.write(scheduler._agent_model_snapshot(_Agent()), "RuntimeError: quota")
        row = _read_jsonl(scheduler._usage_audit_path())[0]
        assert row["served_model"] == "claude-haiku-4.5"
        assert row["served_provider"] == "copilot"
        assert row["fallback_used"] is True
        assert row["error"] == "RuntimeError: quota"

    def test_failed_fire_with_an_opaque_agent_still_writes_its_row(self, tmp_hermes_home):
        from unittest.mock import MagicMock

        audit = scheduler._FireAudit({}, "job1", "qwen3.8-max")
        audit.write(scheduler._agent_model_snapshot(MagicMock()), "boom")
        row = _read_jsonl(scheduler._usage_audit_path())[0]
        assert row["served_model"] is None and row["fallback_used"] is False
        assert row["error"] == "boom"
