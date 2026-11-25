"""AI CLI runner adapters (agy, gemini, codex, claude, template, and deterministic fake for tests)."""

from nightshift.runners.agy import AgyRunner
from nightshift.runners.base import Runner, RunnerResult, resolve_bin, run_subprocess_with_timeout
from nightshift.runners.claude import ClaudeRunner
from nightshift.runners.codex import CodexRunner
from nightshift.runners.fake import FakeRunner
from nightshift.runners.gemini import GeminiRunner
from nightshift.runners.template import TemplateRunner

__all__ = [
    "Runner",
    "RunnerResult",
    "resolve_bin",
    "run_subprocess_with_timeout",
    "AgyRunner",
    "GeminiRunner",
    "CodexRunner",
    "ClaudeRunner",
    "TemplateRunner",
    "FakeRunner",
]
