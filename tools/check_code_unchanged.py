"""Prove that an edit touched only comments and docstrings.

Compares each Python file's syntax tree against a git revision with docstrings removed. Comments
are not part of the tree, so a pure comment/docstring translation leaves it identical; any change to
code, string literals or identifiers shows up as a mismatch.

    python tools/check_code_unchanged.py [REV] [PATH ...]      (default REV: HEAD, PATH: every tracked .py)

Makes no network calls.
"""
from __future__ import annotations

import ast
import subprocess
import sys


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _fingerprint(source: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(source)), include_attributes=False)


def main(argv: list[str]) -> int:
    rev = argv[0] if argv and not argv[0].endswith(".py") else "HEAD"
    paths = [a for a in argv if a.endswith(".py")] or subprocess.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout.split()
    changed = []
    for path in paths:
        try:
            before = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, check=True).stdout.decode("utf-8")
        except subprocess.CalledProcessError:
            continue                                   # new file: nothing to compare
        with open(path, encoding="utf-8") as f:
            after = f.read()
        if _fingerprint(before) != _fingerprint(after):
            changed.append(path)
    for path in changed:
        print(f"CODE CHANGED: {path}")
    print(f"{len(paths) - len(changed)}/{len(paths)} files: code identical to {rev} (comments/docstrings ignored)")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
