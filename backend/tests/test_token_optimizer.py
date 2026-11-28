"""Tests for the Token Optimizer (BLUEPRINT section 5g).

Covers:
- tools.lock.json policy & SHA-256 hash verification (mismatch refusal + warning)
- No-global-write guard using a fake home directory hashed before/after
- agy-fast isolation probe logic with fake agy (--settings, project-local, mutation, failure)
- ccusage --offline --json parsing, CLI cross-check, and Antigravity ledger fallback
- repomix --compress repo map generation, token budget cap, and .git/info/exclude
- rtk-oss Apache-2.0 OSS asset enforcement + portable rg.exe runner-only PATH wiring
- A/B framework decision table, auto-disable, deck summary, and morning digest
- Backend module discovery & BridgeAPI RPC dispatch
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from nightshift.bridge import BridgeAPI
from nightshift.engine.deck import generate_deck_data, write_morning_digest
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.optimizer.ab_framework import ABFramework, evaluate_ab_samples
from nightshift.optimizer.agy_fast import (
    build_agy_fast_session_args,
    probe_agy_isolation,
    snapshot_directory_hashes,
)
from nightshift.optimizer.ccusage import CcusageAdapter, parse_ccusage_json
from nightshift.optimizer.repomix import (
    RepomixAdapter,
    ensure_worktree_git_excluded,
    estimate_tokens,
    truncate_to_token_budget,
)
from nightshift.optimizer.rtk import build_rtk_runner_env
from nightshift.optimizer.tool_manager import (
    FORBIDDEN_NON_OSS_RTK_ASSET,
    REQUIRED_AGY_FAST_COMMIT,
    REQUIRED_OSS_RTK_ASSET,
    ToolManager,
    compute_sha256,
    resolve_lockfile_path,
)
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "nightshift.db")
    store.migrate()
    return store


def _write_lockfile(tmp_path: Path, tools: dict[str, Any]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path = tmp_path / "tools.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": {
                    "telemetry_env": {"DO_NOT_TRACK": "1"},
                    "no_global_installs": True,
                    "no_global_config_writes": True,
                    "auto_update": False,
                },
                "tools": tools,
            }
        ),
        encoding="utf-8",
    )
    return lock_path


def test_repo_tools_lockfile_enforces_blueprint_5g_policy() -> None:
    lock_path = resolve_lockfile_path()
    assert lock_path.exists()
    tm = ToolManager(lockfile_path=lock_path)

    rtk = tm.get_spec("rtk-oss")
    assert rtk is not None
    assert REQUIRED_OSS_RTK_ASSET in rtk.artifact
    assert FORBIDDEN_NON_OSS_RTK_ASSET not in rtk.artifact
    assert rtk.license == "Apache-2.0"
    assert set(rtk.runners) == {"gemini", "claude", "codex"}

    agy_fast = tm.get_spec("agy-fast")
    assert agy_fast is not None
    assert agy_fast.commit == REQUIRED_AGY_FAST_COMMIT
    assert agy_fast.auto_update is False
    assert agy_fast.isolation_gate_required is True

    ast_grep = tm.get_spec("ast-grep-mcp")
    assert ast_grep is not None
    assert ast_grep.catalogue_only is True
    assert ast_grep.enabled_by_default is False

    rg = tm.get_spec("ripgrep")
    assert rg is not None
    assert rg.executable == "rg.exe"

    env = tm.build_process_env()
    assert env["DO_NOT_TRACK"] == "1"


def test_tool_manager_sha256_verification_and_mismatch_refusal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    good_bytes = b"pinned-repomix-payload-v0.3.9"
    good_sha = hashlib.sha256(good_bytes).hexdigest()

    lock_path = _write_lockfile(
        tmp_path,
        {
            "repomix": {
                "display_name": "repomix",
                "role": "Compact repo map",
                "version": "0.3.9",
                "artifact": "repomix-0.3.9.tgz",
                "url": "https://example.invalid/repomix-0.3.9.tgz",
                "sha256": good_sha,
                "executable": "repomix.cmd",
                "flags": ["--compress"],
                "auto_update": False,
            }
        },
    )

    downloads: list[str] = []

    def fake_download(url: str, dest: Path) -> Path:
        downloads.append(url)
        dest.write_bytes(good_bytes)
        return dest

    tools_root = tmp_path / "localappdata" / "NightShift" / "tools"
    tm = ToolManager(store=store, tools_root=tools_root, lockfile_path=lock_path, downloader=fake_download)

    # First call downloads once and verifies SHA-256
    res1 = tm.ensure_installed("repomix")
    assert res1.ok is True
    assert res1.actual_sha256 == good_sha
    assert len(downloads) == 1
    assert (tools_root / "repomix" / "0.3.9" / "repomix-0.3.9.tgz").exists()

    # Second call reuses local copy without re-downloading
    res2 = tm.ensure_installed("repomix")
    assert res2.ok is True
    assert len(downloads) == 1

    # Tamper artifact -> SHA-256 mismatch refuses to run, disables tool, records plain-English warning
    store.upsert_token_tool_state("repomix", enabled=True)
    tm.artifact_path("repomix").write_bytes(b"corrupted-payload")
    res_bad = tm.verify_installed("repomix")
    assert res_bad.ok is False
    assert res_bad.actual_sha256 != good_sha
    assert "SHA-256" in res_bad.warning
    state = store.get_token_tool_state("repomix")
    assert state is not None
    assert state["enabled"] == 0
    assert state["sha256_verified"] == 0
    assert "SHA-256" in state["warning"]


def test_no_global_write_guard_fake_home_before_after(tmp_path: Path) -> None:
    """Verify tool operations and isolation probes never modify ~/.gemini/config."""
    fake_home = tmp_path / "fake_home"
    gemini_cfg = fake_home / ".gemini" / "config"
    (gemini_cfg / "plugins").mkdir(parents=True)
    (gemini_cfg / "mcp_config.json").write_text('{"servers": {}}', encoding="utf-8")
    (gemini_cfg / "plugins" / "existing.json").write_text('{"name": "existing"}', encoding="utf-8")

    before_hashes = snapshot_directory_hashes(gemini_cfg)

    store = _make_store(tmp_path)
    payload = b"fake-agy-fast-bundle"
    lock_path = _write_lockfile(
        tmp_path,
        {
            "agy-fast": {
                "display_name": "agy-fast",
                "version": REQUIRED_AGY_FAST_COMMIT,
                "commit": REQUIRED_AGY_FAST_COMMIT,
                "artifact": "agy-fast.zip",
                "url": "https://example.invalid/agy-fast.zip",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "executable": "agy-fast.js",
                "auto_update": False,
                "isolation_gate_required": True,
            }
        },
    )
    tm = ToolManager(
        store=store,
        tools_root=tmp_path / "tools",
        lockfile_path=lock_path,
        downloader=lambda _u, d: (d.write_bytes(payload), d)[1],
    )
    tm.ensure_installed("agy-fast")

    def clean_fake_agy(cmd: list[str], cwd: Path | None, env: dict[str, str], _timeout: int):
        assert env.get("DO_NOT_TRACK") == "1"
        assert env.get("AGY_FAST_AUTO_UPDATE") == "0"
        if "--settings" in cmd:
            return 0, '{"reply": "AGY_FAST_LOADED"}', "", False
        return 0, '{"reply": "NO"}', "", False

    probe = probe_agy_isolation(tool_manager=tm, home_dir=fake_home, executor=clean_fake_agy)
    assert probe.passed is True
    assert probe.method == "settings_flag"
    assert probe.global_config_modified is False

    after_hashes = snapshot_directory_hashes(gemini_cfg)
    assert before_hashes == after_hashes


def test_agy_isolation_probe_scenarios(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    gemini_cfg = fake_home / ".gemini" / "config"
    gemini_cfg.mkdir(parents=True)
    (gemini_cfg / "mcp_config.json").write_text('{"v": 1}', encoding="utf-8")

    lock_path = _write_lockfile(
        tmp_path,
        {
            "agy-fast": {
                "display_name": "agy-fast",
                "version": REQUIRED_AGY_FAST_COMMIT,
                "commit": REQUIRED_AGY_FAST_COMMIT,
                "artifact": "agy-fast.zip",
                "url": "https://example.invalid/agy-fast.zip",
                "sha256": "a" * 64,
                "executable": "agy-fast.js",
                "auto_update": False,
                "isolation_gate_required": True,
            }
        },
    )
    tm = ToolManager(tools_root=tmp_path / "tools", lockfile_path=lock_path)

    # 1. Fails --settings, succeeds via project-local folder
    def project_local_agy(cmd: list[str], cwd: Path | None, _env: dict[str, str], _timeout: int):
        if "--settings" in cmd:
            return 2, "", "unknown option --settings", False
        if cwd and (cwd / ".gemini" / "plugins" / "agy-fast" / "plugin.json").exists():
            return 0, "AGY_FAST_LOADED", "", False
        return 0, "NOT_LOADED", "", False

    res_local = probe_agy_isolation(tool_manager=tm, home_dir=fake_home, executor=project_local_agy)
    assert res_local.passed is True
    assert res_local.method == "project_local"
    assert res_local.global_config_modified is False

    # 2. Mutates ~/.gemini/config -> MUST fail isolation gate even if output says loaded
    def mutating_agy(_cmd: list[str], _cwd: Path | None, _env: dict[str, str], _timeout: int):
        (gemini_cfg / "mcp_config.json").write_text('{"v": 2, "mutated": true}', encoding="utf-8")
        return 0, "AGY_FAST_LOADED", "", False

    res_mutated = probe_agy_isolation(tool_manager=tm, home_dir=fake_home, executor=mutating_agy)
    assert res_mutated.passed is False
    assert res_mutated.global_config_modified is True
    assert "modified ~/.gemini/config" in res_mutated.reason

    # 3. Neither candidate loads -> stays disabled with plain-English explanation
    def unsupported_agy(_cmd: list[str], _cwd: Path | None, _env: dict[str, str], _timeout: int):
        return 1, "", "plugin not supported", False

    res_fail = probe_agy_isolation(tool_manager=tm, home_dir=fake_home, executor=unsupported_agy)
    assert res_fail.passed is False
    assert res_fail.method == "none"
    assert "stayed disabled" in res_fail.reason

    # 4. Session args helper creates git-excluded .nightshift settings file
    wt = tmp_path / "wt"
    wt.mkdir()
    args, env = build_agy_fast_session_args(tm, wt, method="settings_flag")
    assert args[0] == "--settings"
    assert Path(args[1]).exists()
    assert env["DO_NOT_TRACK"] == "1"


def test_rtk_oss_policy_and_portable_ripgrep_runner_path_wiring(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    rtk_zip = _make_zip_bytes({"rtk.exe": b"fake-rtk-oss-binary"})
    rg_zip = _make_zip_bytes({"rg.exe": b"fake-rg-binary"})

    lock_path = _write_lockfile(
        tmp_path,
        {
            "rtk-oss": {
                "display_name": "rtk-oss",
                "version": "1.2.1",
                "artifact": REQUIRED_OSS_RTK_ASSET,
                "url": f"https://github.com/rtk-ai/rtk/releases/download/v1.2.1/{REQUIRED_OSS_RTK_ASSET}",
                "sha256": hashlib.sha256(rtk_zip).hexdigest(),
                "executable": "rtk.exe",
                "windows_binary_available": True,
                "requires_tools": ["ripgrep"],
                "runners": ["gemini", "claude", "codex"],
                "auto_update": False,
                "license": "Apache-2.0",
            },
            "ripgrep": {
                "display_name": "ripgrep (rg.exe)",
                "version": "14.1.1",
                "artifact": "ripgrep-14.1.1-x86_64-pc-windows-msvc.zip",
                "url": "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip",
                "sha256": hashlib.sha256(rg_zip).hexdigest(),
                "executable": "rg.exe",
                "windows_binary_available": True,
                "auto_update": False,
                "license": "MIT/Unlicense",
            },
        },
    )

    payloads = {
        REQUIRED_OSS_RTK_ASSET: rtk_zip,
        "ripgrep-14.1.1-x86_64-pc-windows-msvc.zip": rg_zip,
    }

    def dl(url: str, dest: Path) -> Path:
        for key, data in payloads.items():
            if key in url:
                dest.write_bytes(data)
                return dest
        raise RuntimeError(url)

    tm = ToolManager(store=store, tools_root=tmp_path / "tools", lockfile_path=lock_path, downloader=dl)
    assert tm.ensure_installed("rtk-oss").ok is True
    assert tm.ensure_installed("ripgrep").ok is True

    global_path_before = os.environ.get("PATH", "")

    # Wired for gemini, claude, codex ONLY
    for runner in ("gemini", "claude", "codex"):
        res = build_rtk_runner_env(tm, runner, base_path="C:\\Windows\\System32")
        assert res.enabled is True
        assert res.env["DO_NOT_TRACK"] == "1"
        assert res.env["RTK_TELEMETRY_DISABLED"] == "1"
        path_entries = res.env["PATH"].split(os.pathsep)
        assert path_entries[0] == str(tm.tool_dir("ripgrep"))
        assert path_entries[1] == str(tm.tool_dir("rtk-oss"))

    # Skipped for agy
    agy_res = build_rtk_runner_env(tm, "agy", base_path="C:\\Windows\\System32")
    assert agy_res.enabled is False
    assert os.environ.get("PATH", "") == global_path_before

    # Rejects non-OSS asset name
    bad_lock = _write_lockfile(
        tmp_path / "bad",
        {
            "rtk-oss": {
                "display_name": "rtk-oss",
                "version": "1.2.1",
                "artifact": FORBIDDEN_NON_OSS_RTK_ASSET,
                "url": f"https://github.com/rtk-ai/rtk/releases/download/v1.2.1/{FORBIDDEN_NON_OSS_RTK_ASSET}",
                "sha256": "b" * 64,
                "executable": "rtk.exe",
                "windows_binary_available": True,
                "auto_update": False,
            }
        },
    )
    (tmp_path / "bad").mkdir(exist_ok=True)
    bad_lock.write_text(bad_lock.read_text(encoding="utf-8"), encoding="utf-8")
    tm_bad = ToolManager(store=store, tools_root=tmp_path / "bad_tools", lockfile_path=bad_lock)
    bad_res = build_rtk_runner_env(tm_bad, "claude")
    assert bad_res.enabled is False
    assert REQUIRED_OSS_RTK_ASSET in bad_res.warning


def test_ccusage_parsing_crosscheck_and_antigravity_fallback(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ledger = QuotaLedger(store)
    cc_payload = b"fake-ccusage"
    lock_path = _write_lockfile(
        tmp_path,
        {
            "ccusage": {
                "display_name": "ccusage",
                "version": "15.2.0",
                "artifact": "ccusage-15.2.0.tgz",
                "url": "https://registry.npmjs.org/ccusage/-/ccusage-15.2.0.tgz",
                "sha256": hashlib.sha256(cc_payload).hexdigest(),
                "executable": "ccusage.cmd",
                "flags": ["--offline", "--json"],
                "auto_update": False,
            }
        },
    )
    tm = ToolManager(
        store=store,
        tools_root=tmp_path / "tools",
        lockfile_path=lock_path,
        downloader=lambda _u, d: (d.write_bytes(cc_payload), d)[1],
    )
    assert tm.ensure_installed("ccusage").ok is True

    # Output has Claude (matches CLI usage) and Codex (drifts 60%), but NO Antigravity
    cc_json = json.dumps(
        {
            "entries": [
                {"engine": "claude", "model": "claude-3-7-sonnet", "inputTokens": 4000, "outputTokens": 1000, "totalCost": 0.05},
                {"engine": "codex", "model": "o3-mini", "inputTokens": 1000, "outputTokens": 500},
            ]
        }
    )
    assert len(parse_ccusage_json(cc_json)) == 2

    def fake_cc_exec(cmd: list[str], _cwd: Path | None, env: dict[str, str], _timeout: int):
        assert "--offline" in cmd
        assert "--json" in cmd
        assert env["DO_NOT_TRACK"] == "1"
        return 0, cc_json, "", False

    adapter = CcusageAdapter(tm, ledger, executor=fake_cc_exec)
    sync = adapter.sync_usage(
        cli_usage_by_engine={
            "claude": (4100, 1000),  # ~2% drift -> accept ccusage
            "codex": (5000, 2000),   # >25% drift -> fallback to CLI JSON
            "agy": (8000, 2000),     # missing in ccusage -> fallback to CLI JSON and say so
        },
        expect_antigravity=True,
    )
    assert sync.ok is True
    assert "agy" in sync.used_fallback_engines
    assert "codex" in sync.used_fallback_engines
    assert "claude" not in sync.used_fallback_engines
    assert any("Antigravity" in w for w in sync.warnings)
    assert any("codex" in w for w in sync.warnings)

    entries = store.get_ledger_entries(limit=20)
    by_runner = {e["runner"]: e for e in entries}
    assert by_runner["claude"]["event"] == "ccusage"
    assert by_runner["agy"]["event"] == "cli_fallback"
    assert by_runner["agy"]["tokens_in"] == 8000


def test_repomix_repo_map_budget_and_git_exclude(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    rm_payload = b"fake-repomix"
    lock_path = _write_lockfile(
        tmp_path,
        {
            "repomix": {
                "display_name": "repomix",
                "version": "0.3.9",
                "artifact": "repomix-0.3.9.tgz",
                "url": "https://registry.npmjs.org/repomix/-/repomix-0.3.9.tgz",
                "sha256": hashlib.sha256(rm_payload).hexdigest(),
                "executable": "repomix.cmd",
                "flags": ["--compress"],
                "token_budget": 100,
                "auto_update": False,
            }
        },
    )
    tm = ToolManager(
        store=store,
        tools_root=tmp_path / "tools",
        lockfile_path=lock_path,
        downloader=lambda _u, d: (d.write_bytes(rm_payload), d)[1],
    )
    assert tm.ensure_installed("repomix").ok is True

    # Simulate a git worktree where .git is a pointer file
    real_gitdir = tmp_path / "main_repo" / ".git" / "worktrees" / "p7"
    real_gitdir.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real_gitdir}\n", encoding="utf-8")

    huge_map = "def fn(): pass\n" * 200

    def fake_repomix(cmd: list[str], cwd: Path | None, env: dict[str, str], _timeout: int):
        assert "--compress" in cmd
        assert env["DO_NOT_TRACK"] == "1"
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_text(huge_map, encoding="utf-8")
        return 0, "", "", False

    adapter = RepomixAdapter(tm, executor=fake_repomix)
    res = adapter.generate_repo_map(wt, token_budget=100)
    assert res.ok is True
    assert res.map_path == wt / ".nightshift" / "repo_map.md"
    assert res.estimated_tokens <= 105
    assert "truncated" in res.content

    exclude_file = real_gitdir / "info" / "exclude"
    assert exclude_file.exists()
    exclude_text = exclude_file.read_text(encoding="utf-8")
    assert ".nightshift/" in exclude_text
    assert ".nightshift_prompt.md" in exclude_text

    injected = RepomixAdapter.inject_into_prompt("Do the task", res.content)
    assert "COMPACT REPO MAP" in injected
    assert "Do the task" in injected
    assert estimate_tokens(truncate_to_token_budget("abcd" * 50, 10)) <= 15


def test_ab_decision_table_and_auto_disable_and_digest(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bus = EventBus(store)
    ab = ABFramework(store, bus, min_samples=3)

    # 1. Insufficient samples -> keep_measuring
    dec_small = evaluate_ab_samples(
        "repomix",
        "backend",
        [{"tokens_in": 10000, "tokens_out": 2000, "turns": 5, "retry_turns": 0, "success": 1}],
        [{"tokens_in": 7000, "tokens_out": 1500, "turns": 3, "retry_turns": 0, "success": 1}],
        min_samples=3,
    )
    assert dec_small.action == "keep_measuring"

    # 2. Cuts net tokens without raising retries or failures -> keep_enabled
    for _ in range(3):
        ab.record_run(tool="repomix", enabled=False, phase_kind="backend", engine="agy", tokens_in=15000, tokens_out=5000, turns=6, retry_turns=0, success=True)
        ab.record_run(tool="repomix", enabled=True, phase_kind="backend", engine="agy", tokens_in=9000, tokens_out=4000, turns=4, retry_turns=0, success=True)
    store.upsert_token_tool_state("repomix", enabled=True)
    dec_good = ab.evaluate_and_apply("repomix", "backend")
    assert dec_good.action == "keep_enabled"
    assert dec_good.tokens_saved_per_run == 7000

    # 3. Does not cut net tokens -> auto_disable
    store.upsert_token_tool_state("rtk-oss", enabled=True)
    for _ in range(3):
        ab.record_run(tool="rtk-oss", enabled=False, phase_kind="ui", engine="claude", tokens_in=8000, tokens_out=2000, turns=4, retry_turns=0, success=True)
        ab.record_run(tool="rtk-oss", enabled=True, phase_kind="ui", engine="claude", tokens_in=8500, tokens_out=2000, turns=4, retry_turns=0, success=True)
    dec_no_save = ab.evaluate_and_apply("rtk-oss", "ui")
    assert dec_no_save.action == "auto_disable"
    assert "did not cut net tokens" in dec_no_save.reason
    rtk_state = store.get_token_tool_state("rtk-oss")
    assert rtk_state is not None and rtk_state["enabled"] == 0 and rtk_state["auto_disabled"] == 1

    # 4. Raises retry turns -> auto_disable
    dec_retries = evaluate_ab_samples(
        "rtk-oss",
        "backend",
        [{"tokens_in": 10000, "tokens_out": 2000, "turns": 5, "retry_turns": 0, "success": 1}] * 3,
        [{"tokens_in": 7000, "tokens_out": 1500, "turns": 5, "retry_turns": 1, "success": 1}] * 3,
    )
    assert dec_retries.action == "auto_disable"
    assert "retry turns" in dec_retries.reason

    # 5. Lowers success rate -> auto_disable
    dec_fail = evaluate_ab_samples(
        "repomix",
        "ui",
        [{"tokens_in": 10000, "tokens_out": 2000, "turns": 5, "retry_turns": 0, "success": 1}] * 3,
        [
            {"tokens_in": 7000, "tokens_out": 1500, "turns": 3, "retry_turns": 0, "success": 1},
            {"tokens_in": 7000, "tokens_out": 1500, "turns": 3, "retry_turns": 0, "success": 1},
            {"tokens_in": 7000, "tokens_out": 1500, "turns": 3, "retry_turns": 0, "success": 0},
        ],
    )
    assert dec_fail.action == "auto_disable"
    assert "success rate" in dec_fail.reason

    # 6. agy-fast 15% turn-drop threshold
    dec_agy_low = evaluate_ab_samples(
        "agy-fast",
        "backend",
        [{"tokens_in": 10000, "tokens_out": 2000, "turns": 10, "retry_turns": 0, "success": 1}] * 3,
        [{"tokens_in": 9000, "tokens_out": 1800, "turns": 9, "retry_turns": 0, "success": 1}] * 3,
    )
    assert dec_agy_low.action == "auto_disable"
    assert "15%" in dec_agy_low.reason

    # 7. Deck and morning digest include token savings estimate and warnings
    store.create_project("demo", "Demo App", "owner/demo", str(tmp_path))
    deck = generate_deck_data(store, "demo")
    ts = deck["token_savings_tonight"]
    assert ts["totalTokensSaved"] == 21000  # 3 enabled repomix runs * 7000
    assert "estimate" in ts["estimateLabel"]
    assert len(ts["warnings"]) >= 1

    digest_path = write_morning_digest(tmp_path / "out", "Demo App", [], deck=deck)
    digest_text = digest_path.read_text(encoding="utf-8")
    assert "Token Optimizer Savings (Estimate)" in digest_text
    assert "21,000" in digest_text


def test_backend_module_discovery_and_rpc_gates(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bus = EventBus(store)
    registry = ModuleRegistry()
    discovered = registry.discover()
    assert "token_optimizer" in discovered

    bridge = BridgeAPI(store, bus, registry)
    state_resp = bridge.call("token_optimizer.getState")
    assert state_resp["ok"] is True
    tools = {t["name"]: t for t in state_resp["data"]["tools"]}
    assert "ccusage" in tools
    assert "repomix" in tools
    assert "agy-fast" in tools
    assert "rtk-oss" in tools
    assert "ripgrep" in tools
    assert "ast-grep-mcp" in tools

    # ast-grep-mcp cannot be enabled (catalogue-only)
    cat_resp = bridge.call("token_optimizer.setToolEnabled", {"tool": "ast-grep-mcp", "enabled": True})
    assert cat_resp["ok"] is True
    cat_tool = next(t for t in cat_resp["data"]["tools"] if t["name"] == "ast-grep-mcp")
    assert cat_tool["enabled"] is False
    assert cat_tool["status"] == "catalogue_only"

    # agy-fast cannot be enabled before isolation probe passes
    agy_resp = bridge.call("token_optimizer.setToolEnabled", {"tool": "agy-fast", "enabled": True})
    agy_tool = next(t for t in agy_resp["data"]["tools"] if t["name"] == "agy-fast")
    assert agy_tool["enabled"] is False
    assert agy_tool["status"] == "isolation_blocked"

    # repomix can be toggled on/off
    rm_resp = bridge.call("token_optimizer.setToolEnabled", {"tool": "repomix", "enabled": True})
    rm_tool = next(t for t in rm_resp["data"]["tools"] if t["name"] == "repomix")
    assert rm_tool["enabled"] is True


def test_settings_truth_for_missing_verified_and_tampered_tool(tmp_path: Path) -> None:
    from nightshift.modules.token_optimizer import TokenOptimizerService
    store = _make_store(tmp_path)
    payload = b"known artifact"
    lock = _write_lockfile(tmp_path, {"repomix": {"version": "0.3.9", "artifact": "repomix.tgz", "sha256": hashlib.sha256(payload).hexdigest(), "executable": "repomix.cmd"}})
    service = TokenOptimizerService(store, tools_root=tmp_path / "tools", lockfile_path=lock)
    store.upsert_token_tool_state("repomix", enabled=True)
    item = service.get_state()["tools"][0]
    assert item["status"] == "not_installed" and item["enabled"] is True
    artifact = service.manager.artifact_path("repomix")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)
    service.manager.executable_path("repomix").write_text("stub")
    item = service.get_state()["tools"][0]
    assert item["status"] == "enabled" and item["sha256Verified"]
    artifact.write_bytes(b"tampered")
    item = service.get_state()["tools"][0]
    assert item["status"] == "hash_mismatch" and not item["enabled"]
    assert not store.get_token_tool_state("repomix")["enabled"]
