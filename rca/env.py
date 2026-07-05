"""cognee configuration + single-dataset constant + hermetic reset.

Hard rules honored here:
  * ONE dataset name (`DATASET`) is the single scope for every insert/read.
  * cognee's storage is redirected project-local (GOTCHA 1) so nothing lands
    in site-packages.
  * Backend access control is disabled (GOTCHA 2) so the embedded single-user
    ladybug/LanceDB/SQLite path is used with no auth/multi-tenant machinery.

The environment variable that disables access control must be set *before*
cognee is imported, because cognee reads it at import time to decide its auth
posture. We therefore set it at module import, ahead of the first `import
cognee` anywhere in the process.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- must run before cognee is imported anywhere -------------------------
# GOTCHA 2: cognee 1.2.2 defaults to authentication=required + multi_tenant.
# Local single-user deterministic flows need this off.
os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
# No network / no LLM in tests. Keep any accidental provider call cheap-fail
# rather than hang; we never actually call an LLM on the deterministic path.
os.environ.setdefault("LLM_API_KEY", "")

# Project-local storage roots (GOTCHA 1). Resolved relative to the repo root
# (parent of this package), not the cwd, so tests are stable regardless of
# where pytest is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_ROOT = _PROJECT_ROOT / ".cognee_system"
_DATA_ROOT = _PROJECT_ROOT / ".cognee_data"

# Single dataset for the whole RCA graph. Baseline nodes and diff/release nodes
# MUST land here together or ChangedSymbol -> FunctionDefinition edges span a
# boundary traversal won't cross.
DATASET = "rca_codegraph"

# Deterministic UUID namespace for every node id we mint (SPEC hard rule 2).
# uuid5(NAMESPACE, f"{type}:{file_path}:{name}") -> re-ingest upserts.
import uuid

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_OID, "rca_codegraph")


_configured = False


def _configure() -> None:
    """Point cognee at project-local storage. Idempotent."""
    global _configured
    if _configured:
        return
    import cognee

    _SYSTEM_ROOT.mkdir(parents=True, exist_ok=True)
    _DATA_ROOT.mkdir(parents=True, exist_ok=True)
    cognee.config.system_root_directory(str(_SYSTEM_ROOT))
    cognee.config.data_root_directory(str(_DATA_ROOT))
    _configured = True


async def init() -> None:
    """Configure cognee storage dirs for the single RCA dataset."""
    _configure()


async def reset() -> None:
    """Prune all cognee graph/vector/relational state for hermetic tests."""
    _configure()
    import cognee

    # prune_data clears relational + file storage; prune_system clears the
    # graph + vector stores. Together they give a clean slate per test.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(graph=True, vector=True)


async def get_graph_engine():
    """Return the configured embedded graph adapter (ladybug by default)."""
    _configure()
    from cognee.infrastructure.databases.graph import get_graph_engine as _gge

    return await _gge()
