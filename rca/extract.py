"""Tree-sitter extraction of functions / classes / imports with line ranges.

Emits cognee's built-in CodeGraphEntities models (CodeFile, FunctionDefinition,
ClassDefinition, ImportStatement) so every node in the graph is uniform
regardless of which language produced it. Those models carry only
start_point/end_point tuples (no 1-indexed line-range fields), so we wrap each
in a light `Symbol` dataclass carrying 1-indexed start_line/end_line alongside
the DataPoint instance -- that's what diff hunk -> symbol mapping needs.

SPEC rule 5 decision (see rca/DECISIONS.md): cognee 1.2.2 ships no importable,
deterministic-id, per-file-refreshable native code-graph entry usable keyless,
so we extract BOTH Python and JS/TS ourselves here, emitting the same built-in
models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid5

from cognee.shared.CodeGraphEntities import (
    ClassDefinition as _BaseClassDefinition,
    CodeFile,
    FunctionDefinition as _BaseFunctionDefinition,
    ImportStatement,
)

from rca.env import NAMESPACE


# The built-in FunctionDefinition/ClassDefinition carry only start_point/
# end_point tuples. Tests read 1-indexed `.start_line`/`.end_line` directly off
# the extracted symbols, so we extend the models with those fields. They remain
# DataPoints (same graph, same index_fields), so the graph layer is unaffected.
class FunctionDefinition(_BaseFunctionDefinition):
    start_line: int
    end_line: int


class ClassDefinition(_BaseClassDefinition):
    start_line: int
    end_line: int

# ---------------------------------------------------------------------------
# Deterministic node ids (SPEC hard rule 2).
# ---------------------------------------------------------------------------


def code_file_id(file_path: str) -> UUID:
    return uuid5(NAMESPACE, f"CodeFile:{file_path}")


def function_id(file_path: str, name: str) -> UUID:
    return uuid5(NAMESPACE, f"FunctionDefinition:{file_path}:{name}")


def class_id(file_path: str, name: str) -> UUID:
    return uuid5(NAMESPACE, f"ClassDefinition:{file_path}:{name}")


def import_id(file_path: str, module: str, name: str) -> UUID:
    return uuid5(NAMESPACE, f"ImportStatement:{file_path}:{module}:{name}")


# ---------------------------------------------------------------------------
# Light wrappers carrying line ranges alongside the DataPoint.
# ---------------------------------------------------------------------------


@dataclass
class Symbol:
    """A function/class definition with its 1-indexed line range."""

    name: str
    kind: str  # "function" | "class"
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    node: FunctionDefinition | ClassDefinition


@dataclass
class ExtractedFile:
    code_file: CodeFile
    functions: list[FunctionDefinition] = field(default_factory=list)
    classes: list[ClassDefinition] = field(default_factory=list)
    imports: list[ImportStatement] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Language / parser plumbing.
# ---------------------------------------------------------------------------

_PY_LANGS = {"python", "py"}
_JS_LANGS = {"javascript", "js", "jsx"}
_TS_LANGS = {"typescript", "ts", "tsx"}


def _normalize_language(language: str) -> str:
    lang = language.lower()
    if lang in _PY_LANGS:
        return "python"
    if lang in _JS_LANGS:
        return "javascript"
    if lang in _TS_LANGS:
        return "typescript"
    return lang


def _get_parser(language: str):
    from tree_sitter import Language, Parser

    if language == "python":
        import tree_sitter_python as ts

        return Parser(Language(ts.language()))
    if language in ("javascript", "typescript"):
        # NOTE: in this environment tree_sitter_javascript (0.25.0) compiles to
        # grammar ABI 15, but the installed tree_sitter core (0.24.0) only
        # supports ABI <= 14, so it fails to load. The TypeScript grammar
        # (0.23.2, ABI 14) loads and is a strict superset of JS, producing the
        # same node types we rely on (function_declaration, lexical_declaration,
        # variable_declarator, import_statement). We route JS through it. See
        # rca/DECISIONS.md.
        import tree_sitter_typescript as ts

        return Parser(Language(ts.language_typescript()))
    raise ValueError(f"unsupported language for extraction: {language!r}")


def _text(source_bytes: bytes, node) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _lines(node) -> tuple[int, int]:
    # tree-sitter points are (row, col) 0-indexed; convert to 1-indexed lines.
    return node.start_point[0] + 1, node.end_point[0] + 1


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def extract_file(path: str, source: str, language: str) -> ExtractedFile:
    """Extract CodeFile + function/class/import nodes with line ranges."""
    lang = _normalize_language(language)
    parser = _get_parser(lang)
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)

    code_file = CodeFile(
        id=code_file_id(path),
        name=path.rsplit("/", 1)[-1],
        file_path=path,
        language=lang,
        source_code=source,
    )
    extracted = ExtractedFile(code_file=code_file)

    if lang == "python":
        _walk_python(tree.root_node, source_bytes, path, extracted)
    else:  # javascript / typescript share enough grammar for our needs
        _walk_js(tree.root_node, source_bytes, path, extracted)

    return extracted


def symbols_for_lines(extracted: ExtractedFile, lines: set[int]) -> list[Symbol]:
    """Map a set of 1-indexed line numbers to the symbols enclosing them.

    A symbol is returned if any of the given lines falls within its inclusive
    [start_line, end_line] range. Order follows source (by start_line).
    """
    hits: list[Symbol] = []
    for sym in sorted(extracted.symbols, key=lambda s: s.start_line):
        if any(sym.start_line <= ln <= sym.end_line for ln in lines):
            hits.append(sym)
    return hits


# ---------------------------------------------------------------------------
# Python extraction.
# ---------------------------------------------------------------------------


def _walk_python(root, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    for child in root.children:
        _visit_python(child, source_bytes, path, out)


def _visit_python(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    t = node.type
    if t == "function_definition":
        _add_python_function(node, source_bytes, path, out)
        return
    if t == "decorated_definition":
        # unwrap to the underlying def/class
        inner = node.child_by_field_name("definition") or node.children[-1]
        _visit_python(inner, source_bytes, path, out)
        return
    if t == "class_definition":
        _add_python_class(node, source_bytes, path, out)
        return
    if t in ("import_statement", "import_from_statement"):
        _add_python_imports(node, source_bytes, path, out)
        return


def _add_python_function(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(source_bytes, name_node)
    start_line, end_line = _lines(node)
    fn = FunctionDefinition(
        id=function_id(path, name),
        name=name,
        start_point=node.start_point,
        end_point=node.end_point,
        start_line=start_line,
        end_line=end_line,
        source_code=_text(source_bytes, node),
        file_path=path,
    )
    out.functions.append(fn)
    out.symbols.append(Symbol(name, "function", start_line, end_line, fn))


def _add_python_class(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(source_bytes, name_node)
    start_line, end_line = _lines(node)
    cls = ClassDefinition(
        id=class_id(path, name),
        name=name,
        start_point=node.start_point,
        end_point=node.end_point,
        start_line=start_line,
        end_line=end_line,
        source_code=_text(source_bytes, node),
        file_path=path,
    )
    out.classes.append(cls)
    out.symbols.append(Symbol(name, "class", start_line, end_line, cls))
    # Methods inside the class body become their own FunctionDefinitions.
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.children:
            if child.type == "function_definition":
                _add_python_function(child, source_bytes, path, out)
            elif child.type == "decorated_definition":
                inner = child.child_by_field_name("definition") or child.children[-1]
                if inner.type == "function_definition":
                    _add_python_function(inner, source_bytes, path, out)


def _add_python_imports(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    source_code = _text(source_bytes, node)
    if node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        module = _text(source_bytes, module_node) if module_node else ""
        names = _python_imported_names(node, source_bytes, module_node)
        if not names:
            names = [module.rsplit(".", 1)[-1]]
        for name in names:
            out.imports.append(
                ImportStatement(
                    id=import_id(path, module, name),
                    name=name,
                    module=module,
                    start_point=node.start_point,
                    end_point=node.end_point,
                    source_code=source_code,
                    file_path=path,
                )
            )
    else:  # plain "import a.b.c" / "import a as b"
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                target = child
                if child.type == "aliased_import":
                    target = child.child_by_field_name("name") or child
                module = _text(source_bytes, target)
                name = module.rsplit(".", 1)[-1]
                out.imports.append(
                    ImportStatement(
                        id=import_id(path, module, name),
                        name=name,
                        module=module,
                        start_point=node.start_point,
                        end_point=node.end_point,
                        source_code=source_code,
                        file_path=path,
                    )
                )


def _python_imported_names(node, source_bytes: bytes, module_node) -> list[str]:
    names: list[str] = []
    for child in node.children:
        if child is module_node:
            continue
        if child.type == "dotted_name":
            names.append(_text(source_bytes, child))
        elif child.type == "aliased_import":
            n = child.child_by_field_name("name")
            if n is not None:
                names.append(_text(source_bytes, n))
        elif child.type == "wildcard_import":
            names.append("*")
    return names


# ---------------------------------------------------------------------------
# JavaScript / TypeScript extraction.
# ---------------------------------------------------------------------------


def _walk_js(root, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    for child in root.children:
        _visit_js(child, source_bytes, path, out)


def _visit_js(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    t = node.type
    if t == "function_declaration":
        _add_js_function(node, node, source_bytes, path, out)
        return
    if t == "class_declaration":
        _add_js_class(node, source_bytes, path, out)
        return
    if t in ("lexical_declaration", "variable_declaration"):
        # const/let/var: catch `const f = () => {}` / `const f = function(){}`
        # and CommonJS `const { a, b } = require("...")`.
        _visit_js_variable(node, source_bytes, path, out)
        return
    if t == "import_statement":
        _add_js_es_import(node, source_bytes, path, out)
        return
    if t == "export_statement":
        for child in node.children:
            _visit_js(child, source_bytes, path, out)
        return
    if t == "expression_statement":
        # bare `require(...)` calls without assignment -- rare, skip.
        return


def _add_js_function(name_owner, span_node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    name_node = name_owner.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(source_bytes, name_node)
    start_line, end_line = _lines(span_node)
    fn = FunctionDefinition(
        id=function_id(path, name),
        name=name,
        start_point=span_node.start_point,
        end_point=span_node.end_point,
        start_line=start_line,
        end_line=end_line,
        source_code=_text(source_bytes, span_node),
        file_path=path,
    )
    out.functions.append(fn)
    out.symbols.append(Symbol(name, "function", start_line, end_line, fn))


def _add_js_class(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(source_bytes, name_node)
    start_line, end_line = _lines(node)
    cls = ClassDefinition(
        id=class_id(path, name),
        name=name,
        start_point=node.start_point,
        end_point=node.end_point,
        start_line=start_line,
        end_line=end_line,
        source_code=_text(source_bytes, node),
        file_path=path,
    )
    out.classes.append(cls)
    out.symbols.append(Symbol(name, "class", start_line, end_line, cls))
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.children:
            if child.type in ("method_definition",):
                _add_js_function(child, child, source_bytes, path, out)


def _visit_js_variable(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    for declarator in node.children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        value_node = declarator.child_by_field_name("value")
        if value_node is None:
            continue
        # CommonJS require: const { a, b } = require("mod")  /  const m = require("mod")
        module = _js_require_module(value_node, source_bytes)
        if module is not None:
            for imported_name in _js_destructured_names(name_node, source_bytes):
                out.imports.append(
                    ImportStatement(
                        id=import_id(path, module, imported_name),
                        name=imported_name,
                        module=module,
                        start_point=node.start_point,
                        end_point=node.end_point,
                        source_code=_text(source_bytes, node),
                        file_path=path,
                    )
                )
            continue
        # Arrow / function expression assigned to a name.
        if value_node.type in ("arrow_function", "function_expression", "function") and (
            name_node is not None and name_node.type == "identifier"
        ):
            name = _text(source_bytes, name_node)
            start_line, end_line = _lines(declarator)
            fn = FunctionDefinition(
                id=function_id(path, name),
                name=name,
                start_point=declarator.start_point,
                end_point=declarator.end_point,
                start_line=start_line,
                end_line=end_line,
                source_code=_text(source_bytes, declarator),
                file_path=path,
            )
            out.functions.append(fn)
            out.symbols.append(Symbol(name, "function", start_line, end_line, fn))


def _js_require_module(value_node, source_bytes: bytes) -> str | None:
    if value_node.type != "call_expression":
        return None
    fn = value_node.child_by_field_name("function")
    if fn is None or _text(source_bytes, fn) != "require":
        return None
    args = value_node.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        if arg.type == "string":
            return _strip_quotes(_text(source_bytes, arg))
    return None


def _js_destructured_names(name_node, source_bytes: bytes) -> list[str]:
    if name_node is None:
        return []
    if name_node.type == "identifier":
        return [_text(source_bytes, name_node)]
    if name_node.type == "object_pattern":
        names = []
        for child in name_node.children:
            if child.type == "shorthand_property_identifier_pattern":
                names.append(_text(source_bytes, child))
            elif child.type == "pair_pattern":
                key = child.child_by_field_name("key")
                if key is not None:
                    names.append(_text(source_bytes, key))
        return names
    return []


def _add_js_es_import(node, source_bytes: bytes, path: str, out: ExtractedFile) -> None:
    source_node = node.child_by_field_name("source")
    module = _strip_quotes(_text(source_bytes, source_node)) if source_node else ""
    names = _js_es_import_names(node, source_bytes)
    if not names:
        names = [module.rsplit("/", 1)[-1]]
    for name in names:
        out.imports.append(
            ImportStatement(
                id=import_id(path, module, name),
                name=name,
                module=module,
                start_point=node.start_point,
                end_point=node.end_point,
                source_code=_text(source_bytes, node),
                file_path=path,
            )
        )


def _js_es_import_names(node, source_bytes: bytes) -> list[str]:
    names: list[str] = []
    for child in node.children:
        if child.type != "import_clause":
            continue
        for spec in child.children:
            if spec.type == "identifier":  # default import
                names.append(_text(source_bytes, spec))
            elif spec.type == "namespace_import":
                ident = spec.children[-1]
                names.append(_text(source_bytes, ident))
            elif spec.type == "named_imports":
                for imp in spec.children:
                    if imp.type == "import_specifier":
                        n = imp.child_by_field_name("name")
                        if n is not None:
                            names.append(_text(source_bytes, n))
    return names


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'`" and s[-1] == s[0]:
        return s[1:-1]
    return s
