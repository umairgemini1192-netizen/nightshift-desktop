"""Orchestration engine subpackage: planner, scheduler, state machine, verifier, CI, release, logins, deck."""

from nightshift.engine.ci import CiPipelineManager, CiWatchResult, ReviewFinding, parse_review_findings
from nightshift.engine.council import CouncilRoleResult, CouncilSessionResult, PlanningCouncil
from nightshift.engine.deck import generate_deck_data, write_morning_digest
from nightshift.engine.logins import LoginCheckResult, LoginHealthChecker
from nightshift.engine.pipeline_contracts import DiffPathEntry, MergeAuthorization, MergeGate, provider_for_engine
from nightshift.engine.pipeline_orchestrator import (
    ALLOWED_TRANSITIONS,
    NETWORK_BACKOFF_SECONDS,
    TASK_STATES,
    InvalidTransitionError,
    LeaseConflictError,
    LeaseHeartbeat,
    NetworkTransientError,
    ReconcileResult,
    TaskOrchestrator,
    async_run_with_network_backoff,
    is_process_alive,
    is_transient_network_error,
    network_backoff_delay,
    run_with_network_backoff,
)
from nightshift.engine.planner import PhaseDefinition, get_runnable_phases, resolve_base_branch, validate_plan_dag
from nightshift.engine.release import ReleaseManager, compute_sha256
from nightshift.engine.router import Router, RouterDecision, RouterScore, seed_default_engines
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
    "DiffPathEntry",
    "MergeAuthorization",
    "MergeGate",
    "provider_for_engine",
    "ALLOWED_TRANSITIONS",
    "NETWORK_BACKOFF_SECONDS",
    "TASK_STATES",
    "InvalidTransitionError",
    "LeaseConflictError",
    "LeaseHeartbeat",
    "NetworkTransientError",
    "ReconcileResult",
    "TaskOrchestrator",
    "async_run_with_network_backoff",
    "is_process_alive",
    "is_transient_network_error",
    "network_backoff_delay",
    "run_with_network_backoff",
    "PhaseDefinition",
    "get_runnable_phases",
    "resolve_base_branch",
    "validate_plan_dag",
    "PlanningCouncil",
    "CouncilRoleResult",
    "CouncilSessionResult",
    "Router",
    "RouterDecision",
    "RouterScore",
    "seed_default_engines",
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
