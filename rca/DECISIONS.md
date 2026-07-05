# RCA MVP — Implementation Decisions (cognee 1.2.2)

Findings from inspecting the installed cognee wheel, and the decisions made to
satisfy SPEC.md hard rules while staying keyless/offline.

## Environment facts (verified)
- cognee 1.2.2, Python 3.12.13, `.venv` (no `pip` in venv, network off — cannot
  add/upgrade packages).
- Default graph provider is **`ladybug`** (embedded), NOT kuzu, per
  `infrastructure/databases/graph/config.py` (`graph_database_provider = "ladybug"`).
  Kuzu is present but not the default. LanceDB (vector) + SQLite (relational)
  are the other embedded defaults.
- Auth posture defaults to `authentication=required, multi_tenant=enabled`.

## GOTCHA workarounds (in `rca/env.py`)
1. **Storage in site-packages** → `cognee.config.system_root_directory(...)` and
   `cognee.config.data_root_directory(...)` redirect to project-local
   `./.cognee_system` and `./.cognee_data`. Resolved from the package root, not
   cwd, so tests are stable regardless of invocation dir.
2. **Auth required by default** → set `ENABLE_BACKEND_ACCESS_CONTROL=false` as an
   env var at `rca.env` import time (before any `import cognee`), so cognee reads
   it while deciding its auth posture and stays on the single-user embedded path.

## SPEC rule 5 — baseline extractor decision
Inspected cognee 1.2.2 for a usable native code-graph entry.
- `cognee/shared/CodeGraphEntities.py` **does** define the built-in DataPoint
  models (`Repository, CodeFile, FunctionDefinition, ClassDefinition,
  ImportStatement, CodePart, SourceCodeChunk`) — we reuse these directly.
- BUT the native code-graph *pipeline* is not usable for our constraints: it
  runs through the generic ingest path which requires an embedding engine
  (`add_data_points` → `index_data_points`), and it does not give us
  deterministic per-`(file_path, name)` ids or a clean per-file delete+reinsert
  hook. It is also LLM/embedding-coupled, which SPEC hard rule 3 forbids in the
  ingest loop.

**Decision:** implement `extract.py` with tree-sitter for BOTH Python and JS/TS,
emitting the built-in `CodeGraphEntities` models, and insert via the graph
adapter directly. This is fully deterministic and keyless.

### FunctionDefinition/ClassDefinition line ranges
The built-in models carry only `start_point`/`end_point` tuples (0-indexed
(row,col)), no line-range fields, and tests read `.start_line`/`.end_line`
directly off the extracted symbols. We subclass the two models in `extract.py`
adding 1-indexed `start_line`/`end_line` int fields. They remain DataPoints
(same graph, same `index_fields`), so the graph layer is unaffected. A light
`Symbol` dataclass carries the line range alongside each DataPoint for hunk→symbol
mapping (`symbols_for_lines`).

### JS/TS grammar ABI mismatch (environment blocker)
`tree_sitter_javascript==0.25.0` compiles to grammar **ABI 15**, but the installed
`tree_sitter==0.24.0` core only supports ABI ≤ 14 — so the pure-JS grammar
`ValueError: Incompatible Language version 15`s on load and is unusable here (no
pip/network to downgrade it). `tree_sitter_typescript==0.23.2` loads fine (ABI
14) and the TypeScript grammar is a strict superset of JavaScript, producing the
identical node types we rely on (`function_declaration`, `lexical_declaration`,
`variable_declarator`, `import_statement`). **Decision:** route `language=
"javascript"` through the TypeScript grammar. Line ranges and node types match
the fixture expectations exactly.

## Insertion path (SPEC hard rule 3 — no generic cognify)
`tasks/storage/add_data_points.py` only runs the SQL access-control upserts when
`user AND dataset AND data_item` are all present; otherwise it goes straight to
`graph_engine.add_nodes` + `index_data_points` (embedding-dependent). We bypass
`add_data_points` entirely and call the **graph adapter directly**:
`engine.add_nodes(list[DataPoint])`, `engine.add_edges(list[tuple])`,
`engine.delete_nodes(list[str])`, `engine.get_graph_data()`. This is keyless, has
no LLM/embedding in the loop, and `add_nodes` MERGEs on `id`, so deterministic
uuid5 ids give idempotent upserts (SPEC hard rule 2).

