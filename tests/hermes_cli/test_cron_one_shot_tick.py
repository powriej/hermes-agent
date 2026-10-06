"""The one-shot ``hermes cron tick`` path as a first-class scheduler driver.

A profile can be driven by an external ticker (a systemd timer running ``hermes -p X cron tick``
every minute) instead of a gateway. That path never enters the provider loop, so it must keep the
store's liveness markers honest by itself.
"""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(
        "hermes_cli.observability.shared_metrics_process.begin_process", lambda *_a, **_k: None
    )
    return tmp_path


def _tick_raising(exc):
    def _tick(*_a, **_k):
        if exc is not None:
            raise exc
    return _tick


class TestOneShotTickHeartbeat:
    def test_completed_tick_records_liveness_and_success(self, tmp_cron_dir, monkeypatch):
        from cron import jobs
        from hermes_cli.cron import cron_tick

        monkeypatch.setattr("cron.scheduler.tick", _tick_raising(None))
        assert jobs.get_ticker_heartbeat_age() is None

        assert cron_tick() == 0
        assert jobs.get_ticker_heartbeat_age() < 5
        assert jobs.get_ticker_success_age() < 5

    def test_failed_tick_records_liveness_but_not_success(self, tmp_cron_dir, monkeypatch, capsys):
        from cron import jobs
        from hermes_cli.cron import cron_tick

        monkeypatch.setattr("cron.scheduler.tick", _tick_raising(OSError(24, "Too many open files")))

        assert cron_tick() == 1
        assert "Cron tick failed" in capsys.readouterr().out
        # The store WAS attended; only the success marker is withheld, so status can say
        # "ticking but failing" instead of going blind.
        assert jobs.get_ticker_heartbeat_age() < 5
        assert jobs.get_ticker_success_age() is None

    def test_yielded_tick_leaves_the_owning_process_stamp_alone(self, tmp_cron_dir, monkeypatch):
        from cron import jobs
        from cron.scheduler import CronTickYielded
        from hermes_cli.cron import cron_tick

        (tmp_cron_dir / "cron").mkdir()
        owner_stamp = f"{time.time() - 30} {os.getppid()}"
        (tmp_cron_dir / "cron" / "ticker_heartbeat").write_text(owner_stamp)
        monkeypatch.setattr("cron.scheduler.tick", _tick_raising(CronTickYielded("aaa", "bbb")))

        assert cron_tick() == 1
        assert (tmp_cron_dir / "cron" / "ticker_heartbeat").read_text() == owner_stamp
        assert jobs.get_ticker_success_age() is None

    def test_failing_heartbeat_write_does_not_fail_a_good_tick(self, tmp_cron_dir, monkeypatch, capsys):
        """A health signal must not manufacture the failure it reports: a store that cannot take
        the marker (disk full, permissions) must not turn a completed tick into exit 1."""
        from hermes_cli.cron import cron_tick

        def _disk_full(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("cron.scheduler.tick", _tick_raising(None))
        monkeypatch.setattr("cron.jobs.atomic_write_text", _disk_full)

        assert cron_tick() == 0
        assert "Cron tick failed" not in capsys.readouterr().out
