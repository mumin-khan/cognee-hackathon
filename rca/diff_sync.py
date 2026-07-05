"""git diff -> Release + ChangedSymbol tagging against the baseline graph.

Flow (SPEC contract):
  1. `git diff --name-status prev rel` -> changed files.
  2. Refresh the baseline for the changed files at POST-release (rel) content,
     so incident stack frames match nodes and the MODIFIED_IN edge lands on the
     fresh symbol node.
  3. Parse unified `-U0` hunks on BOTH sides:
       old-side line ranges -> symbols enclosing them at `prev` (deleted/modified);
       new-side line ranges -> symbols enclosing them at `rel`   (added/modified).
  4. Classify + link (linking rules, never drop):
       modified / added -> the FunctionDefinition/ClassDefinition node;
       deleted          -> the containing CodeFile;
       lookup miss      -> the containing CodeFile (fallback, not dropped).
  5. Insert Release + ChangedSymbol nodes and MODIFIED_IN edges, idempotent per
     (release, symbol) via deterministic ids.

All graph writes go through the adapter directly (no cognify / add_data_points),
so the path is keyless and deterministic.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from cognee.shared.CodeGraphEntities import CodeFile

from rca import baseline, extract
from rca.env import get_graph_engine
from rca.models import ChangeType, ChangedSymbol, Release, changed_symbol_id, release_id

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ReleaseSyncResult:
    release: Release
    changed_symbols: list[ChangedSymbol] = field(default_factory=list)


# ---------------------------------------------------------------------------
# git plumbing.
# ---------------------------------------------------------------------------


def _git(repo_path: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _changed_files(repo_path: str, prev_sha: str, rel_sha: str) -> list[tuple[str, str]]:
    """Return [(status, path), ...] for files changed between prev and rel.

    Status is git's short code: A(dded) / M(odified) / D(eleted) / R(enamed).
    """
    out = _git(repo_path, "diff", "--name-status", prev_sha, rel_sha)
    files: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]  # R100 -> R
        path = parts[-1].replace("\\", "/")
        files.append((status, path))
    return files


def _file_at(repo_path: str, sha: str, path: str) -> str | None:
    """Content of `path` at revision `sha`, or None if it did not exist."""
    try:
        return _git(repo_path, "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        return None


def _hunk_ranges(repo_path: str, prev_sha: str, rel_sha: str, path: str) -> tuple[set[int], set[int]]:
    """Return (old_side_lines, new_side_lines) touched by the diff for `path`."""
    out = _git(repo_path, "diff", "-U0", prev_sha, rel_sha, "--", path)
    old_lines: set[int] = set()
    new_lines: set[int] = set()
    for line in out.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        for i in range(old_count):
            old_lines.add(old_start + i)
        for i in range(new_count):
            new_lines.add(new_start + i)
    return old_lines, new_lines


# ---------------------------------------------------------------------------
# Symbol-node lookup (patchable hook — tests monkeypatch this to force a miss).
# ---------------------------------------------------------------------------


async def lookup_symbol_node(
    engine, file_path: str, qualified_name: str, name: str
) -> Optional[dict]:
    """Look up the baseline node for a symbol by its deterministic id.

    Returns the graph node dict (with at least id/name/file_path) or None on a
    miss. Kept as a module-level function so diff_sync's fallback path can be
    exercised by monkeypatching it to return None (SPEC: lookup miss -> file
    fallback, never drop).
    """
    node_id = str(extract.function_id(file_path, name))
    node = await engine.get_node(node_id)
    if node:
        return node
    class_node_id = str(extract.class_id(file_path, name))
    node = await engine.get_node(class_node_id)
    if node:
        return node
    return None


def _qualified_name(file_path: str, symbol_name: str) -> str:
    stem = file_path.rsplit(".", 1)[0]  # drop extension
    dotted = stem.replace("/", ".")
    return f"{dotted}.{symbol_name}"


def _qualified_prefix(file_path: str) -> str:
    stem = file_path.rsplit(".", 1)[0]
    return stem.replace("/", ".")


# ---------------------------------------------------------------------------
# Core.
# ---------------------------------------------------------------------------


async def sync_release(
    repo_path: str,
    prev_sha: str,
    rel_sha: str,
    tag: str,
    timestamp: str,
) -> ReleaseSyncResult:
    engine = await get_graph_engine()

    changed_files = _changed_files(repo_path, prev_sha, rel_sha)

    # (2) Refresh baseline for changed files at post-release content. The repo
    # working tree is at HEAD, which may differ from rel_sha, so we refresh
    # from the exact rel_sha blobs rather than the working tree.
    await _refresh_files_at_revision(repo_path, rel_sha, changed_files)

    release = Release(
        id=release_id(tag),
        commit_sha=rel_sha,
        prev_sha=prev_sha,
        tag=tag,
        timestamp=timestamp,
    )

    changed_symbols: list[ChangedSymbol] = []

    for status, path in changed_files:
        lang = baseline._language_for(path)
        if lang is None:
            continue

        prev_src = _file_at(repo_path, prev_sha, path)
        rel_src = _file_at(repo_path, rel_sha, path)

        prev_ex = extract.extract_file(path, prev_src, lang) if prev_src is not None else None
        rel_ex = extract.extract_file(path, rel_src, lang) if rel_src is not None else None

        old_lines, new_lines = _hunk_ranges(repo_path, prev_sha, rel_sha, path)

        old_names = _enclosing_names(prev_ex, old_lines)
        new_names = _enclosing_names(rel_ex, new_lines)

        prev_all = {s.name for s in prev_ex.symbols} if prev_ex else set()
        rel_all = {s.name for s in rel_ex.symbols} if rel_ex else set()

        for name in sorted(old_names | new_names):
            change_type = _classify(name, prev_all, rel_all)
            diff_hunk = _hunk_for_symbol(repo_path, prev_sha, rel_sha, path, name, prev_ex, rel_ex)
            cs = await _build_changed_symbol(
                engine=engine,
                release=release,
                file_path=path,
                symbol_name=name,
                change_type=change_type,
                diff_hunk=diff_hunk,
            )
            changed_symbols.append(cs)

    # (5) Insert Release + ChangedSymbols + MODIFIED_IN edges (idempotent).
    await _insert(engine, release, changed_symbols)

    # Refreshing a changed file's subtree (step 2) DETACH-DELETEs its old symbol
    # nodes, which also strands MODIFIED_IN edges that EARLIER releases had
    # attached to those symbols. Re-link every ChangedSymbol currently in the
    # graph so no prior release's edges are lost. Idempotent (MERGE on edge).
    await _reconcile_all_modified_in(engine)

    return ReleaseSyncResult(release=release, changed_symbols=changed_symbols)


async def _reconcile_all_modified_in(engine) -> None:
    """Re-establish MODIFIED_IN + in_release edges for every ChangedSymbol node.

    Reads all nodes back from the graph, and for each ChangedSymbol recomputes
    its target baseline-node id (deterministic) and its Release id, re-adding
    both edges. This repairs edges dropped by per-file refresh of a later
    release without needing to track which edges were lost.
    """
    nodes, _edges = await engine.get_graph_data()
    edges: list = []
    for _nid, props in nodes:
        if props.get("type") != "ChangedSymbol":
            continue
        file_path = props.get("file_path")
        qualified_name = props.get("qualified_name", "")
        change_type = props.get("change_type", "modified")
        tag = _tag_from_changed_symbol_id(props, nodes)
        cs_id = props.get("id") or _nid
        symbol_name = qualified_name.rsplit(".", 1)[-1] if qualified_name else ""
        if change_type == "deleted":
            target_id = extract.code_file_id(file_path)
        else:
            # If the function node no longer exists (rare), fall back to file.
            fn_id = extract.function_id(file_path, symbol_name)
            target_id = fn_id if await engine.get_node(str(fn_id)) else extract.code_file_id(file_path)
        edges.append(_edge(cs_id, target_id, "MODIFIED_IN"))
        if tag is not None:
            edges.append(_edge(cs_id, release_id(tag), "in_release"))
    if edges:
        await engine.add_edges(edges)


def _tag_from_changed_symbol_id(props: dict, nodes) -> Optional[str]:
    """Recover a ChangedSymbol's release tag via its in_release edge target."""
    # The tag is embedded in the ChangedSymbol id namespace (tag:qualified_name)
    # but not stored as a prop; recover it from the linked Release node instead.
    # Fall back: scan Release nodes and match by the deterministic cs id.
    cs_id = props.get("id")
    qualified_name = props.get("qualified_name", "")
    for _nid, rel_props in nodes:
        if rel_props.get("type") != "Release":
            continue
        tag = rel_props.get("tag")
        if tag and str(changed_symbol_id(tag, qualified_name)) == str(cs_id):
            return tag
    return None


