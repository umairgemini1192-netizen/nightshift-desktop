"""Tests for the phone_status module (BLUEPRINT section 5i)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nightshift.bridge import BridgeAPI
from nightshift.engine.credentials import MemoryCredentialStore
from nightshift.engine.phone_deck import build_phone_deck
from nightshift.events import Event, EventBus
from nightshift.modules.phone_status import (
    DEBOUNCE_SECONDS,
    HEARTBEAT_SECONDS,
    SECRET_KEY,
    PhoneStatusSender,
    post_json_follow_302,
)
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.wall = datetime(2026, 11, 26, 10, 45, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.now

    def epoch_ms(self) -> int:
        return int(self.now * 1000)

    def wall_clock(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


def _seed_store(tmp_path) -> Store:
    store = Store(tmp_path / "phone.db")
    store.migrate()
    store.create_project("proj-1", "DriveLift", "owner/drivelift", str(tmp_path))
    store.create_plan("plan-1", "proj-1", "Night Plan")
    store.upsert_phase(
        "p1",
        "plan-1",
        "Phase 1: Auth",
        "feat/p1",
        prompt="secret prompt should never leak",
        status="merged",
        position=1,
    )
    store.upsert_phase(
        "p2",
        "plan-1",
        "Phase 2: C:\\Users\\Owner\\secret\\file.py",
        "feat/p2",
        prompt="raw prompt",
        status="implementing",
        position=2,
    )
    store.record_step("p2", "implement", "codex", detail="raw log line C:\\secret\\path")
    for idx in range(5):
        store.upsert_phase(
            f"next-{idx}",
            "plan-1",
            f"Upcoming {idx}",
            f"feat/next-{idx}",
            status="pending",
            position=3 + idx,
        )
    return store


def test_phone_deck_builder_caps_and_redacts(tmp_path) -> None:
    store = _seed_store(tmp_path)
    creds = MemoryCredentialStore()
    creds.set_secret("test-api", "super-secret-token-value")
    store.upsert_phase(
        "p-parked",
        "plan-1",
        "Parked super-secret-token-value",
        "feat/parked",
        status="parked",
        position=9,
    )
    store.park_phase("p-parked", reason="raw stack trace detail", next_step="fix it")

    deck = build_phone_deck(store, "proj-1")
    assert deck["project"] == "DriveLift"
    assert deck["state"] == "attention"
    assert deck["now"]["phase"].startswith("Phase 2:")
    assert deck["now"]["step"] == "implement"
    assert deck["now"]["engine"] == "codex"
    assert "(estimate)" in deck["now"]["eta"]
    assert len(deck["needs_you"]) <= 3
    assert len(deck["next"]) <= 3
    serialized = json.dumps(deck)
    assert "super-secret-token-value" not in serialized
    assert "[REDACTED_SECRET]" in serialized
    assert "raw prompt" not in serialized
    assert "raw log line" not in serialized
    assert "raw stack trace detail" not in serialized
    assert "C:\\Users" not in serialized
    store.close()


def test_phone_deck_strips_relative_file_paths_but_keeps_plain_english(tmp_path) -> None:
    store = Store(tmp_path / "paths.db")
    store.migrate()
    store.create_project("p1", "UI/UX App", "owner/app", str(tmp_path))
    store.create_plan("plan-1", "p1", "Plan")
    store.upsert_phase(
        "ph1",
        "plan-1",
        "Refactor backend/nightshift/secret_file.py and src\\ui\\page.tsx",
        "feat/ph1",
        status="implementing",
        position=1,
    )
    deck = build_phone_deck(store, "p1")
    assert deck["project"] == "UI/UX App"
    assert "secret_file.py" not in deck["now"]["phase"]
    assert "page.tsx" not in deck["now"]["phase"]
    assert "[path removed]" in deck["now"]["phase"]
    store.close()


def test_debounce_urgent_and_heartbeat_with_fake_clock(tmp_path) -> None:
    store = _seed_store(tmp_path)
    creds = MemoryCredentialStore()
    clock = FakeClock()
    sent: list[dict[str, Any]] = []

    def fake_transport(_url: str, payload: dict[str, Any], _timeout: float) -> tuple[bool, str]:
        sent.append(payload)
        return True, "ok"

    sender = PhoneStatusSender(
        store,
        creds,
        clock=clock.monotonic,
        epoch_ms=clock.epoch_ms,
        wall_clock=clock.wall_clock,
        transport=fake_transport,
    )

    # Off by default: routine and urgent events do not send.
    assert sender.notify("run.started") is False
    assert sent == []

    sender.save_settings({"enabled": True, "url": "https://script.google.com/macros/s/abc/exec", "secret": "my-secret"})

    # First routine event sends immediately.
    assert sender.notify("phase.started") is True
    assert len(sent) == 1
    assert sent[0]["kind"] == "nightshift_deck"
    assert sent[0]["secret"] == "my-secret"

    # Second routine event within 60 s is debounced.
    clock.advance(10)
    assert sender.notify("runner.invoked") is False
    assert len(sent) == 1

    # Tick at +59 s total still does not flush.
    clock.advance(49)
    assert sender.tick(is_running=True) is False
    assert len(sent) == 1

    # Tick at +60 s flushes the debounced update.
    clock.advance(1)
    assert sender.tick(is_running=True) is True
    assert len(sent) == 2

    # Urgent event bypasses 60 s debounce immediately.
    clock.advance(5)
    assert sender.notify("phase.merged") is True
    assert len(sent) == 3

    # Heartbeat does not fire when not running, even after 10 min.
    clock.advance(HEARTBEAT_SECONDS + 1)
    assert sender.tick(is_running=False) is False
    assert len(sent) == 3

    # Heartbeat fires when running after 10 min.
    assert sender.tick(is_running=True) is True
    assert len(sent) == 4
    assert sent[-1]["seq"] > sent[-2]["seq"]
    store.close()


def test_sender_never_raises_and_redacts_errors(tmp_path) -> None:
    store = _seed_store(tmp_path)
    creds = MemoryCredentialStore()
    clock = FakeClock()

    def exploding_transport(url: str, payload: dict[str, Any], _timeout: float) -> tuple[bool, str]:
        raise RuntimeError(f"boom {url} {payload['secret']}")

    sender = PhoneStatusSender(
        store,
        creds,
        clock=clock.monotonic,
        epoch_ms=clock.epoch_ms,
        wall_clock=clock.wall_clock,
        transport=exploding_transport,
    )
    secret = "top-secret-bridge-key"
    url = "https://script.google.com/macros/s/private-id/exec"
    sender.save_settings({"enabled": True, "url": url, "secret": secret})

    res = sender.send_test(project_id="proj-1")
    assert res["ok"] is False
    assert res["lastResult"].startswith("failed:")
    assert secret not in res["lastResult"]
    assert url not in res["lastResult"]
    store.close()


def test_post_json_follows_302_redirect() -> None:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received.append(json.loads(body))
            self.send_response(302)
            self.send_header("Location", "/done")
            self.end_headers()

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format: str, *args: Any) -> None:
            _ = (format, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        ok, reason = post_json_follow_302(
            f"http://127.0.0.1:{port}/exec",
            {"secret": "s", "kind": "nightshift_deck", "seq": 1, "deck": {"project": "x"}},
            timeout=5.0,
        )
        assert ok is True
        assert reason == "ok"
        assert len(received) == 1
        assert received[0]["kind"] == "nightshift_deck"
    finally:
        server.shutdown()
        server.server_close()


def test_module_settings_round_trip_and_secret_masking(tmp_path) -> None:
    store = _seed_store(tmp_path)
    events = EventBus(store=store)
    registry = ModuleRegistry()
    discovered = registry.discover()
    assert "phone_status" in discovered

    creds = MemoryCredentialStore()
    bridge = BridgeAPI(store, events, registry, creds=creds)
    initial = bridge.call("phone_status.getSettings")["data"]
    assert initial["enabled"] is False
    assert initial["hasSecret"] is False

    secret_val = "my-dpapi-protected-secret"
    saved = bridge.call(
        "phone_status.saveSettings",
        {"enabled": True, "url": "https://script.google.com/macros/s/test/exec", "secret": secret_val},
    )["data"]
    assert saved["enabled"] is True
    assert saved["hasSecret"] is True
    assert "secret" not in saved
    assert secret_val not in json.dumps(saved)
    assert bridge._creds.get_secret(SECRET_KEY) == secret_val
    # Secret must not be stored in plaintext SQLite settings table.
    assert store.get_setting(SECRET_KEY) is None

    mod = registry._modules["phone_status"]
    registry.stop_all()
    assert mod._worker is None or not mod._worker.is_alive()
    store.close()


def test_post_json_rejects_invalid_scheme_and_html_login_redirect() -> None:
    ok, reason = post_json_follow_302("file:///C:/Windows/win.ini", {"secret": "s"}, timeout=2.0)
    assert ok is False
    assert "https" in reason

    class HtmlHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Sign in</body></html>")

        def log_message(self, format: str, *args: Any) -> None:
            _ = (format, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), HtmlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        ok_html, reason_html = post_json_follow_302(
            f"http://127.0.0.1:{port}/exec",
            {"secret": "s", "kind": "nightshift_deck", "seq": 1, "deck": {"project": "x"}},
            timeout=5.0,
        )
        assert ok_html is False
        assert "HTML" in reason_html
    finally:
        server.shutdown()
        server.server_close()


def test_sender_never_raises_even_if_store_fails(tmp_path) -> None:
    class BrokenStore:
        def get_setting(self, key: str, default: str | None = None) -> str | None:
            if key == "phone_status.enabled":
                return "1"
            if key == "phone_status.url":
                return "https://script.google.com/macros/s/test/exec"
            return default

        def set_setting(self, _key: str, _value: str) -> None:
            raise OSError("database is locked")

        def list_projects(self) -> list[dict[str, Any]]:
            raise OSError("database is locked")

    creds = MemoryCredentialStore()
    creds.set_secret(SECRET_KEY, "bridge-secret-1234")
    sender = PhoneStatusSender(BrokenStore(), creds)  # type: ignore[arg-type]
    assert sender.notify("run.started") is False
    assert sender.tick(is_running=True) is False


def test_urgent_engine_events_trigger_phone_status_immediately(tmp_path) -> None:
    import asyncio
    from unittest.mock import patch

    from nightshift.engine.logins import LoginHealthChecker
    from nightshift.engine.planner import PhaseDefinition
    from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
    from nightshift.engine.vcs import FakeGitGh
    from nightshift.quota.ledger import QuotaLedger
    from nightshift.runners import FakeRunner

    store = Store(tmp_path / "engine_phone.db")
    store.migrate()
    events = EventBus(store=store)
    quota = QuotaLedger(store)
    vcs = FakeGitGh()
    vcs.branch_protection["main"] = True

    def on_call(_prompt: str, _wd: Any, _model: Any, _effort: str) -> None:
        vcs.add_pr("feat/p1", number=77)

    runners = {
        "agy": FakeRunner(name="agy", on_call=on_call),
        "codex": FakeRunner.success("codex"),
    }
    logins = LoginHealthChecker(
        store=store,
        quota_ledger=quota,
        command_runner=lambda _cmd, _to: (0, "Logged in to github.com\nOK", "", False),
    )
    engine = EngineStateMachine(store, events, quota, vcs=vcs, runners=runners, login_checker=logins)

    sent_events: list[tuple[str, dict[str, Any]]] = []
    creds = MemoryCredentialStore()
    clock = FakeClock()
    sender = PhoneStatusSender(
        store,
        creds,
        clock=clock.monotonic,
        epoch_ms=clock.epoch_ms,
        wall_clock=clock.wall_clock,
        transport=lambda _url, payload, _timeout: (sent_events.append((payload["deck"]["state"], payload)) or True, "ok"),
    )
    sender.save_settings({"enabled": True, "url": "https://script.google.com/macros/s/demo/exec", "secret": "sec-9999"})

    received_kinds: list[str] = []

    def forward(ev: Event) -> None:
        received_kinds.append(ev.kind)
        pid = ev.payload.get("project_id") if isinstance(ev.payload, dict) else None
        sender.notify(ev.kind, project_id=pid, phase_id=ev.phase_id)

    events.subscribe("*", forward)

    project = ProjectConfig(
        id="proj_phone",
        name="DriveLift",
        repo="owner/drivelift",
        local_path=tmp_path / "repo",
        output_dir=tmp_path / "out",
    )
    project.local_path.mkdir(parents=True, exist_ok=True)
    phases = [PhaseDefinition(id="p1", title="Phone Bridge", branch="feat/p1", prompt="Build it", auto_merge=True)]

    with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"), patch(
        "nightshift.engine.logins.resolve_bin", return_value="tool.exe"
    ), patch(
        "nightshift.engine.state_machine.shutil.disk_usage",
        return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
    ):
        asyncio.run(engine.execute_plan(project, phases, plan_id="plan_phone"))

    assert "run.started" in received_kinds
    assert "phase.merged" in received_kinds
    assert "phase.completed" in received_kinds
    assert "run.finished" in received_kinds
    # Urgent events bypass the 60 s debounce even when fake clock never advances.
    assert len(sent_events) >= 4
    assert sent_events[-1][0] == "good"
    assert sent_events[-1][1]["deck"]["project"] == "DriveLift"
    store.close()
