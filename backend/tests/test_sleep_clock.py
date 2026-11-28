"""Suspend-aware clock and subprocess timeout coverage."""
import subprocess

from nightshift.clock import SleepClock
from nightshift.runners import base


def test_sleep_clock_excludes_suspend_from_budget_and_detects_jump():
    values = {"active": 10.0, "wall": 100.0}
    clock = SleepClock(lambda: values["active"], lambda: values["wall"])
    values.update(active=15.0, wall=3705.0)
    assert clock.elapsed() == 5.0
    assert clock.jumped()
    assert not clock.jumped()
    values.update(active=20.0, wall=3710.0)
    assert clock.elapsed() == 10.0
    assert not clock.jumped()


def test_runner_timeout_survives_suspend(monkeypatch):
    values = {"active": 0.0, "wall": 0.0}
    clock = SleepClock(lambda: values["active"], lambda: values["wall"])
    calls = []

    class Process:
        pid = 1
        returncode = 0

        def communicate(self, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                values.update(active=2.0, wall=3602.0)
                raise subprocess.TimeoutExpired("runner", timeout)
            return "done", ""

    monkeypatch.setattr(base, "SleepClock", lambda: clock)
    monkeypatch.setattr(base, "resolve_bin", lambda _: "runner")
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(base, "_attach_windows_kill_on_close_job", lambda _: None)
    assert base.run_subprocess_with_timeout(["runner"], timeout_seconds=10) == (0, "done", "", False)
    assert calls == [5.0, 5.0]


def test_ci_wait_ignores_sleep_and_rechecks_logins(tmp_path, monkeypatch):
    from test_circleci_post_merge_check import (
        MERGE_SHA, _SequencedChecks, _engine, _jobs, _project,
    )
    import nightshift.engine.state_machine as state_machine

    values = {"active": 0.0, "wall": 0.0}
    vcs = _SequencedChecks([[], _jobs(MERGE_SHA, branch="main")])
    store, engine = _engine(tmp_path, vcs)
    checks = []
    monkeypatch.setattr(state_machine, "SleepClock", lambda: SleepClock(
        lambda: values["active"], lambda: values["wall"]
    ))
    engine._ci_clock = lambda: values["active"]
    engine._ci_poll_sleep = lambda _: values.update(active=2.0, wall=3602.0)
    engine.logins.check_all = lambda **kw: checks.append(kw) or {}
    try:
        ok, _ = engine._wait_for_circleci_main(_project(tmp_path, tmp_path), MERGE_SHA, timeout_seconds=10)
        assert ok is True
        assert vcs.post_merge_polls == 2
        assert checks == [{"repo": "owner/nightshift", "force": True}]
    finally:
        store.close()
