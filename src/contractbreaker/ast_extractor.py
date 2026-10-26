"""Narrow deterministic extraction of requests retry/timeout behavior.

Supported syntax is intentionally limited to patterns that can be recognized
from the AST without guessing:

* ``requests`` module calls and tracked ``requests.Session`` variables.
* Literal ``timeout`` values.
* ``for ... in range(...)`` retry loops with resolvable integer bounds.
* ``try``/``except`` handlers that continue a retry loop.
* Literal or simply assigned HTTP headers, including ``Idempotency-Key``.

Everything else is reported as absent, unknown, ambiguous, or unsupported.
No default value is ever invented for a missing fact.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .extraction import (
    ExtractionResult,
    ExtractionStatus,
    ExtractionWarning,
    Fact,
    FactStatus,
    HeaderFact,
    HttpCallFact,
    IdempotencyKeyFact,
    RetryLoopFact,
    SourceLocation,
)

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_METHOD_WITH_REQUEST = _METHODS | {"request"}
_EXCEPTION_CANONICAL = {
    "Timeout": "requests.exceptions.Timeout",
    "ReadTimeout": "requests.exceptions.ReadTimeout",
    "ConnectTimeout": "requests.exceptions.ConnectTimeout",
    "ConnectionError": "requests.exceptions.ConnectionError",
    "RequestException": "requests.exceptions.RequestException",
}
_BROAD_EXCEPTIONS = {
    "Exception",
    "BaseException",
    "requests.exceptions.RequestException",
}
_UNSUPPORTED_IMPORT_ROOTS = {
    "tenacity": ("unsupported.retry_library", "tenacity retry policies are not supported"),
    "backoff": ("unsupported.retry_library", "backoff retry policies are not supported"),
    "urllib3": ("unsupported.retry_policy", "urllib3 Retry policies are not supported"),
    "httpx": ("unsupported.http_client", "httpx is not a supported HTTP client"),
    "aiohttp": ("unsupported.http_client", "aiohttp is not a supported HTTP client"),
}
_RETRY_HINTS = ("retry", "retries", "attempt", "tries", "backoff", "max_")
_HTTP_CALL_OPTIONS = frozenset(
    {"url", "headers", "timeout", "json", "params", "data", "auth", "cookies"}
)
_FRESH_MODULE_FUNCTIONS = {
    "uuid": ("uuid4", "uuid1"),
    "secrets": ("token_hex", "token_urlsafe", "token_bytes"),
    "os": ("urandom",),
}


@dataclass
class _Aliases:
    requests_modules: set[str] = field(default_factory=lambda: {"requests"})
    api_modules: set[str] = field(default_factory=set)
    sessions_modules: set[str] = field(default_factory=set)
    exception_modules: set[str] = field(default_factory=set)
    session_constructors: set[str] = field(
        default_factory=lambda: {
            "requests.Session",
            "requests.sessions.Session",
            "requests.session",
        }
    )
    http_functions: dict[str, str] = field(default_factory=dict)
    exception_map: dict[str, str] = field(default_factory=dict)
    fresh_modules: dict[str, str] = field(default_factory=dict)
    fresh_call_names: set[str] = field(default_factory=set)
    unsupported_modules: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _RawCall:
    node: ast.Call
    method: str | None
    method_expression: str | None
    request_style: bool


@dataclass(frozen=True)
class _ParsedHeader:
    name: str
    value_node: ast.AST
    location: SourceLocation


def extract_from_file(path: str | Path) -> ExtractionResult:
    """Read a UTF-8 Python file and extract supported retry facts."""

    file_path = Path(path)
    return extract_from_source(file_path.read_text(encoding="utf-8"), filename=str(file_path))


def extract_from_source(source: str, *, filename: str = "<string>") -> ExtractionResult:
    """Extract supported requests retry/timeout facts from Python source."""

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        line = exc.lineno or 1
        column = exc.offset or 1
        location = SourceLocation(filename, line, column, line, column)
        warning = ExtractionWarning(
            "parse_error",
            f"source could not be parsed: {exc.msg}",
            location,
        )
        return ExtractionResult(
            file=filename,
            status=ExtractionStatus.PARSE_ERROR,
            calls=(),
            retry_loops=(),
            warnings=(warning,),
        )

    parents = _parent_map(tree)
    index = _NameIndex(tree, parents)
    aliases, warnings = _collect_aliases(tree, filename)
    session_names = _collect_session_names(tree, aliases)
    session_header_mutations = _collect_session_header_mutations(tree)

    raw_calls: list[_RawCall] = []
    call_by_id: dict[int, _RawCall] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        recognized = _recognize_http_call(node, aliases, session_names)
        if recognized is None:
            continue
        raw = _RawCall(node=node, **recognized)
        raw_calls.append(raw)
        call_by_id[id(node)] = raw

    warnings.extend(
        _unsupported_warnings(tree, aliases, call_by_id, session_names, filename)
    )

    retry_loops, loop_nodes = _build_retry_loops(
        tree, call_by_id, aliases, index, filename
    )

    calls = tuple(
        _build_call_fact(
            raw,
            index,
            aliases,
            filename,
            _nearest_retry_loop(raw.node, loop_nodes, parents),
            session_header_mutations,
        )
        for raw in raw_calls
    )

    deduped_warnings = _dedupe_warnings(warnings)
    status = _overall_status(calls, retry_loops, deduped_warnings)
    return ExtractionResult(
        file=filename,
        status=status,
        calls=calls,
        retry_loops=retry_loops,
        warnings=deduped_warnings,
    )


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _scope_id(node: ast.AST, parents: dict[int, ast.AST]) -> int | None:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            return id(current)
        current = parents.get(id(current))
    return None


def _visible_scope_ids(node: ast.AST, parents: dict[int, ast.AST]) -> list[int | None]:
    scopes: list[int | None] = []
    current = parents.get(id(node))
    while current is not None:
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            scopes.append(id(current))
        current = parents.get(id(current))
    scopes.append(None)
    return scopes


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _before(node: ast.AST, use: ast.AST) -> bool:
    return _position(node) < _position(use)


class _NameIndex:
    """Simple, deterministic assignments visible before a use site."""

    def __init__(self, tree: ast.AST, parents: dict[int, ast.AST]) -> None:
        self._parents = parents
        self._assignments: dict[tuple[int | None, str], list[ast.AST]] = defaultdict(list)
        self._header_operations: dict[tuple[int | None, str], list[ast.AST]] = (
            defaultdict(list)
        )
        for node in ast.walk(tree):
            scope = _scope_id(node, parents)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in _assignment_targets(node):
                    text = _qualified_name(target)
                    if text is not None:
                        self._assignments[(scope, text)].append(node)
                    if isinstance(target, ast.Name):
                        self._header_operations[(scope, target.id)].append(node)
                    elif isinstance(target, ast.Subscript) and isinstance(
                        target.value, ast.Name
                    ):
                        self._header_operations[(scope, target.value.id)].append(node)
            elif (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "update"
                and isinstance(node.value.func.value, ast.Name)
            ):
                self._header_operations[
                    (scope, node.value.func.value.id)
                ].append(node)

        for records in self._assignments.values():
            records.sort(key=_position)
        for records in self._header_operations.values():
            records.sort(key=_position)

    def latest_assignment(self, expression: ast.AST, use: ast.AST) -> ast.AST | None:
        text = _qualified_name(expression)
        if text is None:
            return None
        for scope in _visible_scope_ids(use, self._parents):
            candidates = [
                node
                for node in self._assignments.get((scope, text), [])
                if _before(node, use)
            ]
            if candidates:
                return candidates[-1]
        return None

    def header_operations(self, name: str, use: ast.AST) -> list[ast.AST]:
        records: list[ast.AST] = []
        for scope in _visible_scope_ids(use, self._parents):
            records.extend(
                node
                for node in self._header_operations.get((scope, name), [])
                if _before(node, use)
            )
        records.sort(key=_position)
        return records


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _qualified_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _location(file: str, node: ast.AST) -> SourceLocation:
    line = getattr(node, "lineno", 1)
    column = getattr(node, "col_offset", 0) + 1
    end_line = getattr(node, "end_lineno", None) or line
    end_column = getattr(node, "end_col_offset", None)
    if end_column is None:
        end_column = column
    else:
        end_column += 1
    return SourceLocation(file, line, column, end_line, end_column)


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unprintable>"


def _collect_aliases(
    tree: ast.AST, filename: str
) -> tuple[_Aliases, list[ExtractionWarning]]:
    aliases = _Aliases()
    warnings: list[ExtractionWarning] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record_import(aliases, warnings, alias, node, filename)
        elif isinstance(node, ast.ImportFrom):
            _record_import_from(aliases, warnings, node, filename)

    for module in aliases.requests_modules:
        for method in _METHOD_WITH_REQUEST:
            aliases.http_functions[f"{module}.{method}"] = method
        for short, canonical in _EXCEPTION_CANONICAL.items():
            aliases.exception_map[f"{module}.exceptions.{short}"] = canonical
            aliases.exception_map[f"{module}.{short}"] = canonical
        aliases.session_constructors.add(f"{module}.Session")
        aliases.session_constructors.add(f"{module}.session")
    for module in aliases.api_modules:
        for method in _METHOD_WITH_REQUEST:
            aliases.http_functions[f"{module}.{method}"] = method
    for module in aliases.exception_modules:
        for short, canonical in _EXCEPTION_CANONICAL.items():
            aliases.exception_map[f"{module}.{short}"] = canonical
    for module in aliases.sessions_modules:
        aliases.session_constructors.add(f"{module}.Session")
    for module_name, names in _FRESH_MODULE_FUNCTIONS.items():
        for name in names:
            aliases.fresh_call_names.add(f"{module_name}.{name}")
    for module_alias, canonical in aliases.fresh_modules.items():
        for name in _FRESH_MODULE_FUNCTIONS.get(canonical, ()):
            aliases.fresh_call_names.add(f"{module_alias}.{name}")

    return aliases, warnings


def _record_import(
    aliases: _Aliases,
    warnings: list[ExtractionWarning],
    alias: ast.alias,
    node: ast.Import,
    filename: str,
) -> None:
    name = alias.name
    local = alias.asname or name.split(".")[0]
    if name == "requests":
        aliases.requests_modules.add(local)
    elif name == "requests.sessions":
        aliases.sessions_modules.add(local)
    elif name == "requests.exceptions":
        aliases.exception_modules.add(local)
    elif name == "requests.api":
        aliases.api_modules.add(local)
    elif name in _FRESH_MODULE_FUNCTIONS:
        aliases.fresh_modules[local] = name
    else:
        root = name.split(".")[0]
        unsupported = _UNSUPPORTED_IMPORT_ROOTS.get(root)
        if unsupported is not None:
            code, message = unsupported
            aliases.unsupported_modules[local] = code
            warnings.append(ExtractionWarning(code, message, _location(filename, node)))


def _record_import_from(
    aliases: _Aliases,
    warnings: list[ExtractionWarning],
    node: ast.ImportFrom,
    filename: str,
) -> None:
    module = node.module or ""
    root = module.split(".")[0]
    unsupported = _UNSUPPORTED_IMPORT_ROOTS.get(root)
    if unsupported is not None:
        code, message = unsupported
        aliases.unsupported_modules[root] = code
        warnings.append(ExtractionWarning(code, message, _location(filename, node)))

    for alias in node.names:
        local = alias.asname or alias.name
        if module == "requests":
            if alias.name in _METHOD_WITH_REQUEST:
                aliases.http_functions[local] = alias.name
            elif alias.name == "Session":
                aliases.session_constructors.add(local)
            elif alias.name == "session":
                aliases.session_constructors.add(local)
            elif alias.name == "exceptions":
                aliases.exception_modules.add(local)
            elif alias.name == "sessions":
                aliases.sessions_modules.add(local)
            elif alias.name == "api":
                aliases.api_modules.add(local)
            elif alias.name in _EXCEPTION_CANONICAL:
                aliases.exception_map[local] = _EXCEPTION_CANONICAL[alias.name]
        elif module == "requests.exceptions" and alias.name in _EXCEPTION_CANONICAL:
            aliases.exception_map[local] = _EXCEPTION_CANONICAL[alias.name]
        elif module == "requests.api" and alias.name in _METHOD_WITH_REQUEST:
            aliases.http_functions[local] = alias.name
        elif module == "requests.sessions" and alias.name == "Session":
            aliases.session_constructors.add(local)
        elif module in _FRESH_MODULE_FUNCTIONS and alias.name in _FRESH_MODULE_FUNCTIONS[module]:
            aliases.fresh_call_names.add(local)


def _collect_session_header_mutations(tree: ast.AST) -> set[str]:
    """Track session-level header writes that could hide an idempotency key."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in _assignment_targets(node):
                if isinstance(target, ast.Subscript):
                    owner = _header_owner(target.value)
                    if owner is not None:
                        names.add(owner)
                elif isinstance(target, ast.Attribute) and target.attr == "headers":
                    owner = _qualified_name(target.value)
                    if owner is not None:
                        names.add(owner)
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "update"
        ):
            owner = _header_owner(node.value.func.value)
            if owner is not None:
                names.add(owner)
    return names


