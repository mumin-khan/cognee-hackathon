# RCA — Release Root-Cause Analysis on a Code Knowledge Graph

When a bug shows up **after a release**, this tool answers the question that matters first:
**"which change in which release most likely caused it?"**

It keeps a persistent knowledge graph of your codebase (files, functions, classes, imports,
call edges) in [cognee](https://github.com/topoteretes/cognee), tags every release's git diff onto
that graph as first-class nodes, and — given a stack trace — walks the blast radius outward to rank
recently-changed code by proximity to the failure.

```
one-time / per release                          per incident
──────────────────────                          ────────────
[repo]      ──▶ tree-sitter ──▶  CODE GRAPH     [stack trace] ──▶ parse frames
                                 (CodeFile,            │
[git diff   ──▶ changed     ──▶  FunctionDef,          ▼
 prev→rel]      symbols          calls/imports)  seed nodes ──▶ k-hop traversal
                   │                  ▲                │
                   └─▶ Release +──────┘                ▼
                       ChangedSymbol             keep nodes MODIFIED_IN release window
                       (MODIFIED_IN edges)             │
                                                       ▼
                                            ranked (symbol, release, hops, diff)
```

**Why a graph instead of embeddings-only retrieval?** Tools like Cursor/Copilot re-index and
vector-search per query; the index is ephemeral and knows nothing about *time*. Here the release
diff is a durable, queryable object in the same graph as the code structure — which is exactly what
makes "did release v1.4.2 break this?" answerable.

## Status

MVP — validated end-to-end on a synthetic fixture repo (30 tests, all green).
Python + JavaScript/TypeScript
supported; adding a language = adding a tree-sitter grammar to `rca/extract.py`.

## Requirements

- Python ≥ 3.10 (developed on 3.12)
- Everything is pinned in [`requirements.txt`](requirements.txt): `cognee[codegraph]==1.2.2`,
  the tree-sitter grammars (py/js/ts), `networkx`, and the test deps. The dashboard is
  stdlib-only — no web-framework dependency.
- **No API key needed** for the core flow — ingestion and querying are fully deterministic and
  local (embedded graph store, no LLM calls). See [Fully-local mode](#fully-local-mode-vs-keyed-mode).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
# (or: .venv/bin/pip install -r requirements.txt)
```

> The `tree-sitter` core version is pinned deliberately — see the note in `requirements.txt`.

## Quickstart

```python
import asyncio
from rca import env
from rca.baseline import build_baseline
from rca.diff_sync import sync_release
from rca.query import analyze

async def main():
    await env.init()                       # project-local storage, single dataset

    # 1) Baseline graph of the repo (full build; pass files=[...] for a partial refresh)
    await build_baseline("/path/to/your/repo")

    # 2) Tag each release's diff onto the graph — in chronological order
    await sync_release("/path/to/your/repo",
                       prev_sha="a1b2c3d", rel_sha="e4f5a6b",
                       tag="v1.4.2", timestamp="2026-07-01T12:00:00Z")

    # 3) When an incident lands, feed the stack trace in
    findings = await analyze(stack_trace=open("trace.txt").read(),
                             k_hops=2,
                             release_window=["v1.4.1", "v1.4.2"])  # None = all releases
    for f in findings[:5]:
        print(f"{f.qualified_name}  [{f.change_type} in {f.release_tag}] "
              f"hops={f.hops} score={f.score}")
        print(f.diff_hunk)

asyncio.run(main())
```

`analyze()` returns `Finding` objects: `qualified_name`, `file_path`, `release_tag`,
`change_type` (`added`/`modified`/`deleted`), `hops` (graph distance from the trace), `score`,
and the `diff_hunk` that changed it. Python and JS stack-trace formats are auto-detected.

## Dashboard (local web UI)

`dashboard/` is a stdlib-only Python server + playground UI that drives the **same real pipeline**
through a browser — findings, diffs, and graph traversal all come live from `analyze()`; nothing
is canned (if the backend is down, the UI says so instead of showing fake results).

```bash
.venv/bin/python dashboard/server.py
# open http://127.0.0.1:8080/playground.html
```

Click **Ingest Demo Repo** to build the synthetic fixture (real tree-sitter parse + git release
sync), pick an incident, and **Run Graph RCA** to watch the blast-radius traversal and ranked
culprits — every score is live pipeline output. To run RCA against **your own repo**, use the
library Quickstart above (`build_baseline` / `sync_release` / `analyze`); the playground is scoped
to the demo dataset. Fully local and keyless, same as the library.

## How it works

### 1. Baseline graph (`rca/baseline.py`, `rca/extract.py`)
tree-sitter parses every source file into cognee's built-in `CodeGraphEntities` models
(`Repository`, `CodeFile`, `FunctionDefinition`, `ClassDefinition`, `ImportStatement`) plus
**function-level `calls` edges** resolved through same-file definitions and imports. Nodes get
**deterministic IDs** (`uuid5` of `type:file_path:name`), so re-ingesting a file upserts instead of
duplicating.

### 2. Release tagging (`rca/diff_sync.py`, `rca/models.py`)
Per release: `git diff prev..rel` → changed line ranges on both sides → tree-sitter maps them to
enclosing symbols at both revisions → emits a `Release` node plus `ChangedSymbol` nodes with
`MODIFIED_IN` edges into the baseline graph. Linking rules:
- `modified`/`added` → link to the symbol's node
- `deleted` → link to the containing `CodeFile` (deletions break callers; they must stay findable)
- lookup miss → file-level fallback — a ChangedSymbol is **never silently dropped**

The baseline for changed files is refreshed to the post-release revision *before* tagging, and
`MODIFIED_IN` edges from earlier releases are reconciled after each refresh so history survives
repeated syncs.

### 3. Incident query (`rca/query.py`)
Stack-trace frames → direct graph lookup by `(file_path, symbol)` (no LLM, no fuzzing) → k-hop
traversal over calls/imports/containment → keep nodes with `MODIFIED_IN` edges inside the release
window → rank. Ranking is **proximity-first with an exception-relevance boost**: closer hops rank
higher (the failure site and its immediate neighbors lead), but the **raised exception is used as a
causal signal** — a changed symbol whose name or diff matches the exception text (e.g. `Unknown
format code` → `format_currency`) is boosted enough to overtake an equally-close but unrelated
change, including one from a newer release. Recency and change type (`deleted`/`modified` > `added`)
break remaining ties. This is what lets an import-hop root cause in *another file* rank as the top
suspect even though the trace never names it.

## Fully-local mode vs. keyed mode

| Capability | No API key | With `LLM_API_KEY`/embedding key in `.env` |
|---|---|---|
| Baseline build, release tagging | ✅ deterministic | ✅ same |
| Stack-trace incident analysis | ✅ deterministic | ✅ same |
| Description-only fuzzy search (no trace) | ⚠️ returns `[]` with a warning | ✅ vector search over code + diff hunks |
| Natural-language RCA narrative | — | ✅ via cognee `GRAPH_COMPLETION` |

Nothing in the ingest or trace-analysis path ever calls an LLM.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q     # 30 tests, hermetic, no network
```

The suite builds a synthetic payment-app git repo (`tests/fixtures/repo_builder.py`) with three
tagged releases and known ground truth (3 modified + 1 added + 1 deleted symbol in v1.1.0, 1
modified in v1.2.0), then asserts:
- exact extraction (functions/classes/imports with line ranges, Python + JS)
- diff tagging incl. the deleted-symbol and lookup-miss fallbacks, idempotent re-sync (no node
  duplication), and single-graph connectivity (ChangedSymbol→FunctionDefinition in one query)
- ranking (direct-hit incident ranks #1 at 0 hops; 1-hop cause found; negative control returns
  nothing; release-window filtering)
- **chronological replay**: an incident occurring between v1.1.0 and v1.2.0 never sees v1.2.0's
  changes — validation can't cheat by peeking at the future

## Project layout

```
rca/
  env.py        # cognee config: project-local storage, single dataset, access-control off
  models.py     # Release, ChangedSymbol DataPoints (deterministic IDs)
  extract.py    # tree-sitter → CodeGraphEntities (+ line ranges), py/js/ts
  baseline.py   # full/partial graph build, call-edge resolution, subtree refresh
  diff_sync.py  # git diff → ChangedSymbol tagging + edge reconciliation
  query.py      # frame parsing, k-hop blast radius, ranking, get_graph_snapshot
  DECISIONS.md  # every judgment call, with reasons
dashboard/
  server.py        # stdlib HTTP server wrapping the rca pipeline (thin, no new logic)
  playground.html  # dashboard UI — findings/graph rendered from live API responses
  app.js, styles.css
tests/          # 30 tests + fixture repo builder + ground-truth incidents
demo.py         # narrated CLI walkthrough of the four incident types
requirements.txt
SPEC.md         # module contracts the implementation was built against
```

## Design decisions worth knowing (full log in `rca/DECISIONS.md`)

- **Graph writes go through cognee's graph adapter directly** — never `cognify()` (which runs LLM
  extraction and would pollute the graph with nondeterministic entities) and never
  `add_data_points` (which requires an embedding engine). The default embedded store (`ladybug` in
  cognee 1.2.2) MERGEs on node id, which is what makes deterministic-ID upserts work.
- **The community codify package is deliberately not used** — it pins `cognee==0.5.x` and conflicts
  with 1.2.2; tree-sitter extraction emitting the same built-in models replaces it.
- **JS parses through the TypeScript grammar**: `tree_sitter_javascript` 0.25 ships grammar ABI 15,
  incompatible with `tree_sitter` 0.24 (ABI 14, pinned by cognee). TS is a strict JS superset with
  identical node types.

## Roadmap / deferred

CI hook to auto-sync on release tags · Sentry/Datadog webhook ingestion · richer call resolution
(cross-module method dispatch) · DI-container and SQL/ORM edges · feedback loop reweighting
confirmed root causes · more languages (one tree-sitter grammar each).
