"""Incident stack trace -> ranked blast-radius Findings.

Pipeline (SPEC contract):
  1. Parse stack-trace frames (Python + JS) into (file, symbol, line).
  2. Seed: direct graph lookup of the frame symbols -> baseline nodes (hop 0).
  3. Traverse depends_on + containment + calls edges up to k hops (undirected
     view for blast radius) via networkx; record the min hop for each node.
  4. Filter to nodes carrying a MODIFIED_IN edge from a ChangedSymbol whose
     release is inside the window (None = all releases).
  5. Rank: 0-hop dominates, then (hops asc, recency desc, change_type weight
     deleted/modified > added).

Reads come from the graph adapter (get_graph_data) into networkx — no
SearchType dependency. The no-trace path falls back to vector search over
embedded diff_hunk fields and degrades gracefully (returns [] with a warning)
when no embedding provider is configured, so tests never hit a paid API.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import Optional

import networkx as nx

from rca.env import get_graph_engine

# Edges walked for blast radius (undirected). Containment + imports + calls.
_TRAVERSE_EDGES = {
    "contains",
    "provides_function_definition",
    "provides_class_definition",
    "depends_on",
    "imports_symbol",
    "calls",
}

_CHANGE_TYPE_WEIGHT = {"deleted": 2, "modified": 2, "added": 1}


@dataclass
class Finding:
    qualified_name: str
    file_path: str
    release_tag: str
    change_type: str
    hops: int
    score: float
    diff_hunk: str


# ---------------------------------------------------------------------------
# Stack-trace parsing.
# ---------------------------------------------------------------------------

# Python:  File "app/payments.py", line 32, in process_payment
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
# JS:      at formatPrice (web/helpers.js:4:20)   /   at web/client.js:7:20
_JS_FRAME = re.compile(r"at (?:(\S+) )?\(?([^\s():]+\.[jt]sx?):(\d+)(?::\d+)?\)?")


@dataclass
class Frame:
    file_path: str
    name: str
    line: int


def parse_frames(stack_trace: str) -> list[Frame]:
    frames: list[Frame] = []
    for m in _PY_FRAME.finditer(stack_trace):
        frames.append(Frame(file_path=m.group(1), name=m.group(3), line=int(m.group(2))))
    if not frames:
        for m in _JS_FRAME.finditer(stack_trace):
            name = m.group(1) or ""
            frames.append(Frame(file_path=m.group(2), name=name, line=int(m.group(3))))
    return frames


# Terminal exception line, e.g. `ValueError: Unknown format code 'f' for ...`
# (Python) or `Error: Cannot read properties of undefined ...` (JS).
_EXC_LINE = re.compile(r"^\s*(?:[A-Za-z_][\w.]*Error|[A-Za-z_]\w*(?:Exception|Error)):?\s*(.*)$")
# words we ignore when matching an exception message against code — too generic.
_STOPWORDS = frozenset(
    "the a an of for to in on is are be was were error invalid unknown cannot "
    "not none null undefined object type value code reading properties raise "
    "traceback most recent call last file line".split()
)


def parse_exception(stack_trace: str) -> str:
    """The final exception message from a traceback (Python last line, or a JS
    `Error:` line). Empty string if none is recognizable."""
    lines = [ln for ln in stack_trace.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _EXC_LINE.match(ln.strip())
        if m:
            # include the exception class token itself plus its message
            return ln.strip()
    return ""


def _keywords(text: str) -> set[str]:
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())
    return {t for t in toks if t not in _STOPWORDS}


def _exception_relevance(exc_keywords: set[str], qualified_name: str, diff_hunk: str) -> int:
    """How strongly a changed symbol matches the raised exception.

    Compares exception keywords against the symbol's own name and the words it
    ADDED in its diff (lines starting with '+'). Name matches count double —
    `format_currency` matching a "format code" exception is a strong signal.
    Returns a small integer (0 = no evidence).
    """
    if not exc_keywords:
        return 0
    name_words = _keywords(qualified_name.replace(".", " ").replace("_", " "))
    added = " ".join(
        ln[1:] for ln in diff_hunk.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )
    diff_words = _keywords(added)
    score = 2 * len(exc_keywords & name_words) + len(exc_keywords & diff_words)
    return score


# ---------------------------------------------------------------------------
# Graph loading.
# ---------------------------------------------------------------------------


async def _load_graph():
    engine = await get_graph_engine()
    nodes, edges = await engine.get_graph_data()
    node_props = {nid: props for nid, props in nodes}

    g = nx.Graph()
    for nid in node_props:
        g.add_node(nid)

    # ChangedSymbol -> baseline target (MODIFIED_IN) and -> release (in_release).
    modified_in: dict[str, list[str]] = {}  # target_node_id -> [changed_symbol_id]
    cs_release: dict[str, str] = {}  # changed_symbol_id -> release_node_id

    for src, tgt, rel_name, _props in edges:
        if rel_name in _TRAVERSE_EDGES:
            g.add_edge(src, tgt)
        elif rel_name == "MODIFIED_IN":
            modified_in.setdefault(tgt, []).append(src)
        elif rel_name == "in_release":
            cs_release[src] = tgt

    return node_props, g, modified_in, cs_release


async def get_graph_snapshot() -> dict:
    """Public, JSON-serializable view of the current code graph for the dashboard.

    Returns real nodes/edges straight from the graph adapter — no mock data.
    Shape:
        {
          "nodes": [{"id", "type", "name", "file_path", "language"?,
                     "start_line"?, "end_line"?, "change_type"?, "tag"?}, ...],
          "edges": [{"source", "target", "type"}, ...],
          "modified_in": {baseline_node_id: [changed_symbol_id, ...]},
        }
    Node ids are stringified UUIDs so they round-trip through JSON.
    """
    engine = await get_graph_engine()
    nodes, edges = await engine.get_graph_data()

    # keep only the fields the frontend needs — the raw props carry ~20 internal
    # bookkeeping keys (source_pipeline, topological_rank, …) we never render.
    _KEEP = (
        "type", "name", "file_path", "language", "start_line", "end_line",
        "qualified_name", "change_type", "tag", "module",
    )
    out_nodes = []
    for nid, props in nodes:
        node = {"id": str(nid)}
        for k in _KEEP:
            if k in props and props[k] is not None:
                node[k] = props[k]
        out_nodes.append(node)

    out_edges = []
    modified_in: dict[str, list[str]] = {}
    for src, tgt, rel_name, _props in edges:
        out_edges.append({"source": str(src), "target": str(tgt), "type": rel_name})
        if rel_name == "MODIFIED_IN":
            modified_in.setdefault(str(tgt), []).append(str(src))

    return {"nodes": out_nodes, "edges": out_edges, "modified_in": modified_in}


# ---------------------------------------------------------------------------
# Seeding + traversal.
# ---------------------------------------------------------------------------


def _seed_nodes(frames: list[Frame], node_props: dict) -> list[str]:
    seeds: list[str] = []
    for frame in frames:
        for nid, props in node_props.items():
            if props.get("type") not in ("FunctionDefinition", "ClassDefinition"):
                continue
            if props.get("file_path") == frame.file_path and props.get("name") == frame.name:
                seeds.append(nid)
    return seeds


def _hop_distances(g: nx.Graph, seeds: list[str], k_hops: int) -> dict[str, int]:
    dist: dict[str, int] = {}
    for seed in seeds:
        if seed not in g:
            dist.setdefault(seed, 0)
            continue
        lengths = nx.single_source_shortest_path_length(g, seed, cutoff=k_hops)
        for nid, d in lengths.items():
            if nid not in dist or d < dist[nid]:
                dist[nid] = d
    for seed in seeds:  # ensure seeds are hop 0 even if isolated
        dist[seed] = 0
    return dist


# ---------------------------------------------------------------------------
# Ranking.
# ---------------------------------------------------------------------------


def _score(
    hops: int, recency_rank: int, change_weight: int, exc_relevance: int = 0
) -> float:
    """Rank suspects. LOWER hops dominate (the SPEC's 0-hop-first rule), but a
    strong exception-relevance signal can lift a closer-matching suspect within
    and across adjacent hop tiers.

    Layout of the terms (largest coefficient first):
      - hops:            per-tier gap of 1000  -> proximity is the primary axis
      - exc_relevance:   up to ~1500           -> a symbol whose change matches the
                         raised exception can overtake a non-matching neighbour that
                         is one hop closer (e.g. the failing frame that merely
                         propagated the error). Capped so it can jump AT MOST one
                         hop tier, never turn a 3-hop stranger into the top hit.
      - recency_rank:    x10                   -> tie-breaker within a tier
      - change_weight:   x1                    -> deleted/modified > added
    """
    base = 1_000_000.0 - hops * 1000.0
    base += min(exc_relevance, 150) * 10.0  # evidence from the exception itself
    base += recency_rank * 10.0
    base += change_weight
    return base


async def analyze(
    stack_trace: str | None,
    description: str | None = None,
    k_hops: int = 2,
    release_window: list[str] | None = None,
) -> list[Finding]:
    node_props, g, modified_in, cs_release = await _load_graph()

    # Release recency: order tags chronologically by release timestamp.
    release_order = _release_recency(node_props)

    if not stack_trace:
        return await _vector_fallback(description, node_props, modified_in, cs_release, release_window, release_order)

    frames = parse_frames(stack_trace)
    seeds = _seed_nodes(frames, node_props)
    if not seeds:
        return []

    exc_keywords = _keywords(parse_exception(stack_trace))

    dist = _hop_distances(g, seeds, k_hops)

    findings: list[Finding] = []
    for node_id, hops in dist.items():
        cs_ids = modified_in.get(node_id)
        if not cs_ids:
            continue
        for cs_id in cs_ids:
            cs = node_props.get(cs_id)
            if cs is None:
                continue
            release_tag = _release_tag_for(cs_id, cs_release, node_props)
            if release_tag is None:
                continue
            if release_window is not None and release_tag not in release_window:
                continue
            recency_rank = release_order.get(release_tag, 0)
            change_type = cs.get("change_type", "modified")
            weight = _CHANGE_TYPE_WEIGHT.get(change_type, 1)
            qname = cs.get("qualified_name", "")
            hunk = cs.get("diff_hunk", "")
            exc_rel = _exception_relevance(exc_keywords, qname, hunk)
            findings.append(
                Finding(
                    qualified_name=qname,
                    file_path=cs.get("file_path", ""),
                    release_tag=release_tag,
                    change_type=change_type,
                    hops=hops,
                    score=_score(hops, recency_rank, weight, exc_rel),
                    diff_hunk=hunk,
                )
            )

    # Deduplicate (a symbol reachable via multiple paths keeps its min hop /
    # best score).
    findings = _dedupe(findings)
    findings.sort(key=lambda f: (-f.score, f.hops, f.qualified_name))
    return findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    best: dict[tuple[str, str], Finding] = {}
    for f in findings:
        key = (f.qualified_name, f.release_tag)
        cur = best.get(key)
        if cur is None or f.score > cur.score:
            best[key] = f
    return list(best.values())


def _release_tag_for(cs_id, cs_release, node_props) -> Optional[str]:
    rel_id = cs_release.get(cs_id)
    if rel_id is None:
        return None
    rel = node_props.get(rel_id)
    if rel is None:
        return None
    return rel.get("tag")


def _release_recency(node_props: dict) -> dict[str, int]:
    """Map release tag -> recency rank (higher = newer) by timestamp."""
    releases = [
        (props.get("tag"), props.get("timestamp", ""))
        for props in node_props.values()
        if props.get("type") == "Release"
    ]
    releases = [r for r in releases if r[0]]
    releases.sort(key=lambda r: r[1])
    return {tag: rank for rank, (tag, _ts) in enumerate(releases)}


# ---------------------------------------------------------------------------
# No-trace vector fallback (degrades gracefully with no provider).
# ---------------------------------------------------------------------------


async def _vector_fallback(
    description, node_props, modified_in, cs_release, release_window, release_order
) -> list[Finding]:
    if not description:
        return []

    # Fail fast when no embedding provider is configured: attempting a vector
    # search with no LLM_API_KEY otherwise triggers a long retry storm against
    # a non-existent endpoint. The deterministic (stack-trace) path is the
    # primary artifact; description-only search is best-effort.
    import os

    if not os.environ.get("LLM_API_KEY") and not os.environ.get("EMBEDDING_API_KEY"):
        warnings.warn(
            "vector fallback skipped: no embedding provider configured "
            "(LLM_API_KEY/EMBEDDING_API_KEY unset); returning empty findings "
            "for description-only query.",
            RuntimeWarning,
        )
        return []

    try:
        from cognee.infrastructure.databases.vector import get_vector_engine

        vector_engine = get_vector_engine()
        # Attempt a semantic search over the embedded diff_hunk collection.
        results = await vector_engine.search(
            collection_name="ChangedSymbol_diff_hunk",
            query_text=description,
            limit=10,
        )
    except Exception as exc:  # no provider / offline / collection missing
        warnings.warn(
            f"vector fallback unavailable (no embedding provider or collection); "
            f"returning empty findings for description-only query: {exc}",
            RuntimeWarning,
        )
        return []

    findings: list[Finding] = []
    for res in results or []:
        cs_id = str(getattr(res, "id", ""))
        cs = node_props.get(cs_id)
        if cs is None:
            continue
        release_tag = _release_tag_for(cs_id, cs_release, node_props)
        if release_tag is None:
            continue
        if release_window is not None and release_tag not in release_window:
            continue
        recency_rank = release_order.get(release_tag, 0)
        change_type = cs.get("change_type", "modified")
        findings.append(
            Finding(
                qualified_name=cs.get("qualified_name", ""),
                file_path=cs.get("file_path", ""),
                release_tag=release_tag,
                change_type=change_type,
                hops=1,
                score=_score(1, recency_rank, _CHANGE_TYPE_WEIGHT.get(change_type, 1)),
                diff_hunk=cs.get("diff_hunk", ""),
            )
        )
    findings = _dedupe(findings)
    findings.sort(key=lambda f: (-f.score, f.hops, f.qualified_name))
    return findings