def _header_owner(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr == "headers":
        return _qualified_name(node.value)
    return None


def _collect_session_names(tree: ast.AST, aliases: _Aliases) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and _is_session_constructor(value, aliases):
                for target in _assignment_targets(node):
                    text = _qualified_name(target)
                    if text is not None:
                        names.add(text)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and _is_session_constructor(
                    item.context_expr, aliases
                ):
                    text = _qualified_name(item.optional_vars)
                    if text is not None:
                        names.add(text)
    return names


def _is_session_constructor(node: ast.AST, aliases: _Aliases) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _qualified_name(node.func)
    return name in aliases.session_constructors


def _recognize_http_call(
    node: ast.Call, aliases: _Aliases, session_names: set[str]
) -> dict[str, object] | None:
    func = node.func
    if isinstance(func, ast.Name):
        canonical = aliases.http_functions.get(func.id)
        if canonical is None:
            return None
        if canonical in _METHODS:
            return {
                "method": canonical.upper(),
                "method_expression": None,
                "request_style": False,
            }
        if canonical == "request":
            method, expression = _request_method(node)
            return {
                "method": method,
                "method_expression": expression,
                "request_style": True,
            }
        return None

    if not isinstance(func, ast.Attribute):
        return None
    canonical = aliases.http_functions.get(_qualified_name(func) or "")
    if canonical in _METHODS:
        return {
            "method": canonical.upper(),
            "method_expression": None,
            "request_style": False,
        }
    if canonical == "request":
        method, expression = _request_method(node)
        return {
            "method": method,
            "method_expression": expression,
            "request_style": True,
        }

    base = _qualified_name(func.value)
    if base in session_names and func.attr in _METHODS:
        return {
            "method": func.attr.upper(),
            "method_expression": None,
            "request_style": False,
        }
    if base in session_names and func.attr == "request":
        method, expression = _request_method(node)
        return {
            "method": method,
            "method_expression": expression,
            "request_style": True,
        }
    return None


def _request_method(node: ast.Call) -> tuple[str | None, str | None]:
    expression: ast.AST | None = None
    for keyword in node.keywords:
        if keyword.arg == "method":
            expression = keyword.value
            break
    if expression is None and node.args:
        expression = node.args[0]
    if expression is None:
        return None, None
    literal = _literal_string(expression)
    if literal is not None:
        return literal.upper(), _unparse(expression)
    return None, _unparse(expression)


def _build_call_fact(
    raw: _RawCall,
    index: _NameIndex,
    aliases: _Aliases,
    filename: str,
    retry_loop: ast.AST | None,
    session_header_mutations: set[str],
) -> HttpCallFact:
    node = raw.node
    star_kwargs = any(keyword.arg is None for keyword in node.keywords)
    endpoint_node = _call_endpoint(node, raw.request_style)
    if endpoint_node is None:
        endpoint = Fact.unknown("call has no statically identifiable URL argument")
    else:
        literal = _literal_string(endpoint_node)
        endpoint = (
            Fact.known(literal)
            if literal is not None
            else Fact.unknown(
                f"endpoint expression is not a string literal: {_unparse(endpoint_node)}"
            )
        )

    method = (
        Fact.known(raw.method)
        if raw.method is not None
        else Fact.unknown(
            "HTTP method is not a string literal"
            + (f": {raw.method_expression}" if raw.method_expression else "")
        )
    )

    timeout = _extract_timeout(node, star_kwargs)
    receiver = (
        _qualified_name(node.func.value)
        if isinstance(node.func, ast.Attribute)
        else None
    )
    parsed_headers, headers_status, headers_reason = _resolve_headers(
        node,
        index,
        filename,
        star_kwargs,
        receiver in session_header_mutations,
    )
    headers = tuple(
        HeaderFact(
            name=Fact.known(header.name),
            value=_header_value_fact(header.value_node),
            location=header.location,
        )
        for header in parsed_headers
    )
    idempotency = _extract_idempotency(
        parsed_headers,
        headers_status,
        headers_reason,
        node,
        index,
        aliases,
        filename,
        retry_loop,
    )

    notes: list[str] = []
    if retry_loop is not None:
        notes.append(
            "Retry association uses the nearest enclosing recognized range loop."
        )
    return HttpCallFact(
        location=_location(filename, node),
        method=method,
        endpoint=endpoint,
        timeout=timeout,
        headers=headers,
        idempotency_key=idempotency,
        retry_loop=_location(filename, retry_loop) if retry_loop is not None else None,
        notes=tuple(notes),
    )


def _call_endpoint(node: ast.Call, request_style: bool) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == "url":
            return keyword.value
    index = 1 if request_style else 0
    if len(node.args) > index:
        return node.args[index]
    return None


def _extract_timeout(
    node: ast.Call, star_kwargs: bool
) -> Fact[int | float | None]:
    timeout_keyword = next(
        (keyword for keyword in node.keywords if keyword.arg == "timeout"), None
    )
    if timeout_keyword is None:
        if star_kwargs:
            return Fact.unknown("timeout may be supplied through **kwargs")
        return Fact.absent("call has no timeout argument")

    value = timeout_keyword.value
    literal = _literal_value(value)
    if isinstance(literal, bool):
        return Fact.unsupported("boolean timeout values are not supported")
    if isinstance(literal, (int, float)):
        return Fact.known(literal)
    if literal is None and isinstance(value, ast.Constant):
        return Fact.known(None, "timeout is explicitly None")
    if isinstance(literal, (tuple, list)):
        return Fact.unsupported("tuple/list timeouts are not modeled")
    return Fact.unknown(f"timeout is not a literal value: {_unparse(value)}")


def _header_value_fact(node: ast.AST) -> Fact[str]:
    literal = _literal_string(node)
    if literal is not None:
        return Fact.known(literal)
    return Fact.unknown(f"header value is not a string literal: {_unparse(node)}")


def _resolve_headers(
    node: ast.Call,
    index: _NameIndex,
    filename: str,
    star_kwargs: bool,
    session_headers_uncertain: bool,
) -> tuple[list[_ParsedHeader], FactStatus, str | None]:
    headers_keyword = next(
        (keyword for keyword in node.keywords if keyword.arg == "headers"), None
    )
    if headers_keyword is None:
        if star_kwargs:
            return [], FactStatus.UNKNOWN, "headers may be supplied through **kwargs"
        if session_headers_uncertain:
            return (
                [],
                FactStatus.UNKNOWN,
                "session-level headers may supply an Idempotency-Key",
            )
        return [], FactStatus.KNOWN, None

    value = headers_keyword.value
    if isinstance(value, ast.Dict):
        parsed, complete = _parse_header_dict(value, filename)
        if complete:
            return _with_session_header_uncertainty(
                parsed, FactStatus.KNOWN, None, session_headers_uncertain
            )
        return (
            parsed,
            FactStatus.UNKNOWN,
            "headers contain dynamic or ** entries",
        )

    if isinstance(value, ast.Name):
        variable_headers, variable_status, variable_reason = _resolve_header_variable(
            value.id, node, index, filename
        )
        return _with_session_header_uncertainty(
            variable_headers,
            variable_status,
            variable_reason,
            session_headers_uncertain,
        )

    return (
        [],
        FactStatus.UNKNOWN,
        f"headers expression cannot be resolved statically: {_unparse(value)}",
    )


def _with_session_header_uncertainty(
    headers: list[_ParsedHeader],
    status: FactStatus,
    reason: str | None,
    session_headers_uncertain: bool,
) -> tuple[list[_ParsedHeader], FactStatus, str | None]:
    has_key = any(header.name.lower() == "idempotency-key" for header in headers)
    if session_headers_uncertain and not has_key:
        return (
            headers,
            FactStatus.UNKNOWN,
            reason or "session-level headers may supply an Idempotency-Key",
        )
    return headers, status, reason


def _resolve_header_variable(
    name: str, use: ast.AST, index: _NameIndex, filename: str
) -> tuple[list[_ParsedHeader], FactStatus, str | None]:
    operations = index.header_operations(name, use)
    if not operations:
        return (
            [],
            FactStatus.UNKNOWN,
            f"headers variable {name} has no visible assignment",
        )

    parsed: list[_ParsedHeader] = []
    complete = False
    reason: str | None = None
    for operation in operations:
        if isinstance(operation, (ast.Assign, ast.AnnAssign)):
            targets = _assignment_targets(operation)
            direct = next(
                (
                    target
                    for target in targets
                    if isinstance(target, ast.Name) and target.id == name
                ),
                None,
            )
            subscript = next(
                (
                    target
                    for target in targets
                    if isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                ),
                None,
            )
            if direct is not None:
                value = operation.value
                if isinstance(value, ast.Dict):
                    parsed, complete = _parse_header_dict(value, filename)
                    reason = (
                        None if complete else "headers contain dynamic or ** entries"
                    )
                else:
                    parsed = []
                    complete = False
                    reason = (
                        "headers assignment is not a dict literal: "
                        f"{_unparse(value) if value is not None else '<missing>'}"
                    )
                continue
            if subscript is not None and operation.value is not None:
                key = subscript.slice
                header_name = _literal_string(key)
                if header_name is None:
                    complete = False
                    reason = "a header key is not a string literal"
                    continue
                if complete:
                    _set_header(
                        parsed,
                        _ParsedHeader(
                            header_name,
                            operation.value,
                            _location(filename, key),
                        ),
                    )
                continue

        if (
            isinstance(operation, ast.Expr)
            and isinstance(operation.value, ast.Call)
            and isinstance(operation.value.func, ast.Attribute)
        ):
            call = operation.value
            if len(call.args) == 1 and isinstance(call.args[0], ast.Dict):
                updates, updates_complete = _parse_header_dict(
                    call.args[0], filename
                )
                if complete:
                    for update in updates:
                        _set_header(parsed, update)
                complete = complete and updates_complete
                if not updates_complete:
                    reason = "headers.update() contains dynamic or ** entries"
            else:
                complete = False
                reason = "headers.update() argument is not a dict literal"

    return parsed, FactStatus.KNOWN if complete else FactStatus.UNKNOWN, reason


def _set_header(headers: list[_ParsedHeader], header: _ParsedHeader) -> None:
    for index, existing in enumerate(headers):
        if existing.name.lower() == header.name.lower():
            headers[index] = header
            return
    headers.append(header)


def _parse_header_dict(
    node: ast.Dict, filename: str
) -> tuple[list[_ParsedHeader], bool]:
    parsed: list[_ParsedHeader] = []
    complete = True
    for key, value in zip(node.keys, node.values):
        if key is None:
            complete = False
            continue
        name = _literal_string(key)
        if name is None:
            complete = False
            continue
        _set_header(
            parsed,
            _ParsedHeader(name, value, _location(filename, key)),
        )
    return parsed, complete


def _extract_idempotency(
    headers: list[_ParsedHeader],
    headers_status: FactStatus,
    headers_reason: str | None,
    call: ast.Call,
    index: _NameIndex,
    aliases: _Aliases,
    filename: str,
    retry_loop: ast.AST | None,
) -> Fact[IdempotencyKeyFact]:
    key_header = next(
        (header for header in headers if header.name.lower() == "idempotency-key"),
        None,
    )
    if key_header is None:
        if headers_status is FactStatus.KNOWN:
            return Fact.absent("no Idempotency-Key header found")
        return Fact.unknown(
            headers_reason or "headers could not be resolved, so key presence is unknown"
        )

    origin, assignment = _resolve_origin(key_header.value_node, index)
    key_expression = _unparse(key_header.value_node)
    generated = _generated_inside_retry(origin, retry_loop, assignment)
    reused = _reuse_fact(origin, retry_loop, generated, aliases)
    info = IdempotencyKeyFact(
        header_name=key_header.name,
        key_expression=key_expression,
        generated_inside_retry=generated,
        reused_across_attempts=reused,
        location=key_header.location,
    )
    return Fact.known(info)


def _resolve_origin(
    node: ast.AST, index: _NameIndex
) -> tuple[ast.AST, ast.AST | None]:
    current = node
    assignment: ast.AST | None = None
    visited: set[int] = set()
    for _ in range(8):
        if id(current) in visited:
            break
        visited.add(id(current))
        if not isinstance(current, (ast.Name, ast.Attribute)):
            break
        found = index.latest_assignment(current, node)
        if found is None or found.value is None:
            break
        assignment = found
        current = found.value
    return current, assignment


def _generated_inside_retry(
    origin: ast.AST, retry_loop: ast.AST | None, assignment: ast.AST | None
) -> Fact[bool]:
    if retry_loop is None:
        return Fact.absent("call is not inside a recognized retry loop")
    if assignment is None and isinstance(
        origin, (ast.Name, ast.Attribute, ast.Subscript)
    ):
        return Fact.known(
            False, "key value is an external name or parameter evaluated outside the loop"
        )
    inside = _node_within(origin, retry_loop)
    return Fact.known(
        inside,
        "key expression is evaluated inside the retry loop"
        if inside
        else "key expression is evaluated outside the retry loop",
    )


def _reuse_fact(
    origin: ast.AST,
    retry_loop: ast.AST | None,
    generated_inside: Fact[bool],
    aliases: _Aliases,
) -> Fact[bool]:
    if retry_loop is None:
        return Fact.absent("call is not inside a recognized retry loop")
    if generated_inside.status is not FactStatus.KNOWN:
        return Fact.unknown("key generation location could not be determined")
    if not generated_inside.value:
        return Fact.known(True, "key is evaluated once outside the retry loop")
    if _literal_string(origin) is not None:
        return Fact.known(True, "literal key is identical on every attempt")
    if _contains_fresh_generator(origin, aliases):
        return Fact.known(False, "a fresh key is generated on each loop iteration")
    return Fact.unknown(
        "key is generated inside the retry loop but its value stability is not statically known"
    )


def _contains_fresh_generator(node: ast.AST, aliases: _Aliases) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _qualified_name(child.func)
            if name in aliases.fresh_call_names:
                return True
    return False


def _node_within(node: ast.AST, container: ast.AST) -> bool:
    if not (hasattr(node, "lineno") and hasattr(container, "lineno")):
        return False
    start = (node.lineno, node.col_offset)
    end = (
        getattr(node, "end_lineno", node.lineno),
        getattr(node, "end_col_offset", node.col_offset),
    )
    container_start = (container.lineno, container.col_offset)
    container_end = (
        getattr(container, "end_lineno", container.lineno),
        getattr(container, "end_col_offset", container.col_offset),
    )
    return container_start <= start and end <= container_end


def _build_retry_loops(
    tree: ast.AST,
    call_by_id: dict[int, _RawCall],
    aliases: _Aliases,
    index: _NameIndex,
    filename: str,
) -> tuple[tuple[RetryLoopFact, ...], list[ast.AST]]:
    loops: list[RetryLoopFact] = []
    loop_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not _is_range_call(node.iter):
            continue
        calls = _calls_in_scope(node, call_by_id)
        if not calls:
            continue
        loop_nodes.append(node)
        max_attempts = _range_max_attempts(node, index)
        retry_count = (
            Fact.known(
                max(0, max_attempts.value - 1),
                "derived as max_attempts - 1",
            )
            if max_attempts.status is FactStatus.KNOWN
            else Fact.unknown("retry count is unknown because max_attempts is unknown")
        )
        triggers = _extract_triggers(node, call_by_id, aliases)
        loops.append(
            RetryLoopFact(
                location=_location(filename, node),
                loop_expression=_unparse(node.iter),
                max_attempts=max_attempts,
                retry_count=retry_count,
                triggers=triggers,
                http_call_locations=tuple(_location(filename, call) for call in calls),
                notes=(
                    "Assumes each loop iteration performs one HTTP attempt; "
                    "requests before the loop are not counted.",
                ),
            )
        )
    return tuple(loops), loop_nodes


def _is_range_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
    )


