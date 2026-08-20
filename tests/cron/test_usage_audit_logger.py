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

    def test_does_not_use_path_home(self, tmp_hermes_home):
        """Audit path must NOT hardcode Path.home() — it bypasses profile-aware resolution."""
        with patch.object(Path, "home") as mock_home:
            p = scheduler._usage_audit_path()
            mock_home.assert_not_called()
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

    def test_missing_token_info_writes_line_with_null_fields(self, tmp_hermes_home):
        record = {
            "ts": "2026-05-01T04:23:11.123Z",
            "job_id": "j",
            "fire_id": "f",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "response_silent": True,
            "deliver_target": "telegram",
            "model": None,
            "duration_ms": 12,
            "error": "boom",
        }
        scheduler._write_usage_audit(record)
        lines = _read_jsonl(scheduler._usage_audit_path())
        assert len(lines) == 1
        assert lines[0]["prompt_tokens"] is None
        assert lines[0]["completion_tokens"] is None
        assert lines[0]["total_tokens"] is None
        assert lines[0]["error"] == "boom"

    def test_writer_exception_swallowed(self, tmp_hermes_home, caplog):
        # Force json.dumps to raise — writer must NOT propagate.
        with patch("cron.scheduler.json.dumps", side_effect=RuntimeError("kaboom")):
            scheduler._write_usage_audit({"job_id": "x"})

        # File never created.
        assert not scheduler._usage_audit_path().exists()
        # Warning logged with our marker.
        assert any("usage_audit write failed" in rec.message for rec in caplog.records)

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

    Motivated by a real 6-day outage: deepseek-v4-flash went region-dead (HTTP 403) on
    2026-08-11, every cron call silently fell back to Copilot, and the audit logged the
    REQUESTED model as "ok" 153 times. Nothing recorded that a different model answered.
    On 2026-08-17 Copilot hit its quota and every affected job failed at once.
    """

    def test_fallback_is_visible_when_a_different_model_serves(self):
        # The exact shape of the outage: deepseek requested, Copilot actually answered.
        fields = scheduler._served_model_fields(
            "deepseek-v4-flash",
            {"model": "claude-haiku-4.5", "provider": "copilot"},
        )
        assert fields["served_model"] == "claude-haiku-4.5"
        assert fields["served_provider"] == "copilot"
        assert fields["fallback_used"] is True

    def test_no_fallback_when_requested_model_served(self):
        fields = scheduler._served_model_fields(
            "qwen3.8-max",
            {"model": "qwen3.8-max", "provider": "opencode-go"},
        )
        assert fields["served_model"] == "qwen3.8-max"
        assert fields["fallback_used"] is False

    @pytest.mark.parametrize("result", [None, "not-a-dict", 42, []])
    def test_non_dict_result_is_tolerated(self, result):
        """The failure path can reach the audit write before a result exists.

        An audit write must never be the thing that raises.
        """
        fields = scheduler._served_model_fields("qwen3.8-max", result)
        assert fields["served_model"] is None
        assert fields["served_provider"] is None
        assert fields["fallback_used"] is False

    def test_unknown_served_model_is_not_reported_as_no_fallback(self):
        """Absence of evidence is not evidence of absence.

        With no served model there is no basis to claim a fallback did NOT happen — but
        fallback_used must stay False rather than True, because a bare claim of
        "a fallback occurred" with nothing to name is equally unfounded. The signal that
        something is unknown is served_model being null.
        """
        fields = scheduler._served_model_fields("qwen3.8-max", {"provider": "opencode-go"})
        assert fields["served_model"] is None
        assert fields["fallback_used"] is False

    def test_missing_requested_model_does_not_claim_a_fallback(self):
        fields = scheduler._served_model_fields(None, {"model": "qwen3.8-max"})
        assert fields["served_model"] == "qwen3.8-max"
        assert fields["fallback_used"] is False

    def test_fields_survive_a_write_roundtrip(self, tmp_hermes_home):
        record = {
            "ts": "2026-08-19T04:23:11.123Z",
            "job_id": "abc",
            "model": "deepseek-v4-flash",
            **scheduler._served_model_fields(
                "deepseek-v4-flash", {"model": "claude-haiku-4.5", "provider": "copilot"}
            ),
        }
        scheduler._write_usage_audit(record)
        rows = _read_jsonl(scheduler._usage_audit_path())
        assert len(rows) == 1
        assert rows[0]["model"] == "deepseek-v4-flash"
        assert rows[0]["served_model"] == "claude-haiku-4.5"
        assert rows[0]["fallback_used"] is True
