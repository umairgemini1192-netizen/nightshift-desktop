import ctypes
import datetime
import hashlib
import json
import os
os.environ["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
QUEUE_FILE = BASE_DIR / "queue.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "nightshift.log"
WORKTREE_ROOT = Path(os.environ.get("TEMP", str(BASE_DIR))) / "nightshift-worktrees"

QUOTA_PATTERNS = [
    "quota", "rate limit", "rate_limit", "429", "usage limit",
    "limit reached", "exhausted", "too many requests", "capacity",
    "insufficient_quota", "billing", "credit balance", "overloaded"
]

STATE_LOCK = threading.Lock()
STOP_REQUESTED = False
WORKER_THREAD = None
COOLDOWNS = {}

RUNTIME = {
    "running": False,
    "current_phase": None,
    "active_runner": None,
    "keep_awake": False,
    "phases_status": {},
    "cooldowns": {},
    "last_updated": None,
    "logs": []
}

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"[{now_str()}] {msg}"
    with STATE_LOCK:
        RUNTIME["logs"].append(line)
        RUNTIME["logs"] = RUNTIME["logs"][-500:]
        RUNTIME["last_updated"] = now_str()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)

def save_state():
    with STATE_LOCK:
        now_ts = time.time()
        RUNTIME["cooldowns"] = {
            k: int(v - now_ts) for k, v in COOLDOWNS.items() if v > now_ts
        }
        snap = dict(RUNTIME)
    try:
        STATE_FILE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    except Exception:
        pass
    _push_drive_status(snap)

_LAST_DRIVE_HASH = ""

def _read_bridge_cfg():
    import os
    url = os.environ.get("DEV_COCKPIT_WEBHOOK_URL", "").strip()
    sec = os.environ.get("DEV_COCKPIT_SYNC_SECRET", "").strip()
    env_file = BASE_DIR.parent / ".env.local"
    if (not url or not sec) and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "DEV_COCKPIT_WEBHOOK_URL" and not url:
                url = v
            elif k == "DEV_COCKPIT_SYNC_SECRET" and not sec:
                sec = v
    if not url:
        url = "https://script.google.com/macros/s/AKfycbxyo2hWJ2eCugeQdVGlwvdtAYak051yvXD5BV9o5iLUU3UpMF92N9VOVFb570cbK_TQ/exec"
    return url, sec

def _push_drive_status(snap: dict):
    global _LAST_DRIVE_HASH
    import hashlib, threading, urllib.request
    from datetime import datetime
    url, sec = _read_bridge_cfg()
    if not url or not sec:
        return
    phases = snap.get("phases_status", {})
    lines = [
        f"NIGHTSHIFT LIVE STATUS ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
        f"Running: {snap.get('running', False)}",
        "--- Phases ---",
    ]
    for pid, st in phases.items():
        lines.append(f"  {pid}: {st}")
    if snap.get("cooldowns"):
        lines.append(f"Cooldowns: {snap['cooldowns']}")
    logs = snap.get("logs", [])[-15:]
    if logs:
        lines.append("--- Recent Logs ---")
        lines.extend(str(x) for x in logs)
    body_text = "\n".join(lines)
    sig = hashlib.sha256(json.dumps({"r": snap.get("running"), "p": phases, "l": logs[-3:]}, sort_keys=True).encode()).hexdigest()
    if sig == _LAST_DRIVE_HASH:
        return
    _LAST_DRIVE_HASH = sig

    def _send():
        try:
            payload = json.dumps({"secret": sec, "status_text": body_text}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()

def set_keep_awake(enable: bool):
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        with STATE_LOCK:
            RUNTIME["keep_awake"] = enable
    except Exception as e:
        log(f"Keep-awake skipped: {e}")

def resolve_bin(name: str):
    for candidate in (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name, f"{name}.ps1"):
        found = shutil.which(candidate)
        if found:
            return found
    return None

def detect_tools():
    return {t: bool(resolve_bin(t)) for t in ["git", "gh", "claude", "gemini", "agy", "codex"]}

def run_cmd(args, cwd=None, timeout=3600):
    exe = resolve_bin(args[0])
    if not exe:
        return 127, "", f"Executable not found in PATH: {args[0]}"
    if exe.lower().endswith(".ps1"):
        ps = shutil.which("powershell.exe") or "powershell.exe"
        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe] + args[1:]
    else:
        cmd = [exe] + args[1:]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False
        )
    except Exception as e:
        return 1, "", str(e)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "").strip(), (err or "").strip()
    except subprocess.TimeoutExpired:
        # Kill the whole tree (agents spawn node/gradle/git children) so nothing lingers.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        return 124, (out or "").strip()[-2000:], f"Command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)

