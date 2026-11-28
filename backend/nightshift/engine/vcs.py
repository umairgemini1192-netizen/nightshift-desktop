"""VCS and GitHub CLI abstraction layer for engine execution and offline testing."""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from nightshift.runners.base import run_subprocess_with_timeout

if TYPE_CHECKING:
    from nightshift.engine.pipeline_contracts import DiffPathEntry
    from nightshift.engine.pipeline_evidence import CiArtifact, CiJobCheck

_CIRCLECI_WORKFLOW_JOB_RE = re.compile(
    r"circleci\.com/workflow/([0-9a-fA-F-]+)/job/([0-9a-fA-F-]+)",
    re.IGNORECASE,
)
_CIRCLECI_PIPELINE_JOB_RE = re.compile(
    r"circleci\.com/pipelines/([^/]+/[^/]+/[^/]+)/\d+/workflows/([0-9a-fA-F-]+)/jobs/(\d+)",
    re.IGNORECASE,
)
_CIRCLECI_LEGACY_JOB_RE = re.compile(
    r"circleci\.com/(?:jobs/)?(gh|github|bb|bitbucket)/([^/]+)/([^/]+)/(\d+)",
    re.IGNORECASE,
)
_GH_ACTIONS_RUN_RE = re.compile(
    r"/actions/runs/(\d+)(?:/jobs?/(\d+))?",
    re.IGNORECASE,
)
_CONTENT_RANGE_TOTAL_RE = re.compile(
    r"bytes\s+(?:\d+-\d+|\*)/(\d+)",
    re.IGNORECASE,
)


def _is_circleci_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return (
        host == "circleci.com"
        or host.endswith(".circleci.com")
        or host == "circle-artifacts.com"
        or host.endswith(".circle-artifacts.com")
    )