def _classify(name: str, prev_all: set[str], rel_all: set[str]) -> ChangeType:
    in_prev = name in prev_all
    in_rel = name in rel_all
    if in_rel and not in_prev:
        return "added"
    if in_prev and not in_rel:
        return "deleted"
    return "modified"


def _enclosing_names(ex: Optional[extract.ExtractedFile], lines: set[int]) -> set[str]:
    if ex is None or not lines:
        return set()
    return {s.name for s in extract.symbols_for_lines(ex, lines)}


# @@ -old_start,old_count +new_start,new_count @@  (counts default to 1 when omitted)
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _symbol_span(
    name: str, prev_ex: Optional[extract.ExtractedFile], rel_ex: Optional[extract.ExtractedFile]
) -> tuple[Optional[range], Optional[range]]:
    """1-indexed inclusive line ranges of `name` on the old side (prev) and new side (rel)."""
    def find(ex):
        if ex is None:
            return None
        for s in ex.symbols:
            if s.name == name:
                return range(s.start_line, s.end_line + 1)
        return None

    return find(prev_ex), find(rel_ex)


def _hunk_for_symbol(repo_path, prev_sha, rel_sha, path, name, prev_ex, rel_ex) -> str:
    """Diff excerpt sliced to just the hunks that touch `name`.

    The file's unified diff is split into per-hunk blocks; a block is kept when
    its new-side line range overlaps the symbol's span in the post-release file
    (or its old-side range overlaps the pre-release span, for deletions). Falls
    back to the (truncated) full-file diff if slicing yields nothing.
    """
    try:
        full = _git(repo_path, "diff", "-U1", prev_sha, rel_sha, "--", path)
    except subprocess.CalledProcessError:
        return ""

    old_span, new_span = _symbol_span(name, prev_ex, rel_ex)
    if old_span is None and new_span is None:
        return full[:2000]

    lines = full.splitlines(keepends=True)
    # capture the file header (everything before the first @@) to prefix kept hunks.
    header: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        header.append(lines[i])
        i += 1

    kept: list[str] = []
    while i < len(lines):
        m = _HUNK_HEADER.match(lines[i])
        if not m:  # shouldn't happen, but stay safe
            i += 1
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2) or "1")
        new_start = int(m.group(3))
        new_count = int(m.group(4) or "1")
        old_range = range(old_start, old_start + max(old_count, 1))
        new_range = range(new_start, new_start + max(new_count, 1))

        # gather this hunk's body (until the next @@ or EOF)
        j = i + 1
        while j < len(lines) and not lines[j].startswith("@@"):
            j += 1
        block = lines[i:j]

        overlaps = (
            (new_span is not None and _ranges_overlap(new_range, new_span))
            or (old_span is not None and _ranges_overlap(old_range, old_span))
        )
        if overlaps:
            kept.extend(block)
        i = j

    if not kept:
        return full[:2000]
    return ("".join(header) + "".join(kept))[:2000]