def _range_max_attempts(node: ast.For, index: _NameIndex) -> Fact[int]:
    call = node.iter
    assert isinstance(call, ast.Call)
    if len(call.args) not in (1, 2, 3) or call.keywords:
        return Fact.unknown("range() arguments are not statically resolvable")
    values: list[int] = []
    for argument in call.args:
        resolved = _resolve_int(argument, node, index, set())
        if resolved is None:
            return Fact.unknown(
                f"range() bound is not a resolvable integer: {_unparse(argument)}"
            )
        values.append(resolved)
    try:
        attempts = len(range(*values))
    except (TypeError, ValueError) as exc:
        return Fact.unknown(f"range() could not be evaluated: {exc}")
    return Fact.known(attempts, f"range({', '.join(map(str, values))}) iterations")


def _resolve_int(
    node: ast.AST, use: ast.AST, index: _NameIndex, visited: set[int]
) -> int | None:
    if id(node) in visited:
        return None
    visited.add(id(node))
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        assignment = index.latest_assignment(node, use)
        if assignment is None or assignment.value is None:
            return None
        return _resolve_int(assignment.value, assignment, index, visited)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _resolve_int(node.operand, use, index, visited)
        return -value if value is not None else None
    if isinstance(node, ast.BinOp):
        left = _resolve_int(node.left, use, index, visited)
        right = _resolve_int(node.right, use, index, visited)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return None
    return None


