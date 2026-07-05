"""Build / refresh the baseline code graph in the single RCA dataset.

Uses the graph adapter directly (env.get_graph_engine()) — NO generic
cognify() and NO add_data_points() (which would require an embedding engine).
Deterministic node ids mean add_nodes MERGEs idempotently, so re-inserting a
file's subtree upserts rather than duplicates.

Graph shape (all built-in CodeGraphEntities plus function-level call edges):

    Repository --contains--> CodeFile
    CodeFile   --provides_function_definition--> FunctionDefinition
    CodeFile   --provides_class_definition-->    ClassDefinition
    CodeFile   --depends_on-->                   ImportStatement
    ImportStatement --imports_symbol-->          FunctionDefinition   (resolved)
    FunctionDefinition --calls-->                FunctionDefinition   (resolved)

The `calls` and `imports_symbol` edges are what make blast-radius traversal
cross file boundaries at *function* granularity: e.g. process_payment --calls-->
format_currency lands format_currency exactly one hop from a process_payment
seed, which the import-hop incident test requires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from cognee.shared.CodeGraphEntities import CodeFile, ImportStatement, Repository

from rca import extract
from rca.env import NAMESPACE, get_graph_engine

_PY_EXT = ".py"
_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
_TS_EXTS = (".ts", ".tsx")

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".cognee_system", ".cognee_data"}


@dataclass
class BaselineStats:
    files: int = 0
    functions: int = 0
    classes: int = 0
    imports: int = 0
    edges: int = 0
    changed_files: list[str] = field(default_factory=list)


def repository_id(repo_path: str) -> UUID:
    # Stable across full builds and per-file refreshes (which run from a temp
    # snapshot dir): there is exactly one logical repo in the single RCA
    # dataset, so the id must not depend on the on-disk path.
    return uuid5(NAMESPACE, "Repository:rca_codegraph")


def _language_for(rel_path: str) -> str | None:
    if rel_path.endswith(_PY_EXT):
        return "python"
    if rel_path.endswith(_JS_EXTS):
        return "javascript"
    if rel_path.endswith(_TS_EXTS):
        return "typescript"
    return None


def _iter_source_files(repo_path: str) -> list[str]:
    out: list[str] = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_path)
            if _language_for(rel):
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


# ---------------------------------------------------------------------------
# Symbol / module resolution used to build cross-file edges.
# ---------------------------------------------------------------------------


def _module_to_file(module: str, all_files: set[str]) -> str | None:
    """Resolve a Python dotted module (e.g. app.utils) to a repo file path."""
    candidate = module.replace(".", "/") + ".py"
    if candidate in all_files:
        return candidate
    pkg = module.replace(".", "/") + "/__init__.py"
    if pkg in all_files:
        return pkg
    return None


def _js_module_to_file(module: str, importer_path: str, all_files: set[str]) -> str | None:
    """Resolve a JS/TS relative require/import specifier to a repo file path."""
    if not module.startswith("."):
        return None
    base_dir = os.path.dirname(importer_path)
    target = os.path.normpath(os.path.join(base_dir, module)).replace(os.sep, "/")
    for cand in (target, *[target + e for e in (_JS_EXTS + _TS_EXTS)]):
        if cand in all_files:
            return cand
    return None


def _edge(source_id, target_id, name: str):
    return (
        str(source_id),
        str(target_id),
        name,
        {"relationship_name": name, "relationship_type": name},
    )


# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------


async def build_baseline(repo_path: str, files: list[str] | None = None) -> BaselineStats:
    """Full build (files=None) or per-file refresh of the baseline graph.

    Per-file refresh deletes each named file's existing subtree (CodeFile +
    its symbols + its imports) before re-inserting, so a changed file's stale
    symbols never linger.
    """
    engine = await get_graph_engine()
    all_files = set(_iter_source_files(repo_path))
    target_files = list(all_files) if files is None else [f for f in files if f in all_files]

    if files is not None:
        # Per-file refresh: drop the old subtree for each changed file first.
        await _delete_file_subtrees(engine, repo_path, files)

    # Extract everything first so cross-file edges can resolve against the full
    # symbol table.
    extracted_by_file: dict[str, extract.ExtractedFile] = {}
    for rel in target_files:
        lang = _language_for(rel)
        source = _read_file(repo_path, rel)
        if source is None:
            continue
        extracted_by_file[rel] = extract.extract_file(rel, source, lang)

    # For call/import resolution we need the symbol tables of ALL files in the
    # repo, not just the ones being (re)built — a refreshed file may call into
    # an unchanged one. Extract the rest lazily for resolution only.
    resolution_files = extracted_by_file
    if files is not None:
        resolution_files = dict(extracted_by_file)
        for rel in all_files:
            if rel in resolution_files:
                continue
            lang = _language_for(rel)
            source = _read_file(repo_path, rel)
            if source is not None:
                resolution_files[rel] = extract.extract_file(rel, source, lang)

    # name -> FunctionDefinition id, keyed per file, for call resolution.
    func_ids: dict[tuple[str, str], UUID] = {}
    for rel, ex in resolution_files.items():
        for fn in ex.functions:
            func_ids[(rel, fn.name)] = fn.id

    repo = Repository(id=repository_id(repo_path), path=os.path.abspath(repo_path))

    nodes: list = [repo]
    edges: list = []
    stats = BaselineStats(changed_files=list(extracted_by_file.keys()))

    for rel, ex in extracted_by_file.items():
        cf = ex.code_file
        cf.part_of = None  # avoid nested serialization; edge added explicitly
        nodes.append(cf)
        edges.append(_edge(repo.id, cf.id, "contains"))
        stats.files += 1

        for fn in ex.functions:
            nodes.append(fn)
            edges.append(_edge(cf.id, fn.id, "provides_function_definition"))
            stats.functions += 1
        for cls in ex.classes:
            nodes.append(cls)
            edges.append(_edge(cf.id, cls.id, "provides_class_definition"))
            stats.classes += 1
        for imp in ex.imports:
            nodes.append(imp)
            edges.append(_edge(cf.id, imp.id, "depends_on"))
            stats.imports += 1
            # Resolve the import to a target file, then to a specific symbol.
            target_file = _resolve_import_file(rel, imp, all_files)
            if target_file is not None:
                target_fn = func_ids.get((target_file, imp.name))
                if target_fn is not None:
                    edges.append(_edge(imp.id, target_fn, "imports_symbol"))

        # Function-level call edges (Python + JS): resolve called names to
        # known functions via same-file defs and this file's imports.
        edges.extend(
            _call_edges(rel, ex, resolution_files, func_ids, all_files)
        )

    await engine.add_nodes(nodes)
    await engine.add_edges(edges)
    stats.edges = len(edges)
    return stats


def _resolve_import_file(importer: str, imp: ImportStatement, all_files: set[str]) -> str | None:
    if not imp.module:
        return None
    if importer.endswith(_PY_EXT):
        return _module_to_file(imp.module, all_files)
    # JS/TS relative import (e.g. "./helpers.js").
    return _js_module_to_file(imp.module, importer, all_files)


def _call_edges(importer, ex, resolution_files, func_ids, all_files):
    """Resolve calls inside each function of `ex` to known function nodes.

    Resolution scope (deliberately conservative, no full name resolution):
      * same-file function by name;
      * a name imported into this file that resolves to a symbol in another
        repo file.
    """
    edges = []
    # Build the name -> target-fn-id table visible from this file.
    visible: dict[str, UUID] = {}
    for fn in ex.functions:
        visible[fn.name] = fn.id
    for imp in ex.imports:
        target_file = _resolve_import_file(importer, imp, all_files)
        if target_file is not None:
            tid = func_ids.get((target_file, imp.name))
            if tid is not None:
                visible[imp.name] = tid

    for fn in ex.functions:
        called = _called_names(fn.source_code)
        for name in called:
            target = visible.get(name)
            if target is not None and target != fn.id:
                edges.append(_edge(fn.id, target, "calls"))
    return edges


def _called_names(source_code: str) -> set[str]:
    """Cheap call-site extraction: identifiers immediately followed by '('.

    Good enough for the RCA blast-radius signal; avoids re-parsing with a
    grammar and false 'calls' from method chains still resolve by bare name.
    """
    import re

    names = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", source_code):
        names.add(m.group(1))
    return names


# ---------------------------------------------------------------------------
# Per-file refresh helpers.
# ---------------------------------------------------------------------------


async def _delete_file_subtrees(engine, repo_path: str, files: list[str]) -> None:
    """Delete each file's CodeFile + its function/class/import nodes."""
    all_files = set(_iter_source_files(repo_path))
    to_delete: list[str] = []
    for rel in files:
        # Re-extract the *current on-disk* content to know which node ids to
        # remove; ids are deterministic so this recovers the exact set.
        lang = _language_for(rel)
        if lang is None:
            continue
        to_delete.append(str(extract.code_file_id(rel)))
        source = _read_file(repo_path, rel)
        if source is None:
            continue
        ex = extract.extract_file(rel, source, lang)
        for fn in ex.functions:
            to_delete.append(str(fn.id))
        for cls in ex.classes:
            to_delete.append(str(cls.id))
        for imp in ex.imports:
            to_delete.append(str(imp.id))
    if to_delete:
        await engine.delete_nodes(to_delete)


def _read_file(repo_path: str, rel: str) -> str | None:
    full = os.path.join(repo_path, rel)
    try:
        with open(full, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None