## Deterministic ids (SPEC hard rule 2)
Every node id is `uuid5(env.NAMESPACE, f"{Type}:{...}")`:
- `CodeFile:{file_path}`, `FunctionDefinition:{file_path}:{name}`,
  `ClassDefinition:{file_path}:{name}`,
  `ImportStatement:{file_path}:{module}:{name}`,
  `Release:{tag}`, `ChangedSymbol:{tag}:{qualified_name}`,
  `Repository:rca_codegraph` (path-independent so full builds and per-file
  refreshes from a temp snapshot share one Repository node).

## Graph reads (SPEC hard rule 4 — adapter, not SearchType)
`engine.get_graph_data()` → `([(node_id, props)], [(src, tgt, rel_name, props)])`.
The ladybug adapter flattens each node's model fields into `props` (with `type`,
`name`, `file_path`, `qualified_name`, …), so lookups and k-hop traversal load
straight into a networkx `Graph` (undirected view for blast radius). No
`SearchType` dependency.

## Graph shape + blast radius
Edges: `contains` (Repository→CodeFile), `provides_function_definition`,
`provides_class_definition`, `depends_on` (CodeFile→ImportStatement),
`imports_symbol` (ImportStatement→resolved FunctionDefinition), and **`calls`**
(FunctionDefinition→FunctionDefinition), resolved conservatively via same-file
defs + this file's imports. The `calls` edge is what lands `format_currency`
exactly **one hop** from a `process_payment` seed (import-hop incident), matching
the test's `hops == 1`. Call-site detection is a cheap `name(` regex over each
function body — sufficient for the RCA signal, no full name resolution.

## Per-file refresh + MODIFIED_IN edge repair (important)
`sync_release` refreshes each changed file's subtree at post-release content by
DETACH-DELETE + reinsert (`baseline.build_baseline(files=[...])` against a
`git archive` snapshot of `rel_sha`). DETACH DELETE also removes MODIFIED_IN
edges that **earlier** releases attached to those symbol nodes. To avoid
stranding prior releases' edges, after inserting the current release we
**reconcile**: reload all `ChangedSymbol` nodes and re-add their MODIFIED_IN +
in_release edges (deterministic target ids). Idempotent (edge MERGE). Without
this, R2's refresh of `payments.py` silently dropped R1's
`process_payment → FunctionDefinition` edge and the direct-hit query lost its
0-hop finding.

## Linking rules (SPEC)
- `modified`/`added` → MODIFIED_IN to the FunctionDefinition/ClassDefinition.
- `deleted` → MODIFIED_IN to the containing CodeFile (symbol has no rel-side
  node). CodeFile `type != "FunctionDefinition"`, satisfying the deleted-link
  assertion.
- lookup miss → CodeFile fallback, never dropped. `lookup_symbol_node` is a
  module-level patchable hook (tests monkeypatch it to force a miss on
  `validate_card`); on `None` we fall back to the CodeFile.

## Ranking
`score = 1_000_000 − hops*1000 + recency_rank*10 + change_weight`. The `hops*1000`
term dominates recency+change_weight (bounded small), so **0-hop always beats any
≥1-hop** finding regardless of release recency or change type
(deleted/modified=2 > added=1). Release recency ranked by Release `timestamp`.

## Release window / chronological replay
`release_window` is a list of tags to include; `None` = all known releases. The
filter keeps only ChangedSymbols whose linked Release tag is in the window.
Chronological replay works because the test simply never syncs R2 — so R2's
`charge_card` change has no nodes/edges in the graph and cannot leak, independent
of the window filter.

## Degraded functionality (keyless)
- **Vector fallback (no stack trace) is skipped** when no embedding provider is
  configured (`LLM_API_KEY`/`EMBEDDING_API_KEY` unset). `analyze(stack_trace=None,
  description=...)` returns `[]` with a `RuntimeWarning` rather than raising or
  hitting a paid API. Fail-fast guard avoids a multi-minute retry storm against a
  non-existent embedding endpoint. The deterministic stack-trace path is fully
  functional keyless.
- No LLM narrative layer (optional per plan; the ranked Finding list is the
  primary, auditable artifact).
