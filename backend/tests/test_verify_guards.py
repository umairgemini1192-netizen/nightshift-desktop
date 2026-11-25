"""Tests for deliverable verification, NO-OP detection, and diff guards (BLUEPRINT 1, 5d)."""

from __future__ import annotations

from pathlib import Path

from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import (
    assess_diff_risk,
    check_diff_guards,
    redact_secrets,
    verify_deliverable,
)


def test_verify_deliverable_and_noop_detection(tmp_path: Path) -> None:
    vcs = FakeGitGh()

    # 1. No PR, no commit -> NO-OP
    ok, reason = verify_deliverable(vcs, "owner/repo", "feat/phase-1", previous_sha="sha_old", local_repo=tmp_path)
    assert not ok
    assert "NO-OP" in reason

    # 2. Open PR exists -> deliverable verified
    vcs.add_pr("feat/phase-1", number=101)
    ok_pr, reason_pr = verify_deliverable(vcs, "owner/repo", "feat/phase-1")
    assert ok_pr
    assert "PR #101" in reason_pr

    # 3. New commit pushed to remote branch -> deliverable verified
    vcs.branches["feat/phase-2"] = "sha_new"
    ok_c, reason_c = verify_deliverable(vcs, "owner/repo", "feat/phase-2", previous_sha="sha_old", local_repo=tmp_path)
    assert ok_c
    assert "New commit sha_new" in reason_c


def test_diff_guard_no_deleted_tests(tmp_path: Path) -> None:
    diff = """
diff --git a/backend/tests/test_foo.py b/backend/tests/test_foo.py
deleted file mode 100644
--- a/backend/tests/test_foo.py
+++ /dev/null
@@ -1,10 +0,0 @@
-def test_something():
-    assert True
"""
    findings = check_diff_guards(tmp_path, ["backend/tests/test_foo.py"], diff)
    assert any(f.guard == "no_deleted_tests" for f in findings)


def test_diff_guard_no_skipped_tests(tmp_path: Path) -> None:
    diff1 = """
--- a/tests/test_bar.py
+++ b/tests/test_bar.py
@@ -5,3 +5,4 @@
+@pytest.mark.skip(reason="temp skip")
 def test_bar():
"""
    findings1 = check_diff_guards(tmp_path, ["tests/test_bar.py"], diff1)
    assert any(f.guard == "no_skipped_tests" for f in findings1)

    diff2 = """
--- a/frontend/tests/bar.spec.ts
+++ b/frontend/tests/bar.spec.ts
@@ -10,3 +10,4 @@
+it.skip("test case", () => {
"""
    findings2 = check_diff_guards(tmp_path, ["frontend/tests/bar.spec.ts"], diff2)
    assert any(f.guard == "no_skipped_tests" for f in findings2)


def test_diff_guard_no_weakened_ci_workflows(tmp_path: Path) -> None:
    diff = """
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -15,3 +15,2 @@
-      - run: npm test
"""
    # Unauthorized CI change
    findings = check_diff_guards(tmp_path, [".github/workflows/ci.yml"], diff, allow_ci_workflow_edits=False)
    assert any(f.guard == "no_weakened_ci" for f in findings)

    # Allowed CI change (e.g. CI-fix phase)
    findings_allowed = check_diff_guards(tmp_path, [".github/workflows/ci.yml"], diff, allow_ci_workflow_edits=True)
    assert not any(f.guard == "no_weakened_ci" for f in findings_allowed)


def test_diff_guard_no_secrets(tmp_path: Path) -> None:
    # 1. Private key pattern
    diff_key = """
+-----BEGIN RSA PRIVATE KEY-----
+MIIEowIBAAKCAQEA0...
+-----END RSA PRIVATE KEY-----
"""
    findings_key = check_diff_guards(tmp_path, ["server/key.pem"], diff_key)
    assert any(f.guard == "no_secrets" for f in findings_key)

    # 2. GitHub PAT pattern
    diff_pat = """
+const token = "ghp_123456789012345678901234567890123456";
"""
    findings_pat = check_diff_guards(tmp_path, ["src/client.ts"], diff_pat)
    assert any(f.guard == "no_secrets" for f in findings_pat)

    # 3. AWS Key pattern
    diff_aws = """
+AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
"""
    findings_aws = check_diff_guards(tmp_path, ["config.py"], diff_aws)
    assert any(f.guard == "no_secrets" for f in findings_aws)


def test_diff_guard_no_oversized_binaries(tmp_path: Path) -> None:
    big_file = tmp_path / "huge.bin"
    big_file.write_bytes(b"\x00" * (6 * 1024 * 1024))  # 6 MB

    findings = check_diff_guards(tmp_path, ["huge.bin"], "+huge.bin")
    assert any(f.guard == "no_oversized_binaries" for f in findings)


def test_secret_redaction() -> None:
    text = (
        "Logs: Connecting with key ghp_123456789012345678901234567890123456 and "
        "AWS key AKIAIOSFODNN7EXAMPLE to S3."
    )
    redacted = redact_secrets(text)
    assert "ghp_123456789012345678901234567890123456" not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_risk_assessment_critical_vs_low() -> None:
    # Critical due to critical path pattern
    crit_res = assess_diff_risk(
        ["app/src/main/AndroidManifest.xml"],
        "+<uses-permission android:name=\"android.permission.INTERNET\" />",
    )
    assert crit_res.level == "critical"
    assert len(crit_res.reasons) > 0

    # Low risk (routine styling or internal UI component)
    low_res = assess_diff_risk(
        ["src/design/Button.tsx"],
        "+export const Button = () => <button />;",
    )
    assert low_res.level == "low"
    assert len(low_res.reasons) == 0