def _calls_in_scope(container: ast.AST, call_by_id: dict[int, _RawCall]) -> list[ast.Call]:
    return [
        node
        for node in _iter_execution_scope(container)
        if id(node) in call_by_id
    ]


def _calls_in_nodes(
    nodes: Iterable[ast.AST], call_by_id: dict[int, _RawCall]
) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in nodes:
        calls.extend(
            child for child in _iter_execution_scope(node) if id(child) in call_by_id
        )
    return calls


def _iter_execution_scope(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        yield child
        yield from _iter_execution_scope(child)


def _extract_triggers(
    loop: ast.For, call_by_id: dict[int, _RawCall], aliases: _Aliases
) -> Fact[tuple[str, ...]]:
    triggers: list[str] = []
    broad = False
    unrecognized = False
    found_handler = False
    found_retry_handler = False

    for node in _iter_execution_scope(loop):
        if not isinstance(node, ast.Try):
            continue
        try_calls = _calls_in_nodes(node.body, call_by_id)
        if not try_calls:
            continue
        for handler in node.handlers:
            found_handler = True
            if not _handler_retries(handler):
                continue
            found_retry_handler = True
            if handler.type is None:
                broad = True
                continue
            expressions = (
                list(handler.type.elts)
                if isinstance(handler.type, ast.Tuple)
                else [handler.type]
            )
            for expression in expressions:
                name = _qualified_name(expression)
                if name is None:
                    unrecognized = True
                    continue
                canonical = aliases.exception_map.get(name)
                if canonical is None:
                    if name in {"Exception", "BaseException"}:
                        broad = True
                    else:
                        unrecognized = True
                elif canonical in _BROAD_EXCEPTIONS:
                    broad = True
                else:
                    triggers.append(canonical)

    if not found_retry_handler:
        if not found_handler:
            return Fact.unknown("no try/except handler found inside the retry loop")
        return Fact.absent("handlers in the retry loop do not continue the loop")

    known = tuple(dict.fromkeys(triggers))
    if broad:
        return Fact.ambiguous(
            "a handler catches a broad exception, so retry triggers cannot be narrowed",
            value=known or None,
        )
    if unrecognized:
        return Fact.ambiguous(
            "a handler catches an exception that is not a recognized timeout/connection type",
            value=known or None,
        )
    if not known:
        return Fact.unknown("no timeout or connection retry trigger was identified")
    return Fact.known(known)


def _handler_retries(handler: ast.ExceptHandler) -> bool:
    nodes = list(_iter_execution_scope(handler))
    if any(isinstance(node, ast.Continue) for node in nodes):
        return True
    if any(isinstance(node, (ast.Return, ast.Raise, ast.Break)) for node in nodes):
        return False
    return True


def _nearest_retry_loop(
    call: ast.Call, loop_nodes: list[ast.AST], parents: dict[int, ast.AST]
) -> ast.AST | None:
    current = parents.get(id(call))
    while current is not None:
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            return None
        if any(current is loop for loop in loop_nodes):
            return current
        current = parents.get(id(current))
    return None


def _unsupported_warnings(
    tree: ast.AST,
    aliases: _Aliases,
    call_by_id: dict[int, _RawCall],
    session_names: set[str],
    filename: str,
) -> list[ExtractionWarning]:
    warnings: list[ExtractionWarning] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = (
                    decorator.func
                    if isinstance(decorator, ast.Call)
                    else decorator
                )
                name = (_qualified_name(target) or "").lower()
                parts = name.split(".") if name else []
                is_retry_decorator = any(
                    part in {"tenacity", "backoff"} for part in parts
                ) or (bool(parts) and parts[-1] in {"retry", "on_exception"})
                if is_retry_decorator:
                    warnings.append(
                        ExtractionWarning(
                            "unsupported.retry_decorator",
                            f"retry decorator is not extracted: {_unparse(decorator)}",
                            _location(filename, decorator),
                        )
                    )
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func)
            if name is not None and name.endswith("HTTPAdapter"):
                if any(keyword.arg == "max_retries" for keyword in node.keywords):
                    warnings.append(
                        ExtractionWarning(
                            "unsupported.adapter_retry_policy",
                            "HTTPAdapter(max_retries=...) is not extracted",
                            _location(filename, node),
                        )
                    )
            if name is not None and name.split(".")[-1] == "Retry":
                warnings.append(
                    ExtractionWarning(
                        "unsupported.urllib3_retry",
                        "urllib3 Retry objects are not extracted",
                        _location(filename, node),
                    )
                )
        if isinstance(node, ast.While):
            calls = _calls_in_scope(node, call_by_id)
            if calls:
                warnings.append(
                    ExtractionWarning(
                        "unsupported.retry_loop",
                        "while-loop retries are not extracted",
                        _location(filename, node),
                    )
                )
        if isinstance(node, ast.For) and not _is_range_call(node.iter):
            calls = _calls_in_scope(node, call_by_id)
            if calls and _looks_retry_related(node):
                warnings.append(
                    ExtractionWarning(
                        "unsupported.retry_loop",
                        "non-range for-loop retries are not extracted",
                        _location(filename, node),
                    )
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if id(node) in call_by_id:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _METHODS:
            continue
        base = _qualified_name(func.value) or ""
        if base in aliases.requests_modules or base in session_names:
            continue
        if _looks_like_http_url_call(node):
            warnings.append(
                ExtractionWarning(
                    "unsupported.http_call",
                    (
                        "call looks like an HTTP request, but its receiver is not a "
                        "tracked requests module or Session"
                    ),
                    _location(filename, node),
                )
            )
    return warnings


def _looks_retry_related(node: ast.For) -> bool:
    text = f"{_unparse(node.target)} {_unparse(node.iter)}".lower()
    return any(hint in text for hint in _RETRY_HINTS)


def _looks_like_http_url_call(node: ast.Call) -> bool:
    if any(
        keyword.arg in _HTTP_CALL_OPTIONS for keyword in node.keywords
    ):
        return True
    if not node.args:
        return False
    first = node.args[0]
    literal = _literal_string(first)
    return literal is not None and literal.startswith(("http://", "https://"))


def _dedupe_warnings(
    warnings: Iterable[ExtractionWarning],
) -> tuple[ExtractionWarning, ...]:
    seen: set[tuple[str, int, int, str]] = set()
    result: list[ExtractionWarning] = []
    for warning in warnings:
        location = warning.location
        key = (
            warning.code,
            location.line if location else 0,
            location.column if location else 0,
            warning.message,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    result.sort(
        key=lambda warning: (
            warning.location.line if warning.location else 0,
            warning.location.column if warning.location else 0,
            warning.code,
        )
    )
    return tuple(result)


def _overall_status(
    calls: tuple[HttpCallFact, ...],
    retry_loops: tuple[RetryLoopFact, ...],
    warnings: tuple[ExtractionWarning, ...],
) -> ExtractionStatus:
    if not calls:
        return (
            ExtractionStatus.UNSUPPORTED
            if warnings
            else ExtractionStatus.NO_HTTP_CALLS
        )
    uncertain = bool(warnings)
    for call in calls:
        uncertain = uncertain or any(
            fact.status not in {FactStatus.KNOWN, FactStatus.ABSENT}
            for fact in (call.method, call.endpoint, call.timeout, call.idempotency_key)
        )
        if (
            call.idempotency_key.status is FactStatus.KNOWN
            and isinstance(call.idempotency_key.value, IdempotencyKeyFact)
        ):
            uncertain = uncertain or any(
                fact.status not in {FactStatus.KNOWN, FactStatus.ABSENT}
                for fact in (
                    call.idempotency_key.value.generated_inside_retry,
                    call.idempotency_key.value.reused_across_attempts,
                )
            )
    for loop in retry_loops:
        uncertain = uncertain or any(
            fact.status not in {FactStatus.KNOWN, FactStatus.ABSENT}
            for fact in (loop.max_attempts, loop.retry_count, loop.triggers)
        )
    return ExtractionStatus.PARTIAL if uncertain else ExtractionStatus.SUPPORTED


def _literal_value(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError):
        return _UNKNOWN_LITERAL


class _UnknownLiteral:
    pass


_UNKNOWN_LITERAL = _UnknownLiteral()


def _literal_string(node: ast.AST) -> str | None:
    value = _literal_value(node)
    if isinstance(value, str):
        return value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                return None
            parts.append(part.value)
        return "".join(parts)
    return None