class _SafeCircleRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip CircleCI auth tokens when following redirects to external hosts (e.g. S3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and not _is_circleci_host(newurl):
            for header_key in list(redirected.headers.keys()):
                if header_key.lower() == "circle-token":
                    redirected.remove_header(header_key)
            for header_key in list(redirected.unredirected_hdrs.keys()):
                if header_key.lower() == "circle-token":
                    del redirected.unredirected_hdrs[header_key]
        return redirected


_SAFE_CIRCLE_OPENER = urllib.request.build_opener(_SafeCircleRedirectHandler())


def _default_http_json_get(
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _SAFE_CIRCLE_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _default_http_bytes_get(
    url: str,
    headers: dict[str, str],
    timeout: int = 60,
) -> bytes | None:
    """Download raw artifact bytes while stripping CircleCI tokens off external hosts."""
    req_headers = dict(headers)
    if not _is_circleci_host(url):
        req_headers = {k: v for k, v in req_headers.items() if k.lower() != "circle-token"}
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with _SAFE_CIRCLE_OPENER.open(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _default_http_artifact_size(
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
) -> int:
    """Return authoritative byte size for a downloadable artifact without downloading the full file."""
    probe_headers = dict(headers)
    if not _is_circleci_host(url):
        probe_headers = {k: v for k, v in probe_headers.items() if k.lower() != "circle-token"}
    probe_headers["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=probe_headers, method="GET")
    try:
        with _SAFE_CIRCLE_OPENER.open(req, timeout=timeout) as resp:
            content_range = resp.headers.get("Content-Range") or ""
            m = _CONTENT_RANGE_TOTAL_RE.search(content_range)
            if m:
                return int(m.group(1))
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    return max(0, int(content_length))
                except ValueError:
                    pass
            sample = resp.read(1)
            return len(sample)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            content_range = exc.headers.get("Content-Range") or "" if exc.headers else ""
            m = _CONTENT_RANGE_TOTAL_RE.search(content_range)
            if m:
                return int(m.group(1))
            return 0
        return 0
    except Exception:
        return 0


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_circleci_slug(raw_slug: str) -> str:
    parts = [p for p in raw_slug.strip("/").split("/") if p]
    if len(parts) >= 3:
        vcs = parts[0].lower()
        if vcs == "github":
            parts[0] = "gh"
        elif vcs == "bitbucket":
            parts[0] = "bb"
    return "/".join(parts)


@dataclass(frozen=True)
class _CircleCiJobResolution:
    """Authoritative CircleCI job metadata resolved from CircleCI v2 APIs."""

    name: str
    job_uuid: str
    build_number: str
    workflow_id: str
    revision: str
    duration_seconds: float
    status: str
    artifacts: list[CiArtifact]
    verified: bool


def _parse_gh_actions_target(url: str, repo: str) -> tuple[str, str] | None:
    """Parse a GitHub Actions run/job URL strictly bound to github.com and ``repo``."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    path = parts.path or ""
    if repo:
        expected_prefix = f"/{repo.strip('/')}/actions/runs/".lower()
        if not path.lower().startswith(expected_prefix):
            return None
    m = _GH_ACTIONS_RUN_RE.search(path)
    if not m:
        return None
    return (m.group(1), m.group(2) or "")


def _is_circleci_slug_allowed(project_slug: str, repo: str) -> bool:
    """Ensure explicit gh/ project slugs match the target repository."""
    if not project_slug or not repo:
        return True
    slug_low = project_slug.strip("/").lower()
    if slug_low.startswith("gh/"):
        return slug_low == f"gh/{repo.strip('/')}".lower()
    return True


def _parse_circleci_target(url: str, repo: str) -> dict[str, str] | None:
    if not url:
        return None
    m_wf = _CIRCLECI_WORKFLOW_JOB_RE.search(url)
    if m_wf:
        return {
            "workflow_id": m_wf.group(1),
            "job_uuid": m_wf.group(2),
            "project_slug": f"gh/{repo}" if repo else "",
            "job_number": "",
        }
    m_pipe = _CIRCLECI_PIPELINE_JOB_RE.search(url)
    if m_pipe:
        return {
            "workflow_id": m_pipe.group(2),
            "job_uuid": "",
            "project_slug": _normalize_circleci_slug(m_pipe.group(1)),
            "job_number": m_pipe.group(3),
        }
    m_leg = _CIRCLECI_LEGACY_JOB_RE.search(url)
    if m_leg:
        vcs_raw, owner, repo_name, job_num = m_leg.groups()
        vcs_prefix = "gh" if vcs_raw.lower() in ("gh", "github") else "bb"
        return {
            "workflow_id": "",
            "job_uuid": "",
            "project_slug": f"{vcs_prefix}/{owner}/{repo_name}",
            "job_number": job_num,
        }
    return None


class GitGhClient(Protocol):
    """Protocol for all Git and GitHub CLI interactions."""

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]: ...

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool: ...

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None: ...

    def local_head_sha(self, worktree_dir: Path) -> str: ...

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path: ...

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None: ...

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None: ...

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None: ...

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]: ...

    def get_pr_state(self, repo: str, pr_number: int) -> dict[str, Any] | None: ...

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]: ...

    def get_commit_ci_checks(
        self,
        repo: str,
        commit_sha: str,
        branch: str = "",
    ) -> list[CiJobCheck]: ...

    def download_job_screenshot_evidence(
        self,
        repo: str,
        producer_job: CiJobCheck,
        manifest_spec: str,
        dest_dir: Path,
    ) -> tuple[Path | None, str]: ...

    def get_failed_ci_log(self, repo: str, branch: str) -> str: ...

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
        match_head_commit: str | None = None,
    ) -> tuple[bool, str]: ...

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None: ...

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]: ...

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]: ...

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool: ...

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str: ...

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]: ...

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]: ...

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]: ...

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool | None, str, int | None]: ...

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]: ...

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]: ...

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str: ...

    def list_releases(self, repo: str) -> list[dict[str, Any]]: ...


class SubprocessGitGh:
    """Production implementation calling real `git` and `gh` CLIs."""

    def __init__(
        self,
        *,
        http_json_get: Callable[[str, dict[str, str], int], dict[str, Any] | None] | None = None,
        http_artifact_size: Callable[[str, dict[str, str], int], int] | None = None,
        http_bytes_get: Callable[[str, dict[str, str], int], bytes | None] | None = None,
        circle_token: str | None = None,
    ) -> None:
        self._http_json_get = http_json_get or _default_http_json_get
        self._http_artifact_size = http_artifact_size or _default_http_artifact_size
        self._http_bytes_get = http_bytes_get or _default_http_bytes_get
        self._circle_token = circle_token

    def _circleci_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "NightShift-Desktop/0.1",
        }
        token = self._circle_token if self._circle_token is not None else os.environ.get("CIRCLE_TOKEN", "")
        if token:
            headers["Circle-Token"] = token
        return headers

    def _resolve_circleci_pipeline_revision(
        self,
        pipeline_id: str,
        headers: dict[str, str],
        pipeline_revision_cache: dict[str, str],
    ) -> str:
        if not pipeline_id:
            return ""
        if pipeline_id not in pipeline_revision_cache:
            pipe_url = f"https://circleci.com/api/v2/pipeline/{pipeline_id}"
            pipe_payload = self._http_json_get(pipe_url, headers, 30)
            rev = ""
            if pipe_payload:
                resp_id = str(pipe_payload.get("id") or "").strip()
                if not resp_id or resp_id == pipeline_id:
                    vcs_info = pipe_payload.get("vcs")
                    if isinstance(vcs_info, dict) and vcs_info.get("revision"):
                        rev = str(vcs_info["revision"]).strip()
                    elif pipe_payload.get("revision"):
                        rev = str(pipe_payload["revision"]).strip()
            pipeline_revision_cache[pipeline_id] = rev
        return pipeline_revision_cache[pipeline_id]

    def _resolve_circleci_workflow_revision(
        self,
        workflow_id: str,
        headers: dict[str, str],
        workflow_revision_cache: dict[str, str],
        pipeline_revision_cache: dict[str, str],
    ) -> str:
        if not workflow_id:
            return ""
        if workflow_id not in workflow_revision_cache:
            wf_meta_url = f"https://circleci.com/api/v2/workflow/{workflow_id}"
            wf_meta = self._http_json_get(wf_meta_url, headers, 30)
            rev = ""
            if wf_meta:
                resp_id = str(wf_meta.get("id") or "").strip()
                if not resp_id or resp_id == workflow_id:
                    if wf_meta.get("vcs_revision"):
                        rev = str(wf_meta["vcs_revision"]).strip()
                    elif isinstance(wf_meta.get("vcs"), dict) and wf_meta["vcs"].get("revision"):
                        rev = str(wf_meta["vcs"]["revision"]).strip()
                    else:
                        pipeline_id = str(wf_meta.get("pipeline_id") or "").strip()
                        if pipeline_id:
                            rev = self._resolve_circleci_pipeline_revision(
                                pipeline_id,
                                headers,
                                pipeline_revision_cache,
                            )
            workflow_revision_cache[workflow_id] = rev
        return workflow_revision_cache[workflow_id]

    def _resolve_circleci_job(
        self,
        *,
        target_url: str,
        repo: str,
        job_name: str,
        expected_commit_sha: str,
        workflow_cache: dict[str, list[dict[str, Any]]],
        workflow_revision_cache: dict[str, str],
        pipeline_revision_cache: dict[str, str],
        artifacts_cache: dict[tuple[str, str], list[CiArtifact]],
    ) -> _CircleCiJobResolution | None:
        """Resolve authoritative CircleCI job metadata, duration, artifacts, status, and VCS revision.

        Every evidence field is taken strictly from authoritative CircleCI API responses and bound
        to the exact expected job name, job identity, and commit revision. Missing or mismatched
        authoritative fields fail closed without fetching artifacts or falling back to caller values.
        """
        from nightshift.engine.pipeline_evidence import CiArtifact

        parsed = _parse_circleci_target(target_url, repo)
        if not parsed:
            return None

        def _failed(
            *,
            name: str = "",
            job_uuid: str = "",
            workflow_id: str = "",
            revision: str = "",
            status: str = "failed",
        ) -> _CircleCiJobResolution:
            # Never report a "success" status when authoritative verification failed.
            safe_status = status if (status and status != "success") else "failed"
            return _CircleCiJobResolution(
                name=name,
                job_uuid=job_uuid,
                build_number="",
                workflow_id=workflow_id,
                revision=revision,
                duration_seconds=0.0,
                status=safe_status,
                artifacts=[],
                verified=False,
            )

        headers = self._circleci_headers()
        workflow_id = parsed["workflow_id"]
        url_job_uuid = parsed["job_uuid"]
        project_slug = parsed["project_slug"]
        url_job_number = parsed["job_number"]

        if not _is_circleci_slug_allowed(project_slug, repo):
            return _failed()

        auth_job_name = ""
        auth_job_uuid = ""
        auth_build_number = ""
        auth_workflow_id = ""
        auth_status = ""
        circle_revision = ""
        duration = 0.0
        has_authoritative_duration = False

        if workflow_id:
            circle_revision = self._resolve_circleci_workflow_revision(
                workflow_id,
                headers,
                workflow_revision_cache,
                pipeline_revision_cache,
            )
            if workflow_id not in workflow_cache:
                wf_items: list[dict[str, Any]] = []
                page_token = ""
                for _ in range(10):
                    wf_url = f"https://circleci.com/api/v2/workflow/{workflow_id}/job"
                    if page_token:
                        wf_url += f"?page-token={urllib.parse.quote(page_token, safe='')}"
                    payload = self._http_json_get(wf_url, headers, 30)
                    if not payload:
                        break
                    wf_items.extend(
                        item for item in (payload.get("items") or []) if isinstance(item, dict)
                    )
                    page_token = str(payload.get("next_page_token") or "")
                    if not page_token:
                        break
                workflow_cache[workflow_id] = wf_items

            matched_item: dict[str, Any] | None = None
            if url_job_uuid:
                for item in workflow_cache[workflow_id]:
                    if str(item.get("id") or "").strip() == url_job_uuid:
                        matched_item = item
                        break
            elif url_job_number:
                for item in workflow_cache[workflow_id]:
                    raw_num = item.get("job_number")
                    if raw_num is not None and not isinstance(raw_num, bool) and str(raw_num).strip() == url_job_number:
                        matched_item = item
                        break

            # Fail closed if the workflow API does not contain the referenced job.
            if matched_item is None:
                return _failed(workflow_id=workflow_id)

            auth_job_name = str(matched_item.get("name") or "").strip()
            auth_job_uuid = str(matched_item.get("id") or "").strip()
            raw_job_num = matched_item.get("job_number")
            if raw_job_num is not None and not isinstance(raw_job_num, bool):
                auth_build_number = str(raw_job_num).strip()
            auth_workflow_id = workflow_id
            if matched_item.get("project_slug"):
                project_slug = str(matched_item["project_slug"]).strip()
            if not _is_circleci_slug_allowed(project_slug, repo):
                return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)

            auth_status = str(matched_item.get("status") or "").strip().lower()
            started = _parse_iso_dt(matched_item.get("started_at"))
            stopped = _parse_iso_dt(matched_item.get("stopped_at"))
            if started and stopped:
                duration = max(0.0, (stopped - started).total_seconds())
                has_authoritative_duration = True

            # Bind CircleCI evidence strictly to the authoritative job name.
            if not auth_job_name or (job_name and auth_job_name != job_name):
                return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)
            if url_job_uuid and auth_job_uuid != url_job_uuid:
                return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)
            if url_job_number and auth_build_number != url_job_number:
                return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)

        lookup_job_number = auth_build_number if workflow_id else url_job_number
        need_job_detail = bool(
            lookup_job_number
            and project_slug
            and (
                not workflow_id
                or not has_authoritative_duration
                or not circle_revision
                or not auth_status
            )
        )
        if need_job_detail:
            job_url = f"https://circleci.com/api/v2/project/{project_slug}/job/{lookup_job_number}"
            job_payload = self._http_json_get(job_url, headers, 30)
            if job_payload:
                payload_name = str(job_payload.get("name") or "").strip()
                raw_payload_num = job_payload.get("number")
                payload_num = (
                    str(raw_payload_num).strip()
                    if raw_payload_num is not None and not isinstance(raw_payload_num, bool)
                    else ""
                )
                payload_status = str(job_payload.get("status") or "").strip().lower()
                payload_workflow_id = ""
                if isinstance(job_payload.get("latest_workflow"), dict) and job_payload["latest_workflow"].get("id"):
                    payload_workflow_id = str(job_payload["latest_workflow"]["id"]).strip()

                payload_revision = ""
                if job_payload.get("vcs_revision"):
                    payload_revision = str(job_payload["vcs_revision"]).strip()
                elif isinstance(job_payload.get("vcs"), dict) and job_payload["vcs"].get("revision"):
                    payload_revision = str(job_payload["vcs"]["revision"]).strip()
                elif isinstance(job_payload.get("pipeline"), dict) and job_payload["pipeline"].get("id"):
                    payload_revision = self._resolve_circleci_pipeline_revision(
                        str(job_payload["pipeline"]["id"]).strip(),
                        headers,
                        pipeline_revision_cache,
                    )
                elif payload_workflow_id:
                    payload_revision = self._resolve_circleci_workflow_revision(
                        payload_workflow_id,
                        headers,
                        workflow_revision_cache,
                        pipeline_revision_cache,
                    )

                if not workflow_id:
                    auth_job_name = payload_name
                    auth_build_number = payload_num
                    auth_workflow_id = payload_workflow_id
                    auth_status = payload_status
                    circle_revision = payload_revision
                    if url_job_number and payload_num and payload_num != url_job_number:
                        return _failed(name=auth_job_name, workflow_id=auth_workflow_id)
                else:
                    if payload_name and payload_name != auth_job_name:
                        return _failed(name=payload_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)
                    if payload_num and auth_build_number and payload_num != auth_build_number:
                        return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)
                    if payload_workflow_id and auth_workflow_id and payload_workflow_id != auth_workflow_id:
                        return _failed(name=auth_job_name, job_uuid=auth_job_uuid, workflow_id=auth_workflow_id)
                    if circle_revision and payload_revision and payload_revision != circle_revision:
                        return _failed(
                            name=auth_job_name,
                            job_uuid=auth_job_uuid,
                            workflow_id=auth_workflow_id,
                            revision="",
                        )
                    if not circle_revision and payload_revision:
                        circle_revision = payload_revision
                    if not auth_status and payload_status:
                        auth_status = payload_status

                if not has_authoritative_duration:
                    started = _parse_iso_dt(job_payload.get("started_at"))
                    stopped = _parse_iso_dt(job_payload.get("stopped_at"))
                    if started and stopped:
                        duration = max(0.0, (stopped - started).total_seconds())
                        has_authoritative_duration = True
                    else:
                        raw_dur = job_payload.get("duration")
                        if not isinstance(raw_dur, bool) and isinstance(raw_dur, (int, float)):
                            duration = max(0.0, float(raw_dur) / 1000.0)
                            has_authoritative_duration = True

        # Enforce authoritative job name, build number, status, and revision before accepting duration or artifacts.
        name_matches = bool(auth_job_name) and (not job_name or auth_job_name == job_name)
        build_matches = bool(auth_build_number) and (not url_job_number or auth_build_number == url_job_number)
        status_present = bool(auth_status)
        revision_matches = bool(circle_revision) and (
            not expected_commit_sha or circle_revision == expected_commit_sha
        )

        if not (name_matches and build_matches and status_present and revision_matches):
            # If the authoritative job name belongs to a different job, do not surface its revision either.
            exposed_revision = circle_revision if (not auth_job_name or not job_name or auth_job_name == job_name) else ""
            exposed_status = auth_status if name_matches else "failed"
            return _failed(
                name=auth_job_name,
                job_uuid=auth_job_uuid,
                workflow_id=auth_workflow_id,
                revision=exposed_revision,
                status=exposed_status,
            )

        artifacts: list[CiArtifact] = []
        if auth_build_number and project_slug:
            cache_key = (project_slug, auth_build_number)
            if cache_key not in artifacts_cache:
                collected: list[CiArtifact] = []
                page_token = ""
                for _ in range(10):
                    art_url = f"https://circleci.com/api/v2/project/{project_slug}/{auth_build_number}/artifacts"
                    if page_token:
                        art_url += f"?page-token={urllib.parse.quote(page_token, safe='')}"
                    art_payload = self._http_json_get(art_url, headers, 30)
                    if not art_payload:
                        break
                    for item in art_payload.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        art_path = str(item.get("path") or "").strip()
                        dl_url = str(item.get("url") or "").strip()
                        if not art_path:
                            continue
                        raw_size = (
                            item.get("size_bytes")
                            if item.get("size_bytes") is not None
                            else item.get("size")
                        )
                        if raw_size is not None and not isinstance(raw_size, bool):
                            try:
                                size_bytes = max(0, int(raw_size))
                            except (TypeError, ValueError):
                                size_bytes = 0
                        elif dl_url:
                            size_bytes = self._http_artifact_size(dl_url, headers, 30)
                        else:
                            size_bytes = 0
                        collected.append(
                            CiArtifact(
                                path=art_path,
                                size_bytes=size_bytes,
                                build_id=auth_build_number,
                                url=dl_url,
                                scope="job",
                            )
                        )
                    page_token = str(art_payload.get("next_page_token") or "")
                    if not page_token:
                        break
                artifacts_cache[cache_key] = collected
            artifacts = list(artifacts_cache[cache_key])

        return _CircleCiJobResolution(
            name=auth_job_name,
            job_uuid=auth_job_uuid,
            build_number=auth_build_number,
            workflow_id=auth_workflow_id,
            revision=circle_revision,
            duration_seconds=duration if has_authoritative_duration else 0.0,
            status=auth_status,
            artifacts=artifacts,
            verified=True,
        )

    def _fetch_gh_actions_artifacts(
        self,
        repo: str,
        run_id: str,
        expected_commit_sha: str,
        gh_artifacts_cache: dict[str, list[dict[str, Any]]],
    ) -> list[CiArtifact]:
        """Fetch non-expired GitHub Actions workflow run artifacts, preserving workflow-run provenance."""
        from nightshift.engine.pipeline_evidence import CiArtifact

        if not run_id:
            return []
        if run_id not in gh_artifacts_cache:
            rc_art, out_art, _, _ = run_subprocess_with_timeout(
                ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"],
                timeout_seconds=30,
            )
            raw_list: list[dict[str, Any]] = []
            if rc_art == 0 and out_art:
                try:
                    art_payload = json.loads(out_art)
                    raw_list = [
                        item for item in (art_payload.get("artifacts") or []) if isinstance(item, dict)
                    ]
                except Exception:
                    raw_list = []
            gh_artifacts_cache[run_id] = raw_list

        artifacts: list[CiArtifact] = []
        wf_build_id = f"workflow-run:{run_id}"
        for art in gh_artifacts_cache[run_id]:
            if art.get("expired"):
                continue
            art_wf = art.get("workflow_run")
            if isinstance(art_wf, dict):
                if art_wf.get("id") is not None and str(art_wf["id"]) != str(run_id):
                    continue
                if expected_commit_sha and art_wf.get("head_sha") and str(art_wf["head_sha"]) != expected_commit_sha:
                    continue
            art_name = str(art.get("name") or "").strip()
            if not art_name:
                continue
            artifacts.append(
                CiArtifact(
                    path=art_name,
                    size_bytes=max(0, int(art.get("size_in_bytes") or 0)),
                    build_id=wf_build_id,
                    url=str(art.get("archive_download_url") or ""),
                    scope="workflow_run",
                )
            )
        return artifacts

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]:
        rc, out, err, _ = run_subprocess_with_timeout(["git", "fetch", "origin"], cwd=local_repo, timeout_seconds=120)
        return rc, out, err

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=local_repo,
            timeout_seconds=60,
        )
        return rc == 0 and bool(out.strip())

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=local_repo,
            timeout_seconds=60,
        )
        if rc == 0 and out.strip():
            return out.split()[0]
        return None

    def local_head_sha(self, worktree_dir: Path) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(["git", "rev-parse", "HEAD"], cwd=worktree_dir, timeout_seconds=30)
        return out.strip() if rc == 0 else ""

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        self.fetch_origin(local_repo)

        if worktree_dir.exists():
            rc, out, err, _ = run_subprocess_with_timeout(
                ["git", "branch", "--show-current"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_root, root, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--show-toplevel"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_common, common, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_repo, repo_common, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=local_repo, timeout_seconds=30,
            )
            if (rc != 0 or out.strip() != branch or rc_root != 0 or Path(root.strip()).resolve() != worktree_dir.resolve()
                    or rc_common != 0 or rc_repo != 0 or Path(common.strip()).resolve() != Path(repo_common.strip()).resolve()):
                raise RuntimeError(f"Existing worktree does not match branch {branch}: {err}")
            remote = self.remote_branch_sha(local_repo, branch)
            if remote and self.local_head_sha(worktree_dir) != remote:
                def git(*args: str) -> str:
                    rc_git, output, error, _ = run_subprocess_with_timeout(
                        ["git", *args], cwd=worktree_dir, timeout_seconds=120,
                    )
                    if rc_git != 0:
                        raise RuntimeError(f"Could not reconcile worktree: {error or output}")
                    return output.strip()

                # Preserve tracked and untracked work before integrating the remote head.
                if git("status", "--porcelain"):
                    git("add", "-A")
                    git("commit", "-m", "nightshift: checkpoint before resume")
                git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
                git("merge", "--no-edit", f"origin/{branch}")
                if self.local_head_sha(worktree_dir) != self.remote_branch_sha(local_repo, branch):
                    if branch.strip().lower() in ("main", "master"):
                        raise RuntimeError("Cannot push a reconciliation to a protected branch")
                    git("push", "origin", branch)
                if self.local_head_sha(worktree_dir) != self.remote_branch_sha(local_repo, branch):
                    raise RuntimeError("Remote branch changed during worktree reconciliation")
            return worktree_dir
        run_subprocess_with_timeout(["git", "worktree", "prune"], cwd=local_repo, timeout_seconds=60)

        base = f"origin/{base_branch}"
        if self.remote_branch_exists(local_repo, branch):
            run_subprocess_with_timeout(
                ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                cwd=local_repo,
                timeout_seconds=120,
            )
            base = f"origin/{branch}"

        rc, _, err, _ = run_subprocess_with_timeout(
            ["git", "worktree", "add", "-B", branch, str(worktree_dir), base],
            cwd=local_repo,
            timeout_seconds=120,
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add failed: {err}")

        # Configure info/exclude to ignore .nightshift_prompt.md and .nightshift/handoff.md
        rc_gd, out_gd, _, _ = run_subprocess_with_timeout(["git", "rev-parse", "--git-common-dir"], cwd=worktree_dir, timeout_seconds=30)
        if rc_gd == 0 and out_gd:
            exclude_path = Path(out_gd) / "info" / "exclude"
            if not exclude_path.is_absolute():
                exclude_path = worktree_dir / exclude_path
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
            lines_to_add = [".nightshift_prompt.md", ".nightshift/handoff.md", ".nightshift/*"]
            new_lines = [l for l in lines_to_add if l not in existing]
            if new_lines:
                with open(exclude_path, "a", encoding="utf-8") as f:
                    f.write("\n" + "\n".join(new_lines) + "\n")

        return worktree_dir

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None:
        run_subprocess_with_timeout(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=local_repo, timeout_seconds=60)
        shutil.rmtree(worktree_dir, ignore_errors=True)
        run_subprocess_with_timeout(["git", "worktree", "prune"], cwd=local_repo, timeout_seconds=60)

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url,headRefOid,mergeable,mergeStateStatus"],
            timeout_seconds=60,
        )
        if rc == 0 and out:
            try:
                prs = json.loads(out)
                if prs:
                    return prs[0]
            except Exception:
                pass
        return None

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "merged", "--json", "number,url,mergeCommit", "--limit", "1"],
            timeout_seconds=60,
        )
        if rc == 0 and out:
            try:
                prs = json.loads(out)
                if prs:
                    return prs[0]
            except Exception:
                pass
        return None

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        rc, out, err, _ = run_subprocess_with_timeout(
            [
                "gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                "--json", "number,url,state,headRefOid,mergeCommit,mergeable,mergeStateStatus",
            ],
            timeout_seconds=60,
        )
        if rc != 0:
            raise RuntimeError(f"gh pr list failed for {branch}: {err or out}")
        if not out:
            return []
        return list(json.loads(out))

    def get_pr_state(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        rc, out, err, _ = run_subprocess_with_timeout(
            [
                "gh", "pr", "view", str(pr_number), "--repo", repo,
                "--json", "number,url,state,headRefOid,mergeCommit",
            ],
            timeout_seconds=60,
        )
        if rc != 0:
            raise RuntimeError(f"gh pr view #{pr_number} failed: {err or out}")
        if not out:
            return None
        return dict(json.loads(out))

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]:
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "pr", "checks", str(pr_number), "--repo", repo, "--watch"],
            cwd=worktree_dir,
            timeout_seconds=timeout_seconds,
        )
        return (rc == 0, out or err)

    def get_commit_ci_checks(
        self,
        repo: str,
        commit_sha: str,
        branch: str = "",
    ) -> list[CiJobCheck]:
        """Query authoritative CI check-runs/statuses, durations, and artifacts for ``commit_sha``."""
        from nightshift.engine.pipeline_evidence import CiArtifact, CiJobCheck, DEFAULT_GATE_JOB_NAMES

        if not commit_sha:
            return []

        def _map_circleci_status(ci_status: str, fallback: str) -> str:
            if not ci_status:
                return fallback
            if ci_status == "success":
                return "success"
            if ci_status in ("not_run", "skipped"):
                return "skipped"
            if ci_status in ("running", "queued", "on_hold", "blocked"):
                return "running"
            if ci_status == "canceled":
                return "canceled"
            return "failed"

        workflow_cache: dict[str, list[dict[str, Any]]] = {}
        workflow_revision_cache: dict[str, str] = {}
        pipeline_revision_cache: dict[str, str] = {}
        circle_artifacts_cache: dict[tuple[str, str], list[CiArtifact]] = {}
        gh_artifacts_cache: dict[str, list[dict[str, Any]]] = {}

        results: list[CiJobCheck] = []
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "api", f"repos/{repo}/commits/{commit_sha}/check-runs?per_page=100"],
            timeout_seconds=60,
        )
        if rc == 0 and out:
            try:
                payload = json.loads(out)
                for run in payload.get("check_runs") or []:
                    name = str(run.get("name") or "").strip()
                    if not name:
                        continue
                    norm_name = name.split(":", 1)[-1].strip() if name.lower().startswith("ci/circleci:") else name
                    status_raw = str(run.get("status") or "").lower()
                    conclusion = str(run.get("conclusion") or "").lower()
                    if status_raw == "completed":
                        eff_status = conclusion or "failed"
                    else:
                        eff_status = status_raw or "running"
                    started = _parse_iso_dt(run.get("started_at"))
                    completed = _parse_iso_dt(run.get("completed_at"))
                    duration = (
                        max(0.0, (completed - started).total_seconds())
                        if started and completed
                        else 0.0
                    )
                    head_oid = str(run.get("head_sha") or "")
                    build_id = str(run.get("id") or run.get("external_id") or "")
                    details_url = str(run.get("details_url") or run.get("html_url") or "")
                    artifacts: list[CiArtifact] = []
                    gha_run_id = ""

                    circle_info = self._resolve_circleci_job(
                        target_url=details_url,
                        repo=repo,
                        job_name=norm_name,
                        expected_commit_sha=commit_sha,
                        workflow_cache=workflow_cache,
                        workflow_revision_cache=workflow_revision_cache,
                        pipeline_revision_cache=pipeline_revision_cache,
                        artifacts_cache=circle_artifacts_cache,
                    )
                    if circle_info is not None:
                        # Authoritative CircleCI check: never fall back to GitHub check-run
                        # build_id, duration, status, or head_sha when CircleCI fields are zero/missing.
                        build_id = circle_info.build_number
                        duration = circle_info.duration_seconds
                        artifacts = circle_info.artifacts
                        eff_status = _map_circleci_status(circle_info.status, "failed")
                        head_oid = circle_info.revision
                        norm_name = circle_info.name if circle_info.verified else norm_name
                    elif name.lower().startswith("ci/circleci:"):
                        # Check claims to be CircleCI without a valid CircleCI URL: fail closed.
                        build_id = ""
                        duration = 0.0
                        artifacts = []
                        eff_status = "failed"
                        head_oid = ""
                    else:
                        parsed_gha = _parse_gh_actions_target(details_url, repo)
                        if parsed_gha is not None:
                            run_id, gha_job_id = parsed_gha
                            if not gha_job_id or (build_id and gha_job_id == build_id):
                                gha_run_id = run_id
                                if head_oid == commit_sha:
                                    artifacts = self._fetch_gh_actions_artifacts(
                                        repo,
                                        gha_run_id,
                                        commit_sha,
                                        gh_artifacts_cache,
                                    )

                    results.append(
                        CiJobCheck(
                            name=norm_name,
                            status=eff_status,
                            commit_sha=head_oid,
                            build_id=build_id,
                            duration_seconds=duration,
                            artifacts=artifacts,
                            compat_mode=False,
                            is_gate_job=norm_name in DEFAULT_GATE_JOB_NAMES,
                            branch=branch,
                            workflow_run_id=gha_run_id,
                        )
                    )
            except Exception:
                pass

        # Also inspect commit statuses (CircleCI commonly reports ci/circleci: <job>)
        rc_s, out_s, _, _ = run_subprocess_with_timeout(
            ["gh", "api", f"repos/{repo}/commits/{commit_sha}/status?per_page=100"],
            timeout_seconds=60,
        )
        if rc_s == 0 and out_s:
            try:
                st_payload = json.loads(out_s)
                seen = {j.name for j in results}
                for st in st_payload.get("statuses") or []:
                    ctx = str(st.get("context") or "").strip()
                    if not ctx:
                        continue
                    norm_name = ctx.split(":", 1)[-1].strip() if ctx.lower().startswith("ci/circleci:") else ctx
                    if norm_name in seen:
                        continue
                    state = str(st.get("state") or "").lower()
                    eff_status = "success" if state == "success" else ("running" if state == "pending" else "failed")
                    target_url = str(st.get("target_url") or "")
                    # Never derive execution duration from GitHub status created_at/updated_at timestamps:
                    # they record the GitHub status event, not CI execution time.
                    duration = 0.0
                    build_id = str(st.get("id") or "")
                    artifacts = []
                    resolved_commit_sha = str(st_payload.get("sha") or "")
                    gha_run_id = ""

                    circle_info = self._resolve_circleci_job(
                        target_url=target_url,
                        repo=repo,
                        job_name=norm_name,
                        expected_commit_sha=commit_sha,
                        workflow_cache=workflow_cache,
                        workflow_revision_cache=workflow_revision_cache,
                        pipeline_revision_cache=pipeline_revision_cache,
                        artifacts_cache=circle_artifacts_cache,
                    )
                    if circle_info is not None:
                        # Authoritative CircleCI status: never fall back to GitHub status
                        # build_id, duration, status, or commit SHA.
                        build_id = circle_info.build_number
                        duration = circle_info.duration_seconds
                        artifacts = circle_info.artifacts
                        eff_status = _map_circleci_status(circle_info.status, "failed")
                        resolved_commit_sha = circle_info.revision
                        norm_name = circle_info.name if circle_info.verified else norm_name
                    elif ctx.lower().startswith("ci/circleci:"):
                        # Status claims to be CircleCI without a valid CircleCI URL: fail closed.
                        build_id = ""
                        duration = 0.0
                        artifacts = []
                        eff_status = "failed"
                        resolved_commit_sha = ""
                    else:
                        parsed_gha = _parse_gh_actions_target(target_url, repo)
                        if parsed_gha is not None:
                            run_id, gha_job_id = parsed_gha
                            # Fail closed for GitHub Actions commit statuses until the authoritative
                            # Actions job API verifies job name, job id, workflow run id, status, and head_sha.
                            resolved_commit_sha = ""
                            build_id = ""
                            duration = 0.0
                            eff_status = "failed"
                            artifacts = []
                            gha_run_id = ""
                            if gha_job_id:
                                rc_j, out_j, _, _ = run_subprocess_with_timeout(
                                    ["gh", "api", f"repos/{repo}/actions/jobs/{gha_job_id}"],
                                    timeout_seconds=30,
                                )
                                if rc_j == 0 and out_j:
                                    try:
                                        j_payload = json.loads(out_j)
                                        j_name = str(j_payload.get("name") or "").strip()
                                        j_id = str(j_payload.get("id") or "").strip()
                                        j_sha = str(j_payload.get("head_sha") or "").strip()
                                        j_run_id = str(j_payload.get("run_id") or "").strip()
                                        j_status = str(j_payload.get("status") or "").strip().lower()
                                        j_concl = str(j_payload.get("conclusion") or "").strip().lower()

                                        name_ok = bool(j_name) and j_name == norm_name
                                        id_ok = bool(j_id) and j_id == gha_job_id
                                        run_ok = bool(j_run_id) and j_run_id == run_id
                                        sha_ok = bool(j_sha) and j_sha == commit_sha
                                        status_ok = bool(j_status)

                                        if not j_name or j_name == norm_name:
                                            resolved_commit_sha = j_sha
                                        if name_ok and id_ok and run_ok and sha_ok and status_ok:
                                            gha_run_id = j_run_id
                                            build_id = j_id
                                            norm_name = j_name
                                            started = _parse_iso_dt(j_payload.get("started_at"))
                                            completed = _parse_iso_dt(j_payload.get("completed_at"))
                                            if started and completed:
                                                duration = max(0.0, (completed - started).total_seconds())
                                            eff_status = (j_concl or "failed") if j_status == "completed" else j_status
                                            artifacts = self._fetch_gh_actions_artifacts(
                                                repo,
                                                j_run_id,
                                                commit_sha,
                                                gh_artifacts_cache,
                                            )
                                    except Exception:
                                        pass

                    results.append(
                        CiJobCheck(
                            name=norm_name,
                            status=eff_status,
                            commit_sha=resolved_commit_sha,
                            build_id=build_id,
                            duration_seconds=duration,
                            artifacts=artifacts,
                            compat_mode=False,
                            is_gate_job=norm_name in DEFAULT_GATE_JOB_NAMES,
                            branch=branch,
                            workflow_run_id=gha_run_id,
                        )
                    )
                    seen.add(norm_name)
            except Exception:
                pass

        # Record authoritative job count per GitHub Actions workflow run so multi-job runs
        # cannot misattribute workflow-wide artifacts to an arbitrary job.
        run_counts: dict[str, int] = {}
        for job in results:
            if job.workflow_run_id:
                run_counts[job.workflow_run_id] = run_counts.get(job.workflow_run_id, 0) + 1
        if run_counts:
            results = [
                replace(job, workflow_run_job_count=run_counts[job.workflow_run_id])
                if job.workflow_run_id
                else job
                for job in results
            ]
        return results

    def download_job_screenshot_evidence(
        self,
        repo: str,
        producer_job: CiJobCheck,
        manifest_spec: str,
        dest_dir: Path,
    ) -> tuple[Path | None, str]:
        """Download screenshot manifest and referenced PNGs from the exact verified ``producer_job`` build."""
        from nightshift.engine.pipeline_evidence import CiArtifact, _artifact_matches, _norm_posix

        if not producer_job.build_id:
            return None, f"Screenshot producer job '{producer_job.name}' has no verified build_id."
        if not producer_job.artifacts:
            return None, f"Screenshot producer job '{producer_job.name}' (build {producer_job.build_id}) has no downloadable artifacts."

        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_root = dest_dir.resolve()

        spec_norm = _norm_posix(manifest_spec)
        spec_name = Path(spec_norm).name if spec_norm else "screenshot-manifest.json"

        job_scoped_arts = [
            a
            for a in producer_job.artifacts
            if isinstance(a, CiArtifact)
            and (a.scope or "job").strip().lower() == "job"
            and a.build_id == producer_job.build_id
            and a.size_bytes > 0
            and a.url
        ]
        if job_scoped_arts:
            manifest_art: CiArtifact | None = None
            for art in job_scoped_arts:
                art_norm = _norm_posix(art.path)
                if not art_norm.lower().endswith(".json"):
                    continue
                if _artifact_matches(art_norm, spec_norm):
                    manifest_art = art
                    break
            if manifest_art is None:
                return (
                    None,
                    f"No downloadable screenshot manifest matching '{manifest_spec}' found in verified build '{producer_job.build_id}'.",
                )

            headers = self._circleci_headers()
            manifest_bytes = self._http_bytes_get(manifest_art.url, headers, 60)
            if not manifest_bytes:
                return (
                    None,
                    f"Failed to download screenshot manifest '{manifest_art.path}' from verified build '{producer_job.build_id}'.",
                )

            manifest_posix = _norm_posix(manifest_art.path)
            manifest_filename = manifest_posix.rsplit("/", 1)[-1] or "screenshot-manifest.json"
            manifest_prefix = manifest_posix.rsplit("/", 1)[0] + "/" if "/" in manifest_posix else ""
            local_manifest = dest_root / manifest_filename
            local_manifest.write_bytes(manifest_bytes)

            try:
                parsed = json.loads(manifest_bytes.decode("utf-8"))
            except Exception:
                return local_manifest, ""

            if isinstance(parsed, dict) and isinstance(parsed.get("screenshots"), list):
                art_by_path = {_norm_posix(a.path): a for a in job_scoped_arts}
                for entry in parsed["screenshots"]:
                    if not isinstance(entry, dict):
                        continue
                    file_rel = str(entry.get("file") or "").strip()
                    if not file_rel:
                        continue
                    target_png = (dest_root / file_rel).resolve()
                    try:
                        target_png.relative_to(dest_root)
                    except ValueError:
                        continue
                    norm_rel = _norm_posix(file_rel)
                    candidate_key = f"{manifest_prefix}{norm_rel}" if manifest_prefix else norm_rel
                    png_art = art_by_path.get(candidate_key)
                    if png_art is None:
                        continue
                    png_bytes = self._http_bytes_get(png_art.url, headers, 60)
                    if png_bytes:
                        target_png.parent.mkdir(parents=True, exist_ok=True)
                        target_png.write_bytes(png_bytes)

            return local_manifest, ""

        # GitHub Actions workflow-run artifacts: enforce single-job producer provenance before downloading.
        wf_run_id = producer_job.workflow_run_id
        expected_wf_build = f"workflow-run:{wf_run_id}" if wf_run_id else ""
        wf_scoped_arts = [
            a
            for a in producer_job.artifacts
            if isinstance(a, CiArtifact)
            and (a.scope or "").strip().lower() == "workflow_run"
            and expected_wf_build
            and a.build_id == expected_wf_build
            and a.size_bytes > 0
        ]
        if wf_scoped_arts:
            if int(producer_job.workflow_run_job_count or 1) > 1:
                return (
                    None,
                    f"Workflow run '{wf_run_id}' contains multiple jobs; screenshot artifact provenance for job '{producer_job.name}' cannot be verified.",
                )
            top_dir = spec_norm.split("/", 1)[0] if "/" in spec_norm else ""
            chosen_art = next(
                (
                    a
                    for a in wf_scoped_arts
                    if _artifact_matches(a.path, spec_norm)
                    or (top_dir and _artifact_matches(a.path, top_dir))
                    or a.path == "ui-screenshots"
                ),
                None,
            )
            if chosen_art is None:
                return (
                    None,
                    f"No workflow artifact matching '{manifest_spec}' found in verified run '{wf_run_id}'.",
                )
            rc_dl, _, err_dl, _ = run_subprocess_with_timeout(
                ["gh", "run", "download", str(wf_run_id), "--repo", repo, "--name", chosen_art.path, "--dir", str(dest_root)],
                timeout_seconds=180,
            )
            if rc_dl != 0:
                return None, f"Failed to download workflow artifact '{chosen_art.path}' from run '{wf_run_id}': {err_dl}"

            candidates = [
                p
                for p in dest_root.rglob("*.json")
                if p.is_file()
                and (
                    _artifact_matches(_norm_posix(str(p.relative_to(dest_root))), spec_norm)
                    or (spec_name.lower().endswith(".json") and p.name == spec_name)
                    or p.name == "screenshot-manifest.json"
                )
            ]
            if not candidates:
                return (
                    None,
                    f"Downloaded workflow artifact '{chosen_art.path}' from run '{wf_run_id}' does not contain '{manifest_spec}'.",
                )
            return candidates[0], ""

        return (
            None,
            f"No downloadable artifacts bound to verified build '{producer_job.build_id}' for job '{producer_job.name}'.",
        )

    def get_failed_ci_log(self, repo: str, branch: str) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "run", "list", "--repo", repo, "--branch", branch, "--limit", "5", "--json", "databaseId,name,conclusion,status"],
            timeout_seconds=60,
        )
        if rc != 0 or not out:
            return "Could not list GitHub Actions runs."
        try:
            runs = json.loads(out)
            failed = [r for r in runs if r.get("conclusion") == "failure"]
            if not failed:
                return "No failed workflow logs found."
            run_id = str(failed[0]["databaseId"])
            rc2, log_out, err2, _ = run_subprocess_with_timeout(
                ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
                timeout_seconds=120,
            )
            lines = (log_out or err2).splitlines()[-120:]
            return f"Failed run {run_id} ({failed[0].get('name')}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error retrieving failed CI log: {e}"

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
        match_head_commit: str | None = None,
    ) -> tuple[bool, str]:
        cmd = ["gh", "pr", "merge", str(pr_number), "--repo", repo, strategy, "--delete-branch"]
        if match_head_commit:
            cmd.extend(["--match-head-commit", match_head_commit])
        rc, out, err, _ = run_subprocess_with_timeout(
            cmd,
            cwd=local_repo,
            timeout_seconds=120,
        )
        return (rc == 0, out or err)

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None:
        if branch.strip().lower() in ("main", "master"):
            raise ValueError(f"Hard guard violation: direct push to protected branch '{branch}' is forbidden.")
        run_subprocess_with_timeout(["git", "add", "-A"], cwd=worktree_dir, timeout_seconds=60)
        rc_c, _, _, _ = run_subprocess_with_timeout(["git", "commit", "-m", message], cwd=worktree_dir, timeout_seconds=60)
        if rc_c == 0:
            run_subprocess_with_timeout(["git", "push", "origin", branch], cwd=worktree_dir, timeout_seconds=120)
            return self.local_head_sha(worktree_dir)
        return None

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]:
        run_subprocess_with_timeout(["git", "fetch", "origin", "main"], cwd=worktree_dir, timeout_seconds=60)
        rc, out, err, _ = run_subprocess_with_timeout(
            ["git", "merge", "origin/main", "--no-edit"],
            cwd=worktree_dir,
            timeout_seconds=60,
        )
        return (rc == 0, out or err)

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]:
        """Create a revert on an isolated branch and merge only after its checks pass."""
        branch = f"nightshift/revert-{commit_sha}"
        worktree = local_repo.parent / f"nightshift-revert-{commit_sha}"
        if self.find_merged_pr(repo, branch):
            return True, "Revert PR already merged"
        try:
            self.prepare_worktree(local_repo, worktree, branch)
            pr = self.find_open_pr(repo, branch)
            if not pr:
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["git", "revert", "-m", "1", commit_sha, "--no-edit"], cwd=worktree, timeout_seconds=60,
                )
                if rc != 0:
                    return False, out or err
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["git", "push", "-u", "origin", branch], cwd=worktree, timeout_seconds=120,
                )
                if rc != 0:
                    return False, out or err
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["gh", "pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                     "--title", f"Revert {commit_sha[:7]}", "--body", "Revert a merge whose main build failed."],
                    cwd=worktree, timeout_seconds=60,
                )
                if rc != 0:
                    return False, out or err
                pr = self.find_open_pr(repo, branch)
            if not pr or not pr.get("headRefOid"):
                return False, "Could not verify revert PR head"
            passed, msg = self.watch_pr_checks(repo, pr["number"], worktree)
            latest = self.find_open_pr(repo, branch)
            if not passed or not latest or latest.get("headRefOid") != pr["headRefOid"]:
                return False, f"Revert PR checks failed or head changed: {msg}"
            return self.merge_pr(repo, pr["number"], local_repo)
        finally:
            if worktree.exists():
                self.remove_worktree(local_repo, worktree)

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "api", f"repos/{repo}/branches/{branch}/protection"],
            timeout_seconds=60,
        )
        return rc == 0 and bool(out.strip())

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str:
        rc, out, _, _ = run_subprocess_with_timeout(["git", "diff", base], cwd=worktree_dir, timeout_seconds=60)
        return out if rc == 0 else ""

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "diff", "--name-only", "-z", base],
            cwd=worktree_dir,
            timeout_seconds=60,
            strip_output=False,
        )
        return [p for p in out.split("\0") if p] if rc == 0 else []

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]:
        """Extract rename/copy/delete-aware diff entries using ``git diff --name-status -M -C -z``."""
        from nightshift.engine.pipeline_contracts import parse_name_status_diff

        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "diff", "--name-status", "-M", "-C", "-z", base],
            cwd=worktree_dir,
            timeout_seconds=60,
            strip_output=False,
        )
        return parse_name_status_diff(out, nul_delimited=True) if rc == 0 else []

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]:
        """Capture ``(before_source, after_source)`` for modified/deleted/renamed test files."""
        from nightshift.engine.pipeline_contracts import is_test_file_path

        snapshots: dict[str, tuple[str | None, str | None]] = {}
        for entry in entries:
            candidate_paths = entry.all_paths()
            if not any(is_test_file_path(p) for p in candidate_paths):
                continue
            before_ref_path = entry.old_path or entry.path
            before_src: str | None = None
            if entry.status != "A":
                rc, out, _, _ = run_subprocess_with_timeout(
                    ["git", "show", f"{base}:{before_ref_path}"],
                    cwd=worktree_dir,
                    timeout_seconds=30,
                )
                if rc == 0:
                    before_src = out
            after_src: str | None = None
            if entry.status != "D":
                target_file = worktree_dir / entry.path
                if target_file.exists() and target_file.is_file():
                    after_src = target_file.read_text(encoding="utf-8", errors="replace")
            snapshots[before_ref_path if entry.status == "D" else entry.path] = (before_src, after_src)
        return snapshots

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool | None, str, int | None]:
        if not commit_sha:
            return None, "Merged commit SHA is required", None
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "run", "list", "--repo", repo, "--branch", branch, "--workflow", workflow, "--commit", commit_sha, "--limit", "1", "--json", "databaseId,status,conclusion,headSha"],
            timeout_seconds=60,
        )
        if rc != 0 or not out:
            return None, f"Could not locate workflow run: {err}", None
        try:
            runs = json.loads(out)
            if not runs:
                return None, "No workflow runs found", None
            run_head_sha = str(runs[0].get("headSha") or "").strip()
            if run_head_sha and run_head_sha != commit_sha:
                return None, f"Workflow run headSha {run_head_sha[:7]} != expected {commit_sha[:7]}", None
            run_id = int(runs[0]["databaseId"])
            rc_w, out_w, err_w, _ = run_subprocess_with_timeout(
                ["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status"],
                timeout_seconds=timeout_seconds,
            )
            if rc_w == 0:
                return True, out_w or err_w, run_id
            rc_v, out_v, _, _ = run_subprocess_with_timeout(
                ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "status,conclusion"], timeout_seconds=60,
            )
            if rc_v == 0:
                result = json.loads(out_v)
                if result.get("status") == "completed" and result.get("conclusion") == "failure":
                    return False, out_w or err_w, run_id
            return None, out_w or err_w, run_id
        except Exception as e:
            return None, str(e), None

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        rc, _, err, _ = run_subprocess_with_timeout(
            ["gh", "run", "download", str(run_id), "--repo", repo, "--dir", str(dest_dir)],
            timeout_seconds=300,
        )
        if rc != 0:
            raise RuntimeError(f"Artifact download failed: {err}")
        return [p for p in dest_dir.rglob("*") if p.is_file()]

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]:
        cmd = ["gh", "release", "create", tag]
        for f in files:
            cmd.append(str(f))
        cmd.extend(["--repo", repo, "--title", title, "--notes", notes])
        if prerelease:
            cmd.append("--prerelease")
        rc, out, err, _ = run_subprocess_with_timeout(cmd, timeout_seconds=180)
        return (rc == 0, out or err)

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "release", "list", "--repo", repo, "--limit", "40", "--json", "tagName"],
            timeout_seconds=60,
        )
        max_num = 0
        escaped_prefix = re.escape(tag_prefix)
        pattern = re.compile(rf"^{escaped_prefix}(\d+)$")
        if rc == 0 and out:
            try:
                for item in json.loads(out):
                    m = pattern.match(item.get("tagName", ""))
                    if m:
                        max_num = max(max_num, int(m.group(1)))
            except Exception:
                pass
        return f"{tag_prefix}{max_num + 1}"

    def list_releases(self, repo: str) -> list[dict[str, Any]]:
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "api", f"repos/{repo}/releases?per_page=40"],
            timeout_seconds=60,
        )
        if rc != 0:
            raise RuntimeError(f"gh api releases failed for {repo}: {err or out}")
        if not out:
            return []
        raw_items = json.loads(out)
        results: list[dict[str, Any]] = []
        for item in raw_items:
            results.append(
                {
                    "tag": item.get("tag_name") or item.get("tagName") or "",
                    "title": item.get("name") or "",
                    "body": item.get("body") or "",
                    "assets": [
                        {"name": a.get("name") or "", "size": a.get("size") or 0}
                        for a in (item.get("assets") or [])
                    ],
                }
            )
        return results


class FakeGitGh:
    """In-memory fake VCS implementation for completely offline, deterministic testing."""

    def __init__(self) -> None:
        self.branches: dict[str, str] = {"main": "sha_main_001"}
        self.prs: dict[str, dict[str, Any]] = {}
        self.merged_prs: dict[str, dict[str, Any]] = {}
        self.closed_prs: dict[str, dict[str, Any]] = {}
        self.ci_results: dict[int, list[bool]] = {}  # list of outcomes per attempt
        self.ci_flaky: dict[int, bool] = {}
        self.failed_ci_logs: dict[str, str] = {}
        self.worktrees: dict[str, Path] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.reverts: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.branch_protection: dict[str, bool] = {"main": True}
        self.diffs: dict[str, str] = {}
        self.changed_files: dict[str, list[str]] = {}
        self.diff_entries: dict[str, list[Any]] = {}
        self.test_file_snapshots: dict[str, dict[str, tuple[str | None, str | None]]] = {}
        self.workflow_runs: dict[str, bool] = {"Android build": True, "Desktop build": True}
        self.artifacts_to_create: list[str] = ["NightShift-test.zip"]
        self.commit_ci_checks: dict[str, list[Any]] = {}
        self.job_artifact_files: dict[str, dict[str, bytes]] = {}
        self.default_ci_jobs: list[str] = [
            "desktop-gates",
            "desktop-backend",
            "desktop-frontend",
            "desktop-package",
        ]
        self.default_ci_artifacts_by_job: dict[str, list[str]] = {
            "desktop-frontend": ["ui-screenshots/screenshot-manifest.json"],
            "desktop-package": ["nightshift-windows/NightShift-windows.zip"],
        }
        self.merge_conflict: bool = False
        self._next_pr_number = 100

    def add_pr(self, branch: str, number: int | None = None, url: str | None = None, head_sha: str = "sha_head_1") -> dict[str, Any]:
        num = number or self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": num,
            "url": url or f"https://github.com/test/repo/pull/{num}",
            "headRefOid": head_sha,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        self.prs[branch] = pr
        self.branches[branch] = head_sha
        return pr

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]:
        return 0, "From github.com:test/repo\n * [new branch] main -> origin/main", ""

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool:
        return branch in self.branches

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None:
        return self.branches.get(branch)

    def local_head_sha(self, worktree_dir: Path) -> str:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.branches.get(branch, "sha_local_default")
        if len(self.prs) == 1:
            return str(next(iter(self.prs.values())).get("headRefOid") or "")
        return "sha_local_default"

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path:
        worktree_dir.mkdir(parents=True, exist_ok=True)
        if branch not in self.branches:
            self.branches[branch] = f"sha_{branch}_initial"
        self.worktrees[branch] = worktree_dir
        return worktree_dir

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None:
        to_del = [b for b, wt in self.worktrees.items() if wt == worktree_dir]
        for b in to_del:
            del self.worktrees[b]
        shutil.rmtree(worktree_dir, ignore_errors=True)

    def add_closed_pr(
        self,
        branch: str,
        number: int | None = None,
        url: str | None = None,
        head_sha: str = "sha_closed_1",
    ) -> dict[str, Any]:
        num = number or self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": num,
            "url": url or f"https://github.com/test/repo/pull/{num}",
            "state": "CLOSED",
            "headRefOid": head_sha,
        }
        self.closed_prs[branch] = pr
        return pr

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self.prs.get(branch)

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self.merged_prs.get(branch)

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if branch in self.prs:
            pr = dict(self.prs[branch])
            pr.setdefault("state", "OPEN")
            results.append(pr)
        if branch in self.merged_prs:
            pr = dict(self.merged_prs[branch])
            pr.setdefault("state", "MERGED")
            results.append(pr)
        if branch in self.closed_prs:
            pr = dict(self.closed_prs[branch])
            pr.setdefault("state", "CLOSED")
            results.append(pr)
        return results

    def get_pr_state(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        for pr in self.prs.values():
            if int(pr["number"]) == pr_number:
                out = dict(pr)
                out.setdefault("state", "OPEN")
                return out
        for pr in self.merged_prs.values():
            if int(pr["number"]) == pr_number:
                out = dict(pr)
                out.setdefault("state", "MERGED")
                return out
        for pr in self.closed_prs.values():
            if int(pr["number"]) == pr_number:
                out = dict(pr)
                out.setdefault("state", "CLOSED")
                return out
        return None

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]:
        if pr_number in self.ci_flaky and self.ci_flaky[pr_number]:
            # Flaky check: passes on re-run
            self.ci_flaky[pr_number] = False
            return False, "Failed first attempt (flaky)"
        if pr_number in self.ci_results:
            results = self.ci_results[pr_number]
            if results:
                passed = results.pop(0)
                return passed, "CI passed" if passed else "CI failed"
        return True, "All checks passed"

    def get_commit_ci_checks(
        self,
        repo: str,
        commit_sha: str,
        branch: str = "",
    ) -> list[CiJobCheck]:
        from nightshift.engine.pipeline_evidence import CiArtifact, CiJobCheck, DEFAULT_GATE_JOB_NAMES

        if commit_sha in self.commit_ci_checks:
            return list(self.commit_ci_checks[commit_sha])
        checks: list[CiJobCheck] = []
        for idx, job_name in enumerate(self.default_ci_jobs, start=1):
            is_gate = job_name in DEFAULT_GATE_JOB_NAMES
            build_id = f"fake-build-{commit_sha[:7]}-{idx}"
            arts = [
                CiArtifact(path=p, size_bytes=2048, build_id=build_id)
                for p in self.default_ci_artifacts_by_job.get(job_name, [])
            ]
            checks.append(
                CiJobCheck(
                    name=job_name,
                    status="success",
                    commit_sha=commit_sha,
                    build_id=build_id,
                    duration_seconds=12.0 if is_gate else 45.0,
                    artifacts=arts,
                    compat_mode=False,
                    is_gate_job=is_gate,
                    branch=branch,
                )
            )
        return checks

    def register_job_artifacts_from_dir(
        self,
        build_id: str,
        source_dir: Path,
        *,
        artifact_prefix: str = "ui-screenshots",
    ) -> None:
        """Populate in-memory CI artifacts for ``build_id`` from ``source_dir`` for offline tests."""
        from nightshift.engine.pipeline_evidence import _norm_posix

        prefix = _norm_posix(artifact_prefix).rstrip("/")
        bucket = self.job_artifact_files.setdefault(build_id, {})
        for file_path in source_dir.rglob("*"):
            if not file_path.is_file():
                continue
            rel = _norm_posix(str(file_path.relative_to(source_dir)))
            key = f"{prefix}/{rel}" if prefix else rel
            bucket[key] = file_path.read_bytes()

    def download_job_screenshot_evidence(
        self,
        repo: str,
        producer_job: CiJobCheck,
        manifest_spec: str,
        dest_dir: Path,
    ) -> tuple[Path | None, str]:
        """Download screenshot manifest and referenced PNGs strictly from ``producer_job.build_id``."""
        from nightshift.engine.pipeline_evidence import _artifact_matches, _norm_posix

        if not producer_job.build_id:
            return None, f"Screenshot producer job '{producer_job.name}' has no verified build_id."
        files_for_build = self.job_artifact_files.get(producer_job.build_id)
        if not files_for_build:
            return None, f"No screenshot artifacts uploaded by verified build '{producer_job.build_id}'."

        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_root = dest_dir.resolve()

        spec_norm = _norm_posix(manifest_spec)
        spec_name = Path(spec_norm).name if spec_norm else "screenshot-manifest.json"
        norm_files = {_norm_posix(k): v for k, v in files_for_build.items()}

        manifest_key: str | None = None
        for k in norm_files:
            if not k.lower().endswith(".json"):
                continue
            if _artifact_matches(k, spec_norm):
                manifest_key = k
                break
        if manifest_key is None:
            return (
                None,
                f"No screenshot manifest matching '{manifest_spec}' uploaded by verified build '{producer_job.build_id}'.",
            )

        manifest_bytes = norm_files[manifest_key]
        manifest_filename = manifest_key.rsplit("/", 1)[-1] or "screenshot-manifest.json"
        manifest_prefix = manifest_key.rsplit("/", 1)[0] + "/" if "/" in manifest_key else ""
        local_manifest = dest_root / manifest_filename
        local_manifest.write_bytes(manifest_bytes)

        try:
            parsed = json.loads(manifest_bytes.decode("utf-8"))
        except Exception:
            return local_manifest, ""

        if isinstance(parsed, dict) and isinstance(parsed.get("screenshots"), list):
            for entry in parsed["screenshots"]:
                if not isinstance(entry, dict):
                    continue
                file_rel = str(entry.get("file") or "").strip()
                if not file_rel:
                    continue
                target_png = (dest_root / file_rel).resolve()
                try:
                    target_png.relative_to(dest_root)
                except ValueError:
                    continue
                norm_rel = _norm_posix(file_rel)
                candidate_key = f"{manifest_prefix}{norm_rel}" if manifest_prefix else norm_rel
                png_bytes = norm_files.get(candidate_key)
                if png_bytes is not None:
                    target_png.parent.mkdir(parents=True, exist_ok=True)
                    target_png.write_bytes(png_bytes)

        return local_manifest, ""

    def get_failed_ci_log(self, repo: str, branch: str) -> str:
        return self.failed_ci_logs.get(branch, "Build failed at Task :app:compileKotlin: Type mismatch error")

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
        match_head_commit: str | None = None,
    ) -> tuple[bool, str]:
        for branch, pr in list(self.prs.items()):
            if pr["number"] == pr_number:
                if match_head_commit and str(pr.get("headRefOid") or "") != match_head_commit:
                    return False, f"Head commit SHA mismatch: expected {match_head_commit}, got {pr.get('headRefOid')}"
                pr["state"] = "MERGED"
                pr["mergeCommit"] = {"oid": "sha_merge_123"}
                self.merged_prs[branch] = pr
                del self.prs[branch]
                return True, f"Merged PR #{pr_number} cleanly into main."
        return False, f"PR #{pr_number} not found"

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None:
        if branch.strip().lower() in ("main", "master"):
            raise ValueError(f"Hard guard violation: direct push to protected branch '{branch}' is forbidden.")
        new_sha = f"sha_{branch}_{len(self.checkpoints) + 1}"
        self.branches[branch] = new_sha
        self.checkpoints.append({"branch": branch, "message": message, "sha": new_sha, "worktree": str(worktree_dir)})
        if branch in self.prs:
            self.prs[branch]["headRefOid"] = new_sha
        return new_sha

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]:
        if self.merge_conflict:
            return False, "CONFLICT (content): Merge conflict in src/App.kt"
        return True, "Already up to date."

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]:
        branch = f"nightshift/revert-{commit_sha}"
        self.reverts.append({"commit_sha": commit_sha, "branch": branch})
        pr = self.find_open_pr(repo, branch) or self.add_pr(branch)
        passed, msg = self.watch_pr_checks(repo, pr["number"], local_repo)
        if not passed:
            return False, msg
        return self.merge_pr(repo, pr["number"], local_repo)

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool:
        return self.branch_protection.get(branch, True)

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.diffs.get(branch, "diff --git a/file b/file\n+new content")
        return self.diffs.get("default", "")

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.changed_files.get(branch, ["src/main.ts"])
        return self.changed_files.get("default", [])

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]:
        from nightshift.engine.pipeline_contracts import DiffPathEntry

        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                if branch in self.diff_entries:
                    return list(self.diff_entries[branch])
                files = self.changed_files.get(branch, ["src/main.ts"])
                return [DiffPathEntry(status="M", path=f) for f in files]
        if "default" in self.diff_entries:
            return list(self.diff_entries["default"])
        return [DiffPathEntry(status="M", path=f) for f in self.changed_files.get("default", [])]

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return dict(self.test_file_snapshots.get(branch, {}))
        return dict(self.test_file_snapshots.get("default", {}))

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool, str, int | None]:
        success = self.workflow_runs.get(workflow, True)
        return success, "Run completed" if success else "Workflow run failed", 9999

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        for name in self.artifacts_to_create:
            p = dest_dir / name
            p.write_bytes(b"PK\x03\x04mock_zip_content")
            created.append(p)
        return created

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]:
        rel = {
            "tag": tag,
            "title": title,
            "notes": notes,
            "body": notes,
            "files": [str(f) for f in files],
            "assets": [{"name": Path(f).name} for f in files],
            "prerelease": prerelease,
        }
        self.releases.append(rel)
        return True, f"https://github.com/{repo}/releases/tag/{tag}"

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str:
        num = len(self.releases) + 1
        return f"{tag_prefix}{num}"

    def list_releases(self, repo: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.releases]
