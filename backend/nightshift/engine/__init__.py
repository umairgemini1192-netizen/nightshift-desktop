"""Orchestration engine subpackage: planner, scheduler, state machine, verifier, CI, release, logins, deck."""

from nightshift.engine.ci import CiPipelineManager, CiWatchResult, ReviewFinding, parse_review_findings
from nightshift.engine.deck import generate_deck_data, write_morning_digest
from nightshift.engine.logins import LoginCheckResult, LoginHealthChecker
from nightshift.engine.planner import PhaseDefinition, get_runnable_phases, resolve_base_branch, validate_plan_dag
from nightshift.engine.release import ReleaseManager, compute_sha256
from nightshift.engine.scheduler import CircuitBreaker, EngineChoice, EtaSnapshot, SameErrorDetector, Scheduler
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig, SingleInstanceLock, set_keep_awake
from nightshift.engine.vcs import FakeGitGh, GitGhClient, SubprocessGitGh
from nightshift.engine.verify import (
    CRITICAL_PATH_SIGNALS,
    GuardFinding,
    RiskAssessment,
    assess_diff_risk,
    check_diff_guards,
    redact_secrets,
    verify_deliverable,
)

__all__ = [
    "CiPipelineManager",
    "CiWatchResult",
    "ReviewFinding",
    "parse_review_findings",
    "generate_deck_data",
    "write_morning_digest",
    "LoginCheckResult",
    "LoginHealthChecker",
    "PhaseDefinition",
    "get_runnable_phases",
    "resolve_base_branch",
    "validate_plan_dag",
    "ReleaseManager",
    "compute_sha256",
    "CircuitBreaker",
    "EngineChoice",
    "EtaSnapshot",
    "SameErrorDetector",
    "Scheduler",
    "EngineStateMachine",
    "ProjectConfig",
    "SingleInstanceLock",
    "set_keep_awake",
    "GitGhClient",
    "SubprocessGitGh",
    "FakeGitGh",
    "GuardFinding",
    "RiskAssessment",
    "assess_diff_risk",
    "check_diff_guards",
    "redact_secrets",
    "verify_deliverable",
    "CRITICAL_PATH_SIGNALS",
]