def _ranges_overlap(a: range, b: range) -> bool:
    return a.start < b.stop and b.start < a.stop


async def _build_changed_symbol(
    engine,
    release: Release,
    file_path: str,
    symbol_name: str,
    change_type: ChangeType,
    diff_hunk: str,
) -> ChangedSymbol:
    qualified_name = _qualified_name(file_path, symbol_name)

    code_file = _code_file_stub(file_path)

    modified_node = None
    if change_type != "deleted":
        # module-level function reference (patchable lookup hook).
        found = await lookup_symbol_node(
            engine,
            file_path=file_path,
            qualified_name=qualified_name,
            name=symbol_name,
        )
        if found is not None:
            # Rebuild the FunctionDefinition datapoint from its deterministic id
            # so modified_node carries a concrete node (with file_path) for the
            # MODIFIED_IN edge + test assertions.
            modified_node = _function_stub(file_path, symbol_name)

    if modified_node is None:
        # deleted OR lookup miss -> fall back to the containing CodeFile.
        modified_node = code_file

    return ChangedSymbol(
        id=changed_symbol_id(release.tag, qualified_name),
        qualified_name=qualified_name,
        file_path=file_path,
        change_type=change_type,
        diff_hunk=diff_hunk,
        release=release,
        modified_node=modified_node,
    )


