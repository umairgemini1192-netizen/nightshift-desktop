"""Tests for Phase 6 packaging, self-test, icon generation, autostart, tray, and first-run import."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from types import SimpleNamespace

from nightshift.app import default_data_dir, main, parse_args, resolve_frontend_dist_index, run_self_test
from nightshift.autostart import AutostartManager, CommandResult
from nightshift.bridge import BridgeAPI
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store
from nightshift.tray import TrayController


def test_icon_generator_creates_valid_multi_size_ico(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    gen_path = repo_root / "backend" / "packaging" / "generate_icon.py"
    spec = importlib.util.spec_from_file_location("test_generate_icon", str(gen_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ico_path = tmp_path / "nightshift.ico"
    out = mod.generate_icon_ico(ico_path)
    assert out == ico_path
    data = ico_path.read_bytes()
    reserved, ico_type, count = struct.unpack("<HHH", data[:6])
    assert (reserved, ico_type, count) == (0, 1, 4)

    png = mod.generate_png_bytes(32)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_default_data_dir_uses_localappdata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_data_dir() == tmp_path / "NightShift"


def test_self_test_boots_engine_prints_ok_and_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    rc = main(["--self-test"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "OK"
    assert (tmp_path / "NightShift").is_dir()


def test_parse_args_supports_self_test_and_minimized() -> None:
    ns = parse_args(["--self-test", "--minimized"])
    assert ns.self_test is True
    assert ns.minimized is True


def test_resolve_frontend_dist_index_uses_meipass_when_frozen(tmp_path: Path, monkeypatch) -> None:
    dist_index = tmp_path / "frontend" / "dist" / "index.html"
    dist_index.parent.mkdir(parents=True, exist_ok=True)
    dist_index.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert resolve_frontend_dist_index() == dist_index


def test_autostart_manager_uses_per_user_schtasks_with_fake_runner(tmp_path: Path) -> None:
    recorded: list[list[str]] = []
    task_exists = False

    def fake_runner(cmd: list[str]) -> CommandResult:
        nonlocal task_exists
        recorded.append(cmd)
        action = cmd[1]
        if action == "/Query":
            return CommandResult(returncode=0 if task_exists else 1, stderr="" if task_exists else "ERROR: cannot find")
        if action == "/Create":
            task_exists = True
            return CommandResult(returncode=0, stdout="SUCCESS")
        if action == "/Delete":
            if not task_exists:
                return CommandResult(returncode=1, stderr="ERROR: The system cannot find the file specified.")
            task_exists = False
            return CommandResult(returncode=0, stdout="SUCCESS")
        return CommandResult(returncode=1, stderr="unexpected")

    exe = tmp_path / "NightShift.exe"
    mgr = AutostartManager(exe_path=exe, runner=fake_runner, platform="win32")

    assert mgr.is_enabled() is False

    ok, msg = mgr.set_enabled(True)
    assert ok is True
    assert "sign-in" in msg
    create_cmd = recorded[-1]
    assert create_cmd[0] == "schtasks"
    assert "/Create" in create_cmd
    assert "/SC" in create_cmd and create_cmd[create_cmd.index("/SC") + 1] == "ONLOGON"
    assert "/RL" in create_cmd and create_cmd[create_cmd.index("/RL") + 1] == "LIMITED"
    assert "/RL" in create_cmd and "HIGHEST" not in create_cmd
    assert f'"{exe}" --minimized' in create_cmd
    assert mgr.is_enabled() is True

    ok_off, _ = mgr.set_enabled(False)
    assert ok_off is True
    assert recorded[-1][:2] == ["schtasks", "/Delete"]
    assert mgr.is_enabled() is False

    # Disabling again when task is already gone succeeds cleanly
    ok_again, _ = mgr.set_enabled(False)
    assert ok_again is True


def test_autostart_manager_surfaces_error_on_failure() -> None:
    def failing_runner(_cmd: list[str]) -> CommandResult:
        return CommandResult(returncode=1, stderr="Access is denied.")

    mgr = AutostartManager(runner=failing_runner, platform="win32")
    ok, msg = mgr.enable()
    assert ok is False
    assert "Access is denied" in msg


def test_tray_controller_menu_actions_and_lifecycle() -> None:
    actions: list[str] = []
    armed = {"value": False}

    class FakeMenuItem:
        def __init__(self, text, action, default=False, checked=None):
            self.text = text
            self.action = action
            self.default = default
            self.checked = checked

    class FakeMenu:
        def __init__(self, *items):
            self.items = list(items)

    class FakeIcon:
        def __init__(self, name, icon, title, menu):
            self.name = name
            self.icon = icon
            self.title = title
            self.menu = menu
            self.detached = False
            self.stopped = False

        def run_detached(self):
            self.detached = True

        def stop(self):
            self.stopped = True

    fake_pystray = SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)

    tray = TrayController(
        on_open=lambda: actions.append("open"),
        on_stop_after_phase=lambda: (armed.__setitem__("value", not armed["value"]), actions.append("stop")),
        on_quit=lambda: actions.append("quit"),
        is_stop_armed=lambda: armed["value"],
        pystray_module=fake_pystray,
        image_factory=lambda: object(),
    )

    assert tray.start() is True
    assert tray.running is True
    assert tray._icon is not None
    assert tray._icon.detached is True

    items = tray._icon.menu.items
    assert [i.text for i in items] == ["Open", "Stop after current phase", "Quit"]
    assert items[0].default is True

    items[0].action(tray._icon, items[0])
    assert items[1].checked(items[1]) is False
    items[1].action(tray._icon, items[1])
    assert items[1].checked(items[1]) is True
    items[2].action(tray._icon, items[2])

    assert actions == ["open", "stop", "quit"]
    assert tray.running is False


def test_bridge_first_run_and_autostart_and_window_controls(tmp_path: Path) -> None:
    store = Store(tmp_path / "p6.db")
    store.migrate()
    events = EventBus(store=store)
    task_exists = False

    def fake_runner(cmd: list[str]) -> CommandResult:
        nonlocal task_exists
        if cmd[1] == "/Query":
            return CommandResult(returncode=0 if task_exists else 1)
        if cmd[1] == "/Create":
            task_exists = True
            return CommandResult(returncode=0)
        if cmd[1] == "/Delete":
            task_exists = False
            return CommandResult(returncode=0)
        return CommandResult(returncode=1)

    autostart = AutostartManager(exe_path=tmp_path / "NightShift.exe", runner=fake_runner, platform="win32")
    bridge = BridgeAPI(store, events, ModuleRegistry(), autostart=autostart)

    # First run is true when no projects and not dismissed
    fr = bridge.call("app.firstRunStatus")
    assert fr["ok"] and fr["data"]["isFirstRun"] is True

    # Dismissing first run marks onboarding completed
    assert bridge.call("app.dismissFirstRun")["ok"]
    assert bridge.call("app.firstRunStatus")["data"]["isFirstRun"] is False

    # Autostart toggle via BridgeAPI
    assert bridge.call("settings.getAutostart")["data"]["enabled"] is False
    en = bridge.call("settings.setAutostart", {"enabled": True})
    assert en["ok"] and en["data"]["enabled"] is True
    assert bridge.call("settings.getAutostart")["data"]["enabled"] is True
    dis = bridge.call("settings.setAutostart", {"enabled": False})
    assert dis["ok"] and dis["data"]["enabled"] is False

    # Window controls delegate cleanly to attached window
    win_calls: list[str] = []

    class FakeWindow:
        def minimize(self):
            win_calls.append("minimize")

        def maximize(self):
            win_calls.append("maximize")

        def restore(self):
            win_calls.append("restore")

        def destroy(self):
            win_calls.append("destroy")

    bridge.attach_window(FakeWindow())
    assert bridge.call("window.minimize")["ok"]
    assert bridge.call("window.toggleMaximize")["data"]["maximized"] is True
    assert bridge.call("window.toggleMaximize")["data"]["maximized"] is False
    assert bridge.call("window.close")["ok"]
    assert win_calls == ["minimize", "maximize", "restore", "destroy"]

    # Packaging spec exists and references NightShift.exe onedir windowed build
    spec_text = (Path(__file__).resolve().parents[1] / "packaging" / "nightshift.spec").read_text(encoding="utf-8")
    assert "console=False" in spec_text
    assert 'name="NightShift"' in spec_text
    assert "frontend/dist" in spec_text

    store.close()
