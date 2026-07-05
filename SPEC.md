# RCA MVP — Module Contracts (orchestrator spec)

Plan: `/Users/local/.claude/plans/here-is-a-comprehensive-whimsical-hamster.md` (read it first).
Stack: Python 3.12 venv at `.venv/`, `cognee[codegraph]==1.2.2`, tree-sitter (py/js/ts), Kuzu+LanceDB+SQLite (embedded defaults), pytest.

## Layout
```
rca/
  __init__.py
  env.py         # cognee config; single dataset name constant DATASET = "rca_codegraph"
  models.py      # Release, ChangedSymbol (DataPoint subclasses)
  extract.py     # tree-sitter extraction: file -> CodeGraphEntities models (py/js/ts)
  baseline.py    # build/refresh baseline graph
  diff_sync.py   # git diff -> ChangedSymbol tagging
  query.py       # incident -> ranked blast-radius result
tests/
  fixtures/      # synthetic repo builder (git history w/ tagged releases + known bugs)
  test_extract.py test_diff_sync.py test_query.py test_replay.py
```

## Hard rules (from plan review — do not relitigate)
1. **One dataset** — every insert goes to `env.DATASET`. Test asserts ChangedSymbol→FunctionDefinition edge exists in one graph query.
2. **Deterministic node IDs** — all nodes we create use `uuid5(NAMESPACE, f"{type}:{file_path}:{name}")` so re-ingest upserts instead of duplicating. (This resolves plan finding F1; do NOT rely on random UUIDs.)
3. **No generic `cognify()` over diffs or code** — insert structured DataPoints via the low-level path (`cognee.low_level` / `add_data_points` task / `run_custom_pipeline`). No LLM in the ingest loop.
4. **Graph reads via adapter, not SearchType** — `get_graph_data()` → networkx for lookup + N-hop traversal. `SearchType.CYPHER` is optional sugar; never a dependency.
5. **Baseline extractor decision rule** (timebox 30 min): inspect installed cognee for a usable native code-graph entry that (a) is importable, (b) produces `CodeGraphEntities` nodes, (c) allows deterministic IDs or per-file delete+reinsert. If any fails → use OUR `extract.py` tree-sitter extractor for **both** Python and JS/TS, emitting the same built-in models (`Repository, CodeFile, FunctionDefinition, ClassDefinition, ImportStatement` from `cognee.shared.CodeGraphEntities`). Record the decision in `rca/DECISIONS.md`.

## Contracts
```python
# env.py
DATASET = "rca_codegraph"
async def init() -> None      # config dirs under ./.cognee_{system,data}; provider from .env
async def reset() -> None     # prune data+system (test hermeticity)

# models.py  (from cognee.low_level import DataPoint)
class Release(DataPoint):     # commit_sha, prev_sha, tag, timestamp; index_fields=["tag"]
class ChangedSymbol(DataPoint):
    # qualified_name, file_path, change_type: Literal["added","modified","deleted"]
    # diff_hunk (index_fields=["diff_hunk"]), release: Release,
    # modified_node: FunctionDefinition|ClassDefinition|CodeFile  <- the MODIFIED_IN edge

# extract.py
def extract_file(path: str, source: str, language: str) -> ExtractedFile
    # -> CodeFile + FunctionDefinition/ClassDefinition/ImportStatement with line ranges
def symbols_for_lines(extracted: ExtractedFile, lines: set[int]) -> list[Symbol]

# baseline.py
async def build_baseline(repo_path: str, files: list[str] | None = None) -> BaselineStats
    # full build when files=None; per-file refresh (delete-then-reinsert subtree) otherwise

# diff_sync.py
async def sync_release(repo_path, prev_sha, rel_sha, tag, timestamp) -> ReleaseSyncResult
    # 1) git diff names+hunks  2) refresh baseline for changed files (post-release content)
    # 3) map hunks->symbols at both revisions (deleted = present@prev, absent@rel)
    # 4) linking rules: modified/added->symbol node; deleted->CodeFile; miss->CodeFile; never drop
    # 5) insert Release+ChangedSymbols (idempotent per (release,symbol))

# query.py
@dataclass class Finding: qualified_name; file_path; release_tag; change_type; hops; score; diff_hunk
async def analyze(stack_trace: str | None, description: str | None = None,
                  k_hops: int = 2, release_window: list[str] | None = None) -> list[Finding]
    # seed: parse frames (py: File "x", line N, in f; js: at f (x:line)) -> direct graph lookup
    # fallback (no trace): vector search over embedded fields
    # traverse depends_on + containment k hops; filter MODIFIED_IN window
    # rank: hops asc, then exception-relevance boost (symbol name/diff matching the
    #       raised exception can overtake an equally-close unrelated change),
    #       then recency desc, deleted/modified > added
```

## Tests required (pytest, hermetic via env.reset())
- extract: known fixture file → exact function/class/import sets with correct line ranges (py + js).
- diff_sync: synthetic release changing 3-4 known fns incl. **one deleted** (links to CodeFile) and
  **one forced lookup-miss** (falls back to file link, not dropped). Re-run sync twice → node counts
  unchanged (idempotency/identity). Single-query assert of ChangedSymbol→FunctionDefinition edge.
- query: trace hitting changed fn directly → rank 1 (0-hop). Trace one import-hop away → found at hop 1.
  Unrelated change ranks below. Release-window filter excludes older release.
- replay: 2 sequential synthetic releases, incident after R1 → only R1 changes surface (chronological
  replay; R2 must not leak in).

## Embeddings/LLM
May run with local/no-LLM config: ingest path is deterministic; only vector-fallback + narrative need
a provider. Code must degrade gracefully: if no embedding provider, skip vector fallback with a clear
warning (tests for the deterministic path must pass without any API key). If cognee's add_data_points
hard-requires an embedding engine, use its local/fastembed option — never a paid call from tests.
