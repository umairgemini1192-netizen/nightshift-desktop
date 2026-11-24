"""CI gate: no raw colours, arbitrary Tailwind values, inline px styles, or emoji in UI source.

Only frontend/src/design/tokens.ts (generated) may contain colour literals.
Exit 1 with file:line findings on violation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
ALLOWED = {SRC / "design" / "tokens.ts", SRC / "design" / "tokens.css"}

RULES = [
    ("raw hex colour", re.compile(r"#[0-9a-fA-F]{3,8}\b")),
    ("raw rgb/hsl colour", re.compile(r"\b(?:rgb|rgba|hsl|hsla)\s*\(")),
    ("arbitrary Tailwind value", re.compile(r"\b[a-z:-]+-\[[^\]]+\]")),
    ("inline px style", re.compile(r"style=\{\{[^}]*\b\d+px")),
]
EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF⭐⬆↔-⇿️]"
)


def main() -> int:
    if not SRC.exists():
        print("frontend/src not found yet - skipping")
        return 0
    findings: list[str] = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".css", ".html"} or path in ALLOWED:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "ui-rules-ignore" in line:
                continue
            for name, rx in RULES:
                if rx.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{no}: {name}: {line.strip()[:120]}")
            if EMOJI.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{no}: emoji: {line.strip()[:120]}")
    for f in findings:
        print(f)
    print(f"{len(findings)} UI rule violation(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
