"""
scripts/check_import_boundaries.py

NEW in v4 (Bible §4 — Module Boundary Redesign, "enforcement, not just
convention"). The audit found `domain/signals/pump_radar.py` and
`domain/signals/alert_engine.py` importing `app_platform.keyboards`
directly — the service/domain layer reaching into the presentation
layer. That was fixed in this v4 pass via dependency injection (see
domain/signals/keyboard_provider.py), but nothing structurally prevented
it from happening again in some *other* file tomorrow.

This script is that structural guarantee: it parses every .py file's
imports and fails (nonzero exit) if any forbidden edge exists. Wired into
CI (.github/workflows/ci.yml) so a PR introducing a new layering
violation fails the build instead of merging silently.

Run directly:
    python scripts/check_import_boundaries.py
"""

from __future__ import annotations

import ast
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# (source layer prefix, forbidden target layer prefix, reason)
FORBIDDEN_EDGES = [
    ("domain.", "app_platform.", "domain/ must not import the presentation layer (app_platform/) — see Bible §4"),
    ("providers.", "app_platform.", "providers/ must not import the presentation layer"),
    ("providers.", "domain.", "providers/ must not import domain/ — domain depends on providers, not the reverse"),
    ("infra.", "domain.", "infra/ must not import domain/ — infra is a leaf layer"),
    ("infra.", "app_platform.", "infra/ must not import the presentation layer"),
    ("models.", "domain.", "models/ must not import domain/ — models is a leaf layer"),
    ("models.", "app_platform.", "models/ must not import the presentation layer"),
]


def _module_name_for(path: str) -> str:
    rel = os.path.relpath(path, REPO_ROOT)
    return rel[:-3].replace(os.sep, ".")


def _collect_imports(tree: ast.AST) -> list[str]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
    return imports


def main() -> int:
    violations = []

    for dirpath, dirs, files in os.walk(REPO_ROOT):
        if any(seg in dirpath for seg in (".git", "__pycache__", "node_modules")):
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            mod_name = _module_name_for(path)

            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=path)
                except SyntaxError as e:
                    violations.append(f"{path}: SYNTAX ERROR — {e}")
                    continue

            for imported in _collect_imports(tree):
                for src_prefix, forbidden_prefix, reason in FORBIDDEN_EDGES:
                    if mod_name.startswith(src_prefix) and imported.startswith(forbidden_prefix):
                        violations.append(f"{path} imports '{imported}' — {reason}")

    if violations:
        print("Import boundary violations found:\n")
        for v in violations:
            print(f"  ✗ {v}")
        print(f"\n{len(violations)} violation(s). See Bible §4 for the intended layering.")
        return 1

    print("No import boundary violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