def _code_file_stub(file_path: str) -> CodeFile:
    return CodeFile(
        id=extract.code_file_id(file_path),
        name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        language=baseline._language_for(file_path),
    )


def _function_stub(file_path: str, name: str) -> extract.FunctionDefinition:
    return extract.FunctionDefinition(
        id=extract.function_id(file_path, name),
        name=name,
        start_point=(0, 0),
        end_point=(0, 0),
        start_line=0,
        end_line=0,
        source_code="",
        file_path=file_path,
    )


def _edge(source_id, target_id, name: str):
    return (
        str(source_id),
        str(target_id),
        name,
        {"relationship_name": name, "relationship_type": name},
    )


async def _insert(engine, release: Release, changed_symbols: list[ChangedSymbol]) -> None:
    nodes: list = [release]
    edges: list = []
    for cs in changed_symbols:
        nodes.append(cs)
        # ChangedSymbol -> Release (belongs to release window).
        edges.append(_edge(cs.id, release.id, "in_release"))
        # ChangedSymbol -> modified baseline node (the MODIFIED_IN edge). The
        # target node already exists in the baseline graph; add_edges MERGEs on
        # (from,to,relationship_name) so re-sync is idempotent.
        edges.append(_edge(cs.id, cs.modified_node.id, "MODIFIED_IN"))
    await engine.add_nodes(nodes)
    await engine.add_edges(edges)


async def _refresh_files_at_revision(
    repo_path: str, rel_sha: str, changed_files: list[tuple[str, str]]
) -> None:
    """Refresh the baseline subtree of each changed file at `rel_sha` content.

    Deleted files (status D) are dropped from the graph; present files are
    re-extracted from the rel_sha blob and re-inserted. We write the rel_sha
    content to a temp checkout dir so baseline.build_baseline can read + resolve
    cross-file edges against the real post-release tree.
    """
    engine = await get_graph_engine()

    present = [p for (s, p) in changed_files if s != "D" and baseline._language_for(p)]
    deleted = [p for (s, p) in changed_files if s == "D" and baseline._language_for(p)]

    # Drop deleted files' subtrees entirely.
    if deleted:
        to_delete: list[str] = []
        for path in deleted:
            to_delete.append(str(extract.code_file_id(path)))
        await engine.delete_nodes(to_delete)

    if not present:
        return

    # Re-extract present files at rel_sha and refresh their subtrees. We resolve
    # cross-file edges against the rel_sha content of the WHOLE repo, so write a
    # materialized snapshot of rel_sha to a temp dir and build from there for
    # just the changed files.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _materialize_tree(repo_path, rel_sha, tmp)
        await baseline.build_baseline(tmp, files=present)


def _materialize_tree(repo_path: str, sha: str, dest: str) -> None:
    """Extract the full tree at `sha` into `dest` (via git archive)."""
    import io
    import tarfile

    archive = subprocess.run(
        ["git", "-C", repo_path, "archive", sha],
        capture_output=True,
        check=True,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        tf.extractall(dest, filter="data")
