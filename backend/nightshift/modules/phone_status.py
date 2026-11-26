"""Phone status bridge module (BLUEPRINT section 5i).

Sends a strictly redacted, project-scoped deck summary to the owner's own
Google Apps Script web app, never blocking the engine and off by default.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from nightshift.engine.credentials import CredentialStore, get_credential_store
from nightshift.engine.phone_deck import build_phone_deck
from nightshift.engine.verify import redact_secrets
from nightshift.events import Event
from nightshift.modules.base import ModuleBase
from nightshift.store import Store

DEBOUNCE_SECONDS = 60.0
HEARTBEAT_SECONDS = 600.0
TIMEOUT_SECONDS = 20.0

SETTING_ENABLED = "phone_status.enabled"
SETTING_URL = "phone_status.url"
SETTING_LAST_RESULT = "phone_status.last_result"
SETTING_LAST_RESULT_AT = "phone_status.last_result_at"
SECRET_KEY = "phone_status.secret"

URGENT_EVENTS = frozenset({
    "run.started",
    "run.stopped",
    "run.completed",
    "run.finished",
    "circuit_breaker.tripped",
    "phase.merged",
    "phase.completed",
    "phase.parked",
    "release.published",
})

TransportFn = Callable[[str, dict[str, Any], float], tuple[bool, str]]


def _scrub_reason(reason: str, *, url: str = "", secret: str = "") -> str:
    cleaned = redact_secrets(str(reason or ""))
    if secret and secret in cleaned:
        cleaned = cleaned.replace(secret, "[REDACTED_SECRET]")
    if url and url in cleaned:
        cleaned = cleaned.replace(url, "[url]")
    return cleaned.strip()[:80]


def _is_allowed_bridge_url(url: str) -> bool:
    lowered = url.strip().lower()
    if lowered.startswith("https://"):
        return True
    if lowered.startswith("http://127.0.0.1") or lowered.startswith("http://localhost"):
        return True
    return False


def post_json_follow_302(url: str, payload: dict[str, Any], timeout: float = TIMEOUT_SECONDS) -> tuple[bool, str]:
    """POST JSON to the Apps Script web app, following its 302 redirect.

    Never raises; returns ``(ok, short_reason)`` with secrets redacted.
    """
    secret = str(payload.get("secret") or "") if isinstance(payload, dict) else ""
    if not _is_allowed_bridge_url(url):
        return False, "invalid URL (use https://)"
    try:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 200
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if status >= 400:
                return False, f"HTTP {status}"
            if raw:
                low_raw = raw.lstrip().lower()
                if low_raw.startswith("<!doctype html") or low_raw.startswith("<html"):
                    return False, "HTML response (set Web app access to Anyone)"
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and parsed.get("ok") is False:
                        err = _scrub_reason(str(parsed.get("error") or "rejected"), url=url, secret=secret)
                        return False, err or "rejected"
                except ValueError:
                    pass
            return True, "ok"
    except urllib.error.HTTPError as exc:
        if exc.code == 302:
            return True, "ok"
        return False, f"HTTP {exc.code}"
    except (TimeoutError, socket.timeout):
        return False, "timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return False, "timeout"
        reason = _scrub_reason(str(exc.reason or exc), url=url, secret=secret)
        return False, reason or "network error"
    except Exception as exc:
        reason = _scrub_reason(str(exc), url=url, secret=secret)
        return False, reason or "error"


class PhoneStatusSender:
    """Debounced, urgent-aware, non-blocking sender for the 5e phone deck."""

    def __init__(
        self,
        store: Store | None = None,
        creds: CredentialStore | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        epoch_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        transport: TransportFn | None = None,
    ) -> None:
        self._store = store
        self._creds = creds
        self._clock = clock
        self._epoch_ms = epoch_ms
        self._wall_clock = wall_clock
        self._transport: TransportFn = transport or post_json_follow_302
        self._lock = threading.Lock()
        self._last_sent_mono: float | None = None
        self._last_seq: int = 0
        self._pending: bool = False
        self._pending_project_id: str | None = None

    def attach(self, store: Store | None, creds: CredentialStore | None = None) -> None:
        with self._lock:
            self._store = store
            if creds is not None:
                self._creds = creds

    def _get_creds(self) -> CredentialStore:
        if self._creds is None:
            self._creds = get_credential_store()
        return self._creds

    def get_settings(self) -> dict[str, Any]:
        if self._store is None:
            return {
                "enabled": False,
                "url": "",
                "hasSecret": False,
                "lastResult": "not sent yet",
                "lastResultAt": "",
            }
        enabled = self._store.get_setting(SETTING_ENABLED, "0") == "1"
        url = self._store.get_setting(SETTING_URL, "") or ""
        last_result = self._store.get_setting(SETTING_LAST_RESULT, "not sent yet") or "not sent yet"
        last_result_at = self._store.get_setting(SETTING_LAST_RESULT_AT, "") or ""
        has_secret = bool(self._get_creds().get_secret(SECRET_KEY))
        return {
            "enabled": enabled,
            "url": url,
            "hasSecret": has_secret,
            "lastResult": last_result,
            "lastResultAt": last_result_at,
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("Store is not attached")
        if "enabled" in payload:
            self._store.set_setting(SETTING_ENABLED, "1" if payload["enabled"] else "0")
        if "url" in payload:
            self._store.set_setting(SETTING_URL, str(payload["url"] or "").strip())
        creds = self._get_creds()
        if payload.get("clearSecret"):
            creds.delete_secret(SECRET_KEY)
        elif "secret" in payload and str(payload["secret"] or "").strip():
            creds.set_secret(SECRET_KEY, str(payload["secret"]).strip())
        return self.get_settings()

    def _is_configured(self) -> tuple[bool, str, str]:
        if self._store is None:
            return False, "", ""
        if self._store.get_setting(SETTING_ENABLED, "0") != "1":
            return False, "", ""
        url = (self._store.get_setting(SETTING_URL, "") or "").strip()
        secret = (self._get_creds().get_secret(SECRET_KEY) or "").strip()
        if not url or not secret:
            return False, url, secret
        return True, url, secret

    def _record_result(self, ok: bool, detail: str, *, url: str = "", secret: str = "") -> str:
        now = self._wall_clock()
        hhmm = now.strftime("%H:%M")
        safe_detail = _scrub_reason(detail, url=url, secret=secret) or "error"
        result = f"ok {hhmm}" if ok else f"failed: {safe_detail}"
        if self._store is not None:
            try:
                self._store.set_setting(SETTING_LAST_RESULT, result)
                self._store.set_setting(SETTING_LAST_RESULT_AT, now.isoformat())
            except Exception:
                pass
        return result

    def _resolve_project_id(self, project_id: str | None, phase_id: str | None = None) -> str:
        if project_id:
            return project_id
        if self._store is not None:
            if phase_id and hasattr(self._store, "get_phase") and hasattr(self._store, "get_plan"):
                try:
                    phase = self._store.get_phase(phase_id)
                    if phase and phase.get("plan_id"):
                        plan = self._store.get_plan(str(phase["plan_id"]))
                        if plan and plan.get("project_id"):
                            return str(plan["project_id"])
                except Exception:
                    pass
            try:
                projects = self._store.list_projects()
                if projects:
                    return str(projects[0]["id"])
            except Exception:
                pass
        return "nightshift"

    def _send(
        self,
        url: str,
        secret: str,
        project_id: str | None = None,
        phase_id: str | None = None,
    ) -> tuple[bool, str]:
        try:
            if self._store is None:
                return False, self._record_result(False, "store unavailable", url=url, secret=secret)
            pid = self._resolve_project_id(project_id, phase_id=phase_id)
            deck = build_phone_deck(self._store, pid)
            with self._lock:
                seq = max(self._epoch_ms(), self._last_seq + 1)
                self._last_seq = seq
                self._last_sent_mono = self._clock()
                self._pending = False
                self._pending_project_id = None

            payload = {
                "secret": secret,
                "kind": "nightshift_deck",
                "seq": seq,
                "deck": deck,
            }
            ok, reason = self._transport(url, payload, TIMEOUT_SECONDS)
        except Exception as exc:
            ok = False
            reason = _scrub_reason(str(exc), url=url, secret=secret) or "error"

        result = self._record_result(ok, reason, url=url, secret=secret)
        return ok, result

    def send_test(self, project_id: str | None = None) -> dict[str, Any]:
        """Send a test update immediately, regardless of debounce or toggle."""
        if self._store is None:
            return {"ok": False, "lastResult": "failed: store unavailable", "lastResultAt": ""}
        url = (self._store.get_setting(SETTING_URL, "") or "").strip()
        secret = (self._get_creds().get_secret(SECRET_KEY) or "").strip()
        if not url or not secret:
            res = self._record_result(False, "missing URL or secret", url=url, secret=secret)
            return {
                "ok": False,
                "lastResult": res,
                "lastResultAt": self._store.get_setting(SETTING_LAST_RESULT_AT, "") or "",
            }
        ok, res = self._send(url, secret, project_id=project_id)
        return {
            "ok": ok,
            "lastResult": res,
            "lastResultAt": self._store.get_setting(SETTING_LAST_RESULT_AT, "") or "",
        }

    def notify(
        self,
        event_kind: str,
        *,
        project_id: str | None = None,
        phase_id: str | None = None,
    ) -> bool:
        """Handle an engine event; urgent events send immediately, others debounce 60 s."""
        try:
            configured, url, secret = self._is_configured()
            if not configured:
                return False

            urgent = event_kind in URGENT_EVENTS
            now = self._clock()
            with self._lock:
                can_send_now = urgent or self._last_sent_mono is None or (now - self._last_sent_mono) >= DEBOUNCE_SECONDS
                if not can_send_now:
                    self._pending = True
                    if project_id:
                        self._pending_project_id = project_id
                    return False

            ok, _ = self._send(url, secret, project_id=project_id, phase_id=phase_id)
            return ok
        except Exception:
            return False

    def tick(self, *, is_running: bool = False, project_id: str | None = None) -> bool:
        """Flush a debounced update after 60 s or emit a 10-min heartbeat while running."""
        try:
            configured, url, secret = self._is_configured()
            if not configured:
                return False

            now = self._clock()
            with self._lock:
                elapsed = (now - self._last_sent_mono) if self._last_sent_mono is not None else None
                due_pending = self._pending and (elapsed is None or elapsed >= DEBOUNCE_SECONDS)
                due_heartbeat = is_running and (elapsed is None or elapsed >= HEARTBEAT_SECONDS)
                target_project = self._pending_project_id or project_id

            if due_pending or due_heartbeat:
                ok, _ = self._send(url, secret, project_id=target_project)
                return ok
            return False
        except Exception:
            return False


class Module(ModuleBase):
    """Pluggable ``phone_status`` module (BLUEPRINT sections 4 & 5i)."""

    id = "phone_status"

    def __init__(self) -> None:
        self.sender = PhoneStatusSender()
        self._engine: Any = None
        self._queue: list[tuple[str, str | None, str | None]] = []
        self._queue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def configure(
        self,
        *,
        store: Any = None,
        events: Any = None,
        creds: Any = None,
        engine: Any = None,
    ) -> None:
        _ = events
        self._engine = engine
        self.sender.attach(store, creds)
        if store is not None and (self._worker is None or not self._worker.is_alive()):
            self._stop.clear()
            self._worker = threading.Thread(target=self._run_loop, daemon=True, name="PhoneStatusWorker")
            self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                with self._queue_lock:
                    items = list(self._queue)
                    self._queue.clear()
                for kind, pid, phase_id in items:
                    self.sender.notify(kind, project_id=pid, phase_id=phase_id)
                is_running = bool(self._engine and getattr(self._engine, "is_running", False))
                self.sender.tick(is_running=is_running)
            except Exception:
                # Never allow background sender errors to crash or disrupt the engine.
                pass

    def api(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "getSettings": lambda _args: self.sender.get_settings(),
            "saveSettings": self.sender.save_settings,
            "sendTest": lambda args: self.sender.send_test(project_id=args.get("projectId")),
        }

    def on_event(self, event: Event) -> None:
        pid = event.payload.get("project_id") if isinstance(event.payload, dict) else None
        with self._queue_lock:
            self._queue.append((event.kind, pid, event.phase_id))
        self._wake.set()