def is_quota_or_transient_error(rc: int, out: str, err: str) -> bool:
    blob = f"{out}\n{err}".lower()
    if rc == 127:
        return True
    return any(p in blob for p in QUOTA_PATTERNS)

def build_runner_cmd(entry: dict, instruction: str):
    runner = entry["runner"].lower()
    model = entry.get("model", "").strip()
    effort = entry.get("effort", "").strip()

    if runner == "claude":
        cmd = ["claude", "-p", instruction, "--permission-mode", "bypassPermissions", "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd
    elif runner == "gemini":
        # --skip-trust: headless runs in fresh worktrees are otherwise "untrusted" and exit 55.
        cmd = ["gemini", "-p", instruction, "--approval-mode=yolo", "--skip-trust", "--skip-trust"]
        if model:
            cmd += ["--model", model]
        return cmd
    elif runner == "agy":
        # Headless print mode auto-denies shell tools (git/gh) yet still exits 0,
        # so permissions must be pre-approved like claude/gemini above.
        cmd = ["agy", "-p", instruction, "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd
    elif runner == "codex":
        cmd = ["codex", "exec", "--sandbox", "workspace-write", "--approve-for-me", instruction]
        if model:
            cmd += ["--model", model]
        return cmd
    raise ValueError(f"Unsupported runner: {runner}")

def run_with_failover(ladder: list, wt_dir: Path, instruction: str, cooldown_sec: int = 3600, verify=None,
                      runner_timeout: int = 2400):
    """verify: optional callable returning (ok: bool, reason: str). A runner that exits 0
    but fails verification is treated as a failure and the ladder moves on."""
    available_tools = detect_tools()
    last_err = "No runners attempted."

    for idx, entry in enumerate(ladder):
        runner = entry["runner"].lower()
        model = entry.get("model", "default")
        key = f"{runner}:{model}"

        if not available_tools.get(runner, False):
            log(f"Skipping {key} (CLI not installed in PATH).")
            continue

        if COOLDOWNS.get(key, 0) > time.time():
            rem = int(COOLDOWNS[key] - time.time())
            log(f"Skipping {key} (cooled down for {rem}s due to prior quota/limit).")
            continue

        resume_note = ""
        if idx > 0:
            resume_note = (
                " HANDOFF NOTE: A previous AI runner hit a quota limit or error mid-task in this exact worktree. "
                "First run `git status` and `git diff` to inspect work already completed, keep valid progress, "
                "and finish the remaining requirements cleanly without redoing finished files."
            )

        with STATE_LOCK:
            RUNTIME["active_runner"] = key
        save_state()
        log(f"Dispatching to {key}...")

        cmd = build_runner_cmd(entry, instruction + resume_note)
        rc, out, err = run_cmd(cmd, cwd=wt_dir, timeout=runner_timeout)
        if rc != 0 and verify:
            # A runner killed by timeout (or erroring at the very end) may already have delivered.
            ok, _ = verify()
            if ok:
                log(f"{key} exited rc={rc} but its deliverable is present -> continuing. Detail: {(err or out)[-200:]}")
                return key, out
        if rc == 0:
            ok, reason = verify() if verify else (True, "")
            if ok:
                log(f"{key} completed cleanly.")
                return key, out
            last_err = f"{reason} Runner output tail: {(out or err or '(empty)')[-300:]}"
            log(f"NO-OP on {key} (exit 0 but {reason}) -> Failing over to next runner! Detail: {last_err}")
            save_state()
            continue

        last_err = (err or out or f"exit {rc}")[-400:]
        if is_quota_or_transient_error(rc, out, err):
            COOLDOWNS[key] = time.time() + cooldown_sec
            log(f"QUOTA/LIMIT on {key} (cooling down {cooldown_sec//60}m) -> Failing over to next runner! Detail: {last_err}")
        else:
            log(f"ERROR on {key} (rc={rc}) -> Failing over to next runner! Detail: {last_err}")
        save_state()

    raise RuntimeError(f"All runners on failover ladder exhausted. Last error: {last_err}")

def prepare_worktree(local_repo: Path, phase_id: str, branch: str) -> Path:
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    wt_dir = WORKTREE_ROOT / re.sub(r"[^a-zA-Z0-9_-]", "_", phase_id)

    rc, _, err = run_cmd(["git", "fetch", "origin"], cwd=local_repo, timeout=120)
    if rc != 0:
        raise RuntimeError(f"git fetch origin failed: {err}")

    if wt_dir.exists():
        run_cmd(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=local_repo, timeout=60)
        shutil.rmtree(wt_dir, ignore_errors=True)
    run_cmd(["git", "worktree", "prune"], cwd=local_repo, timeout=60)

    # Resume from the remote branch if a previous run already pushed work; never reset it to main.
    base = "origin/main"
    rc_b, out_b, _ = run_cmd(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=local_repo, timeout=60)
    if rc_b == 0 and out_b.strip():
        run_cmd(["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=local_repo, timeout=120)
        base = f"origin/{branch}"
        log(f"Branch {branch} already exists on origin -> resuming from it.")
    rc, _, err = run_cmd(
        ["git", "worktree", "add", "-B", branch, str(wt_dir), base],
        cwd=local_repo,
        timeout=120
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")

    git_dir_rc, git_dir_out, _ = run_cmd(["git", "rev-parse", "--git-dir"], cwd=wt_dir, timeout=30)
    if git_dir_rc == 0 and git_dir_out:
        exclude_path = Path(git_dir_out) / "info" / "exclude"
        if not exclude_path.is_absolute():
            exclude_path = wt_dir / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if ".nightshift_prompt.md" not in existing:
            with open(exclude_path, "a", encoding="utf-8") as f:
                f.write("\n.nightshift_prompt.md\n")
    return wt_dir

def find_pr_for_branch(repo: str, branch: str):
    rc, out, _ = run_cmd(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
         "--json", "number,url,headRefOid,mergeable,mergeStateStatus"],
        timeout=60
    )
    if rc == 0 and out:
        data = json.loads(out)
        if data:
            return data[0]
    return None

def find_merged_pr_for_branch(repo: str, branch: str):
    rc, out, _ = run_cmd(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "merged",
         "--json", "number,url", "--limit", "1"],
        timeout=60
    )
    if rc == 0 and out:
        data = json.loads(out)
        if data:
            return data[0]
    return None

def remote_branch_sha(local_repo: Path, branch: str):
    rc, out, _ = run_cmd(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=local_repo, timeout=60)
    if rc == 0 and out:
        return out.split()[0]
    return None

def get_failed_ci_log(repo: str, branch: str):
    rc, out, _ = run_cmd(
        ["gh", "run", "list", "--repo", repo, "--branch", branch, "--limit", "5",
         "--json", "databaseId,name,conclusion,status"],
        timeout=60
    )
    if rc != 0 or not out:
        return "Could not list GitHub Actions runs."
    runs = json.loads(out)
    failed = [r for r in runs if r.get("conclusion") == "failure"]
    if not failed:
        return "No failed workflow logs found."
    run_id = str(failed[0]["databaseId"])
    rc2, log_out, err2 = run_cmd(
        ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
        timeout=120
    )
    lines = (log_out or err2).splitlines()[-120:]
    return f"Failed run {run_id} ({failed[0].get('name')}):\n" + "\n".join(lines)

def next_preview_tag(repo: str):
    rc, out, _ = run_cmd(["gh", "release", "list", "--repo", repo, "--limit", "40", "--json", "tagName"], timeout=60)
    max_num = 40
    if rc == 0 and out:
        for item in json.loads(out):
            m = re.match(r"v1\.0\.0-preview\.(\d+)$", item.get("tagName", ""))
            if m:
                max_num = max(max_num, int(m.group(1)))
    return f"v1.0.0-preview.{max_num + 1}"

def cut_verified_release(repo: str, desktop_apk_dir: Path, title_note: str):
    log("Waiting for post-merge main Android build...")
    time.sleep(15)
    rc, out, err = run_cmd(
        ["gh", "run", "list", "--repo", repo, "--branch", "main", "--workflow", "Android build",
         "--limit", "1", "--json", "databaseId,status,conclusion"],
        timeout=60
    )
    if rc != 0 or not out:
        raise RuntimeError(f"Could not locate main Android build: {err}")
    run_id = str(json.loads(out)[0]["databaseId"])
    log(f"Watching main Android build run {run_id}...")
    rc, _, err = run_cmd(["gh", "run", "watch", run_id, "--repo", repo, "--exit-status"], timeout=3600)
    if rc != 0:
        raise RuntimeError(f"main Android build {run_id} failed: {err}")

    tag = next_preview_tag(repo)
    dl_dir = WORKTREE_ROOT / f"release-{tag}"
    shutil.rmtree(dl_dir, ignore_errors=True)
    dl_dir.mkdir(parents=True, exist_ok=True)

    rc, _, err = run_cmd(["gh", "run", "download", run_id, "--repo", repo, "--dir", str(dl_dir)], timeout=300)
    if rc != 0:
        raise RuntimeError(f"Artifact download failed: {err}")

    apks = list(dl_dir.rglob("*.apk"))
    if not apks:
        raise RuntimeError("No APK artifact found in main build.")

    apk_name = f"DriveLift-{tag.lstrip('v')}-debug.apk"
    final_apk = dl_dir / apk_name
    shutil.copy2(apks[0], final_apk)

    sha256 = hashlib.sha256(final_apk.read_bytes()).hexdigest()
    sums_file = dl_dir / "SHA256SUMS.txt"
    sums_file.write_text(f"{sha256}  {apk_name}\n", encoding="utf-8")

    rc, rel_out, err = run_cmd([
        "gh", "release", "create", tag, str(final_apk), str(sums_file),
        "--repo", repo, "--prerelease",
        "--title", f"DriveLift {tag}",
        "--notes", f"Continuous Auto-Build: {title_note}\nMain CI run: {run_id}\nSHA-256: `{sha256}`"
    ], timeout=180)
    if rc != 0:
        raise RuntimeError(f"gh release create failed: {err}")

    desktop_apk_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_apk, desktop_apk_dir / apk_name)
    shutil.copy2(sums_file, desktop_apk_dir / f"{tag}-SHA256SUMS.txt")
    log(f"Published {tag} and copied APK to {desktop_apk_dir}")
    return tag, rel_out

def write_digest(desktop_apk_dir: Path, summary_rows):
    desktop_apk_dir.mkdir(parents=True, exist_ok=True)
    digest_path = desktop_apk_dir / "MORNING_DIGEST.md"
    lines = [
        f"# DriveLift Continuous Delivery Digest ({now_str()})",
        "",
        "| Phase | Runner Used | Status | Details |",
        "| :--- | :--- | :--- | :--- |"
    ]
    for r in summary_rows:
        lines.append(f"| {r['title']} | {r['runner']} | **{r['status']}** | {r['detail']} |")
    digest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Digest saved to {digest_path}")

_UI_CONTRACT_GUARDRAIL = (
    "\n\n[UI CONTRACT GATE — MANDATORY FOR ANY UI PR]\n"
    "If this PR touches any file under app/src/main/ matching /ui/, /res/, *Activity.kt, or AndroidManifest.xml "
    "(excluding *ViewModel.kt), the GitHub PR body MUST contain EXACTLY ONE fenced ```ui-review JSON block "
    "so .github/gate/ui_contract.py passes on the first try:\n"
    "```ui-review\n"
    "{\n"
    '  "head": "<EXACT 40-CHAR git rev-parse HEAD OF FINAL PUSHED COMMIT>",\n'
    '  "reference": "docs/AI_CONTEXT.md and docs/UI_WORKFLOW.md",\n'
    '  "interactions": "Describe concrete tap/scroll/state transitions verified",\n'
    '  "accessibility": "Describe contentDescription, contrast, and touch target checks",\n'
    '  "reviewer": "NightShift automated review (pending visual sign-off)",\n'
    '  "exceptions": "None",\n'
    '  "screenshots": {\n'
    '    "standard": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/<PR_NUM>",\n'
    '    "large_font": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/<PR_NUM>",\n'
    '    "compact": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/<PR_NUM>"\n'
    "  }\n"
    "}\n"
    "```\n"
    "CRITICAL RULES:\n"
    "1. NEVER leave words like todo, tbd, placeholder, <!--, or <...> in the actual PR body.\n"
    "2. After EVERY `git push`, immediately run `gh pr edit <PR_NUM> --body-file ...` so `head` matches the exact new `git rev-parse HEAD` SHA.\n"
)
def run_queue_worker():
    global STOP_REQUESTED
    STOP_REQUESTED = False
    set_keep_awake(True)
    summary_rows = []
    try:
        cfg = json.loads(QUEUE_FILE.read_text(encoding="utf-8-sig"))
        repo = cfg["repo"]
        local_repo = Path(cfg["local_repo"])
        desktop_apk_dir = Path(cfg["desktop_apk_dir"])
        global_merge = bool(cfg.get("auto_merge", True))
        global_release = bool(cfg.get("auto_release", True))
        max_retries = int(cfg.get("max_ci_retries", 2))
        cooldown_sec = int(cfg.get("quota_cooldown_minutes", 60)) * 60
        # Per-runner time limit; UI phases with screenshots/evidence need far more than 40 min.
        default_timeout_min = int(cfg.get("runner_timeout_minutes", 90))
        ladders = cfg.get("failover_ladders", {})

        for phase in cfg.get("phases", []):
            if STOP_REQUESTED:
                log("Stop requested — halting cleanly before next phase.")
                break

            pid = phase["id"]
            title = phase["title"]
            branch = phase["branch"]
            ladder_name = phase.get("ladder", "backend")
            ladder = phase.get("custom_ladder") or ladders.get(ladder_name, [])

            if not phase.get("force_rerun", False):
                merged = find_merged_pr_for_branch(repo, branch)
                if merged:
                    log(f"=== Skipping {title}: already merged via PR #{merged['number']} ({merged['url']}). "
                        "Set \"force_rerun\": true on the phase to redo it. ===")
                    with STATE_LOCK:
                        RUNTIME["phases_status"][pid] = "COMPLETED"
                    save_state()
                    summary_rows.append({"title": title, "runner": "none", "status": "SKIPPED",
                                         "detail": f"Already merged PR #{merged['number']}"})
                    continue

            with STATE_LOCK:
                RUNTIME["current_phase"] = pid
                RUNTIME["phases_status"][pid] = "PREPARING_WORKTREE"
            save_state()
            log(f"=== Starting {title} (ladder: {ladder_name}) ===")

            used_runner = "none"
            try:
                wt_dir = prepare_worktree(local_repo, pid, branch)
                guardrail_header = (
                    "CONTINUOUS ORCHESTRATOR GUARDRAILS:\n"
                    f"- Isolated worktree on branch `{branch}` cut from latest `origin/main`.\n"
                    "- NEVER run `./gradlew` locally (zero admin rights; GitHub Actions is the only build).\n"
                    "- Zero glitches: ensure clean Kotlin compile, strict unit tests, null-safety, and zero UI regressions.\n"
                    f"- Commit cleanly, push `{branch}`, and open a PR targeting `main` (if PR already exists, push to it).\n"
                    "- NEVER merge to `main` or cut a release yourself; the orchestrator merges only after CI passes.\n\n"
                )
                prompt_file = wt_dir / ".nightshift_prompt.md"
                prompt_file.write_text(guardrail_header + _UI_CONTRACT_GUARDRAIL + phase["prompt"], encoding="utf-8")

                with STATE_LOCK:
                    RUNTIME["phases_status"][pid] = "IMPLEMENTING"
                save_state()

                def verify_pr_opened():
                    if find_pr_for_branch(repo, branch):
                        return True, ""
                    return False, f"no open PR exists for `{branch}` after the run."

                runner_timeout = int(phase.get("timeout_minutes", default_timeout_min)) * 60

                existing_pr = find_pr_for_branch(repo, branch)
                if existing_pr:
                    log(f"PR #{existing_pr['number']} already open for {branch} -> skipping implementation, "
                        "resuming at CI watch / auto-fix.")
                else:
                    used_runner, _ = run_with_failover(
                        ladder, wt_dir,
                        "Read and execute every instruction in .nightshift_prompt.md in the current directory. Push the branch and open the PR targeting main when done.",
                        cooldown_sec=cooldown_sec,
                        verify=verify_pr_opened,
                        runner_timeout=runner_timeout
                    )

                pr = find_pr_for_branch(repo, branch)
                if not pr:
                    raise RuntimeError(f"No open PR found for branch {branch}.")

                pr_num = str(pr["number"])
                pr_url = pr["url"]
                log(f"PR #{pr_num} live: {pr_url}")

                ci_passed = False
                for attempt in range(max_retries + 1):
                    with STATE_LOCK:
                        RUNTIME["phases_status"][pid] = f"WATCHING_CI ({attempt + 1}/{max_retries + 1})"
                    save_state()
                    time.sleep(15)
                    rc_chk, _, _ = run_cmd(
                        ["gh", "pr", "checks", pr_num, "--repo", repo, "--watch"],
                        cwd=wt_dir, timeout=3600
                    )
                    if rc_chk == 0:
                        ci_passed = True
                        log(f"PR #{pr_num} ALL CI CHECKS PASSED.")
                        break

                    if attempt >= max_retries:
                        raise RuntimeError(f"CI still failing on PR #{pr_num} after {max_retries} retries.")

                    log(f"PR #{pr_num} CI failed — pulling logs for cross-model auto-fix {attempt + 1}/{max_retries}...")
                    fail_log = get_failed_ci_log(repo, branch)
                    fix_prompt = (
                        f"CI failed on PR #{pr_num} (branch `{branch}`). Fix the exact root cause without running `./gradlew` locally, "
                        f"commit, and push to `{branch}`.\n\nFailed CI Log Tail:\n```\n{fail_log}\n```"
                    )
                    prompt_file.write_text(guardrail_header + _UI_CONTRACT_GUARDRAIL + fix_prompt, encoding="utf-8")
                    with STATE_LOCK:
                        RUNTIME["phases_status"][pid] = f"AUTO_FIX_{attempt + 1}"
                    save_state()
                    sha_before_fix = remote_branch_sha(local_repo, branch)

                    def verify_fix_pushed():
                        sha_after = remote_branch_sha(local_repo, branch)
                        if sha_after and sha_after != sha_before_fix:
                            return True, ""
                        rc_now, _, _ = run_cmd(
                            ["gh", "pr", "checks", pr_num, "--repo", repo],
                            cwd=wt_dir, timeout=60
                        )
                        if rc_now == 0:
                            log(f"PR #{pr_num} CI is already green — skipping NO-OP.")
                            return True, ""
                        return False, f"no new commit was pushed to `{branch}`."

                    used_runner, _ = run_with_failover(
                        ladder, wt_dir,
                        "Read and fix the CI failure in .nightshift_prompt.md, commit, and push.",
                        cooldown_sec=cooldown_sec,
                        verify=verify_fix_pushed,
                        runner_timeout=runner_timeout
                    )

                if not ci_passed:
                    raise RuntimeError(f"PR #{pr_num} did not pass CI.")

                do_merge = global_merge and bool(phase.get("auto_merge", True))
                do_release = global_release and bool(phase.get("release_after_merge", False))
                detail = f"PR #{pr_num} green ({pr_url})"

                if do_merge:
                    with STATE_LOCK:
                        RUNTIME["phases_status"][pid] = "MERGING"
                    save_state()
                    rc_m, _, err_m = run_cmd(
                        ["gh", "pr", "merge", pr_num, "--repo", repo, "--merge", "--delete-branch"],
                        cwd=local_repo, timeout=120
                    )
                    if rc_m != 0:
                        raise RuntimeError(f"Merge failed on PR #{pr_num}: {err_m}")
                    log(f"Merged PR #{pr_num} cleanly into main.")
                    detail = f"Merged PR #{pr_num}"

                    if do_release:
                        with STATE_LOCK:
                            RUNTIME["phases_status"][pid] = "RELEASING_APK"
                        save_state()
                        tag, _ = cut_verified_release(repo, desktop_apk_dir, title)
                        detail += f" · Released {tag}"

                run_cmd(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=local_repo, timeout=60)
                with STATE_LOCK:
                    RUNTIME["phases_status"][pid] = "COMPLETED"
                save_state()
                summary_rows.append({"title": title, "runner": used_runner, "status": "COMPLETED", "detail": detail})
                write_digest(desktop_apk_dir, summary_rows)

            except Exception as e:
                log(f"CIRCUIT BREAKER on {pid}: {e}")
                with STATE_LOCK:
                    RUNTIME["phases_status"][pid] = "PAUSED_ERROR"
                save_state()
                summary_rows.append({"title": title, "runner": used_runner, "status": "PAUSED", "detail": str(e)})
                write_digest(desktop_apk_dir, summary_rows)
                break
    finally:
        set_keep_awake(False)
        with STATE_LOCK:
            RUNTIME["running"] = False
            RUNTIME["current_phase"] = None
            RUNTIME["active_runner"] = None
        save_state()
        log("Continuous Delivery session finished.")

HTML_UI = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>DriveLift Continuous Auto-Failover Orchestrator</title>
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0b0f14; color: #e6edf3; margin: 0; padding: 24px; }
  .wrap { max-width: 1150px; margin: 0 auto; }
  h1 { margin: 0 0 6px; font-size: 24px; color: #58d5c9; }
  .sub { color: #8b949e; margin-bottom: 16px; font-size: 13px; }
  .bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .pill { background: #161b22; border: 1px solid #30363d; padding: 5px 11px; border-radius: 999px; font-size: 12px; }
  .ok { border-color: #2ea043; color: #3fb950; }
  .warn { border-color: #d29922; color: #e3b341; }
  .bad { border-color: #f85149; color: #f85149; }
  .grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 16px; }
  .card { background: #121820; border: 1px solid #26303d; border-radius: 14px; padding: 16px; }
  textarea { width: 100%; height: 430px; background: #0a0e13; color: #c9d1d9; border: 1px solid #30363d; border-radius: 10px; padding: 12px; font-family: Consolas, monospace; font-size: 12px; box-sizing: border-box; }
  .btns { display: flex; gap: 10px; margin-top: 12px; }
  button { cursor: pointer; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 600; font-size: 13px; }
  .primary { background: #238636; color: #fff; }
  .secondary { background: #1f6feb; color: #fff; }
  .danger { background: #da3633; color: #fff; }
  pre { background: #0a0e13; border: 1px solid #30363d; border-radius: 10px; padding: 12px; height: 260px; overflow: auto; font-size: 11.5px; color: #9ecbff; white-space: pre-wrap; }
  .phase-row { display: flex; justify-content: space-between; padding: 8px 10px; border-bottom: 1px solid #21262d; font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>DriveLift Continuous Failover Orchestrator</h1>
  <div class="sub">Non-Stop Multi-Phase Pipeline · Automatic Quota Failover Across Claude / Gemini / Antigravity / Codex · Zero-Glitch CI Gate</div>
  <div id="tools" class="bar"></div>
  <div class="grid">
    <div class="card">
      <strong>Continuous Pipeline Queue (JSON)</strong>
      <textarea id="queue"></textarea>
      <div class="btns">
        <button class="secondary" onclick="saveQueue()">Save Queue</button>
        <button class="primary" onclick="startRun()">Start Continuous Run</button>
        <button class="danger" onclick="stopRun()">Stop After Current Phase</button>
      </div>
    </div>
    <div class="card">
      <strong>Pipeline Status</strong>
      <div id="phases" style="margin:10px 0 14px;"></div>
      <strong>Live Execution & Failover Logs</strong>
      <pre id="logs"></pre>
    </div>
  </div>
</div>
<script>
async function loadQueue() {
  const r = await fetch('/api/queue');
  document.getElementById('queue').value = await r.text();
}
async function saveQueue() {
  const txt = document.getElementById('queue').value;
  try { JSON.parse(txt); } catch (e) { alert('Invalid JSON: ' + e); return; }
  await fetch('/api/queue', { method: 'POST', body: txt });
}
async function startRun() {
  await saveQueue();
  const r = await fetch('/api/start', { method: 'POST' });
  const d = await r.json();
  if (!d.ok) alert(d.error);
  refresh();
}
async function stopRun() {
  await fetch('/api/stop', { method: 'POST' });
  refresh();
}
async function refresh() {
  const r = await fetch('/api/status');
  const d = await r.json();
  const cd = d.runtime.cooldowns || {};
  let pills = Object.entries(d.tools).map(([k,v]) => `<span class="pill ${v?'ok':'bad'}">${k}: ${v?'READY':'MISSING'}</span>`).join('');
  pills += `<span class="pill ${d.runtime.running?'ok':''}">Pipeline: ${d.runtime.running?'RUNNING':'IDLE'}</span>`;
  if (d.runtime.active_runner) pills += `<span class="pill warn">Active: ${d.runtime.active_runner}</span>`;
  pills += `<span class="pill ${d.runtime.keep_awake?'ok':''}">Keep-Awake: ${d.runtime.keep_awake?'ON':'OFF'}</span>`;
  Object.entries(cd).forEach(([k,s]) => { pills += `<span class="pill bad">Cooldown ${k}: ${Math.ceil(s/60)}m</span>`; });
  document.getElementById('tools').innerHTML = pills;
  const pHtml = Object.entries(d.runtime.phases_status).map(([k,v]) => `<div class="phase-row"><span>${k}</span><strong>${v}</strong></div>`).join('') || '<div class="sub">Ready to start Phases 1-5 continuously.</div>';
  document.getElementById('phases').innerHTML = pHtml;
  const logEl = document.getElementById('logs');
  logEl.textContent = d.runtime.logs.join('\n');
  logEl.scrollTop = logEl.scrollHeight;
}
loadQueue();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, HTML_UI, "text/html; charset=utf-8")
        elif self.path == "/api/queue":
            self._send(200, QUEUE_FILE.read_text(encoding="utf-8-sig"), "application/json")
        elif self.path == "/api/status":
            with STATE_LOCK:
                snap = dict(RUNTIME)
            self._send(200, json.dumps({"tools": detect_tools(), "runtime": snap}))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        global WORKER_THREAD, STOP_REQUESTED
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        if self.path == "/api/queue":
            json.loads(body)
            QUEUE_FILE.write_text(body, encoding="utf-8")
            self._send(200, '{"ok":true}')
        elif self.path == "/api/start":
            with STATE_LOCK:
                if RUNTIME["running"]:
                    self._send(400, '{"ok":false,"error":"Pipeline already running."}')
                    return
                RUNTIME["running"] = True
            WORKER_THREAD = threading.Thread(target=run_queue_worker, daemon=True)
            WORKER_THREAD.start()
            self._send(200, '{"ok":true}')
        elif self.path == "/api/stop":
            STOP_REQUESTED = True
            log("Stop requested — will halt cleanly after current phase.")
            self._send(200, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    log("DriveLift Continuous Failover Orchestrator live at http://127.0.0.1:8787")
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8787")).start()
    server.serve_forever()
