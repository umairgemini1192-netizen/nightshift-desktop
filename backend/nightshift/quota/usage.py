"""Usage capture and token parsing from CLI outputs (BLUEPRINT 5a, 5h).

Parses per-run token usage from CLI JSON output where available
(e.g., claude --output-format json, gemini/agy/codex when exposed),
otherwise records an invocation run count to the quota ledger.
Never calls remote quota APIs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nightshift.quota.ledger import QuotaLedger


def parse_cli_usage(stdout_or_stderr: str) -> tuple[int, int]:
    """Extract (tokens_in, tokens_out) from CLI process output if available.

    Returns (0, 0) if no structured usage data is found.
    """
    if not stdout_or_stderr:
        return 0, 0

    text = stdout_or_stderr.strip()

    # 1. Attempt full or line-by-line JSON parse
    for line in reversed(text.splitlines()):
        line_clean = line.strip()
        if (line_clean.startswith("{") and line_clean.endswith("}")) or ("usage" in line_clean and "{" in line_clean):
            try:
                # Find outermost JSON object in the line
                start = line_clean.find("{")
                end = line_clean.rfind("}") + 1
                data = json.loads(line_clean[start:end])
                tokens_in, tokens_out = _extract_tokens_from_dict(data)
                if tokens_in > 0 or tokens_out > 0:
                    return tokens_in, tokens_out
            except Exception:
                continue

    # 2. Try whole text as JSON
    try:
        data = json.loads(text)
        tokens_in, tokens_out = _extract_tokens_from_dict(data)
        if tokens_in > 0 or tokens_out > 0:
            return tokens_in, tokens_out
    except Exception:
        pass

    # 3. Regex search for common token usage patterns
    # e.g. "input_tokens": 1234, "output_tokens": 567
    in_match = re.search(r"""(?:["']?(?:input_tokens|prompt_tokens|tokens_in)["']?\s*[:=]\s*(\d+))""", text, re.IGNORECASE)
    out_match = re.search(r"""(?:["']?(?:output_tokens|completion_tokens|tokens_out)["']?\s*[:=]\s*(\d+))""", text, re.IGNORECASE)

    t_in = int(in_match.group(1)) if in_match else 0
    t_out = int(out_match.group(1)) if out_match else 0

    if t_in > 0 or t_out > 0:
        return t_in, t_out

    return 0, 0


def _extract_tokens_from_dict(data: dict[str, Any]) -> tuple[int, int]:
    """Helper to locate token counts within nested JSON structures."""
    usage = data.get("usage") or data.get("token_usage") or data.get("usage_metadata") or data
    if isinstance(usage, dict):
        t_in = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("prompt_token_count")
            or usage.get("tokens_in")
            or 0
        )
        t_out = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("candidates_token_count")
            or usage.get("tokens_out")
            or 0
        )
        return int(t_in), int(t_out)
    return 0, 0


def record_cli_usage(
    ledger: QuotaLedger,
    runner: str,
    model: str,
    stdout_or_stderr: str,
    default_estimate_tokens: int = 0,
) -> tuple[int, int]:
    """Parse usage from output and record to QuotaLedger. Always records an invocation event."""
    t_in, t_out = parse_cli_usage(stdout_or_stderr)
    if t_in == 0 and t_out == 0 and default_estimate_tokens > 0:
        t_in = int(default_estimate_tokens * 0.7)
        t_out = int(default_estimate_tokens * 0.3)

    ledger.record_usage(
        runner=runner,
        model=model or "default",
        tokens_in=t_in,
        tokens_out=t_out,
        event="run",
    )
    return t_in, t_out
