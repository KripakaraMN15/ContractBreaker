"""Narrow executable-source contract for runtime verification.

The contract deliberately supports a small module-level ``requests`` client
script. It is a safety and determinism boundary, not a general Python sandbox.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from .extraction import ExtractionResult, SourceLocation

_ALLOWED_IMPORT_ROOTS = {
    "base64",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "requests",
    "secrets",
    "time",
    "typing",
    "uuid",
}
_FORBIDDEN_IMPORT_PARTS = {"adapters", "packages", "urllib3"}
_FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "vars",
}
_NESTED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


@dataclass(frozen=True)
class RuntimeContract:
    """Whether one source file can be executed by the runtime worker."""

    supported: bool
    blockers: tuple[str, ...]
    entrypoint: str = "module"


def check_runtime_contract(
    source: str, extraction: ExtractionResult
) -> RuntimeContract:
    """Validate the supported module-level requests client contract."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return RuntimeContract(
            False, (f"source could not be parsed for execution: {exc.msg}",)
        )

    blockers: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                blocker = _import_blocker(alias.name)
                if blocker is not None:
                    blockers.append(blocker)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                blockers.append("relative imports are not supported for execution")
            module = node.module or ""
            blocker = _import_blocker(module)
            if blocker is not None:
                blockers.append(blocker)
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_PARTS:
                    blockers.append(
                        f"importing {module}.{alias.name} is not supported for execution"
                    )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                blockers.append(
                    f"call to {node.func.id} is not supported for execution"
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr == "mount":
                blockers.append(
                    "session.mount is not supported for execution because the "
                    "harness controls the transport"
                )

    if not extraction.calls:
        blockers.append("runtime execution requires one recognized requests call")
    elif len(extraction.calls) != 1:
        blockers.append(
            "runtime execution supports exactly one recognized requests call site"
        )
    else:
        call = _find_call(tree, extraction.calls[0].location)
        if call is None:
            blockers.append("recognized requests call could not be located in the AST")
        elif _is_nested(call, tree):
            blockers.append(
                "runtime execution supports module-level client scripts only; "
                "the recognized call is inside a function, class, or lambda"
            )

    unique = tuple(dict.fromkeys(blockers))
    return RuntimeContract(not unique, unique)


def _import_blocker(module: str) -> str | None:
    root = module.split(".")[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        return f"import of {module or '<relative>'} is not supported for execution"
    parts = set(module.split("."))
    if parts & _FORBIDDEN_IMPORT_PARTS:
        return f"import of {module} is not supported for execution"
    return None


def _find_call(tree: ast.AST, location: SourceLocation) -> ast.Call | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and node.lineno == location.line
            and node.col_offset + 1 == location.column
        ):
            return node
    return None


def _is_nested(call: ast.Call, tree: ast.AST) -> bool:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    current = parents.get(id(call))
    while current is not None:
        if isinstance(current, _NESTED_SCOPES):
            return True
        current = parents.get(id(current))
    return False