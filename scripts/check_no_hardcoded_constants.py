#!/usr/bin/env python3
"""Fail if a financial constant is hardcoded outside the config files.

The rule this enforces: any number that encodes a judgement about companies or
markets -- a threshold, a coefficient, a price level, a sector default -- lives
in config/ where it is visible, auditable and editable.  Only *structural*
numbers may appear in code: array indices, the 2 in an average, the 100 that
converts a fraction to a percent.

Run: python scripts/check_no_hardcoded_constants.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = [ROOT / "src" / "ledger" / "methods", ROOT / "src" / "ledger" / "analyze.py",
        ROOT / "src" / "ledger" / "reconcile.py", ROOT / "src" / "ledger" / "scoring.py"]

#: Structural numbers with no financial meaning.
ALLOWED = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 24, 60, 100, 200, 1000,
           0.0, 1.0, 2.0, 0.5, 1e-6, 1e-9}

#: Keyword-argument names whose defaults are analysis *windows* (how many years
#: of history to use), not financial judgements.
WINDOW_KWARGS = {"years", "lookback", "limit", "min_n", "n", "i", "horizon_years",
                 "max_iter", "period", "quarters", "max_metrics", "annual_years"}


def scan_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    problems: list[str] = []

    window_default_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = [a.arg for a in args.args[-len(args.defaults):]] if args.defaults else []
            for name, d in zip(names, args.defaults):
                if name in WINDOW_KWARGS:
                    window_default_nodes.add(id(d))
            kwnames = [a.arg for a in args.kwonlyargs]
            for name, d in zip(kwnames, args.kw_defaults):
                if d is not None and name in WINDOW_KWARGS:
                    window_default_nodes.add(id(d))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, (int, float)):
            continue
        if isinstance(node.value, bool):
            continue
        if id(node) in window_default_nodes:
            continue
        if node.value in ALLOWED:
            continue
        problems.append(
            f"{path.relative_to(ROOT)}:{node.lineno}: hardcoded numeric "
            f"constant {node.value!r} -- move it to config/ if it encodes a "
            f"financial judgement")
    return problems


def main() -> int:
    problems: list[str] = []
    for target in SCAN:
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for f in files:
            problems.extend(scan_file(f))
    if problems:
        print(f"FAIL: {len(problems)} hardcoded constant(s) found\n")
        for p in problems:
            print("  " + p)
        return 1
    print(f"OK: no hardcoded financial constants in "
          f"{', '.join(str(s.relative_to(ROOT)) for s in SCAN)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
