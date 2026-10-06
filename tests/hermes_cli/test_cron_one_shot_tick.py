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


class TestStatusForExternallyTickedProfile:
    """With no gateway, lock, multiplexer or live in-process ticker, a profile ticked by a one-shot
    external ticker is healthy — `cron status` must not call it dead or prescribe a gateway."""

    @pytest.fixture()
    def no_gateway(self, tmp_cron_dir, monkeypatch):
        from hermes_cli import cron

        (tmp_cron_dir / "locks").mkdir()
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_cron_dir / "locks"))
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        monkeypatch.setattr("hermes_cli.gateway.named_profile_served_by_running_multiplexer", lambda: False)
        monkeypatch.setattr("gateway.status.is_gateway_runtime_lock_active", lambda lock_path=None: False)
        monkeypatch.setattr("gateway.host_topology.host_gateway_serving", lambda profile_name=None: None)
        monkeypatch.setattr(cron, "_active_cron_provider_name", lambda: "builtin")
        return tmp_cron_dir

    @staticmethod
    def _one_shot_tick(monkeypatch, exc=None):
        """Run the real one-shot path in a child process, so the stamped pid is dead afterwards —
        exactly what a status reader sees between two timer firings."""
        import multiprocessing

        from hermes_cli.cron import cron_tick

        monkeypatch.setattr("cron.scheduler.tick", _tick_raising(exc))
        child = multiprocessing.get_context("fork").Process(target=cron_tick)
        child.start()
        child.join(30)
        assert child.exitcode is not None

    def test_live_external_ticker_is_reported_healthy(self, no_gateway, monkeypatch, capsys):
        from cron import jobs
        from hermes_cli import cron

        self._one_shot_tick(monkeypatch)
        capsys.readouterr()
        assert jobs.ticker_heartbeat_writer_alive() is False  # the one-shot process is gone

        cron._warn_if_gateway_not_running()  # the `cron list` / `cron create` warning
        assert capsys.readouterr().out == ""
        cron.cron_status()
        out = capsys.readouterr().out
        assert "driven by an external ticker" in out and "it is live" in out
        assert "Ticker heartbeat:" in out
        assert "NOT fire" not in out
        assert "gateway install" not in out
        assert "may be failing" not in out

    def test_live_but_failing_external_ticker_is_flagged(self, no_gateway, monkeypatch, capsys):
        from hermes_cli import cron

        self._one_shot_tick(monkeypatch, OSError(13, "Permission denied"))
        capsys.readouterr()

        cron.cron_status()
        out = capsys.readouterr().out
        assert "driven by an external ticker" in out
        assert "no tick has succeeded yet" in out and "may be failing" in out
        assert "gateway install" not in out

    def test_stopped_external_ticker_is_dead_and_points_at_the_timer(self, no_gateway, monkeypatch, capsys):
        from cron import jobs
        from hermes_cli import cron

        self._one_shot_tick(monkeypatch)
        capsys.readouterr()
        old = time.time() - 3600
        for name in ("ticker_heartbeat", "ticker_last_success", "ticker_external"):
            (jobs.CRON_DIR / name).write_text(str(old))

        cron._warn_if_gateway_not_running()
        assert "Scheduler is not ready" in capsys.readouterr().out
        cron.cron_status()
        out = capsys.readouterr().out
        assert "No scheduler is serving profile" in out and "NOT fire" in out
        assert "driven by an external ticker" in out and "check that timer, not the gateway" in out
        assert "it is live" not in out

    def test_dead_writer_without_external_marker_is_still_not_a_scheduler(self, no_gateway, capsys):
        """A killed serve/Desktop ticker's fresh stamp must not be mistaken for an external ticker."""
        import subprocess
        import sys

        from cron import jobs
        from hermes_cli import cron

        dead = subprocess.Popen([sys.executable, "-c", ""], stdin=subprocess.DEVNULL)
        dead.wait()
        (jobs.CRON_DIR).mkdir(exist_ok=True)
        (jobs.CRON_DIR / "ticker_heartbeat").write_text(f"{time.time()} {dead.pid}")

        assert cron._builtin_gateway_liveness() is False
        cron.cron_status()
        out = capsys.readouterr().out
        assert "No scheduler is serving profile" in out
        assert "external ticker" not in out
