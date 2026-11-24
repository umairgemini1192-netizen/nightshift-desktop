"""CI gate: design/tokens.json is valid, all {references} resolve, both themes define the same
semantic keys, and key text/background pairs meet WCAG AA (4.5:1) in both themes."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
REF = re.compile(r"^\{([^}]+)\}$")


def lookup(path: str):
    node = TOKENS
    for part in path.split("."):
        if part not in node:
            # semantic keys contain dots (e.g. "text.primary") - try joining the rest
            raise KeyError(path)
        node = node[part]
    return node["$value"] if isinstance(node, dict) and "$value" in node else node


def resolve(value):
    m = REF.match(value) if isinstance(value, str) else None
    return resolve(lookup(m.group(1))) if m else value


def rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    raise ValueError(value)


def lum(c):
    def ch(x):
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((lum(rgb(a)), lum(rgb(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main() -> int:
    errors: list[str] = []
    themes = TOKENS["semantic"]
    keys = {name: set(t) for name, t in themes.items()}
    if keys["dark"] != keys["light"]:
        errors.append(f"theme key mismatch: {keys['dark'] ^ keys['light']}")
    resolved = {}
    for name, theme in themes.items():
        resolved[name] = {}
        for k, v in theme.items():
            try:
                resolved[name][k] = resolve(v["$value"])
            except KeyError as e:
                errors.append(f"{name}.{k}: unresolved reference {e}")
    pairs = [("text.primary", "bg.canvas"), ("text.secondary", "bg.canvas"), ("text.muted", "bg.canvas"),
             ("accent.primary", "bg.canvas"), ("status.success", "bg.canvas"), ("status.warning", "bg.canvas"),
             ("status.danger", "bg.canvas")]
    for name, t in resolved.items():
        for fg, bg in pairs:
            try:
                ratio = contrast(t[fg], t[bg])
            except (KeyError, ValueError):
                continue
            if ratio < 4.5:
                errors.append(f"{name}: {fg} on {bg} contrast {ratio:.2f} < 4.5")
    for e in errors:
        print(e)
    print("tokens OK" if not errors else f"{len(errors)} token error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
