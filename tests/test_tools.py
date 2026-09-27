"""tools/ guards — tested WITHOUT RUNNING the tools, by parsing their sources.

If a tool is run and there is a `.env` with real keys in the parent folder, it makes a live call.
That is why the test only reads the tool's code.
"""
import ast
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
LIVE_TOOLS = ["diagnose_lv.py", "measure_partial.py", "verify_lv_discrimination.py",
              "measure_planner_reliability.py", "run_live_planner.py"]


def _is_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "require_live_flag")


def _first_real_statement(body: list[ast.stmt]) -> ast.stmt:
    """The first statement, skipping the docstring."""
    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        return node
    raise AssertionError("empty body")


@pytest.mark.parametrize("name", LIVE_TOOLS)
def test_live_tools_refuse_to_run_without_the_live_flag(name):
    tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is not None:
        assert _is_guard(_first_real_statement(main.body)), f"{name}: main() must start with require_live_flag()"
    else:   # script-style tool: the guard comes before the first call (load_env)
        calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        assert calls and _is_guard(calls[0]), f"{name}: require_live_flag() must be the first call"


@pytest.mark.parametrize("path", sorted(TOOLS.glob("*.py")), ids=lambda p: p.name)
def test_tools_do_not_depend_on_the_clone_folder_name(path):
    text = path.read_text(encoding="utf-8")
    assert "parents[2]" not in text and '"heatshield" /' not in text


def test_the_live_probe_has_no_live_default():
    tree = ast.parse((TOOLS / "verify_live.py").read_text(encoding="utf-8"))
    mode = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == "--mode")
    kw = {k.arg: k.value for k in mode.keywords}
    assert "default" not in kw and isinstance(kw.get("required"), ast.Constant) and kw["required"].value is True
