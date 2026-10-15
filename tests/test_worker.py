from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import worker


def test_worker_once_runs_backfill_and_honors_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    run_once = Mock(return_value={"processed": 3})
    monkeypatch.setattr(sys, "argv", ["worker", "--once", "--concurrency", "4"])
    monkeypatch.setattr(worker, "run_worker_once", run_once)

    worker.main()

    run_once.assert_called_once_with(concurrency=4, run_backfill=True)


def test_sync_once_recovers_enqueues_runs_and_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    recover = Mock(return_value={"analysis": 1})
    enqueue = Mock(return_value=True)
    run_pending = Mock(return_value=[{"id": 5}])
    sweep = Mock(return_value=2)
    monkeypatch.setattr(sys, "argv", ["worker", "--sync-once"])
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(session))
    monkeypatch.setattr(worker, "recover_interrupted_jobs", recover)
    monkeypatch.setattr(worker, "maybe_enqueue_scheduled_sync", enqueue)
    monkeypatch.setattr(worker, "run_pending_sync_jobs", run_pending)
    monkeypatch.setattr(worker, "sweep_blocker_lifecycle", sweep)

    worker.main()

    recover.assert_called_once_with(session)
    enqueue.assert_called_once_with(session)
    run_pending.assert_called_once_with(session, limit=3)
    sweep.assert_called_once_with(session)


def test_reap_stale_jobs_obeys_lease_check_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    recover = Mock()
    ticks = iter([100.0, 200.0, 400.0])
    monkeypatch.setattr(worker, "_last_lease_check", 0.0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(worker_job_lease_minutes=45))
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(session))
    monkeypatch.setattr(worker, "recover_interrupted_jobs", recover)

    worker._reap_stale_jobs()
    worker._reap_stale_jobs()
    worker._reap_stale_jobs()

    recover.assert_called_once_with(session, max_age_minutes=45)


def test_backfill_interval_runs_once_then_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([100.0, 120.0, 160.0])
    monkeypatch.setattr(worker, "_last_backfill_at", 0.0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(ai_backfill_interval_seconds=60))

    assert worker._should_run_backfill() is True
    assert worker._should_run_backfill() is False
    assert worker._should_run_backfill() is True


def test_daemon_worker_continues_after_a_failed_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    settings = SimpleNamespace(
        ai_worker_concurrency=2,
        ai_worker_poll_seconds=0,
        ai_backfill_interval_seconds=0,
    )
    recover = Mock(return_value={"sync": 1})
    run_once = Mock(side_effect=RuntimeError("worker cycle failed"))
    log_warning = Mock()
    log_error = Mock()
    monkeypatch.setattr(sys, "argv", ["worker"])
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(session))
    monkeypatch.setattr(worker, "recover_interrupted_jobs", recover)
    monkeypatch.setattr(worker, "_reap_stale_jobs", Mock())
    monkeypatch.setattr(worker, "maybe_enqueue_scheduled_sync", Mock())
    monkeypatch.setattr(worker, "run_pending_sync_jobs", Mock())
    monkeypatch.setattr(worker, "maybe_send_scheduled_reminders", Mock())
    monkeypatch.setattr(worker, "sweep_blocker_lifecycle", Mock())
    monkeypatch.setattr(worker, "run_worker_once", run_once)
    monkeypatch.setattr(worker.logger, "warning", log_warning)
    monkeypatch.setattr(worker.logger, "error", log_error)

    def stop_after_one_cycle(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop_after_one_cycle)
    with pytest.raises(KeyboardInterrupt):
        worker.main()

    recover.assert_called_once_with(session)
    run_once.assert_called_once()
    assert log_warning.called
    log_error.assert_called_once()
