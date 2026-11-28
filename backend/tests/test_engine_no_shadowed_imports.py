"""Regression: a function-local re-import of a module-level name makes it local to the whole
function, so earlier uses raise UnboundLocalError.

Seen live on 2026-11-28: with the repomix token tool enabled, every phase crashed at
``tm = ToolManager(store=self.store)`` in ``_execute_phase_leased`` because a later line in the
same function did ``from nightshift.optimizer.tool_manager import ToolManager``.
"""
from __future__ import annotations

import ast
import inspect
import types

import pytest

from nightshift.engine import state_machine


def _module_imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _iter_code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _iter_code_objects(const)


def test_state_machine_functions_never_shadow_module_imports() -> None:
    source = inspect.getsource(state_machine)
    imported = _module_imported_names(ast.parse(source))
    module_code = compile(source, state_machine.__file__, "exec")
    offenders = []
    for code in _iter_code_objects(module_code):
        if code is module_code:
            continue
        shadowed = imported.intersection(code.co_varnames)
        if shadowed:
            offenders.append(f"{code.co_name}: {sorted(shadowed)}")
    assert offenders == [], (
        "Module-level imports re-bound locally (causes UnboundLocalError on earlier uses): "
        + "; ".join(offenders)
    )


def test_execute_phase_leased_uses_module_level_tool_manager() -> None:
    code = state_machine.EngineStateMachine._execute_phase_leased.__code__
    assert "ToolManager" not in code.co_varnames
    assert "ToolManager" in code.co_names
